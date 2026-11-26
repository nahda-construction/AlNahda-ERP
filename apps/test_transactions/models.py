import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class TestTransaction(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    document_no = models.CharField(max_length=40, editable=False)
    title = models.CharField(max_length=160)
    status = models.CharField(max_length=9, choices=Status, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_test_transactions",
    )
    created_at = models.DateTimeField(default=timezone.now)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_test_transactions",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cancelled_test_transactions",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(null=True, blank=True)
    corrects = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="corrections",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("project", "document_no"), name="test_transaction_document_no_unique",
            ),
            models.UniqueConstraint(
                fields=("id", "project"), name="test_transaction_project_target_unique",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="DRAFT",
                        approved_by__isnull=True,
                        approved_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancelled_at__isnull=True,
                        cancel_reason__isnull=True,
                    )
                    | Q(
                        status="APPROVED",
                        approved_by__isnull=False,
                        approved_at__isnull=False,
                        cancelled_by__isnull=True,
                        cancelled_at__isnull=True,
                        cancel_reason__isnull=True,
                    )
                    | (
                        Q(
                            status="CANCELLED",
                            approved_by__isnull=True,
                            approved_at__isnull=True,
                            cancelled_by__isnull=False,
                            cancelled_at__isnull=False,
                            cancel_reason__isnull=False,
                        )
                        & ~Q(cancel_reason="")
                    )
                ),
                name="test_transaction_status_fields_valid",
            ),
            models.CheckConstraint(
                condition=Q(corrects__isnull=True) | ~Q(corrects=F("id")),
                name="test_transaction_correction_not_self",
            ),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding and not self.document_no:
            self.document_no = f"TST-{self.id}"
        return super().save(*args, **kwargs)


class TestTransactionLine(models.Model):
    transaction = models.ForeignKey(
        TestTransaction, on_delete=models.PROTECT, related_name="lines",
    )
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    work_node = models.ForeignKey("projects.WorkNode", on_delete=models.PROTECT)
    line_no = models.PositiveIntegerField()
    description = models.CharField(max_length=240, blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=4)
    unit_price = models.DecimalField(max_digits=16, decimal_places=4)
    amount = models.DecimalField(max_digits=24, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("transaction", "line_no"), name="test_line_transaction_number_unique",
            ),
            models.CheckConstraint(condition=Q(line_no__gt=0), name="test_line_number_positive"),
            models.CheckConstraint(condition=Q(qty__gt=0), name="test_line_qty_positive"),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0), name="test_line_unit_price_nonnegative",
            ),
            models.CheckConstraint(condition=Q(amount__gte=0), name="test_line_amount_nonnegative"),
        ]


class Attachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    transaction = models.ForeignKey(
        TestTransaction, on_delete=models.PROTECT, related_name="attachments",
    )
    storage_key = models.CharField(max_length=64, unique=True)
    original_name = models.CharField(max_length=255)
    media_type = models.CharField(max_length=100)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(size_bytes__gt=0), name="test_attachment_size_positive",
            ),
        ]


class AuditEvent(models.Model):
    class EventType(models.TextChoices):
        CREATED = "CREATED", "Created"
        APPROVED = "APPROVED", "Approved"

    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    occurred_at = models.DateTimeField(default=timezone.now)
    event_type = models.CharField(max_length=8, choices=EventType)
    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False)
    test_transaction = models.ForeignKey(
        TestTransaction, on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events",
    )
    stock_issue = models.ForeignKey(
        "inventory.StockIssue", on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events",
    )
    prior_state = models.CharField(
        max_length=9, choices=TestTransaction.Status, null=True, blank=True,
    )
    next_state = models.CharField(max_length=9, choices=TestTransaction.Status)
    reason = models.TextField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(test_transaction__isnull=False, stock_issue__isnull=True)
                    | Q(test_transaction__isnull=True, stock_issue__isnull=False)
                ),
                name="audit_single_source",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type="CREATED", test_transaction__isnull=False,
                        prior_state__isnull=True, next_state="DRAFT",
                    )
                    | Q(
                        event_type="APPROVED",
                        prior_state__isnull=False,
                        prior_state="DRAFT",
                        next_state="APPROVED",
                    )
                ),
                name="test_audit_transition_valid",
            ),
            models.UniqueConstraint(
                fields=("test_transaction", "event_type"), name="test_audit_source_event_unique",
            ),
            models.UniqueConstraint(
                fields=("stock_issue",), condition=Q(stock_issue__isnull=False),
                name="uq_stock_issue_audit_event",
            ),
        ]


class IdempotencyRecord(models.Model):
    class Operation(models.TextChoices):
        CREATE_TEST_DRAFT = "CREATE_TEST_DRAFT", "Create test draft"

    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    operation = models.CharField(max_length=32, choices=Operation)
    key = models.UUIDField()
    payload_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=11, choices=Status, default=Status.IN_PROGRESS)
    created_at = models.DateTimeField(default=timezone.now)
    test_transaction = models.ForeignKey(
        TestTransaction, on_delete=models.PROTECT, null=True, blank=True,
    )
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("actor", "project", "operation", "key"),
                name="uq_idem_actor_proj_op_key",
            ),
            models.UniqueConstraint(
                fields=("test_transaction",),
                condition=Q(status="COMPLETED"),
                name="uq_idem_completed_transaction",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="IN_PROGRESS",
                        test_transaction__isnull=True,
                        response_status__isnull=True,
                        response_body__isnull=True,
                    )
                    | (
                        Q(
                            status="COMPLETED",
                            test_transaction__isnull=False,
                            response_status__isnull=False,
                            response_body__isnull=False,
                        )
                        & ~Q(response_body=None)
                    )
                ),
                name="idem_status_result_valid",
            ),
        ]
