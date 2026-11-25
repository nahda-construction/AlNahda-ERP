from decimal import Decimal
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.formats import localize

from apps.access_control.models import ProjectAccess
from apps.access_control.policies import EMPLOYEE_GROUP
from apps.access_control.services import grant_project_access
from apps.projects.models import Project, WorkNode
from apps.test_transactions.models import Attachment, AuditEvent, TestTransaction, TestTransactionLine


TEST_PASSWORD = "synthetic-transaction-test"
TEST_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
PDF_A = b"%PDF-1.7\n% Synthetic receipt A\n%%EOF\n"
PDF_B = b"%PDF-1.7\n% Synthetic receipt B\n%%EOF\n"


def transaction_url(name, *identifiers):
    return reverse(f"test_transactions:{name}", args=identifiers)


def access_permission(codename):
    return Permission.objects.get(
        content_type__app_label="access_control", content_type__model="projectaccess", codename=codename,
    )


def make_employee(username):
    employee = get_user_model().objects.create_user(username, password=TEST_PASSWORD)
    employee.groups.add(Group.objects.get_or_create(name=EMPLOYEE_GROUP)[0])
    return employee


def make_grantor():
    grantor = make_employee("test-grantor")
    grantor.user_permissions.add(access_permission("manage_project_access"))
    return grantor


