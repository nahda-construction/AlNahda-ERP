import re
from unittest.mock import patch
from uuid import UUID, uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TransactionTestCase, override_settings

from apps.access_control.models import ProjectAccess
from apps.access_control.services import grant_project_access
from apps.projects.models import Project, WorkNode
from apps.test_transactions import services
from apps.test_transactions.models import (
    Attachment,
    AuditEvent,
    IdempotencyRecord,
    TestTransaction,
    TestTransactionLine,
)
from tests.test_s1_05_http import (
    PDF_A,
    TEST_HASHERS,
    TransactionHTTPTestsMixin,
    csrf_headers,
    make_employee,
    make_grantor,
    private_root,
    transaction_url,
)


KEY_CONSTRAINT = "uq_idem_actor_proj_op_key"
RESULT_CONSTRAINT = "uq_idem_completed_transaction"
SHAPE_CONSTRAINT = "idem_status_result_valid"
CREATE = IdempotencyRecord.Operation.CREATE_TEST_DRAFT
COMPLETED = IdempotencyRecord.Status.COMPLETED


@override_settings(PASSWORD_HASHERS=TEST_HASHERS)
class IdempotencyTests(TransactionHTTPTestsMixin, TransactionTestCase):
    fixtures = ["s1_03_demo"]

    def setUp(self):
        self.project_a = Project.objects.get(code="DEMO-A")
        self.project_b = Project.objects.get(code="DEMO-B")
        self.node_a = WorkNode.objects.get(code="A-ITEM")
        self.operator = make_employee("idempotency-operator")
        self.other_operator = make_employee("idempotency-other-operator")
        self.manager = make_employee("idempotency-manager")
        self.other_manager = make_employee("idempotency-other-manager")
        grantor = make_grantor()
        for employee, role in (
            (self.operator, ProjectAccess.Role.OPERATOR),
            (self.other_operator, ProjectAccess.Role.OPERATOR),
            (self.manager, ProjectAccess.Role.MANAGER),
            (self.other_manager, ProjectAccess.Role.MANAGER),
        ):
            grant_project_access(actor=grantor, user=employee, project=self.project_a, role=role)
        grant_project_access(
            actor=grantor, user=self.operator, project=self.project_b, role=ProjectAccess.Role.OPERATOR,
        )
        self.root = private_root(self)
        self.key = uuid4()

    def scope(self, **changes):
        return {
            "actor_id": self.operator.pk,
            "project_id": self.project_a.pk,
            "operation": CREATE,
            "key": self.key,
        } | changes

    def counts(self):
        return tuple(model.objects.count() for model in (
            TestTransaction, TestTransactionLine, Attachment, AuditEvent, IdempotencyRecord,
        ))

    def web_fields(self, client, *, title="Synthetic retry", content=PDF_A, price="12.3400"):
        return self.draft_data(
            client, self.project_a, (self.node_a, "Synthetic line", "3", price),
            title=title, content=content,
        )

    def post_fields(self, client, fields):
        return client.post(
            transaction_url("create", self.project_a.pk), fields, headers=csrf_headers(client),
        )

    def retry(self, client, *, title="Synthetic retry", content=PDF_A, price="12.3400"):
        fields = self.web_fields(client, title=title, content=content, price=price)
        fields["idempotency_key"] = str(self.key)
        return self.post_fields(client, fields)

    def approve_as(self, actor, document_id):
        client = self.sign_in(actor)
        return client.post(
            transaction_url("approve", self.project_a.pk, document_id), headers=csrf_headers(client),
        )

    def submit_service(self, key):
        return services.create_draft_once(
            actor=self.operator, project_id=self.project_a.pk, key=key, title="Synthetic retry",
            lines=[{
                "work_node": self.node_a, "description": "Synthetic line",
                "qty": "3.0000", "unit_price": "12.3400",
            }],
            uploaded_file=SimpleUploadedFile("receipt.pdf", PDF_A, content_type="application/pdf"),
        )

    def assert_postgresql_rejects(self, write, sqlstate, *, constraint=None, column=None):
        with self.assertRaises(IntegrityError) as failure, transaction.atomic():
            write()
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        original = failure.exception.__cause__
        self.assertEqual(original.sqlstate, sqlstate)
        if constraint:
            self.assertEqual(original.diag.constraint_name, constraint)
        if column:
            self.assertEqual(original.diag.column_name, column)

    def observe_insert(self, changes, errors):
        original_create = IdempotencyRecord.objects.create

        def insert(**arguments):
            try:
                return original_create(**(arguments | changes))
            except IntegrityError as error:
                errors.append(error)
                raise

        return patch.object(IdempotencyRecord.objects, "create", side_effect=insert)

    def test_fresh_form_keys_and_lost_response_replay_after_approval(self):
        client = self.sign_in(self.operator)
        first_form = self.web_fields(client)
        retry_key = UUID(first_form["idempotency_key"])
        self.assertEqual(retry_key.version, 4)
        other_key = UUID(self.web_fields(client)["idempotency_key"])
        self.assertNotEqual(retry_key, other_key)

        missing_key = self.web_fields(client)
        missing_key.pop("idempotency_key")
        self.assertEqual(self.post_fields(client, missing_key).status_code, 400)
        invalid_key = self.web_fields(client)
        invalid_key["idempotency_key"] = "invalid-uuid"
        self.assertEqual(self.post_fields(client, invalid_key).status_code, 400)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))

        self.key = retry_key
        first = self.post_fields(client, first_form)
        self.assertEqual(first.status_code, 303)
        self.assertTrue(transaction.get_autocommit())
        record = IdempotencyRecord.objects.get(**self.scope())
        document = record.test_transaction
        expected_receipt = {
            "transaction_id": str(document.pk),
            "document_no": document.document_no,
            "location": first["Location"],
        }
        self.assertEqual(record.status, COMPLETED)
        self.assertEqual(record.response_status, 303)
        self.assertEqual(record.response_body, expected_receipt)
        self.assertEqual(self.counts(), (1, 1, 1, 1, 1))

        # A later approval cannot change the original receipt.
        self.assertIn(self.approve_as(self.manager, document.pk).status_code, (302, 303))
        replay = self.retry(self.sign_in(self.operator), price="12.34")
        self.assertEqual(replay.status_code, 303)
        self.assertEqual(replay["Location"], first["Location"])
        record.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(document.status, TestTransaction.Status.APPROVED)
        self.assertEqual(record.response_body, expected_receipt)
        self.assertEqual(record.response_status, 303)
        self.assertEqual(self.counts(), (1, 1, 1, 2, 1))
        self.assertEqual(AuditEvent.objects.filter(event_type=AuditEvent.EventType.CREATED).count(), 1)
        self.assertEqual(list(self.root.glob("*.pdf")), [self.root / document.attachments.get().storage_key])

    def test_same_key_with_changed_title_or_pdf_byte_conflicts_without_effect(self):
        client = self.sign_in(self.operator)
        original = self.retry(client)
        self.assertEqual(original.status_code, 303)
        record = IdempotencyRecord.objects.get(**self.scope())
        receipt = record.response_body
        payload_sha256 = record.payload_sha256
        changed_pdf = PDF_A.replace(b"receipt A", b"receipt Z")
        self.assertEqual(sum(before != after for before, after in zip(PDF_A, changed_pdf, strict=True)), 1)

        for change in ({"title": "Changed synthetic title"}, {"content": changed_pdf}):
            with self.subTest(change=change):
                response = self.retry(client, **change)
                self.assertEqual(response.status_code, 409)
                self.assertNotIn("Location", response.headers)
                self.assertNotContains(response, receipt["transaction_id"], status_code=409)
        record.refresh_from_db()
        self.assertEqual(record.response_body, receipt)
        self.assertEqual(record.payload_sha256, payload_sha256)
        self.assertEqual(self.counts(), (1, 1, 1, 1, 1))
        self.assertEqual(len(list(self.root.glob("*.pdf"))), 1)

    def test_receipt_lookup_scopes_by_actor_project_operation_and_key(self):
        client = self.sign_in(self.operator)
        original = self.retry(client)
        self.assertEqual(original.status_code, 303)
        for differences in (
            {"actor_id": self.other_operator.pk},
            {"project_id": self.project_b.pk},
            {"operation": "SYNTHETIC_OTHER_OPERATION"},
        ):
            scope = self.scope(**differences)
            decoy = TestTransaction.objects.create(
                project_id=scope["project_id"], created_by_id=scope["actor_id"], title="Synthetic decoy",
            )
            IdempotencyRecord.objects.create(
                **scope, payload_sha256="7" * 64, status=COMPLETED, test_transaction=decoy,
                response_status=303, response_body={"location": f"/synthetic-decoy/{decoy.pk}/"},
            )

        lookups = []

        def capture_lookup(execute, sql, params, many, context):
            if sql.lstrip().upper().startswith("SELECT") and IdempotencyRecord._meta.db_table in sql:
                lookups.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture_lookup):
            replay = self.retry(client)
        self.assertEqual(replay.status_code, 303)
        self.assertEqual(replay["Location"], original["Location"])
        self.assertEqual(len(lookups), 1)
        query, params = lookups[0]
        fields = re.findall(r'\."([a-z_]+)"\s*=\s*%s', query.partition(" WHERE ")[2])
        bound_scope = dict(zip(fields, params, strict=True))
        self.assertEqual(bound_scope, self.scope(status=COMPLETED))
        self.assertEqual(IdempotencyRecord.objects.filter(key=self.key).count(), 4)

    def test_second_manager_cannot_approve_and_result_cannot_cross_projects(self):
        saved = self.submit_service(self.key)
        document = saved.test_transaction
        self.assertIn(self.approve_as(self.manager, document.pk).status_code, (302, 303))
        self.assertEqual(self.approve_as(self.other_manager, document.pk).status_code, 409)
        document.refresh_from_db()
        self.assertEqual(document.approved_by_id, self.manager.pk)
        self.assertEqual(
            document.audit_events.filter(event_type=AuditEvent.EventType.APPROVED).get().actor_id,
            self.manager.pk,
        )

        foreign = TestTransaction.objects.create(
            project=self.project_b, created_by=self.operator, title="Foreign project result",
        )
        self.assert_postgresql_rejects(
            lambda: IdempotencyRecord.objects.create(
                **self.scope(key=uuid4()), payload_sha256=saved.payload_sha256, status=COMPLETED,
                test_transaction=foreign, response_status=303, response_body=saved.response_body,
            ),
            "23503", constraint="idem_transaction_project_fk",
        )

    def test_key_violation_recovers_original_driver_diagnostics_after_savepoint(self):
        original = self.submit_service(self.key)
        real_lookup = services._get_completed_record

        def lookup(**scope):
            self.assertFalse(connection.needs_rollback)
            with connection.cursor() as cursor:
                cursor.execute("SHOW transaction_isolation")
                self.assertEqual(cursor.fetchone()[0], "read committed")
            return real_lookup(**scope)

        errors = []
        with (
            self.observe_insert({}, errors),
            patch.object(services, "_get_completed_record", side_effect=lookup) as recovered,
        ):
            replay = self.submit_service(self.key)
        recovered.assert_called_once_with(**self.scope())
        error, = errors
        self.assertEqual(error.__cause__.sqlstate, "23505")
        self.assertEqual(error.__cause__.diag.constraint_name, KEY_CONSTRAINT)
        self.assertEqual(replay.pk, original.pk)
        self.assertEqual(replay.response_body, original.response_body)
        self.assertEqual(self.counts(), (1, 1, 1, 1, 1))

    def test_other_real_insert_constraint_failures_propagate_original_exception(self):
        original = self.submit_service(self.key)
        for updates, sqlstate, constraint in (
            (
                {
                    "status": COMPLETED,
                    "test_transaction_id": original.test_transaction_id,
                    "response_status": original.response_status,
                    "response_body": original.response_body,
                },
                "23505", RESULT_CONSTRAINT,
            ),
            ({"status": COMPLETED}, "23514", SHAPE_CONSTRAINT),
        ):
            with self.subTest(constraint=constraint):
                errors = []
                with (
                    self.observe_insert(updates, errors),
                    patch.object(services, "_get_completed_record", wraps=services._get_completed_record) as lookup,
                    self.assertRaises(IntegrityError) as failure,
                ):
                    self.submit_service(uuid4())
                error, = errors
                self.assertIs(failure.exception, error)
                self.assertEqual(error.__cause__.sqlstate, sqlstate)
                self.assertEqual(error.__cause__.diag.constraint_name, constraint)
                lookup.assert_not_called()
                self.assertEqual(self.counts(), (1, 1, 1, 1, 1))
                self.assertEqual(len(list(self.root.glob("*.pdf"))), 1)

    def test_postgresql_rejects_null_scopes_and_invalid_result_shapes(self):
        for required in ("actor_id", "project_id", "operation", "key"):
            with self.subTest(required=required):
                scope = self.scope(**{required: None})
                self.assert_postgresql_rejects(
                    lambda: IdempotencyRecord.objects.create(**scope, payload_sha256="8" * 64),
                    "23502", column=required,
                )

        document = TestTransaction.objects.create(
            project=self.project_a, created_by=self.operator, title="Receipt shape evidence",
        )
        result_fields = {
            "test_transaction_id": document.pk,
            "response_status": 303,
            "response_body": {"transaction_id": str(document.pk)},
        }
        progress = IdempotencyRecord.objects.create(
            **self.scope(key=uuid4()), payload_sha256="8" * 64,
        )
        for field, value in result_fields.items():
            with self.subTest(field=field):
                incomplete = result_fields | {field: None}
                self.assert_postgresql_rejects(
                    lambda: IdempotencyRecord.objects.create(
                        **self.scope(), payload_sha256="8" * 64, status=COMPLETED, **incomplete,
                    ),
                    "23514", constraint=SHAPE_CONSTRAINT,
                )
                self.assert_postgresql_rejects(
                    lambda: IdempotencyRecord.objects.filter(pk=progress.pk).update(**{field: value}),
                    "23514", constraint=SHAPE_CONSTRAINT,
                )

        def store_json_null():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE test_transactions_idempotencyrecord
                    SET status = 'COMPLETED', test_transaction_id = %s,
                        response_status = 303, response_body = 'null'::jsonb
                    WHERE id = %s
                    """,
                    [document.pk, progress.pk],
                )

        self.assert_postgresql_rejects(store_json_null, "23514", constraint=SHAPE_CONSTRAINT)

    def test_postgresql_protects_completed_receipt_from_rewrite_or_reset(self):
        saved = self.submit_service(self.key)
        receipt = saved.response_body
        for fields in (
            {"response_body": {"location": "/rewritten/"}},
            {
                "status": IdempotencyRecord.Status.IN_PROGRESS,
                "test_transaction_id": None,
                "response_status": None,
                "response_body": None,
            },
        ):
            with self.subTest(fields=fields):
                self.assert_postgresql_rejects(
                    lambda: IdempotencyRecord.objects.filter(pk=saved.pk).update(**fields),
                    "23514", constraint="idem_completed_receipt_immutable",
                )
        saved.refresh_from_db()
        self.assertEqual(saved.status, COMPLETED)
        self.assertEqual(saved.response_body, receipt)
