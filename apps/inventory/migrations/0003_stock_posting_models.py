import uuid

import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import PROTECT, Q


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("inventory", "0002_general_pool_owner_fk"),
        ("projects", "0003_worknode_project_target"),
    ]

    operations = [
        migrations.CreateModel(
            name="StockIssue",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("document_no", models.CharField(max_length=40)),
                (
                    "status",
                    models.CharField(
                        choices=[("DRAFT", "Draft"), ("APPROVED", "Approved"), ("CANCELLED", "Cancelled")],
                        default="DRAFT", max_length=9,
                    ),
                ),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=PROTECT,
                        related_name="approved_stock_issues", to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="created_stock_issues", to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("project", models.ForeignKey(on_delete=PROTECT, to="projects.project")),
                (
                    "reverses",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=PROTECT,
                        related_name="reversals", to="inventory.stockissue",
                    ),
                ),
                ("work_node", models.ForeignKey(on_delete=PROTECT, to="projects.worknode")),
            ],
            options={
                "constraints": [
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
                ],
            },
        ),
        migrations.CreateModel(
            name="StockIssueLine",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("qty", models.DecimalField(decimal_places=4, max_digits=14)),
                ("line_no", models.PositiveIntegerField()),
                ("balance", models.ForeignKey(on_delete=PROTECT, to="inventory.stocklocationbalance")),
                (
                    "issue",
                    models.ForeignKey(on_delete=PROTECT, related_name="lines", to="inventory.stockissue"),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("issue", "line_no"), name="uq_stock_issue_line_issue_number",
                    ),
                    models.UniqueConstraint(fields=("id", "balance"), name="uq_stock_issue_line_id_balance"),
                    models.CheckConstraint(
                        condition=Q(line_no__gt=0), name="stock_issue_line_number_positive",
                    ),
                    models.CheckConstraint(condition=Q(qty__gt=0), name="stock_issue_line_qty_positive"),
                ],
            },
        ),
        migrations.CreateModel(
            name="StockMovement",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "event_kind",
                    models.CharField(choices=[("OPENING", "Opening"), ("ISSUE", "Issue")], max_length=7),
                ),
                ("signed_qty", models.DecimalField(decimal_places=4, max_digits=14)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "balance",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="movements", to="inventory.stocklocationbalance",
                    ),
                ),
                (
                    "issue_line",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=PROTECT,
                        related_name="movements", to="inventory.stockissueline",
                    ),
                ),
                (
                    "reverses",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=PROTECT,
                        related_name="reversals", to="inventory.stockmovement",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("balance",),
                        condition=Q(event_kind="OPENING"),
                        name="uq_stock_movement_opening_balance",
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
                ],
            },
        ),
    ]