def hidden_inputs(content):
    class HiddenInputs(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "input" and attributes.get("type") == "hidden" and "name" in attributes:
                values[attributes["name"]] = attributes.get("value", "")

    values = {}
    HiddenInputs().feed(content.decode("utf-8"))
    return values


def draft_fields(*lines, title="Synthetic transaction"):
    fields = {"title": title, "lines-TOTAL_FORMS": str(len(lines)), "lines-INITIAL_FORMS": "0"}
    for index, (node, description, quantity, price) in enumerate(lines):
        fields.update({
            f"lines-{index}-work_node": str(node.pk),
            f"lines-{index}-description": description,
            f"lines-{index}-qty": quantity,
            f"lines-{index}-unit_price": price,
        })
    return fields


def private_root(test_case):
    directory = TemporaryDirectory()
    test_case.addCleanup(directory.cleanup)
    root = Path(directory.name)
    test_case.enterContext(override_settings(ERP_PRIVATE_ROOT=root))
    return root


def csrf_headers(client):
    return {"X-CSRFToken": client.cookies[settings.CSRF_COOKIE_NAME].value}


class TransactionHTTPTestsMixin:
    def sign_in(self, actor):
        client = Client(enforce_csrf_checks=True)
        login = transaction_url("login")
        self.assertEqual(client.get(login).status_code, 200)
        response = client.post(
            login, {"username": actor.username, "password": TEST_PASSWORD}, headers=csrf_headers(client),
        )
        self.assertIn(response.status_code, (302, 303))
        return client

    def draft_data(self, client, project, *lines, title="Synthetic transaction", content=PDF_A):
        page = client.get(transaction_url("create", project.pk))
        self.assertEqual(page.status_code, 200)
        fields = hidden_inputs(page.content)
        fields.update(draft_fields(*lines, title=title))
        fields["attachment"] = SimpleUploadedFile("receipt.pdf", content, content_type="application/pdf")
        return fields

    def create_draft(self, client, project, *lines, title="Synthetic transaction", content=PDF_A):
        response = client.post(
            transaction_url("create", project.pk),
            self.draft_data(client, project, *lines, title=title, content=content),
            headers=csrf_headers(client),
        )
        self.assertIn(response.status_code, (302, 303))
        document = TestTransaction.objects.get(title=title, project=project)
        self.assertEqual(response["Location"], transaction_url("detail", project.pk, document.pk))
        return document


@override_settings(PASSWORD_HASHERS=TEST_HASHERS)
class TransactionHTTPTests(TransactionHTTPTestsMixin, TestCase):
    fixtures = ["s1_03_demo"]

    @classmethod
    def setUpTestData(cls):
        cls.project_a = Project.objects.get(code="DEMO-A")
        cls.project_b = Project.objects.get(code="DEMO-B")
        cls.node_a = WorkNode.objects.get(code="A-ITEM")
        cls.leaf_a = WorkNode.objects.get(code="A-ACT-2")
        cls.node_b = WorkNode.objects.get(code="B-ITEM")
        cls.operator_a = make_employee("test-operator-a")
        cls.operator_b = make_employee("test-operator-b")
        cls.manager_a = make_employee("test-manager-a")
        cls.manager_b = make_employee("test-manager-b")
        cls.viewer_a = make_employee("test-viewer-a")
        cls.viewer_download_a = make_employee("test-viewer-download-a")
        cls.viewer_download_a.user_permissions.add(access_permission("download_test_attachment"))
        cls.outsider = get_user_model().objects.create_user("external-account", password=TEST_PASSWORD)
        grantor = make_grantor()
        for project, actor, role in (
            (cls.project_a, cls.operator_a, ProjectAccess.Role.OPERATOR),
            (cls.project_b, cls.operator_b, ProjectAccess.Role.OPERATOR),
            (cls.project_a, cls.manager_a, ProjectAccess.Role.MANAGER),
            (cls.project_b, cls.manager_b, ProjectAccess.Role.MANAGER),
            (cls.project_a, cls.viewer_a, ProjectAccess.Role.VIEWER),
            (cls.project_a, cls.viewer_download_a, ProjectAccess.Role.VIEWER),
        ):
            grant_project_access(actor=grantor, user=actor, project=project, role=role)

    def setUp(self):
        private_root(self)

    def test_save_reopen_and_download_in_fresh_staff_session(self):
        sender_a = self.sign_in(self.operator_a)
        listing = sender_a.get(transaction_url("projects"))
        self.assertContains(listing, self.project_a.name)
        self.assertNotContains(listing, self.project_b.name)
        document = self.create_draft(
            sender_a, self.project_a,
            (self.node_a, "First synthetic line", "3", "12.3450"),
            (self.leaf_a, "Second synthetic line", "2", "5.0050"),
        )
        other_document = self.create_draft(
            self.sign_in(self.operator_b), self.project_b,
            (self.node_b, "Other project line", "1", "0"), content=PDF_B,
        )
        self.assertNotEqual(document.document_no, other_document.document_no)
        self.assertEqual(document.document_no[:4], "TST-")
        UUID(document.document_no[4:])
        self.assertEqual(document.status, "DRAFT")
        self.assertEqual(document.created_by, self.operator_a)
        self.assertEqual(sender_a.post(transaction_url("logout"), headers=csrf_headers(sender_a)).status_code, 302)

        reader = self.sign_in(self.operator_a)
        self.assertNotEqual(reader.cookies[settings.SESSION_COOKIE_NAME], sender_a.cookies[settings.SESSION_COOKIE_NAME])
        reopened = reader.get(transaction_url("detail", self.project_a.pk, document.pk))
        for token in (document.document_no, "First synthetic line", "Second synthetic line", localize(Decimal("47.05"))):
            self.assertContains(reopened, token)
        self.assertEqual(reopened.context["total"], Decimal("47.05"))
        self.assertIn("no-store", reopened["Cache-Control"])
        self.assertIn("private", reopened["Cache-Control"])
        self.assertEqual(list(TestTransactionLine.objects.filter(transaction=document).order_by("line_no").values_list(
            "line_no", "amount",
        )), [(1, Decimal("37.04")), (2, Decimal("10.01"))])

        attachment = Attachment.objects.get(transaction=document)
        response = reader.get(transaction_url("download", self.project_a.pk, document.pk, attachment.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        content = b"".join(response.streaming_content)
        self.assertEqual(content, PDF_A)
        self.assertEqual(attachment.size_bytes, len(content))
        self.assertEqual(attachment.sha256, sha256(content).hexdigest())

        event = AuditEvent.objects.get(test_transaction=document)
        self.assertEqual((event.event_type, event.prior_state, event.next_state), ("CREATED", None, "DRAFT"))
        self.assertEqual((event.actor, event.project), (self.operator_a, self.project_a))
        self.assertIsNotNone(event.occurred_at)
        self.assertIsNotNone(event.correlation_id)

    def test_attachment_and_document_visibility_respect_membership_and_download_permission(self):
        own = self.sign_in(self.operator_a)
        other = self.sign_in(self.operator_b)
        document = self.create_draft(own, self.project_a, (self.node_a, "Line A", "1", "1"))
        foreign = self.create_draft(other, self.project_b, (self.node_b, "Line B", "1", "1"), content=PDF_B)
        attachment = Attachment.objects.get(transaction=document)
        foreign_attachment = Attachment.objects.get(transaction=foreign)
        view = transaction_url("detail", self.project_a.pk, document.pk)
        download = transaction_url("download", self.project_a.pk, document.pk, attachment.pk)
        for client, path, missing in (
            (other, view, transaction_url("detail", self.project_a.pk, uuid4())),
            (other, download, transaction_url("download", self.project_a.pk, uuid4(), uuid4())),
            (own, transaction_url("download", self.project_a.pk, document.pk, foreign_attachment.pk),
             transaction_url("download", self.project_a.pk, document.pk, uuid4())),
        ):
            response, absent = client.get(path), client.get(missing)
            self.assertEqual((response.status_code, absent.status_code), (404, 404))
            self.assertEqual(response.content, absent.content)
            self.assertNotIn(attachment.storage_key.encode(), response.content)

        guessed = own.get(f"/{attachment.storage_key}")
        self.assertEqual(guessed.status_code, 404)
        self.assertNotIn(PDF_A, guessed.content)
        denied = Client().get(download)
        self.assertIn(denied.status_code, (302, 401, 403, 404))
        self.assertNotIn(PDF_A, denied.content)
        no_permission = self.sign_in(self.viewer_a)
        self.assertEqual(no_permission.get(view).status_code, 200)
        self.assertEqual(no_permission.get(download).status_code, 403)
        with_permission = self.sign_in(self.viewer_download_a)
        response = with_permission.get(download)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), PDF_A)

    def test_external_accounts_disabled_accounts_and_writes_without_csrf_are_rejected(self):
        own = self.sign_in(self.operator_a)
        create = transaction_url("create", self.project_a.pk)
        data = self.draft_data(own, self.project_a, (self.node_a, "CSRF rejected", "1", "1"))
        data.pop("csrfmiddlewaretoken", None)
        self.assertEqual(own.post(create, data).status_code, 403)
        self.assertEqual(own.post(transaction_url("logout")).status_code, 403)
        outsider = Client(enforce_csrf_checks=True)
        outsider.force_login(self.outsider)
        self.assertEqual(outsider.get(create).status_code, 403)
        get_user_model().objects.filter(pk=self.operator_a.pk).update(is_active=False)
        self.assertIn(own.get(create).status_code, (302, 401, 403))
        self.assertFalse(TestTransaction.objects.exists())

    def test_only_manager_of_the_project_can_approve_and_create_one_event(self):
        sender = self.sign_in(self.operator_a)
        document = self.create_draft(sender, self.project_a, (self.node_a, "Unchanged line", "1", "1.99"))
        attachment = Attachment.objects.get(transaction=document)
        before_lines = list(TestTransactionLine.objects.filter(transaction=document).values())
        approve = transaction_url("approve", self.project_a.pk, document.pk)
        manager = self.sign_in(self.manager_a)
        other_manager = self.sign_in(self.manager_b)
        self.assertEqual(manager.post(approve).status_code, 403)
        self.assertEqual(sender.post(approve, headers=csrf_headers(sender)).status_code, 403)
        self.assertEqual(other_manager.post(approve, headers=csrf_headers(other_manager)).status_code, 404)
        self.assertIn(manager.post(approve, headers=csrf_headers(manager)).status_code, (302, 303))
        self.assertEqual(manager.post(approve, headers=csrf_headers(manager)).status_code, 409)

        document.refresh_from_db()
        self.assertEqual(document.status, "APPROVED")
        self.assertEqual(document.approved_by, self.manager_a)
        self.assertIsNotNone(document.approved_at)
        self.assertEqual(list(TestTransactionLine.objects.filter(transaction=document).values()), before_lines)
        self.assertEqual(Attachment.objects.get(transaction=document).pk, attachment.pk)
        event = AuditEvent.objects.get(test_transaction=document, event_type="APPROVED")
        self.assertEqual((event.prior_state, event.next_state), ("DRAFT", "APPROVED"))
        self.assertEqual((event.actor, event.project), (self.manager_a, self.project_a))
        self.assertEqual(AuditEvent.objects.filter(test_transaction=document).count(), 2)

    def test_max_precision_product_uses_full_decimal_precision(self):
        document = self.create_draft(
            self.sign_in(self.operator_a), self.project_a,
            (self.node_a, "Precision boundary", "9101625612.9373", "980335932389.9697"),
        )
        line = TestTransactionLine.objects.get(transaction=document)
        self.assertEqual(line.amount, Decimal("8922650631523317462853.07"))
