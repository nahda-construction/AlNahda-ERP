from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.access_control.models import ProjectAccess
from apps.access_control.services import grant_project_access
from apps.printing.client import PdfBusy, PdfUnavailable
from apps.projects.models import Project, WorkNode
from apps.test_transactions.models import TestTransaction, TestTransactionLine

from .test_s1_05_http import PDF_A, TEST_HASHERS, access_permission, make_employee, make_grantor, transaction_url


@override_settings(PASSWORD_HASHERS=TEST_HASHERS)
class PrintAccessTests(TestCase):
    fixtures = ["s1_03_demo"]

    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.get(code="DEMO-A")
        cls.other_project = Project.objects.get(code="DEMO-B")
        cls.node = WorkNode.objects.get(code="A-ITEM")
        cls.operator = make_employee("printing-operator")
        cls.reader = make_employee("printing-reader")
        cls.print_reader = make_employee("printing-permitted-reader")
        cls.other_operator = make_employee("printing-other-operator")
        cls.owner = get_user_model().objects.create_user("printing-external-owner")
        cls.print_reader.user_permissions.add(access_permission("print_test_document"))
        grantor = make_grantor()
        for actor, project, role in (
            (cls.operator, cls.project, ProjectAccess.Role.OPERATOR),
            (cls.reader, cls.project, ProjectAccess.Role.VIEWER),
            (cls.print_reader, cls.project, ProjectAccess.Role.VIEWER),
            (cls.other_operator, cls.other_project, ProjectAccess.Role.OPERATOR),
        ):
            grant_project_access(actor=grantor, user=actor, project=project, role=role)

        cls.draft = TestTransaction.objects.create(
            project=cls.project, created_by=cls.operator, title="عينة <script>خارجية</script>",
        )
        TestTransactionLine.objects.create(
            transaction=cls.draft, project=cls.project, work_node=cls.node, line_no=1,
            description='توريد PO-17 ١٢٣ 456 <img src="https://outside.invalid/private">',
            qty=Decimal("2.0000"), unit_price=Decimal("5.0000"), amount=Decimal("10.00"),
        )
        cls.approved = TestTransaction.objects.create(
            project=cls.project, created_by=cls.operator, title="عينة معتمدة",
            status=TestTransaction.Status.APPROVED, approved_by=grantor, approved_at=timezone.now(),
        )
        cls.cancelled = TestTransaction.objects.create(
            project=cls.project, created_by=cls.operator, title="عينة ملغاة",
            status=TestTransaction.Status.CANCELLED, cancelled_by=grantor,
            cancelled_at=timezone.now(), cancel_reason="اختبار",
        )

    def client_as(self, actor):
        client = Client()
        client.force_login(actor)
        return client

    def print_url(self, document):
        return transaction_url("print", document.project_id, document.pk)

    def assert_private(self, response):
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")

    @patch("apps.test_transactions.services.print_client.render_pdf", return_value=PDF_A)
    def test_draft_pdf_uses_the_database_state_and_escapes_worker_markup(self, render_pdf):
        response = self.client_as(self.operator).get(self.print_url(self.draft), {
            "status": "APPROVED", "total": "999.99", "url": "https://outside.invalid/request",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PDF_A)
        self.assertEqual(response.headers["Content-Type"], "application/pdf")
        self.assertEqual(response.headers["Content-Disposition"], f'attachment; filename="{self.draft.document_no}.pdf"')
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assert_private(response)
        render_pdf.assert_called_once()
        markup = render_pdf.call_args.args[0]
        for value in (
            self.draft.document_no, 'lang="ar" dir="rtl"', "مسودة", "PO-17 ١٢٣ 456", "10.00",
            "&lt;script&gt;", "&lt;img src=&quot;", "https://assets.erp.invalid/print.css",
            '<template id="print-logo">', "https://assets.erp.invalid/logo.png",
            "شركة النهضة للبناء والتشييد", "display: table-header-group",
        ):
            self.assertIn(value, markup)
        for value in ("معتمدة", "999.99", "outside.invalid/request", "<script>", '<img src="https://outside.invalid/private"'):
            self.assertNotIn(value, markup)

    @patch("apps.test_transactions.services.print_client.render_pdf", return_value=PDF_A)
    def test_print_permission_controls_detail_link_and_actual_approved_pdf(self, render_pdf):
        detail = transaction_url("detail", self.project.pk, self.approved.pk)
        print_url = self.print_url(self.approved)
        reader = self.client_as(self.reader)
        self.assertNotContains(reader.get(detail), print_url)
        denied = reader.get(print_url)
        self.assertEqual(denied.status_code, 403)
        self.assert_private(denied)
        render_pdf.assert_not_called()

        permitted = self.client_as(self.print_reader)
        self.assertContains(permitted.get(detail), print_url)
        self.assertEqual(permitted.get(print_url, {"status": "DRAFT"}).status_code, 200)
        render_pdf.assert_called_once()
        self.assertIn("الحالة</strong> معتمدة", render_pdf.call_args.args[0])
        self.assertIn("تاريخ الاعتماد", render_pdf.call_args.args[0])

    @patch("apps.test_transactions.services.print_client.render_pdf", return_value=PDF_A)
    def test_unauthorized_or_unprintable_requests_never_call_worker(self, render_pdf):
        path = self.print_url(self.draft)
        no_session = Client().get(path)
        self.assertEqual(no_session.status_code, 302)
        self.assert_private(no_session)
        owner = self.client_as(self.owner).get(path)
        self.assertEqual(owner.status_code, 403)
        self.assert_private(owner)

        other_operator = self.client_as(self.other_operator)
        cross_project = other_operator.get(path)
        missing = other_operator.get(transaction_url("print", self.project.pk, uuid4()))
        wrong_project = other_operator.get(transaction_url("print", self.other_project.pk, self.draft.pk))
        self.assertEqual(cross_project.status_code, 404)
        self.assertEqual((cross_project.status_code, cross_project.content), (missing.status_code, missing.content))
        self.assertEqual((cross_project.status_code, cross_project.content), (wrong_project.status_code, wrong_project.content))
        self.assert_private(cross_project)

        operator = self.client_as(self.operator)
        detail = transaction_url("detail", self.project.pk, self.cancelled.pk)
        cancelled_path = self.print_url(self.cancelled)
        self.assertNotContains(operator.get(detail), cancelled_path)
        cancelled = operator.get(cancelled_path, {"status": "APPROVED"})
        self.assertEqual(cancelled.status_code, 404)
        self.assert_private(cancelled)
        self.assertEqual(operator.head(path).status_code, 405)
        render_pdf.assert_not_called()

    @patch("apps.test_transactions.services.print_client.render_pdf")
    def test_worker_busy_and_failure_are_private_and_expose_no_worker_details(self, render_pdf):
        client = self.client_as(self.operator)
        for error, status in ((PdfBusy, 429), (PdfUnavailable, 503)):
            with self.subTest(status=status):
                render_pdf.side_effect = error("internal worker information")
                response = client.get(self.print_url(self.draft))
                self.assertContains(response, "الطباعة", status_code=status)
                self.assertNotContains(response, "internal worker information", status_code=status)
                self.assert_private(response)
                self.assertEqual(response.headers.get("Retry-After"), "5" if status == 429 else None)
