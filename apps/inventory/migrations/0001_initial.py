import uuid
from decimal import Decimal

from django.db import migrations, models
from django.db.models import PROTECT, Q


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("projects", "0003_worknode_project_target"),
    ]

    operations = [
        migrations.CreateModel(
            name="Material",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.CharField(max_length=40)),
                ("name", models.CharField(max_length=160)),
                ("base_unit", models.CharField(max_length=40)),
                ("valuation_method", models.CharField(max_length=40)),
                ("active", models.BooleanField(default=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("code",), name="uq_inventory_material_code"),
                ],
            },
        ),
        migrations.CreateModel(
            name="Warehouse",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.CharField(max_length=40)),
                ("name", models.CharField(max_length=160)),
                ("active", models.BooleanField(default=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("code",), name="uq_inventory_warehouse_code"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ValuationPool",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "scope_type",
                    models.CharField(
                        choices=[
                            ("GENERAL_COMPANY", "General company stock"),
                            ("PROJECT_STOCK", "Project stock"),
                        ],
                        max_length=15,
                    ),
                ),
                (
                    "general_owner_kind",
                    models.CharField(
                        choices=[("COMPANY", "Company"), ("EXTERNAL", "External")],
                        max_length=8, null=True, blank=True,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("ACTIVE", "Active"), ("ARCHIVED", "Archived")],
                        default="ACTIVE", max_length=8,
                    ),
                ),
                ("project", models.ForeignKey(on_delete=PROTECT, to="projects.project", null=True, blank=True)),
                ("owner", models.ForeignKey(on_delete=PROTECT, to="projects.party")),
                ("material", models.ForeignKey(on_delete=PROTECT, to="inventory.material")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("scope_type", "project", "owner", "material"),
                        nulls_distinct=False,
                        name="uq_inventory_pool_scope_project_owner_material",
                    ),
                    models.CheckConstraint(
                        condition=(
                            Q(
                                scope_type="GENERAL_COMPANY",
                                project__isnull=True,
                                general_owner_kind__isnull=False,
                                general_owner_kind="COMPANY",
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
                ],
            },
        ),
        migrations.CreateModel(
            name="StockLocationBalance",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "stock_state",
                    models.CharField(choices=[("AVAILABLE", "Available")], default="AVAILABLE", max_length=9),
                ),
                ("quantity", models.DecimalField(max_digits=14, decimal_places=4, default=Decimal("0"))),
                ("pool", models.ForeignKey(on_delete=PROTECT, to="inventory.valuationpool")),
                ("warehouse", models.ForeignKey(on_delete=PROTECT, to="inventory.warehouse")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("pool", "warehouse", "stock_state"),
                        name="uq_inventory_balance_pool_warehouse_state",
                    ),
                    models.CheckConstraint(
                        condition=Q(stock_state="AVAILABLE"),
                        name="inventory_balance_stock_state_valid",
                    ),
                    models.CheckConstraint(
                        condition=Q(quantity__gte=0),
                        name="inventory_balance_quantity_nonnegative",
                    ),
                ],
            },
        ),
        # PostgreSQL rejects infinities for precision-limited numeric columns, but accepts NaN.
        migrations.RunSQL(
            """
            ALTER TABLE inventory_stocklocationbalance
            ADD CONSTRAINT inventory_balance_quantity_not_nan CHECK (quantity <> 'NaN'::numeric);
            """,
            """
            ALTER TABLE inventory_stocklocationbalance
            DROP CONSTRAINT inventory_balance_quantity_not_nan;
            """,
        ),
    ]
