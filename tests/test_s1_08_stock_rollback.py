import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from hashlib import sha256
from queue import Queue
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from psycopg import sql

from apps.inventory.models import (
    StockIssue,
    StockIssueLine,
    StockLocationBalance,
    StockMovement,
    ValuationPool,
    Warehouse,
)
from apps.inventory.services import _post_stock_issue, post_opening_stock
from apps.projects.models import Project, WorkNode
from apps.test_transactions.models import AuditEvent, TestTransaction

from .test_s1_07_inventory_constraints import create_material


OPENING_QUANTITY = Decimal("10.0000")
ISSUE_QUANTITIES = (Decimal("2.2500"), Decimal("1.5000"))
EXPECTED_QUANTITY = OPENING_QUANTITY - sum(ISSUE_QUANTITIES)
WAIT_TIMEOUT = 10


def execute(statement, parameters=()):
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)


def insert_row(model, fields):
    columns = sql.SQL(", ").join(map(sql.Identifier, fields))
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in fields)
    execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(model._meta.db_table), columns, placeholders,
        ),
        tuple(fields.values()),
    )


def backend_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def seed_stock(target):
    target.project = Project.objects.get(code="DEMO-A")
    target.other_project = Project.objects.get(code="DEMO-B")
    target.node = WorkNode.objects.get(code="A-ITEM")
    target.other_node = WorkNode.objects.get(code="B-ITEM")
    target.actor = get_user_model().objects.create_user(username="synthetic-stock-operator")
    material = create_material("S108-SYNTHETIC-MATERIAL")
    target.pool = ValuationPool.objects.create(
        scope_type=ValuationPool.ScopeType.PROJECT_STOCK,
        project=target.project, owner=target.project.owner_party, material=material,
    )
    warehouse = Warehouse.objects.create(code="S108-WAREHOUSE", name="Synthetic stock warehouse")
    second_warehouse = Warehouse.objects.create(code="S108-OTHER-WAREHOUSE", name="Other synthetic warehouse")
    target.balance = StockLocationBalance.objects.create(pool=target.pool, warehouse=warehouse)
    target.other_balance = StockLocationBalance.objects.create(pool=target.pool, warehouse=second_warehouse)


