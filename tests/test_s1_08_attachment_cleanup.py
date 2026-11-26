import errno
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, connections, transaction
from django.test import LiveServerTestCase, override_settings
from django.test.client import encode_multipart
from psycopg import sql

from apps.access_control.models import ProjectAccess
from apps.access_control.services import grant_project_access
from apps.projects.models import Project, WorkNode
from apps.test_transactions import services, storage
from apps.test_transactions.models import (
    Attachment,
    AuditEvent,
    IdempotencyRecord,
    TestTransaction,
    TestTransactionLine,
)
from tests.test_s1_05_http import (
    TEST_HASHERS,
    TEST_PASSWORD,
    draft_fields,
    hidden_inputs,
    make_employee,
    make_grantor,
    private_root,
    transaction_url,
)
from tests.test_s1_05_storage import synthetic_pdf
from tests.test_s1_06_idempotency_concurrency import WireSession


EFFECT_MODELS = (IdempotencyRecord, TestTransaction, TestTransactionLine, Attachment, AuditEvent)
BOUNDARY = "s108-synthetic-upload"


@override_settings(PASSWORD_HASHERS=TEST_HASHERS, STATIC_URL="/qa-static/", MEDIA_URL="/qa-media/")
class AttachmentCleanupTests(LiveServerTestCase):
    fixtures = ["s1_03_demo"]

    def setUp(self):
        self.root = private_root(self)
        self.project = Project.objects.get(code="DEMO-A")
        self.node = WorkNode.objects.get(code="A-ITEM")
        self.operator = make_employee("cleanup-operator")
        grant_project_access(
            actor=make_grantor(), user=self.operator, project=self.project,
            role=ProjectAccess.Role.OPERATOR,
        )
        self.pdf = synthetic_pdf(32 * 1024)
        self.create_path = transaction_url("create", self.project.pk)
        self.session = WireSession(self.live_server_url)
        login = transaction_url("login")
        self.assertEqual(self.session.request(login)[0], 200)
        body = urlencode({"username": self.operator.username, "password": TEST_PASSWORD}).encode()
        self.assertIn(self.session.request(login, body, self.session.csrf_header())[0], (302, 303))

    def form(self):
        status, _, content = self.session.request(self.create_path)
        self.assertEqual(status, 200)
        fields = hidden_inputs(content)
        fields.update(draft_fields((self.node, "Synthetic rollback line", "3", "12.3450")))
        return fields

    def upload(self, form):
        fields = {**form, "attachment": self.uploaded_file()}
        body = encode_multipart(BOUNDARY, fields)
        headers = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", **self.session.csrf_header()}
        return self.session.request(self.create_path, body, headers)

    def uploaded_file(self):
        return SimpleUploadedFile("synthetic.pdf", self.pdf, content_type="application/pdf")

    def effects(self):
        return tuple(list(model.objects.order_by("pk").values()) for model in EFFECT_MODELS)

    def make_orphan(self, root):
        path = root / f"{uuid4().hex}.pdf"
        path.write_bytes(self.pdf)
        path.chmod(0o600)
        return path

    def test_wire_after_attachment_insert_rollback_retry_and_cleanup(self):
        self.assertEqual(self.upload(self.form())[0], 303)
        prior = self.effects()
        retained = self.root / Attachment.objects.get().storage_key
        form = self.form()
        original_create = Attachment.objects.create
        attempted = []

        def fail_after_insert(**fields):
            attempted.append(original_create(**fields))
            raise IntegrityError("Synthetic failure after inserting the attachment reference.")

        with patch.object(Attachment.objects, "create", side_effect=fail_after_insert):
            self.assertEqual(self.upload(form)[0], 500)
        self.assertEqual(self.effects(), prior)
        self.assertEqual(len(attempted), 1)
        failed = attempted[0]
        orphan = self.root / failed.storage_key
        self.assertEqual(orphan.read_bytes(), self.pdf)
        denied = transaction_url("download", self.project.pk, failed.transaction_id, failed.pk)
        self.assertEqual(self.session.request(denied)[0], 404)

        status, headers, _ = self.upload(form)
        self.assertEqual(status, 303)
        record = IdempotencyRecord.objects.get(key=form["idempotency_key"])
        committed = Attachment.objects.get(transaction=record.test_transaction)
        download = transaction_url("download", self.project.pk, record.test_transaction_id, committed.pk)
        download_status, _, content = self.session.request(download)
        self.assertEqual((download_status, content), (200, self.pdf))
        self.assertEqual(committed.sha256, failed.sha256)
        self.assertEqual(tuple(map(len, self.effects())), (2, 2, 2, 2, 2))
        self.assertEqual(self.upload(form)[1]["Location"], headers["Location"])
        self.assertEqual(tuple(map(len, self.effects())), (2, 2, 2, 2, 2))

        outcome = services.cleanup_orphaned_attachments()
        self.assertEqual(outcome.removed, (orphan.name,))
        self.assertEqual(outcome.failures, ())
        self.assertFalse(orphan.exists())
        self.assertEqual(retained.read_bytes(), self.pdf)
        self.assertEqual((self.root / committed.storage_key).read_bytes(), self.pdf)

    def test_wire_write_failure_leaves_no_committed_effect_or_reference(self):
        form = self.form()
        prior = self.effects()
        with patch.object(storage.os, "link", side_effect=PermissionError(errno.EACCES, "Synthetic write failure")):
            self.assertEqual(self.upload(form)[0], 503)
        self.assertEqual(self.effects(), prior)
        self.assertEqual(list(self.root.glob("*.pdf")), [])
        self.assertEqual(list((self.root / "uploads").iterdir()), [])

    def test_cleanup_waits_for_outer_attachment_commit(self):
        other_orphan = self.make_orphan(self.root)

        def attempt_from_separate_connection(attachment_pk):
            try:
                self.assertFalse(Attachment.objects.filter(pk=attachment_pk).exists())
                with self.assertRaises(services.AttachmentCleanupBusy):
                    services.cleanup_orphaned_attachments()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                with transaction.atomic():
                    result = services.create_draft_once(
                        actor=self.operator, project_id=self.project.pk, key=uuid4(), title="Outer commit",
                        lines=[{
                            "work_node": self.node.pk, "description": "", "qty": "1", "unit_price": "2",
                        }],
                        uploaded_file=self.uploaded_file(),
                    )
                attachment = Attachment.objects.get(transaction_id=result.test_transaction_id)
                committed_path = self.root / attachment.storage_key
                self.assertTrue(committed_path.is_file())
                executor.submit(attempt_from_separate_connection, attachment.pk).result(timeout=10)
                self.assertTrue(other_orphan.exists())
                self.assertTrue(committed_path.exists())

        outcome = services.cleanup_orphaned_attachments()
        self.assertEqual(outcome.removed, (other_orphan.name,))
        self.assertEqual(committed_path.read_bytes(), self.pdf)
        self.assertEqual(services.cleanup_orphaned_attachments().removed, ())

    def test_cleanup_keeps_non_candidates_and_reports_partial_failure(self):
        first = self.make_orphan(self.root)
        second = self.make_orphan(self.root)
        staging = self.root / "uploads"
        staging.mkdir(mode=0o700)
        temporary = staging / "synthetic.tmp"
        temporary.write_bytes(b"Upload in progress")
        non_candidate = self.root / "notes.txt"
        non_candidate.write_text("Keep")
        with TemporaryDirectory() as second_root:
            elsewhere = self.make_orphan(Path(second_root))
            link = self.root / f"{uuid4().hex}.pdf"
            link.symlink_to(elsewhere)
            original_unlink = storage.os.unlink

            def fail_one(key, *, dir_fd=None):
                if key == first.name:
                    raise PermissionError(errno.EACCES, "Synthetic unlink failure")
                return original_unlink(key, dir_fd=dir_fd)

            with patch.object(storage.os, "unlink", side_effect=fail_one):
                outcome = services.cleanup_orphaned_attachments()
            self.assertEqual(outcome.removed, (second.name,))
            self.assertEqual(outcome.failures, ((first.name, errno.EACCES),))
            self.assertTrue(first.exists())
            self.assertFalse(second.exists())
            self.assertEqual(services.cleanup_orphaned_attachments().removed, (first.name,))
            self.assertEqual(services.cleanup_orphaned_attachments().removed, ())
            self.assertTrue(elsewhere.is_file())
            self.assertTrue(link.is_symlink())
            self.assertTrue(temporary.is_file())
            self.assertTrue(non_candidate.is_file())

    def test_cleanup_denies_non_owner_even_with_attachment_select(self):
        path = self.make_orphan(self.root)
        role = sql.Identifier(f"s108_cleanup_qa_{uuid4().hex}")
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(role))
            try:
                cursor.execute(sql.SQL("GRANT SELECT ON public.test_transactions_attachment TO {}").format(role))
                cursor.execute(sql.SQL("SET ROLE {}").format(role))
                try:
                    with self.assertRaises(PermissionDenied):
                        services.cleanup_orphaned_attachments()
                    self.assertTrue(path.exists())
                finally:
                    cursor.execute("RESET ROLE")
            finally:
                cursor.execute(sql.SQL("DROP OWNED BY {}").format(role))
                cursor.execute(sql.SQL("DROP ROLE {}").format(role))
