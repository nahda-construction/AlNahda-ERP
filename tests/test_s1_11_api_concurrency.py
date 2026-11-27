import json
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from django.db import IntegrityError, connection
from django.test import LiveServerTestCase, override_settings

from apps.access_control.models import ProjectAccess
from apps.access_control.services import grant_project_access, revoke_project_access
from apps.projects.models import Project, WorkNode
from apps.test_transactions.models import AuditEvent, IdempotencyRecord

from .test_s1_05_http import TEST_HASHERS, TEST_PASSWORD, make_employee, make_grantor, private_root, transaction_url
from .test_s1_06_idempotency_concurrency import WireSession, current_backend_pid
from .test_s1_11_internal_api import EFFECT_MODELS, api_path, draft_payload


WAIT_TIMEOUT = 10
KEY_CONSTRAINT = "uq_idem_actor_proj_op_key"


class SyntheticRequestFailure(Exception):
    pass


@override_settings(PASSWORD_HASHERS=TEST_HASHERS)
class InternalAPIConcurrencyTests(LiveServerTestCase):
    fixtures = ["s1_03_demo"]

    def setUp(self):
        self.root = private_root(self)
        self.project = Project.objects.get(code="DEMO-A")
        self.node = WorkNode.objects.get(code="A-ITEM")
        self.operator = make_employee("s111-race-operator")
        self.grantor = make_grantor()
        self.membership = grant_project_access(
            actor=self.grantor, user=self.operator, project=self.project, role=ProjectAccess.Role.OPERATOR,
        )
        self.key = uuid4()
        self.path = api_path(self.project.pk)
        self.payload = draft_payload(self.node.pk)
        self.first = self.sign_in()
        self.second = self.sign_in()

    def sign_in(self):
        session = WireSession(self.live_server_url)
        login = transaction_url("login")
        self.assertEqual(session.request(login)[0], 200)
        data = urlencode({"username": self.operator.username, "password": TEST_PASSWORD}).encode()
        self.assertIn(session.request(login, data, session.csrf_header())[0], (302, 303))
        return session

    def post_json(self, session, payload):
        return session.request(self.path, json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "Idempotency-Key": str(self.key), **session.csrf_header(),
        })

    def counts(self):
        return tuple(model.objects.count() for model in EFFECT_MODELS)

    def wait_for_lock(self, winner_pid, waiter_pid):
        self.assertNotEqual(winner_pid, waiter_pid)
        self.assertNotIn(current_backend_pid(), (winner_pid, waiter_pid))
        deadline = monotonic() + WAIT_TIMEOUT
        while monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_blocking_pids(%s)", [waiter_pid])
                if winner_pid in cursor.fetchone()[0]:
                    return
            sleep(0.02)
        self.fail(f"PostgreSQL did not report backend {winner_pid} blocking backend {waiter_pid}")

    def race(self, *, second_changes=None, on_wait=None, fail_first=False):
        lock_attempts = Queue()
        observed_conflicts = Queue()
        first_wrote_audit = Event()
        release_first = Event()
        create_record = IdempotencyRecord.objects.create
        create_audit = AuditEvent.objects.create

        def record_insert(**fields):
            lock_attempts.put(current_backend_pid())
            try:
                return create_record(**fields)
            except IntegrityError as error:
                observed_conflicts.put((error.__cause__.sqlstate, error.__cause__.diag.constraint_name))
                raise

        def record_audit(**fields):
            event = create_audit(**fields)
            if not first_wrote_audit.is_set():
                self.assertEqual(self.counts(), (1, 1, 0, 1, 1))
                self.assertEqual(IdempotencyRecord.objects.get(key=self.key).status, "IN_PROGRESS")
                first_wrote_audit.set()
                if not release_first.wait(timeout=WAIT_TIMEOUT * 2):
                    raise AssertionError("First request did not receive the release signal")
                if fail_first:
                    raise SyntheticRequestFailure("Rollback before completing the API receipt")
            return event

        second_payload = self.payload | (second_changes or {})
        with (
            patch.object(IdempotencyRecord.objects, "create", side_effect=record_insert),
            patch.object(AuditEvent.objects, "create", side_effect=record_audit),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(self.post_json, self.first, self.payload)
            try:
                winner_pid = lock_attempts.get(timeout=WAIT_TIMEOUT)
                self.assertTrue(first_wrote_audit.wait(timeout=WAIT_TIMEOUT))
                second = executor.submit(self.post_json, self.second, second_payload)
                waiter_pid = lock_attempts.get(timeout=WAIT_TIMEOUT)
                self.wait_for_lock(winner_pid, waiter_pid)
                self.assertFalse(second.done())
                self.assertEqual(self.counts(), (0, 0, 0, 0, 0))
                if on_wait is not None:
                    on_wait()
            finally:
                release_first.set()
            first_result = first.result(timeout=WAIT_TIMEOUT)
            second_result = second.result(timeout=WAIT_TIMEOUT)

        if fail_first:
            self.assertTrue(observed_conflicts.empty())
        else:
            self.assertEqual(observed_conflicts.get_nowait(), ("23505", KEY_CONSTRAINT))
            self.assertTrue(observed_conflicts.empty())
        self.assertEqual(list(self.root.iterdir()), [])
        return first_result, second_result

    def assert_committed_result(self, response):
        status, headers, body = response
        self.assertEqual(status, 201, body)
        self.assertEqual(headers.get("Cache-Control"), "private, no-store")
        self.assertEqual(self.counts(), (1, 1, 0, 1, 1))
        result = json.loads(body)
        receipt = IdempotencyRecord.objects.get(key=self.key)
        self.assertEqual((receipt.status, receipt.response_status), ("COMPLETED", 201))
        self.assertEqual((str(receipt.test_transaction_id), receipt.actor_id, receipt.project_id), (
            result["id"], self.operator.pk, self.project.pk,
        ))
        self.assertEqual(result["attachment_ids"], [])
        self.assertEqual(AuditEvent.objects.get().test_transaction_id, receipt.test_transaction_id)
        return result

    def test_identical_requests_wait_and_receive_one_committed_snapshot(self):
        first, second = self.race()
        result = self.assert_committed_result(first)
        self.assertEqual((second[0], second[2]), (201, first[2]))
        self.assertEqual(result["total"], "37.04")

    def test_changed_payload_waits_then_returns_conflict(self):
        first, second = self.race(second_changes={"title": "Changed after blocking"})
        result = self.assert_committed_result(first)
        self.assertEqual(second[0], 409)
        self.assertEqual(json.loads(second[2])["code"], "conflict")
        self.assertNotIn(result["id"].encode(), second[2])

    def test_membership_revoked_while_waiting_cannot_disclose_receipt(self):
        first, second = self.race(on_wait=lambda: revoke_project_access(
            actor=self.grantor, membership_id=self.membership.pk,
        ))
        result = self.assert_committed_result(first)
        self.assertEqual(second[0], 404)
        self.assertEqual(json.loads(second[2])["code"], "not_found")
        self.assertNotIn(result["id"].encode(), second[2])
        self.assertNotIn(result["reference"].encode(), second[2])
        self.assertEqual(self.second.request(api_path(self.project.pk, item_id=result["id"]))[0], 404)

    def test_waiting_request_can_commit_once_after_winner_rolls_back(self):
        first, second = self.race(fail_first=True)
        self.assertEqual(first[0], 500)
        self.assert_committed_result(second)
        subsequent = self.post_json(self.first, self.payload)
        self.assertEqual((subsequent[0], subsequent[2]), (201, second[2]))
        self.assertEqual(self.counts(), (1, 1, 0, 1, 1))
