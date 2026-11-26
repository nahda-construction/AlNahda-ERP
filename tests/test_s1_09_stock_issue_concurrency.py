from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.http import Http404
from django.test import TransactionTestCase, override_settings

from apps.access_control.services import revoke_project_access
from apps.inventory import services
from apps.inventory.models import StockIssue, StockLocationBalance, StockMovement
from apps.projects.models import Project
from apps.test_transactions.models import AuditEvent

from .test_s1_05_http import TEST_HASHERS
from .test_s1_08_stock_rollback import OPENING_QUANTITY, backend_pid
from .test_s1_09_stock_issue import StockIssueAssertions, seed_approval


WAIT_TIMEOUT = 8


@override_settings(PASSWORD_HASHERS=TEST_HASHERS)
class StockIssueConcurrencyTests(StockIssueAssertions, TransactionTestCase):
    fixtures = ["s1_03_demo"]

    def setUp(self):
        seed_approval(self)

    def approve_on_own_connection(self, issue, actor, started, statements):
        def capture_balance_lock(execute, statement, params, many, context):
            if "FOR UPDATE" in statement and StockLocationBalance._meta.db_table in statement:
                statements.put((issue.pk, statement))
            return execute(statement, params, many, context)

        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout TO '16s'")
            started.put(backend_pid())
            with connection.execute_wrapper(capture_balance_lock):
                return self.approve(issue, actor)
        finally:
            connections.close_all()

    def assert_waiting_for_row_lock(self, waiting_pid, locking_pid, model):
        self.assertNotEqual(waiting_pid, locking_pid)
        deadline = monotonic() + WAIT_TIMEOUT
        while monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_stat_clear_snapshot()")
                cursor.execute(
                    "SELECT pg_blocking_pids(pid), query FROM pg_stat_activity WHERE pid = %s", [waiting_pid],
                )
                blockers, query = cursor.fetchone()
            if locking_pid in blockers and "FOR UPDATE" in query and model._meta.db_table in query:
                return
            sleep(0.02)
        self.fail(f"PostgreSQL did not show backend {waiting_pid} waiting on {locking_pid}")

    def race(self, winner, waiter, *, lock_model, expected_error):
        winner_issue, winner_actor = winner
        waiting_issue, waiting_actor = waiter
        started = Queue()
        statements = Queue()
        winner_locked = Event()
        release_winner = Event()
        postings = Queue()
        actual_post = services._post_stock_issue

        def post_after_release(**kwargs):
            postings.put((kwargs["issue"].pk, kwargs["actor"].pk, tuple(kwargs["balances_by_id"])))
            if not winner_locked.is_set():
                winner_locked.set()
                if not release_winner.wait(timeout=WAIT_TIMEOUT * 2):
                    raise AssertionError("Winning approval did not receive the release signal")
            return actual_post(**kwargs)

        with (
            patch("apps.inventory.services._post_stock_issue", side_effect=post_after_release),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(self.approve_on_own_connection, winner_issue, winner_actor, started, statements)
            try:
                first_pid = started.get(timeout=WAIT_TIMEOUT)
                self.assertTrue(winner_locked.wait(timeout=WAIT_TIMEOUT), "Winner did not reach posting after locking")
                second = executor.submit(
                    self.approve_on_own_connection, waiting_issue, waiting_actor, started, statements,
                )
                second_pid = started.get(timeout=WAIT_TIMEOUT)
                self.assertEqual(len({first_pid, second_pid, backend_pid()}), 3)
                self.assert_waiting_for_row_lock(second_pid, first_pid, lock_model)
                self.assertFalse(second.done())
                self.assert_draft_without_effects(winner_issue)
                self.assert_draft_without_effects(waiting_issue)
            finally:
                release_winner.set()
            approved = first.result(timeout=WAIT_TIMEOUT)
            with self.assertRaises(expected_error):
                second.result(timeout=WAIT_TIMEOUT)

        self.assertEqual(approved, winner_issue)
        posted_issue_id, posted_actor_id, locked_balances = postings.get_nowait()
        self.assertEqual((posted_issue_id, posted_actor_id), (winner_issue.pk, winner_actor.pk))
        self.assertTrue(postings.empty())
        self.assert_posted_once(winner_issue, winner_actor)
        lock_statements = []
        while not statements.empty():
            lock_statements.append(statements.get_nowait())
        return locked_balances, lock_statements

    def assert_seven_unit_race(self, first_number):
        issue_a = self.draft("S109-SEVEN-A", [(self.balance, "7.0000")])
        issue_b = self.draft("S109-SEVEN-B", [(self.balance, "7.0000")])
        attempts = ((issue_a, self.manager), (issue_b, self.second_manager))
        winner, waiter = attempts[first_number], attempts[1 - first_number]
        self.race(winner, waiter, lock_model=StockLocationBalance, expected_error=services.InsufficientStock)
        self.assert_draft_without_effects(waiter[0])
        self.assert_stock_quantity(self.balance, "3.0000")
        self.assertEqual(StockMovement.objects.filter(event_kind=StockMovement.EventKind.ISSUE).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(stock_issue__isnull=False).count(), 1)

    def test_seven_unit_approvals_allow_first_issue_to_win_and_reject_second(self):
        self.assert_seven_unit_race(0)

    def test_reversing_order_allows_second_issue_to_win_and_rejects_first(self):
        self.assert_seven_unit_race(1)

    def test_opposite_line_orders_lock_balances_in_same_order_and_reject_whole_shortage(self):
        services.post_opening_stock(balance_id=self.other_balance.pk, quantity=OPENING_QUANTITY)
        low, high = sorted((self.balance, self.other_balance), key=lambda balance: balance.pk)
        first = self.draft("S109-ORDER-HIGH-FIRST", [(high, "3.0000"), (low, "7.0000")])
        second = self.draft("S109-ORDER-LOW-FIRST", [(low, "5.0000"), (high, "4.0000")])
        locked, statements = self.race(
            (first, self.manager), (second, self.second_manager),
            lock_model=StockLocationBalance, expected_error=services.InsufficientStock,
        )
        self.assertEqual(locked, (low.pk, high.pk))
        self.assertCountEqual((issue_id for issue_id, _ in statements), (first.pk, second.pk))
        for _, statement in statements:
            self.assertRegex(statement, r'ORDER BY\s+"inventory_stocklocationbalance"\."id"\s+ASC\s+FOR UPDATE')
        self.assert_draft_without_effects(second)
        self.assert_stock_quantity(low, "3.0000")
        self.assert_stock_quantity(high, "7.0000")
        self.assertEqual(StockMovement.objects.filter(event_kind=StockMovement.EventKind.ISSUE).count(), 2)
        self.assertEqual(AuditEvent.objects.filter(stock_issue__isnull=False).count(), 1)

    def test_two_managers_approve_the_same_issue_once_under_the_header_lock(self):
        issue = self.draft("S109-SAME-DRAFT", [(self.balance, "3.0000"), (self.balance, "4.0000")])
        self.race(
            (issue, self.manager), (issue, self.second_manager), lock_model=StockIssue,
            expected_error=services.StockIssueConflict,
        )
        self.assert_stock_quantity(self.balance, "3.0000")
        self.assertEqual(StockMovement.objects.filter(event_kind=StockMovement.EventKind.ISSUE).count(), 2)
        self.assertEqual(AuditEvent.objects.filter(stock_issue__isnull=False).count(), 1)

    def change_on_own_connection(self, change):
        try:
            pid = backend_pid()
            with transaction.atomic():
                change()
            return pid
        finally:
            connections.close_all()

    def assert_rejected_after_concurrent_access_change(self, change, expected_error):
        issue = self.draft("S109-ACCESS-CHANGED", [(self.balance, "4.0000")])
        started = Queue()
        with ThreadPoolExecutor(max_workers=2) as executor:
            with transaction.atomic():
                StockLocationBalance.objects.select_for_update().get(pk=self.balance.pk)
                observer_pid = backend_pid()
                approval = executor.submit(self.approve_on_own_connection, issue, self.manager, started, Queue())
                approval_pid = started.get(timeout=WAIT_TIMEOUT)
                self.assert_waiting_for_row_lock(approval_pid, observer_pid, StockLocationBalance)
                changer_pid = executor.submit(self.change_on_own_connection, change).result(timeout=WAIT_TIMEOUT)
                self.assertEqual(len({observer_pid, approval_pid, changer_pid}), 3)
                self.assertFalse(approval.done())
            with self.assertRaises(expected_error):
                approval.result(timeout=WAIT_TIMEOUT)
        self.assert_draft_without_effects(issue)
        self.assert_stock_quantity(self.balance, "10.0000")

    def test_membership_revoked_while_waiting_for_stock_is_rechecked(self):
        self.assert_rejected_after_concurrent_access_change(
            lambda: revoke_project_access(actor=self.grantor, membership_id=self.manager_access.pk), Http404,
        )
        self.manager_access.refresh_from_db()
        self.assertFalse(self.manager_access.active)

    def test_project_archived_while_waiting_for_stock_is_rechecked(self):
        self.assert_rejected_after_concurrent_access_change(
            lambda: Project.objects.filter(pk=self.project.pk).update(status=Project.Status.ARCHIVED), ValidationError,
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ARCHIVED)
