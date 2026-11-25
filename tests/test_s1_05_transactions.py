from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep

from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.http import Http404
from django.test import TransactionTestCase, override_settings

from apps.access_control.models import ProjectAccess
from apps.access_control.services import grant_project_access, revoke_project_access
from apps.projects.models import Project, WorkNode
from apps.test_transactions.models import Attachment, AuditEvent, TestTransaction, TestTransactionLine
from apps.test_transactions.services import TransitionConflict, approve_transaction

from .test_s1_05_http import (
    TEST_HASHERS,
    TransactionHTTPTestsMixin,
    csrf_headers,
    make_employee,
    make_grantor,
    private_root,
    transaction_url,
)


LOCK_TIMEOUT = 8


@override_settings(PASSWORD_HASHERS=TEST_HASHERS)
class TransactionApprovalConcurrencyTests(TransactionHTTPTestsMixin, TransactionTestCase):
    fixtures = ["s1_03_demo"]

    def setUp(self):
        private_root(self)
        self.project = Project.objects.get(code="DEMO-A")
        self.node = WorkNode.objects.get(code="A-ITEM")
        self.operator = make_employee("race-operator")
        self.manager = make_employee("race-manager")
        self.waiting_manager = make_employee("race-second-manager")
        self.grantor = make_grantor()
        for actor, role in (
            (self.operator, ProjectAccess.Role.OPERATOR),
            (self.manager, ProjectAccess.Role.MANAGER),
            (self.waiting_manager, ProjectAccess.Role.MANAGER),
        ):
            grant_project_access(actor=self.grantor, user=actor, project=self.project, role=role)
        self.operator_client = self.sign_in(self.operator)
        self.document = self.create_draft(
            self.operator_client, self.project, (self.node, "Approval race line", "1", "2"),
        )

    def wait_until_postgresql_reports_blocking(self, ready):
        waiting_pid = ready.get(timeout=LOCK_TIMEOUT)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            locking_pid = cursor.fetchone()[0]
        self.assertNotEqual(waiting_pid, locking_pid)
        deadline = monotonic() + LOCK_TIMEOUT
        while monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_blocking_pids(%s)", [waiting_pid])
                if locking_pid in cursor.fetchone()[0]:
                    return
            sleep(0.02)
        self.fail(f"PostgreSQL did not report backend {waiting_pid} blocked by {locking_pid}")

    def approve_on_own_connection(self, ready):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout TO '12s'")
                cursor.execute("SELECT pg_backend_pid()")
                ready.put(cursor.fetchone()[0])
            return approve_transaction(
                actor=self.waiting_manager, project_id=self.project.pk, transaction_id=self.document.pk,
            )
        finally:
            connections.close_all()

    def revoke_waiting_manager_on_own_connection(self):
        try:
            membership = ProjectAccess.objects.get(user=self.waiting_manager, project=self.project)
            revoke_project_access(actor=self.grantor, membership_id=membership.pk)
        finally:
            connections.close_all()

    def archive_on_own_connection(self):
        try:
            with transaction.atomic():
                Project.objects.filter(pk=self.project.pk).update(status=Project.Status.ARCHIVED)
        finally:
            connections.close_all()

    def test_two_managers_contending_on_the_document_can_approve_only_once(self):
        ready = Queue()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                TestTransaction.objects.select_for_update().get(pk=self.document.pk)
                waiting_approval = executor.submit(self.approve_on_own_connection, ready)
                self.wait_until_postgresql_reports_blocking(ready)
                approve_transaction(
                    actor=self.manager, project_id=self.project.pk, transaction_id=self.document.pk,
                )
            with self.assertRaises(TransitionConflict):
                waiting_approval.result(timeout=LOCK_TIMEOUT)

        self.document.refresh_from_db()
        self.assertEqual(self.document.approved_by, self.manager)
        self.assertEqual(self.document.status, "APPROVED")
        event = AuditEvent.objects.get(test_transaction=self.document, event_type="APPROVED")
        self.assertEqual(event.actor, self.manager)

    def test_manager_whose_membership_is_revoked_while_waiting_cannot_approve(self):
        ready = Queue()
        with ThreadPoolExecutor(max_workers=2) as executor:
            with transaction.atomic():
                TestTransaction.objects.select_for_update().get(pk=self.document.pk)
                waiting_approval = executor.submit(self.approve_on_own_connection, ready)
                self.wait_until_postgresql_reports_blocking(ready)
                executor.submit(self.revoke_waiting_manager_on_own_connection).result(timeout=LOCK_TIMEOUT)
            with self.assertRaises(Http404):
                waiting_approval.result(timeout=LOCK_TIMEOUT)
        self.assert_unapproved()

    def test_project_archived_while_request_waits_for_lock_cannot_be_approved(self):
        ready = Queue()
        with ThreadPoolExecutor(max_workers=2) as executor:
            with transaction.atomic():
                TestTransaction.objects.select_for_update().get(pk=self.document.pk)
                waiting_approval = executor.submit(self.approve_on_own_connection, ready)
                self.wait_until_postgresql_reports_blocking(ready)
                executor.submit(self.archive_on_own_connection).result(timeout=LOCK_TIMEOUT)
            with self.assertRaises(ValidationError):
                waiting_approval.result(timeout=LOCK_TIMEOUT)
        self.assert_unapproved()

    def test_archived_project_refuses_new_create_and_approve_but_allows_history(self):
        url = transaction_url("create", self.project.pk)
        payload = self.draft_data(
            self.operator_client, self.project, (self.node, "Archived item", "1", "2"), title="Must not save",
        )
        payload.pop("csrfmiddlewaretoken")
        manager_client = self.sign_in(self.manager)
        self.archive_on_own_connection()
        self.assertEqual(self.operator_client.post(url, payload).status_code, 403)
        payload["attachment"].seek(0)
        self.assertEqual(self.operator_client.post(url, payload, headers=csrf_headers(self.operator_client)).status_code, 400)
        approval = manager_client.post(
            transaction_url("approve", self.project.pk, self.document.pk), headers=csrf_headers(manager_client),
        )
        self.assertEqual(approval.status_code, 400)
        self.assertEqual(manager_client.get(transaction_url("detail", self.project.pk, self.document.pk)).status_code, 200)
        for model in (TestTransaction, TestTransactionLine, Attachment, AuditEvent):
            self.assertEqual(model.objects.count(), 1)
        self.assert_unapproved()

    def assert_unapproved(self):
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, "DRAFT")
        self.assertIsNone(self.document.approved_by)
        self.assertIsNone(self.document.approved_at)
        self.assertFalse(AuditEvent.objects.filter(test_transaction=self.document, event_type="APPROVED").exists())
