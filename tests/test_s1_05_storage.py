import os
import socket
from http.client import HTTPResponse
from http.cookiejar import CookieJar
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import LiveServerTestCase, override_settings
from django.test.client import encode_multipart

from apps.access_control.models import ProjectAccess
from apps.access_control.services import grant_project_access
from apps.projects.models import Project, WorkNode
from apps.test_transactions.models import Attachment, AuditEvent, TestTransaction, TestTransactionLine

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


FIVE_MIB = 5 * 1024 * 1024
BOUNDARY = "s105-synthetic-multipart-boundary"


def synthetic_pdf(size=None):
    result = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
    offsets = []
    for index, value in enumerate((
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] /Resources << >> >>",
    ), start=1):
        offsets.append(len(result))
        result += f"{index} 0 obj\n".encode() + value + b"\nendobj\n"
    xref = b"xref\n0 4\n0000000000 65535 f \n" + b"".join(
        f"{offset:010} 00000 n \n".encode() for offset in offsets
    )
    trailer = b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n"
    end = b"\n%%EOF\n"
    if size is not None:
        padding = size - len(result + xref + trailer + end) - len(str(size - len(xref + trailer + end)))
        lines, remainder = divmod(padding, 72)
        result += (b"%" + b" " * 70 + b"\n") * lines
        if remainder:
            result += b"%" + b" " * (remainder - 2) + b"\n" if remainder > 1 else b"\n"
    content = result + xref + trailer + str(len(result)).encode() + end
    if size is not None and len(content) != size:
        raise ValueError("Synthetic PDF did not reach the requested size")
    return content


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@override_settings(
    PASSWORD_HASHERS=TEST_HASHERS,
    FILE_UPLOAD_MAX_MEMORY_SIZE=1024,
    STATIC_URL="/qa-static/",
    MEDIA_URL="/qa-media/",
)
class PrivateUploadHTTPTests(LiveServerTestCase):
    fixtures = ["s1_03_demo"]

    def setUp(self):
        self.root = private_root(self)
        self.uploading = self.root / "uploads"
        self.project = Project.objects.get(code="DEMO-A")
        self.node = WorkNode.objects.get(code="A-ITEM")
        self.operator = make_employee("live-upload-operator")
        grant_project_access(
            actor=make_grantor(), user=self.operator, project=self.project, role=ProjectAccess.Role.OPERATOR,
        )
        self.cookies = CookieJar()
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(self.cookies))
        self.login()

    def request(self, path, data=None, headers=None):
        request = Request(self.live_server_url + path, data=data, headers=headers or {})
        try:
            response = self.opener.open(request, timeout=12)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, response.headers, response.read()

    def csrf_header(self):
        return {"X-CSRFToken": next(cookie.value for cookie in self.cookies if cookie.name == settings.CSRF_COOKIE_NAME)}

    def login(self):
        path = transaction_url("login")
        self.assertEqual(self.request(path)[0], 200)
        payload = urlencode({"username": self.operator.username, "password": TEST_PASSWORD}).encode()
        self.assertIn(self.request(path, payload, self.csrf_header())[0], (302, 303))

    def form_input(self):
        self.create_path = transaction_url("create", self.project.pk)
        status, _, form = self.request(self.create_path)
        self.assertEqual(status, 200)
        fields = hidden_inputs(form)
        fields.update(draft_fields((self.node, "Wire upload", "1", "1")))
        return fields

    def upload(self, content, name="receipt.pdf", media_type="application/pdf", *, extra_file=False, **overrides):
        fields = self.form_input()
        fields.update(overrides)
        upload = SimpleUploadedFile(name, content, content_type=media_type)
        fields["attachment"] = [upload, SimpleUploadedFile("extra.pdf", synthetic_pdf(), "application/pdf")] if extra_file else upload
        body = encode_multipart(BOUNDARY, fields)
        headers = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", **self.csrf_header()}
        return self.request(self.create_path, body, headers)

    def counts(self):
        return tuple(model.objects.count() for model in (TestTransaction, TestTransactionLine, Attachment, AuditEvent))

    def pending_uploads(self):
        return list(self.uploading.iterdir()) if self.uploading.exists() else []

    def test_wire_upload_exactly_at_limit_succeeds_while_invalid_uploads_leave_no_rows(self):
        valid = synthetic_pdf(FIVE_MIB)
        for case, args, kwargs in (
            ("empty", (b"",), {}),
            ("oversized", (synthetic_pdf(FIVE_MIB + 1),), {}),
            ("no PDF signature", (b"Not a PDF",), {}),
            ("wrong extension", (synthetic_pdf(), "receipt.txt"), {}),
            ("wrong media type", (synthetic_pdf(), "receipt.pdf", "text/plain"), {}),
            ("multiple files", (synthetic_pdf(),), {"extra_file": True}),
            ("extra file field", (synthetic_pdf(),), {
                "unrecognized": SimpleUploadedFile("other.pdf", synthetic_pdf(), "application/pdf"),
            }),
            ("client sets amount", (synthetic_pdf(),), {"lines-0-amount": "999.99"}),
            ("client sets status", (synthetic_pdf(),), {"status": "APPROVED"}),
            ("client sets total", (synthetic_pdf(),), {"total": "999.99"}),
        ):
            with self.subTest(case=case):
                status, _, _ = self.upload(*args, **kwargs)
                self.assertIn(status, (400, 413))
                self.assertEqual(self.counts(), (0, 0, 0, 0))
                self.assertEqual(self.pending_uploads(), [])

        status, headers, _ = self.upload(valid)
        self.assertIn(status, (302, 303))
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(self.pending_uploads(), [])
        attachment = Attachment.objects.get()
        self.assertEqual(headers["Location"], transaction_url("detail", self.project.pk, attachment.transaction_id))
        self.assertEqual(attachment.size_bytes, FIVE_MIB)
        download = transaction_url("download", self.project.pk, attachment.transaction_id, attachment.pk)
        download_status, _, content = self.request(download)
        self.assertEqual(download_status, 200)
        self.assertEqual(content, valid)

    def test_wire_upload_without_content_length_is_rejected_before_consuming_oversized_body(self):
        fields = self.form_input()
        fields["attachment"] = SimpleUploadedFile("receipt.pdf", synthetic_pdf(FIVE_MIB + 1), "application/pdf")
        body = encode_multipart(BOUNDARY, fields)
        url = urlsplit(self.live_server_url)
        cookies = "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.cookies)
        request = (
            f"POST {self.create_path} HTTP/1.1\r\n"
            f"Host: {url.netloc}\r\n"
            f"Content-Type: multipart/form-data; boundary={BOUNDARY}\r\n"
            f"Cookie: {cookies}\r\n"
            f"X-CSRFToken: {self.csrf_header()['X-CSRFToken']}\r\n"
            "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            f"{len(body):x}\r\n"
        ).encode()
        with patch("apps.test_transactions.storage.PrivatePDFUploadHandler.new_file") as begin_staging:
            with socket.create_connection((url.hostname, url.port), timeout=8) as wire:
                # Announce the full oversized chunk, then wait for early rejection after sending its prefix.
                wire.sendall(request + body[:4096])
                with HTTPResponse(wire) as response:
                    response.begin()
                    self.assertIn(response.status, (400, 411, 413))
                    response.read()
                self.assertEqual(wire.recv(1), b"")
        begin_staging.assert_not_called()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.pending_uploads(), [])

    def test_wire_audit_failure_observes_disk_upload_and_rolls_back_all_rows(self):
        observed_uploads = []
        content = synthetic_pdf(32 * 1024)

        def fail_after_upload(**kwargs):
            observed_uploads.extend((path, path.read_bytes()) for path in self.pending_uploads())
            self.assertEqual(self.counts(), (1, 1, 1, 0))
            raise IntegrityError("synthetic failure while saving creation audit")

        with patch("apps.test_transactions.services.AuditEvent.objects.create", side_effect=fail_after_upload):
            status, _, _ = self.upload(content)
        self.assertGreaterEqual(status, 400)
        self.assertGreaterEqual(len(observed_uploads), 1)
        for observed_path, observed_content in observed_uploads:
            self.assertEqual(observed_path.parent, self.root / "uploads")
            self.assertEqual(observed_content, content)
            self.assertFalse(observed_path.exists())
        self.assertEqual(self.pending_uploads(), [])
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_wire_storage_failure_does_not_start_database_writes(self):
        observed_counts = []

        def fail_storage(*args, **kwargs):
            observed_counts.append(self.counts())
            raise OSError("synthetic storage write failure")

        with patch("apps.test_transactions.storage.publish", side_effect=fail_storage):
            status, _, _ = self.upload(synthetic_pdf(32 * 1024))
        self.assertGreaterEqual(status, 400)
        self.assertEqual(observed_counts, [(0, 0, 0, 0)])
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.pending_uploads(), [])

    def test_file_publish_collision_preserves_the_existing_file(self):
        original = synthetic_pdf()
        self.assertIn(self.upload(original)[0], (302, 303))
        attachment = Attachment.objects.get()
        target = self.root / attachment.storage_key
        actual_link = os.link
        attempts = []

        def force_collision(source, destination, **kwargs):
            attempts.append(destination)
            return actual_link(source, target.name, **kwargs)

        with patch("apps.test_transactions.storage.os.link", side_effect=force_collision):
            status, _, _ = self.upload(synthetic_pdf(32 * 1024))
        self.assertGreaterEqual(status, 400)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(self.pending_uploads(), [])

    def test_authorized_download_refuses_uncommitted_file_invalid_key_and_symlink(self):
        self.assertIn(self.upload(synthetic_pdf())[0], (302, 303))
        attachment = Attachment.objects.get()
        existing_key = attachment.storage_key
        existing_file = self.root / existing_key
        self.assertEqual(existing_file.read_bytes(), synthetic_pdf())
        download = transaction_url("download", self.project.pk, attachment.transaction_id, attachment.pk)
        missing = transaction_url("download", self.project.pk, attachment.transaction_id, uuid4())
        missing_status, _, missing_body = self.request(missing)
        self.assertEqual(missing_status, 404)

        orphan_id = uuid4()
        orphan = existing_file.with_stem(orphan_id.hex)
        orphan.write_bytes(b"%PDF-1.7\n% Private orphan marker\n%%EOF\n")
        orphan.chmod(0o600)
        absent_status, _, absent_body = self.request(
            transaction_url("download", self.project.pk, attachment.transaction_id, orphan_id),
        )
        self.assertEqual((absent_status, absent_body), (404, missing_body))

        outside = self.root.parent / f"{self.root.name}-outside.pdf"
        outside.write_bytes(b"%PDF-1.7\n% Outside storage marker\n%%EOF\n")
        self.addCleanup(outside.unlink)
        Attachment.objects.filter(pk=attachment.pk).update(storage_key=f"../{outside.name}")
        status, _, content = self.request(download)
        self.assertEqual((status, content), (404, missing_body))

        Attachment.objects.filter(pk=attachment.pk).update(storage_key=existing_key)
        safe = existing_file.with_name("saved-" + existing_file.name)
        existing_file.rename(safe)
        existing_file.symlink_to(outside)
        status, _, content = self.request(download)
        self.assertEqual((status, content), (404, missing_body))
        self.assertEqual(safe.read_bytes(), synthetic_pdf())
