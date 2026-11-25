from decimal import Decimal
from uuid import UUID

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from apps.projects.models import Project, WorkNode
from apps.test_transactions.models import Attachment, AuditEvent, TestTransaction, TestTransactionLine


ATTACHMENT_KEYS = tuple(f"{UUID(int=number).hex}.pdf" for number in (1, 2))


class TestTransactionSchemaTests(TestCase):
    fixtures = ["s1_03_demo"]

    @classmethod
    def setUpTestData(cls):
        accounts = get_user_model().objects
        cls.creator = accounts.create_user(username="transaction-schema-creator")
        cls.uploader = accounts.create_user(username="transaction-schema-uploader")
        cls.auditor = accounts.create_user(username="transaction-schema-auditor")
        cls.manager = accounts.create_user(username="transaction-schema-manager")
        cls.project_a = Project.objects.get(code="DEMO-A")
        cls.project_b = Project.objects.get(code="DEMO-B")
        cls.item_a = WorkNode.objects.get(code="A-ITEM")
        cls.item_b = WorkNode.objects.get(code="B-ITEM")
        cls.document_a = TestTransaction.objects.create(
            project=cls.project_a, title="Synthetic transaction A", created_by=cls.creator,
        )
        cls.document_b = TestTransaction.objects.create(
            project=cls.project_b, title="Synthetic transaction B", created_by=cls.creator,
        )
        cls.line, cls.second_line = [
            TestTransactionLine.objects.create(
                transaction=cls.document_a,
                project=cls.project_a,
                work_node=cls.item_a,
                line_no=line_no,
                qty=Decimal("2.0000"),
                unit_price=Decimal("5.0050"),
                amount=Decimal("10.01"),
            )
            for line_no in (1, 2)
        ]
        cls.attachment = Attachment.objects.create(
            project=cls.project_a,
            transaction=cls.document_a,
            uploaded_by=cls.uploader,
            original_name="synthetic.pdf",
            media_type="application/pdf",
            size_bytes=19,
            sha256="1" * 64,
            storage_key=ATTACHMENT_KEYS[0],
        )
        cls.creation = AuditEvent.objects.create(
            project=cls.project_a,
            actor=cls.auditor,
            test_transaction=cls.document_a,
            event_type=AuditEvent.EventType.CREATED,
            next_state=TestTransaction.Status.DRAFT,
        )

    def assert_constraint_rejected(self, name, write):
        with self.assertRaises(IntegrityError) as error, transaction.atomic():
            write()
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertEqual(error.exception.__cause__.diag.constraint_name, name)

    def update_row(self, record, **fields):
        quote = connection.ops.quote_name
        table = quote(record._meta.db_table)
        assignments = ", ".join(f"{quote(column)} = %s" for column in fields)
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET {assignments} WHERE id = %s", (*fields.values(), record.pk))

    def assert_update_rejected(self, name, record, **fields):
        self.assert_constraint_rejected(name, lambda: self.update_row(record, **fields))

    def delete_row(self, record):
        table = connection.ops.quote_name(record._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE id = %s", [record.pk])

    def test_postgresql_requires_matching_projects_for_every_composite_reference(self):
        cases = [
            ("test_line_work_node_project_fk", self.line, {"work_node_id": self.item_b.pk}),
            ("test_line_transaction_project_fk", self.line, {"transaction_id": self.document_b.pk}),
            (
                "test_attachment_transaction_project_fk",
                self.attachment,
                {"transaction_id": self.document_b.pk},
            ),
            (
                "test_audit_transaction_project_fk",
                self.creation,
                {"test_transaction_id": self.document_b.pk},
            ),
            (
                "test_transaction_corrects_project_fk",
                self.document_a,
                {"corrects_id": self.document_b.pk},
            ),
            (
                "test_transaction_correction_not_self",
                self.document_a,
                {"corrects_id": self.document_a.pk},
            ),
        ]
        for constraint, row, fields in cases:
            with self.subTest(constraint=constraint):
                self.assert_update_rejected(constraint, row, **fields)

        correction = TestTransaction.objects.create(
            project=self.project_a, title="Correction", created_by=self.creator, corrects=self.document_a,
        )
        self.assertEqual(correction.corrects_id, self.document_a.pk)

    def test_document_and_line_number_uniqueness_are_scoped(self):
        self.assertEqual(self.document_a.document_no, f"TST-{self.document_a.pk}")
        same_project = TestTransaction.objects.create(
            project=self.project_a, title="Another draft", created_by=self.creator,
        )
        self.assert_update_rejected(
            "test_transaction_document_no_unique", same_project, document_no=self.document_a.document_no,
        )
        self.update_row(self.document_b, document_no=self.document_a.document_no)
        self.document_b.refresh_from_db()
        self.assertEqual(self.document_b.document_no, self.document_a.document_no)
        self.assert_update_rejected("test_line_transaction_number_unique", self.second_line, line_no=1)
        self.assert_update_rejected("test_line_number_positive", self.second_line, line_no=0)

    def test_postgresql_rejects_out_of_range_amounts_and_nan(self):
        for field, value, constraint in (
            ("qty", Decimal("-1"), "test_line_qty_positive"),
            ("qty", Decimal("0"), "test_line_qty_positive"),
            ("unit_price", Decimal("-1"), "test_line_unit_price_nonnegative"),
            ("amount", Decimal("-1"), "test_line_amount_nonnegative"),
            ("qty", Decimal("NaN"), "test_line_qty_not_nan"),
            ("unit_price", Decimal("NaN"), "test_line_unit_price_not_nan"),
            ("amount", Decimal("NaN"), "test_line_amount_not_nan"),
        ):
            with self.subTest(field=field, constraint=constraint, value=value):
                self.assert_update_rejected(constraint, self.line, **{field: value})

    def test_status_requires_consistent_approval_and_cancellation_fields(self):
        now = timezone.now()
        approved = {"approved_by_id": self.manager.pk, "approved_at": now}
        cancelled = {"cancelled_by_id": self.manager.pk, "cancelled_at": now, "cancel_reason": "Synthetic"}
        for fields in (
            {"status": "UNKNOWN"},
            approved,
            cancelled,
            {"status": "APPROVED"},
            {"status": "APPROVED", "approved_at": now},
            {"status": "APPROVED", "approved_by_id": self.manager.pk},
            {"status": "APPROVED", **approved, **cancelled},
            {"status": "CANCELLED"},
            {"status": "CANCELLED", **cancelled, "cancel_reason": None},
            {"status": "CANCELLED", **cancelled, "cancel_reason": ""},
            {"status": "CANCELLED", **cancelled, **approved},
        ):
            with self.subTest(fields=fields):
                self.assert_update_rejected("test_transaction_status_fields_valid", self.document_a, **fields)

        self.update_row(self.document_a, status="APPROVED", **approved)
        self.update_row(self.document_b, status="CANCELLED", **cancelled)
        self.document_a.refresh_from_db()
        self.document_b.refresh_from_db()
        self.assertEqual(self.document_a.approved_by, self.manager)
        self.assertEqual(self.document_b.cancelled_by, self.manager)

    def test_postgresql_blocks_direct_rewrites_of_approved_transactions(self):
        document = TestTransaction.objects.create(
            project=self.project_a, title="Approval to preserve", created_by=self.creator,
        )
        now = timezone.now()
        self.update_row(document, status="APPROVED", approved_by_id=self.manager.pk, approved_at=now)

        self.assert_update_rejected(
            "test_transaction_approved_immutable", document, approved_by_id=self.creator.pk,
        )
        self.assert_constraint_rejected(
            "test_transaction_approved_immutable", lambda: self.delete_row(document),
        )
        document.refresh_from_db()
        self.assertEqual(document.status, TestTransaction.Status.APPROVED)
        self.assertEqual(document.approved_by_id, self.manager.pk)
        self.assertEqual(document.approved_at, now)

    def test_postgresql_preserves_lines_and_attachments_of_approved_transactions(self):
        draft = TestTransaction.objects.create(
            project=self.project_a, title="Separate draft", created_by=self.creator,
        )
        self.update_row(
            self.document_a, status="APPROVED", approved_by_id=self.manager.pk, approved_at=timezone.now(),
        )

        line_fields = {
            "project": self.project_a,
            "work_node": self.item_a,
            "line_no": 3,
            "qty": self.line.qty,
            "unit_price": self.line.unit_price,
            "amount": self.line.amount,
        }
        attachment_fields = {
            "project": self.project_a,
            "uploaded_by": self.uploader,
            "original_name": self.attachment.original_name,
            "media_type": self.attachment.media_type,
            "size_bytes": self.attachment.size_bytes,
            "sha256": self.attachment.sha256,
            "storage_key": ATTACHMENT_KEYS[1],
        }
        constraint = "test_transaction_approved_children_immutable"
        for model, approved_row, fields, edit in (
            (TestTransactionLine, self.line, line_fields, {"description": "Rewritten"}),
            (Attachment, self.attachment, attachment_fields, {"original_name": "rewritten.pdf"}),
        ):
            with self.subTest(model=model.__name__):
                self.assert_constraint_rejected(
                    constraint, lambda: model.objects.create(transaction=self.document_a, **fields),
                )
                draft_row = model.objects.create(transaction=draft, **fields)
                self.update_row(draft_row, **edit)
                self.assert_update_rejected(constraint, draft_row, transaction_id=self.document_a.pk)
                self.assert_update_rejected(constraint, approved_row, transaction_id=draft.pk)
                self.assert_update_rejected(constraint, approved_row, **edit)
                self.assert_constraint_rejected(constraint, lambda: self.delete_row(approved_row))
                self.delete_row(draft_row)

    def test_event_type_matches_transition_and_occurs_once_per_document(self):
        for fields in (
            {"event_type": "UNKNOWN"},
            {"event_type": "APPROVED", "next_state": "APPROVED"},
            {"prior_state": "DRAFT"},
            {"next_state": "APPROVED"},
            {"event_type": "APPROVED", "prior_state": "CANCELLED", "next_state": "APPROVED"},
        ):
            with self.subTest(fields=fields):
                self.assert_update_rejected("test_audit_transition_valid", self.creation, **fields)

        approval = AuditEvent.objects.create(
            project=self.project_a,
            actor=self.manager,
            test_transaction=self.document_a,
            event_type=AuditEvent.EventType.APPROVED,
            prior_state=TestTransaction.Status.DRAFT,
            next_state=TestTransaction.Status.APPROVED,
        )
        for original in (self.creation, approval):
            with self.subTest(event_type=original.event_type):
                self.assert_constraint_rejected(
                    "test_audit_source_event_unique",
                    lambda: AuditEvent.objects.create(
                        project=original.project,
                        actor=original.actor,
                        test_transaction=original.test_transaction,
                        event_type=original.event_type,
                        prior_state=original.prior_state,
                        next_state=original.next_state,
                    ),
                )

    def test_attachment_size_and_storage_key_constraints(self):
        self.assert_update_rejected("test_attachment_size_positive", self.attachment, size_bytes=0)
        second = Attachment.objects.create(
            project=self.project_b,
            transaction=self.document_b,
            uploaded_by=self.uploader,
            original_name=self.attachment.original_name,
            media_type=self.attachment.media_type,
            size_bytes=self.attachment.size_bytes,
            sha256=self.attachment.sha256,
            storage_key=ATTACHMENT_KEYS[1],
        )
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, Attachment._meta.db_table)
        storage_unique = next(
            name for name, details in constraints.items()
            if details["unique"] and details["columns"] == ["storage_key"]
        )
        self.assert_update_rejected(storage_unique, second, storage_key=self.attachment.storage_key)

    def test_django_protects_referenced_records(self):
        for target, referenced_by in (
            (self.project_a, {self.document_a, self.line, self.attachment, self.creation}),
            (self.document_a, {self.line, self.attachment, self.creation}),
            (self.item_a, {self.line, self.second_line}),
            (self.creator, {self.document_a, self.document_b}),
            (self.uploader, {self.attachment}),
            (self.auditor, {self.creation}),
        ):
            with self.subTest(target=target):
                with self.assertRaises(ProtectedError) as error:
                    target.delete()
                self.assertTrue(referenced_by.issubset(error.exception.protected_objects))
