import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import DecimalValidator
from django.db import IntegrityError, connection, transaction
from django.http import Http404
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.access_control.policies import Action, is_internal_employee, projects_for
from apps.printing import client as print_client
from apps.projects.models import Project, WorkNode

from . import storage
from .models import Attachment, AuditEvent, IdempotencyRecord, TestTransaction, TestTransactionLine


MAX_LINES = 100
LINE_FIELDS = ("work_node", "description", "qty", "unit_price")
QUANTITY_DIGITS = 14
UNIT_PRICE_DIGITS = 16
INPUT_DECIMAL_PLACES = 4
ACCOUNTING_PRECISION = 30
CENT = Decimal("0.01")
PRINTABLE_STATUSES = (TestTransaction.Status.DRAFT, TestTransaction.Status.APPROVED)


class TransitionConflict(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


class AttachmentCleanupBusy(Exception):
    pass


def require_employee(actor):
    if not is_internal_employee(actor):
        raise PermissionDenied


def require_project(actor, project_id, action, *, for_write=False):
    require_employee(actor)
    try:
        project = projects_for(actor, action).get(pk=project_id)
    except Project.DoesNotExist:
        if projects_for(actor).filter(pk=project_id).exists():
            raise PermissionDenied from None
        raise Http404 from None
    if for_write and project.status != Project.Status.ACTIVE:
        raise ValidationError("المشروع مؤرشف ولا يقبل الكتابة.")
    return project


def eligible_work_nodes(project_id):
    return WorkNode.objects.filter(
        project_id=project_id, status=Project.Status.ACTIVE,
        node_type__in=(WorkNode.NodeType.ITEM, WorkNode.NodeType.ACTIVITY_1,
                       WorkNode.NodeType.ACTIVITY_2),
    )


def visible_transactions(actor, project_id):
    return TestTransaction.objects.filter(
        project_id=project_id, project__in=projects_for(actor, Action.VIEW_TEST_TRANSACTION),
    )


def _get_transaction(queryset, transaction_id):
    try:
        return queryset.get(pk=transaction_id)
    except TestTransaction.DoesNotExist:
        raise Http404 from None


def get_transaction(*, actor, project_id, transaction_id):
    require_employee(actor)
    return _get_transaction(visible_transactions(actor, project_id), transaction_id)


def transaction_lines(document):
    return list(document.lines.select_related("work_node").order_by("line_no"))


def can_print_transaction(actor, document):
    return document.status in PRINTABLE_STATUSES and projects_for(
        actor, Action.PRINT_TEST_DOCUMENT,
    ).filter(pk=document.project_id).exists()


def print_transaction_pdf(*, actor, project_id, transaction_id):
    document = get_transaction(actor=actor, project_id=project_id, transaction_id=transaction_id)
    require_project(actor, project_id, Action.PRINT_TEST_DOCUMENT)
    if document.status not in PRINTABLE_STATUSES:
        raise Http404

    lines = transaction_lines(document)
    markup = render_to_string("test_transactions/transaction_print.html", {
        "document": document, "lines": lines, "total": total_amount(lines),
    })
    return document.document_no, print_client.render_pdf(markup)


def get_attachment(*, actor, project_id, transaction_id, attachment_id):
    try:
        attachment = Attachment.objects.get(
            pk=attachment_id, project_id=project_id, transaction_id=transaction_id,
            transaction__in=visible_transactions(actor, project_id),
        )
    except Attachment.DoesNotExist:
        raise Http404 from None
    require_project(actor, project_id, Action.DOWNLOAD_TEST_ATTACHMENT)
    return attachment


def _text(value, max_length, *, required=False):
    if isinstance(value, str):
        value = value.strip()
        if (value or not required) and "\x00" not in value and len(value) <= max_length:
            return value
    raise ValidationError("قيمة نصية غير صالحة.")


def _decimal(value, max_digits, minimum):
    if not isinstance(value, (Decimal, str, int)) or isinstance(value, bool):
        raise ValidationError("قيمة عشرية غير صالحة.")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError("قيمة عشرية غير صالحة.") from exc
    if not number.is_finite() or number < minimum:
        raise ValidationError("قيمة عشرية خارج النطاق.")
    DecimalValidator(max_digits, INPUT_DECIMAL_PLACES)(number)
    return number


def _validate_nodes(project_id, node_ids):
    if len(eligible_work_nodes(project_id).in_bulk(node_ids)) != len(node_ids):
        raise ValidationError("اختر بنودًا أو أنشطة نشطة من المشروع.")


def _prepare_lines(project_id, lines):
    if not 1 <= len(lines) <= MAX_LINES:
        raise ValidationError("أدخل من سطر واحد إلى 100 سطر.")
    prepared = []
    for line_no, line in enumerate(lines, start=1):
        if set(line) != set(LINE_FIELDS):
            raise ValidationError("حقول السطر غير صالحة.")
        value = line["work_node"]
        try:
            node_id = value.pk if isinstance(value, WorkNode) else uuid.UUID(str(value))
        except ValueError as exc:
            raise ValidationError("معرّف البند غير صالح.") from exc
        qty = _decimal(line["qty"], QUANTITY_DIGITS, Decimal("0.0001"))
        unit_price = _decimal(line["unit_price"], UNIT_PRICE_DIGITS, Decimal("0"))
        with localcontext(prec=ACCOUNTING_PRECISION):
            amount = (qty * unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
        prepared.append(TestTransactionLine(
            project_id=project_id, work_node_id=node_id, line_no=line_no,
            description=_text(line["description"], 240), qty=qty, unit_price=unit_price,
            amount=amount,
        ))
    _validate_nodes(project_id, {line.work_node_id for line in prepared})
    return prepared


def total_amount(lines):
    with localcontext(prec=ACCOUNTING_PRECISION):
        return sum((line.amount for line in lines), Decimal("0.00"))


def _prepare_draft(actor, project_id, title, lines):
    project = require_project(actor, project_id, Action.CREATE_TEST_DRAFT, for_write=True)
    return project.pk, _text(title, 160, required=True), _prepare_lines(project.pk, lines)


def _save_prepared_draft(actor, project_id, title, lines, staged_file, storage_key):
    require_project(actor, project_id, Action.CREATE_TEST_DRAFT, for_write=True)
    _validate_nodes(project_id, {line.work_node_id for line in lines})
    draft = TestTransaction.objects.create(project_id=project_id, title=title, created_by=actor)
    for line in lines:
        line.transaction = draft
    TestTransactionLine.objects.bulk_create(lines)
    Attachment.objects.create(
        project_id=project_id, transaction=draft, storage_key=storage_key,
        original_name=staged_file.original_name, media_type=storage.PDF_MEDIA_TYPE,
        size_bytes=staged_file.size_bytes, sha256=staged_file.sha256, uploaded_by=actor,
    )
    AuditEvent.objects.create(
        project_id=project_id, actor=actor, test_transaction=draft,
        event_type=AuditEvent.EventType.CREATED, next_state=TestTransaction.Status.DRAFT,
    )
    return draft


def _decimal_for_fingerprint(value):
    if value.is_zero():
        value = value.copy_abs()
    return format(value, f".{INPUT_DECIMAL_PLACES}f")


def _draft_fingerprint(project_id, title, lines, staged_file):
    payload = {
        "project_id": str(project_id),
        "title": title,
        "lines": [
            {
                "work_node": str(line.work_node_id),
                "description": line.description,
                "qty": _decimal_for_fingerprint(line.qty),
                "unit_price": _decimal_for_fingerprint(line.unit_price),
            }
            for line in lines
        ],
        "attachment": {"sha256": staged_file.sha256, "original_name": staged_file.original_name},
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_completed_record(*, actor_id, project_id, operation, key):
    return IdempotencyRecord.objects.get(
        actor_id=actor_id, project_id=project_id, operation=operation, key=key,
        status=IdempotencyRecord.Status.COMPLETED,
    )


def create_draft_once(*, actor, project_id, key, title, lines, uploaded_file):
    project_id, title, lines = _prepare_draft(actor, project_id, title, lines)
    try:
        key = uuid.UUID(str(key))
    except ValueError as exc:
        raise ValidationError("مفتاح الطلب غير صالح.") from exc
    scope = {
        "actor_id": actor.pk, "project_id": project_id,
        "operation": IdempotencyRecord.Operation.CREATE_TEST_DRAFT, "key": key,
    }
    with storage.stage_pdf(uploaded_file) as staged_file:
        fingerprint = _draft_fingerprint(project_id, title, lines, staged_file)
        with transaction.atomic():
            try:
                with transaction.atomic():
                    record = IdempotencyRecord.objects.create(**scope, payload_sha256=fingerprint)
            except IntegrityError as exc:
                cause = exc.__cause__
                if (
                    getattr(cause, "sqlstate", None) != "23505"
                    or getattr(getattr(cause, "diag", None), "constraint_name", None)
                    != "uq_idem_actor_proj_op_key"
                ):
                    raise
                require_project(actor, project_id, Action.CREATE_TEST_DRAFT, for_write=True)
                record = _get_completed_record(**scope)
                if record.payload_sha256 != fingerprint:
                    raise IdempotencyConflict("استُخدم مفتاح الطلب نفسه لبيانات مختلفة.") from None
                return record

            # The insert can also wait for a competing transaction that rolls back.
            require_project(actor, project_id, Action.CREATE_TEST_DRAFT, for_write=True)
            storage_key = storage.publish(staged_file)
            draft = _save_prepared_draft(actor, project_id, title, lines, staged_file, storage_key)
            record.test_transaction = draft
            record.response_status = 303
            record.response_body = {
                "transaction_id": str(draft.pk),
                "document_no": draft.document_no,
                "location": reverse("test_transactions:detail", args=(project_id, draft.pk)),
            }
            record.status = IdempotencyRecord.Status.COMPLETED
            record.save(update_fields=("test_transaction", "response_status", "response_body", "status"))
            return record


@transaction.atomic(durable=True)
def cleanup_orphaned_attachments():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT relowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = CURRENT_USER)
            FROM pg_catalog.pg_class
            WHERE oid = 'public.test_transactions_attachment'::regclass
        """)
        if not cursor.fetchone()[0]:
            raise PermissionDenied("Attachment cleanup requires the maintenance role.")
        cursor.execute("SELECT pg_catalog.pg_try_advisory_xact_lock(%s, %s)", storage.ATTACHMENT_STORAGE_LOCK)
        if not cursor.fetchone()[0]:
            raise AttachmentCleanupBusy("Attachment writes are still active.")

        def is_referenced(key):
            cursor.execute("""
                SELECT EXISTS(
                    SELECT 1 FROM public.test_transactions_attachment WHERE storage_key = %s
                )
            """, [key])
            return cursor.fetchone()[0]

        return storage._remove_unreferenced_files(is_referenced)


@transaction.atomic
def approve_transaction(*, actor, project_id, transaction_id):
    visible = visible_transactions(actor, project_id)
    _get_transaction(visible, transaction_id)
    require_project(actor, project_id, Action.APPROVE_TEST_TRANSACTION, for_write=True)
    draft = _get_transaction(visible.select_for_update(of=("self",)), transaction_id)
    require_project(actor, project_id, Action.APPROVE_TEST_TRANSACTION, for_write=True)
    if draft.status != TestTransaction.Status.DRAFT:
        raise TransitionConflict("لا يمكن اعتماد هذه المعاملة مرة أخرى.")

    draft.status = TestTransaction.Status.APPROVED
    draft.approved_by = actor
    draft.approved_at = timezone.now()
    draft.save(update_fields=("status", "approved_by", "approved_at"))
    AuditEvent.objects.create(
        project_id=project_id, actor=actor, test_transaction=draft,
        event_type=AuditEvent.EventType.APPROVED, occurred_at=draft.approved_at,
        prior_state=TestTransaction.Status.DRAFT, next_state=TestTransaction.Status.APPROVED,
    )
    return draft
