import uuid
from decimal import Decimal

from django.db import models
from django.db.models import Q

from apps.projects.models import Party


POOL_UNIQUE_CONSTRAINT = "uq_inventory_pool_scope_project_owner_material"
BALANCE_UNIQUE_CONSTRAINT = "uq_inventory_balance_pool_warehouse_state"


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