class StockSchemaTests(TestCase):
    fixtures = ["s1_03_demo"]

    @classmethod
    def setUpTestData(cls):
        seed_stock(cls)

    def assert_rejected(self, operation, error_state, constraint):
        with self.assertRaises(IntegrityError) as capture, transaction.atomic():
            operation()
            execute("SET CONSTRAINTS ALL IMMEDIATE")
        original = capture.exception.__cause__
        self.assertEqual(original.sqlstate, error_state)
        self.assertEqual(original.diag.constraint_name, constraint)

    def draft(self):
        return StockIssue.objects.create(
            project=self.project, work_node=self.node, document_no="S108-SCHEMA", created_by=self.actor,
        )

    def issue_line(self, issue):
        return StockIssueLine.objects.create(issue=issue, balance=self.balance, qty=Decimal("1.0000"), line_no=1)

    def insert_line(self, issue, **changes):
        insert_row(StockIssueLine, {
            "id": uuid4(), "issue_id": issue.pk, "balance_id": self.balance.pk,
            "qty": Decimal("1.0000"), "line_no": 2, **changes,
        })

    def insert_movement(self, **changes):
        insert_row(StockMovement, {
            "id": uuid4(), "balance_id": self.balance.pk, "issue_line_id": None,
            "signed_qty": Decimal("2.0000"), "created_at": timezone.now(),
            "event_kind": StockMovement.EventKind.OPENING, **changes,
        })

    def insert_audit(self, **changes):
        insert_row(AuditEvent, {
            "project_id": self.project.pk, "actor_id": self.actor.pk,
            "event_type": AuditEvent.EventType.APPROVED,
            "prior_state": "DRAFT", "next_state": "APPROVED",
            "occurred_at": timezone.now(), "correlation_id": uuid4(), **changes,
        })

    def test_opening_posts_once_and_keeps_the_first_positive_quantity(self):
        self.assertEqual(self.balance.quantity, Decimal("0.0000"))
        movement = post_opening_stock(balance_id=self.balance.pk, quantity=OPENING_QUANTITY)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.quantity, OPENING_QUANTITY)
        self.assertEqual(movement.balance, self.balance)
        self.assertEqual(movement.event_kind, StockMovement.EventKind.OPENING)
        self.assertEqual(movement.signed_qty, OPENING_QUANTITY)
        self.assertIsNone(movement.issue_line_id)

        with self.assertRaises(ValidationError):
            post_opening_stock(balance_id=self.balance.pk, quantity=Decimal("1.0000"))
        self.assert_rejected(self.insert_movement, "23505", "uq_stock_movement_opening_balance")
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.quantity, OPENING_QUANTITY)
        self.assertEqual(list(StockMovement.objects.values_list("pk", "signed_qty")), [(movement.pk, OPENING_QUANTITY)])

    def test_invalid_opening_quantities_leave_the_zero_balance_untouched(self):
        for quantity in (Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError):
                post_opening_stock(balance_id=self.balance.pk, quantity=quantity)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.quantity, Decimal("0.0000"))
        self.assertFalse(StockMovement.objects.exists())

    def test_database_rejects_cross_project_work_node_and_wrong_movement_balance(self):
        self.assert_rejected(
            lambda: insert_row(StockIssue, {
                "id": uuid4(), "project_id": self.project.pk, "work_node_id": self.other_node.pk,
                "document_no": "S108-CROSS-PROJECT", "status": "DRAFT", "created_by_id": self.actor.pk,
            }),
            "23503", "stock_issue_work_node_project_fk",
        )
        issue = self.draft()
        line = self.issue_line(issue)
        self.assert_rejected(
            lambda: self.insert_movement(
                event_kind="ISSUE", issue_line_id=line.pk, balance_id=self.other_balance.pk, signed_qty=Decimal("-1"),
            ),
            "23503", "stock_movement_issue_line_balance_fk",
        )
        self.assertEqual(list(StockIssue.objects.values_list("pk", flat=True)), [issue.pk])
        self.assertFalse(StockMovement.objects.exists())

    def test_database_rejects_unsupported_movement_sources_and_signs(self):
        line = self.issue_line(self.draft())
        cases = (
            {"issue_line_id": line.pk},
            {"signed_qty": Decimal("-1")},
            {"signed_qty": Decimal("0")},
            {"event_kind": "ISSUE", "signed_qty": Decimal("-1")},
            {"event_kind": "ISSUE", "issue_line_id": line.pk, "signed_qty": Decimal("1")},
            {"event_kind": "ISSUE", "issue_line_id": line.pk, "signed_qty": Decimal("0")},
            {"event_kind": "OTHER"},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                self.assert_rejected(
                    lambda: self.insert_movement(**fields), "23514", "stock_movement_source_sign_valid",
                )
        self.assertFalse(StockMovement.objects.exists())
        valid = {"event_kind": "ISSUE", "issue_line_id": line.pk, "signed_qty": Decimal("-1")}
        self.insert_movement(**valid)
        self.assert_rejected(
            lambda: self.insert_movement(**valid), "23505", "uq_stock_movement_issue_line",
        )
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_database_rejects_nonpositive_line_quantity_and_nan_in_both_tables(self):
        issue = self.draft()
        for value in (Decimal("0"), Decimal("-0.0001")):
            with self.subTest(quantity=value):
                self.assert_rejected(
                    lambda: self.insert_line(issue, qty=value), "23514", "stock_issue_line_qty_positive",
                )
        self.assert_rejected(
            lambda: self.insert_line(issue, qty=Decimal("NaN")), "23514", "stock_issue_line_qty_not_nan",
        )
        self.assert_rejected(
            lambda: self.insert_movement(signed_qty=Decimal("NaN")), "23514", "stock_movement_signed_qty_not_nan",
        )
        self.assertFalse(StockIssueLine.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_document_and_line_numbers_are_unique_within_their_parent(self):
        issue = self.draft()
        self.assert_rejected(self.draft, "23505", "uq_stock_issue_project_document_no")
        other = StockIssue.objects.create(
            project=self.other_project, work_node=self.other_node,
            document_no=issue.document_no, created_by=self.actor,
        )
        self.issue_line(issue)
        self.issue_line(other)
        self.assert_rejected(
            lambda: self.insert_line(issue, line_no=1), "23505", "uq_stock_issue_line_issue_number",
        )
        self.assert_rejected(
            lambda: self.insert_line(issue, line_no=0), "23514", "stock_issue_line_number_positive",
        )

    def test_database_requires_one_same_project_audit_source_and_preserves_unique_approval(self):
        issue = self.draft()
        trial = TestTransaction.objects.create(project=self.project, created_by=self.actor, title="Existing synthetic trial")
        self.assert_rejected(self.insert_audit, "23514", "audit_single_source")
        self.assert_rejected(
            lambda: self.insert_audit(stock_issue_id=issue.pk, test_transaction_id=trial.pk),
            "23514", "audit_single_source",
        )
        self.assert_rejected(
            lambda: self.insert_audit(stock_issue_id=issue.pk, project_id=self.other_project.pk),
            "23503", "stock_audit_issue_project_fk",
        )
        for fields in (
            {"event_type": "CREATED", "prior_state": None, "next_state": "DRAFT"},
            {"prior_state": None},
            {"next_state": "DRAFT"},
        ):
            with self.subTest(fields=fields):
                self.assert_rejected(
                    lambda: self.insert_audit(stock_issue_id=issue.pk, **fields),
                    "23514", "test_audit_transition_valid",
                )
        self.insert_audit(
            test_transaction_id=trial.pk, event_type="CREATED", prior_state=None, next_state="DRAFT",
        )
        StockIssue.objects.filter(pk=issue.pk).update(status="APPROVED", approved_by=self.actor, approved_at=timezone.now())
        self.insert_audit(stock_issue_id=issue.pk)
        self.assert_rejected(
            lambda: self.insert_audit(stock_issue_id=issue.pk), "23505", "uq_stock_issue_audit_event",
        )
        self.assertCountEqual(AuditEvent.objects.values_list("stock_issue_id", "test_transaction_id"), [
            (None, trial.pk), (issue.pk, None),
        ])

    def test_database_reserves_reversal_references_and_checks_approval_state(self):
        draft = self.draft()
        issues = {
            "project_id": self.project.pk, "work_node_id": self.node.pk,
            "created_by_id": self.actor.pk, "status": "DRAFT",
        }
        for fields in ({"status": "APPROVED"}, {"approved_by_id": self.actor.pk, "approved_at": timezone.now()}):
            with self.subTest(fields=fields):
                self.assert_rejected(
                    lambda: insert_row(StockIssue, {"id": uuid4(), "document_no": "S108-INVALID", **issues, **fields}),
                    "23514", "stock_issue_approval_state_valid",
                )
        self.assert_rejected(
            lambda: insert_row(StockIssue, {
                "id": uuid4(), "document_no": "S108-REVERSE", "reverses_id": draft.pk, **issues,
            }),
            "23514", "stock_issue_reverses_reserved",
        )
        opening = post_opening_stock(balance_id=self.balance.pk, quantity=OPENING_QUANTITY)
        self.assert_rejected(
            lambda: self.insert_movement(balance_id=self.other_balance.pk, reverses_id=opening.pk),
            "23514", "stock_movement_reverses_reserved",
        )

    def test_database_rejects_changes_to_posted_inventory_and_approval(self):
        issue = self.draft()
        line = self.issue_line(issue)
        opening = post_opening_stock(balance_id=self.balance.pk, quantity=OPENING_QUANTITY)
        locked_balance = StockLocationBalance.objects.select_for_update().get(pk=self.balance.pk)
        _post_stock_issue(issue=issue, actor=self.actor, lines=[line], balances_by_id={self.balance.pk: locked_balance})
        issue.refresh_from_db()
        self.assertEqual(issue.status, StockIssue.Status.APPROVED)
        spare = Warehouse.objects.create(code="S108-UNUSED-WAREHOUSE", name="Unused synthetic warehouse")

        for model, key, assignment, value, name in (
            (StockMovement, opening.pk, "signed_qty", Decimal("12"), "stock_movement_append_only"),
            (StockIssue, issue.pk, "document_no", "MODIFIED", "stock_issue_approved_immutable"),
            (StockIssueLine, line.pk, "qty", Decimal("2"), "stock_issue_approved_line_immutable"),
            (StockLocationBalance, self.balance.pk, "warehouse_id", spare.pk, "stock_balance_identity_immutable"),
            (ValuationPool, self.pool.pk, "material_id", create_material("NEW-MATERIAL").pk, "stock_pool_identity_immutable"),
        ):
            with self.subTest(update=model._meta.label):
                self.assert_rejected(
                    lambda: execute(sql.SQL("UPDATE {} SET {} = %s WHERE id = %s").format(
                        sql.Identifier(model._meta.db_table), sql.Identifier(assignment),
                    ), [value, key]),
                    "23514", name,
                )
        for model, key, name in (
            (StockMovement, opening.pk, "stock_movement_append_only"),
            (StockIssue, issue.pk, "stock_issue_approved_immutable"),
            (StockIssueLine, line.pk, "stock_issue_approved_line_immutable"),
        ):
            with self.subTest(delete=model._meta.label):
                self.assert_rejected(
                    lambda: execute(sql.SQL("DELETE FROM {} WHERE id = %s").format(
                        sql.Identifier(model._meta.db_table),
                    ), [key]),
                    "23514", name,
                )
        self.assert_rejected(
            lambda: self.insert_line(issue), "23514", "stock_issue_approved_line_immutable",
        )
        with self.assertRaises(ProtectedError):
            self.balance.delete()
        self.assertEqual(StockLocationBalance.objects.get(pk=self.balance.pk).quantity, Decimal("9.0000"))


class CheckpointFailure(Exception):
    pass


class StockRollbackTests(TransactionTestCase):
    fixtures = ["s1_03_demo"]

    def setUp(self):
        seed_stock(self)
        post_opening_stock(balance_id=self.balance.pk, quantity=OPENING_QUANTITY)
        prior = StockIssue.objects.create(
            project=self.project, work_node=self.node, document_no="S108-EXISTING-DRAFT", created_by=self.actor,
        )
        StockIssueLine.objects.create(issue=prior, balance=self.balance, qty=Decimal("1.0000"), line_no=1)
        trial = TestTransaction.objects.create(project=self.project, created_by=self.actor, title="Existing synthetic trial")
        AuditEvent.objects.create(
            project=self.project, actor=self.actor, test_transaction=trial, event_type="CREATED", next_state="DRAFT",
        )
        self.issue_id = uuid4()
        self.line_ids = [uuid4() for _ in ISSUE_QUANTITIES]

    def snapshot(self):
        rows = []
        for model in (StockIssue, StockIssueLine, StockMovement, AuditEvent, StockLocationBalance):
            values = list(model.objects.order_by("pk").values())
            fingerprint = sha256(json.dumps(values, default=str, sort_keys=True).encode()).hexdigest()
            rows.append((model._meta.label, len(values), fingerprint))
        quantity = StockLocationBalance.objects.values_list("quantity", flat=True).get(pk=self.balance.pk)
        return rows, quantity

    def submit_issue(self):
        with transaction.atomic():
            issue = StockIssue.objects.create(
                id=self.issue_id, project=self.project, work_node=self.node, document_no="S108-RETRY", created_by=self.actor,
            )
            lines = [
                StockIssueLine.objects.create(id=line_id, issue=issue, balance=self.balance, qty=qty, line_no=index)
                for index, (line_id, qty) in enumerate(zip(self.line_ids, ISSUE_QUANTITIES, strict=True), start=1)
            ]
            issue = StockIssue.objects.select_for_update().get(pk=issue.pk)
            balance = StockLocationBalance.objects.select_for_update().get(pk=self.balance.pk)
            return _post_stock_issue(issue=issue, actor=self.actor, lines=lines, balances_by_id={balance.pk: balance})

    def submit_on_own_connection(self):
        try:
            return self.submit_issue()
        finally:
            connections.close_all()

    def in_transaction_state(self):
        return (
            StockIssue.objects.values_list("status", flat=True).get(pk=self.issue_id),
            StockIssueLine.objects.filter(issue_id=self.issue_id).count(),
            StockMovement.objects.filter(issue_line__issue_id=self.issue_id).count(),
            AuditEvent.objects.filter(stock_issue_id=self.issue_id).count(),
            StockLocationBalance.objects.values_list("quantity", flat=True).get(pk=self.balance.pk),
        )

    def prove_rollback_and_retry(self, checkpoint, expected_inside):
        before = self.snapshot()
        checkpoint_info = Queue()
        release = Event()
        actual_insert = checkpoint.objects.create

        def insert_then_fail(**fields):
            actual_insert(**fields)
            checkpoint_info.put((backend_pid(), self.in_transaction_state()))
            if not release.wait(timeout=WAIT_TIMEOUT * 2):
                raise AssertionError("Failure checkpoint was not released")
            raise CheckpointFailure(checkpoint._meta.label)

        with (
            patch.object(checkpoint.objects, "create", side_effect=insert_then_fail),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            failed = executor.submit(self.submit_on_own_connection)
            try:
                writer_pid, inside = checkpoint_info.get(timeout=WAIT_TIMEOUT)
                self.assertNotEqual(writer_pid, backend_pid())
                self.assertEqual(inside, expected_inside)
                self.assertFalse(failed.done())
                self.assertEqual(self.snapshot(), before)
            finally:
                release.set()
            with self.assertRaisesRegex(CheckpointFailure, checkpoint._meta.label):
                failed.result(timeout=WAIT_TIMEOUT)

        self.assertEqual(self.snapshot(), before)
        approved = self.submit_issue()
        self.assertEqual(approved.pk, self.issue_id)
        self.assertEqual(approved.approved_by, self.actor)
        self.assertIsNotNone(approved.approved_at)
        self.assertEqual(self.in_transaction_state(), ("APPROVED", 2, 2, 1, EXPECTED_QUANTITY))
        self.assertEqual(list(StockMovement.objects.filter(issue_line__issue_id=self.issue_id).order_by(
            "issue_line__line_no",
        ).values_list("signed_qty", flat=True)), [-qty for qty in ISSUE_QUANTITIES])
        event = AuditEvent.objects.get(stock_issue_id=self.issue_id)
        self.assertEqual((event.project, event.actor, event.event_type, event.prior_state, event.next_state), (
            self.project, self.actor, "APPROVED", "DRAFT", "APPROVED",
        ))

    def test_rollback_after_first_movement_keeps_the_committed_fingerprints(self):
        self.prove_rollback_and_retry(StockMovement, ("DRAFT", 2, 1, 0, OPENING_QUANTITY))

    def test_rollback_after_audit_keeps_the_committed_fingerprints(self):
        self.prove_rollback_and_retry(AuditEvent, ("APPROVED", 2, 2, 1, EXPECTED_QUANTITY))


class StockPoolIdentityTests(TransactionTestCase):
    fixtures = ["s1_03_demo"]

    def test_pool_identity_edit_waits_for_first_opening_and_then_rechecks_movements(self):
        seed_stock(self)
        original_material_id = self.pool.material_id
        replacement_id = create_material("S108-POOL-EDIT").pk
        editor_ready = Queue()

        def edit_pool():
            try:
                execute("SET lock_timeout TO '15s'")
                editor_ready.put(backend_pid())
                ValuationPool.objects.filter(pk=self.pool.pk).update(material_id=replacement_id)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                post_opening_stock(balance_id=self.balance.pk, quantity=OPENING_QUANTITY)
                posting_pid = backend_pid()
                edit = executor.submit(edit_pool)
                editor_pid = editor_ready.get(timeout=WAIT_TIMEOUT)
                self.assertNotEqual(editor_pid, posting_pid)
                deadline = monotonic() + WAIT_TIMEOUT
                while monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_blocking_pids(%s)", [editor_pid])
                        if posting_pid in cursor.fetchone()[0]:
                            break
                    sleep(0.02)
                else:
                    self.fail("PostgreSQL did not report the pool identity edit waiting on the posting")

            with self.assertRaises(IntegrityError) as failure:
                edit.result(timeout=WAIT_TIMEOUT)

        original = failure.exception.__cause__
        self.assertEqual(original.sqlstate, "23514")
        self.assertEqual(original.diag.constraint_name, "stock_pool_identity_immutable")
        self.pool.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(self.pool.material_id, original_material_id)
        self.assertEqual(self.balance.quantity, OPENING_QUANTITY)
        self.assertEqual(StockMovement.objects.filter(balance=self.balance).count(), 1)
