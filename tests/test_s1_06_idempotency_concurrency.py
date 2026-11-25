from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.cookiejar import CookieJar
from queue import Queue
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection
from django.test import LiveServerTestCase, override_settings
from django.test.client import encode_multipart

from apps.access_control.models import ProjectAccess
from apps.access_control.services import grant_project_access, revoke_project_access
from apps.projects.models import Project, WorkNode
from apps.test_transactions.models import (
    Attachment,
    AuditEvent,
    IdempotencyRecord,
    TestTransaction,
    TestTransactionLine,
)

from .test_s1_05_http import (
    TEST_HASHERS,
    TEST_PASSWORD,
    draft_fields,
    hidden_inputs,
    make_employee,
    make_grantor,
    private_root,
    transaction_url,
)
from .test_s1_05_storage import NoRedirect, synthetic_pdf


WAIT_TIMEOUT = 10
BOUNDARY = "s106-synthetic-upload-boundary"
EFFECT_MODELS = (IdempotencyRecord, TestTransaction, TestTransactionLine, Attachment, AuditEvent)


class WireSession:
    def __init__(self, base_url):
        self.base_url = base_url
        self.cookies = CookieJar()
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(self.cookies))

    def request(self, path, data=None, headers=None):
        request = Request(self.base_url + path, data=data, headers=headers or {})
        try:
            response = self.opener.open(request, timeout=WAIT_TIMEOUT * 3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, response.headers, response.read()

    def csrf_header(self):
        cookie = next(cookie for cookie in self.cookies if cookie.name == settings.CSRF_COOKIE_NAME)
        return {"X-CSRFToken": cookie.value}


@dataclass(frozen=True)
class PendingInsert:
    pid: int
    uploads: frozenset


def current_backend_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


@override_settings(
    PASSWORD_HASHERS=TEST_HASHERS,
    STATIC_URL="/qa-static/",
    MEDIA_URL="/qa-media/",
    FILE_UPLOAD_MAX_MEMORY_SIZE=1024,
)
class IdempotencyConcurrencyTests(LiveServerTestCase):
    fixtures = ["s1_03_demo"]

    def setUp(self):
        self.root = private_root(self)
        self.project = Project.objects.get(code="DEMO-A")
        self.node = WorkNode.objects.get(code="A-ITEM")
        self.operator = make_employee("idempotency-race-operator")
        self.grantor = make_grantor()
        self.membership = grant_project_access(
            actor=self.grantor, user=self.operator, project=self.project, role=ProjectAccess.Role.OPERATOR,
        )
        self.create_path = transaction_url("create", self.project.pk)
        self.pdf = synthetic_pdf(32 * 1024)
        self.first, self.second = self.sign_in(), self.sign_in()
        self.first_form = self.form(self.first)
        self.second_form = self.form(self.second)
        self.key = self.first_form["idempotency_key"]
        self.second_form["idempotency_key"] = self.key

    def sign_in(self):
        session = WireSession(self.live_server_url)
        login = transaction_url("login")
        self.assertEqual(session.request(login)[0], 200)
        data = urlencode({"username": self.operator.username, "password": TEST_PASSWORD}).encode()
        self.assertIn(session.request(login, data, session.csrf_header())[0], (302, 303))
        return session

    def form(self, session):
        status, _, content = session.request(self.create_path)
        self.assertEqual(status, 200)
        fields = hidden_inputs(content)
        fields.update(draft_fields((self.node, "Wire race line", "3", "12.3450")))
        return fields

    def upload(self, session, form):
        fields = dict(form)
        fields["attachment"] = SimpleUploadedFile("race.pdf", self.pdf, content_type="application/pdf")
        data = encode_multipart(BOUNDARY, fields)
        headers = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", **session.csrf_header()}
        return session.request(self.create_path, data, headers)

    def pending_uploads(self):
        directory = self.root / "uploads"
        return frozenset(directory.iterdir()) if directory.exists() else frozenset()

    def assert_blocks_in_postgresql(self, winner, waiting):
        self.assertNotEqual(winner.pid, waiting.pid)
        self.assertNotIn(current_backend_pid(), (winner.pid, waiting.pid))
        deadline = monotonic() + WAIT_TIMEOUT
        while monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_blocking_pids(%s)", [waiting.pid])
                if winner.pid in cursor.fetchone()[0]:
                    return
            sleep(0.02)
        self.fail(f"PostgreSQL did not report waiter {waiting.pid} blocked by winner {winner.pid}")

    def race(self, *, on_wait=None, fail_first=False):
        attempted_inserts = Queue()
        reached_audit = Event()
        release_first = Event()
        first_attachment = Queue()
        create_record = IdempotencyRecord.objects.create
        create_audit = AuditEvent.objects.create

        def record_insert(**kwargs):
            attempted_inserts.put(PendingInsert(current_backend_pid(), self.pending_uploads()))
            return create_record(**kwargs)

        def record_audit(**kwargs):
            if not reached_audit.is_set():
                document = kwargs["test_transaction"]
                first_attachment.put(Attachment.objects.get(transaction=document))
                self.assertEqual(IdempotencyRecord.objects.get().status, "IN_PROGRESS")
                reached_audit.set()
                if not release_first.wait(timeout=WAIT_TIMEOUT * 2):
                    raise AssertionError("First request did not receive the transaction release signal")
                if fail_first:
                    raise IntegrityError("Synthetic failure after staging and before creation audit")
            return create_audit(**kwargs)

        with (
            patch.object(IdempotencyRecord.objects, "create", side_effect=record_insert),
            patch.object(AuditEvent.objects, "create", side_effect=record_audit),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(self.upload, self.first, self.first_form)
            try:
                winner = attempted_inserts.get(timeout=WAIT_TIMEOUT)
                self.assertTrue(reached_audit.wait(timeout=WAIT_TIMEOUT), "First request did not reach audit write")
                original_attachment = first_attachment.get_nowait()
                second = executor.submit(self.upload, self.second, self.second_form)
                waiting = attempted_inserts.get(timeout=WAIT_TIMEOUT)
                self.assert_blocks_in_postgresql(winner, waiting)
                self.assertFalse(second.done())
                self.assertEqual([model.objects.count() for model in EFFECT_MODELS], [0, 0, 0, 0, 0])
                waiter_uploads = waiting.uploads - winner.uploads
                self.assertTrue(winner.uploads)
                self.assertTrue(waiter_uploads)
                self.assertTrue(winner.uploads.issubset(waiting.uploads))
                for path in waiting.uploads:
                    self.assertEqual(path.parent, self.root / "uploads")
                    self.assertEqual(path.read_bytes(), self.pdf)
                if on_wait is not None:
                    on_wait()
            finally:
                release_first.set()
            first_response = first.result(timeout=WAIT_TIMEOUT)
            self.assertTrue(all(not path.exists() for path in winner.uploads))
            second_response = second.result(timeout=WAIT_TIMEOUT)
            self.assertTrue(all(not path.exists() for path in waiter_uploads))
        self.assertEqual(self.pending_uploads(), frozenset())
        return first_response, second_response, original_attachment

    def assert_single_completed_effect(self, location):
        self.assertEqual([model.objects.count() for model in EFFECT_MODELS], [1, 1, 1, 1, 1])
        record = IdempotencyRecord.objects.get()
        self.assertEqual(record.status, "COMPLETED")
        self.assertEqual(record.actor, self.operator)
        self.assertEqual(record.project, self.project)
        self.assertEqual(str(record.key), self.key)
        self.assertEqual(AuditEvent.objects.get().event_type, "CREATED")
        self.assertEqual(location, transaction_url("detail", self.project.pk, record.test_transaction_id))
        return record

    def test_identical_requests_contend_for_one_key_and_receive_same_location(self):
        first, second, _ = self.race()
        self.assertEqual((first[0], second[0]), (303, 303))
        self.assertEqual(first[1]["Location"], second[1]["Location"])
        self.assert_single_completed_effect(first[1]["Location"])

    def test_different_payload_contending_for_one_key_returns_conflict(self):
        self.second_form["title"] = "Different payload for the same key"
        first, second, _ = self.race()
        self.assertEqual((first[0], second[0]), (303, 409))
        self.assertIsNone(second[1].get("Location"))
        record = self.assert_single_completed_effect(first[1]["Location"])
        self.assertEqual(record.test_transaction.title, self.first_form["title"])

    def test_membership_revoked_during_key_wait_hides_the_winners_receipt(self):
        first, second, _ = self.race(on_wait=lambda: revoke_project_access(
            actor=self.grantor, membership_id=self.membership.pk,
        ))
        self.assertEqual((first[0], second[0]), (303, 404))
        self.assertIsNone(second[1].get("Location"))
        record = self.assert_single_completed_effect(first[1]["Location"])
        self.assertNotIn(str(record.test_transaction_id).encode(), second[2])
        self.assertNotIn(record.test_transaction.document_no.encode(), second[2])
        self.assertEqual(self.second.request(first[1]["Location"])[0], 404)

    def test_failed_wire_upload_rolls_back_key_and_waiter_retries_with_one_durable_effect(self):
        first, second, orphan = self.race(fail_first=True)
        self.assertEqual((first[0], second[0]), (500, 303))
        record = self.assert_single_completed_effect(second[1]["Location"])
        attachment = Attachment.objects.get(transaction=record.test_transaction)
        self.assertNotEqual(orphan.pk, attachment.pk)
        self.assertNotEqual(orphan.storage_key, attachment.storage_key)
        self.assertEqual((self.root / orphan.storage_key).read_bytes(), self.pdf)
        download = transaction_url("download", self.project.pk, record.test_transaction_id, attachment.pk)
        status, _, content = self.second.request(download)
        self.assertEqual((status, content), (200, self.pdf))

        orphan_url = transaction_url("download", self.project.pk, orphan.transaction_id, orphan.pk)
        missing_url = transaction_url("download", self.project.pk, orphan.transaction_id, uuid4())
        status, _, content = self.first.request(orphan_url)
        missing_status, _, missing_content = self.first.request(missing_url)
        self.assertEqual((status, content), (404, missing_content))
        self.assertEqual(missing_status, 404)
        self.assertNotIn(self.pdf, content)
        self.assertFalse(Attachment.objects.filter(pk=orphan.pk).exists())

        replay = self.upload(self.first, self.first_form)
        self.assertEqual((replay[0], replay[1]["Location"]), (303, second[1]["Location"]))
        self.assertEqual(self.pending_uploads(), frozenset())
        self.assert_single_completed_effect(second[1]["Location"])
