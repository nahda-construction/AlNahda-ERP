import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.projects.models import Party


POOL_UNIQUE_CONSTRAINT = "uq_inventory_pool_scope_project_owner_material"
BALANCE_UNIQUE_CONSTRAINT = "uq_inventory_balance_pool_warehouse_state"
OPENING_UNIQUE_CONSTRAINT = "uq_stock_movement_opening_balance"


class Material(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=160)
    base_unit = models.CharField(max_length=40)
    valuation_method = models.CharField(max_length=40)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("code",), name="uq_inventory_material_code"),
        ]


class Warehouse(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=160)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("code",), name="uq_inventory_warehouse_code"),
        ]


class ValuationPool(models.Model):
    class ScopeType(models.TextChoices):
        GENERAL_COMPANY = "GENERAL_COMPANY", "General company stock"
        PROJECT_STOCK = "PROJECT_STOCK", "Project stock"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ARCHIVED = "ARCHIVED", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope_type = models.CharField(max_length=15, choices=ScopeType)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, null=True, blank=True)
    owner = models.ForeignKey(Party, on_delete=models.PROTECT)
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    general_owner_kind = models.CharField(max_length=8, choices=Party.Kind, null=True, blank=True)
    status = models.CharField(max_length=8, choices=Status, default=Status.ACTIVE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("scope_type", "project", "owner", "material"),
                nulls_distinct=False,
                name=POOL_UNIQUE_CONSTRAINT,
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        scope_type="GENERAL_COMPANY",
                        project__isnull=True,
                        general_owner_kind__isnull=False,
                        general_owner_kind=Party.Kind.COMPANY,
                    )
                    | Q(
                        scope_type="PROJECT_STOCK",
                        project__isnull=False,
                        general_owner_kind__isnull=True,
                    )
                ),
                name="inventory_pool_scope_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("ACTIVE", "ARCHIVED")),
                name="inventory_pool_status_valid",
            ),
        ]


class StockLocationBalance(models.Model):
    class StockState(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pool = models.ForeignKey(ValuationPool, on_delete=models.PROTECT)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT)
    stock_state = models.CharField(max_length=9, choices=StockState, default=StockState.AVAILABLE)
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal("0"))

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("pool", "warehouse", "stock_state"),
                name=BALANCE_UNIQUE_CONSTRAINT,
            ),
            models.CheckConstraint(
                condition=Q(stock_state="AVAILABLE"),
                name="inventory_balance_stock_state_valid",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gte=0),
                name="inventory_balance_quantity_nonnegative",
            ),
        ]


class StockIssue(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    document_no = models.CharField(max_length=40)
    work_node = models.ForeignKey("projects.WorkNode", on_delete=models.PROTECT)
    status = models.CharField(max_length=9, choices=Status, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_stock_issues",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="approved_stock_issues",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    reverses = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversals",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("project", "document_no"), name="uq_stock_issue_project_document_no",
            ),
            models.UniqueConstraint(fields=("id", "project"), name="uq_stock_issue_id_project"),
            models.CheckConstraint(
                condition=(
                    Q(status__in=("DRAFT", "CANCELLED"), approved_by__isnull=True, approved_at__isnull=True)
                    | Q(status="APPROVED", approved_by__isnull=False, approved_at__isnull=False)
                ),
                name="stock_issue_approval_state_valid",
            ),
            models.CheckConstraint(
                condition=Q(reverses__isnull=True), name="stock_issue_reverses_reserved",
            ),
        ]


class StockIssueLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue = models.ForeignKey(StockIssue, on_delete=models.PROTECT, related_name="lines")
    balance = models.ForeignKey(StockLocationBalance, on_delete=models.PROTECT)
    qty = models.DecimalField(max_digits=14, decimal_places=4)
    line_no = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("issue", "line_no"), name="uq_stock_issue_line_issue_number",
            ),
            models.UniqueConstraint(fields=("id", "balance"), name="uq_stock_issue_line_id_balance"),
            models.CheckConstraint(
                condition=Q(line_no__gt=0), name="stock_issue_line_number_positive",
            ),
            models.CheckConstraint(condition=Q(qty__gt=0), name="stock_issue_line_qty_positive"),
        ]


class StockMovement(models.Model):
    class EventKind(models.TextChoices):
        OPENING = "OPENING", "Opening"
        ISSUE = "ISSUE", "Issue"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    balance = models.ForeignKey(StockLocationBalance, on_delete=models.PROTECT, related_name="movements")
    issue_line = models.ForeignKey(
        StockIssueLine, on_delete=models.PROTECT, null=True, blank=True, related_name="movements",
    )
    event_kind = models.CharField(max_length=7, choices=EventKind)
    signed_qty = models.DecimalField(max_digits=14, decimal_places=4)
    created_at = models.DateTimeField(default=timezone.now)
    reverses = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversals",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("balance",),
                condition=Q(event_kind="OPENING"),
                name=OPENING_UNIQUE_CONSTRAINT,
            ),
            models.UniqueConstraint(
                fields=("issue_line",),
                condition=Q(event_kind="ISSUE"),
                name="uq_stock_movement_issue_line",
            ),
            models.CheckConstraint(
                condition=(
                    Q(event_kind="OPENING", issue_line__isnull=True, signed_qty__gt=0)
                    | Q(event_kind="ISSUE", issue_line__isnull=False, signed_qty__lt=0)
                ),
                name="stock_movement_source_sign_valid",
            ),
            models.CheckConstraint(
                condition=Q(reverses__isnull=True), name="stock_movement_reverses_reserved",
            ),
        ]
