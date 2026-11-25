import uuid

import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import F, PROTECT, Q


TRANSACTION_STATUSES = [("DRAFT", "Draft"), ("APPROVED", "Approved"), ("CANCELLED", "Cancelled")]


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0003_worknode_project_target"),
    ]

    operations = [
        migrations.CreateModel(
            name="TestTransaction",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("document_no", models.CharField(editable=False, max_length=40)),
                ("title", models.CharField(max_length=160)),
                ("status", models.CharField(choices=TRANSACTION_STATUSES, default="DRAFT", max_length=9)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("cancel_reason", models.TextField(blank=True, null=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=PROTECT,
                        related_name="approved_test_transactions", to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "cancelled_by",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=PROTECT,
                        related_name="cancelled_test_transactions", to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "corrects",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=PROTECT, related_name="corrections",
                        to="test_transactions.testtransaction",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="created_test_transactions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("project", models.ForeignKey(on_delete=PROTECT, to="projects.project")),
            ],
            options={
                "constraints": [
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
                ],
            },
        ),
        migrations.CreateModel(
            name="AuditEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID",
                    ),
                ),
                ("occurred_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "event_type",
                    models.CharField(choices=[("CREATED", "Created"), ("APPROVED", "Approved")], max_length=8),
                ),
                ("correlation_id", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("prior_state", models.CharField(choices=TRANSACTION_STATUSES, max_length=9, blank=True, null=True)),
                ("next_state", models.CharField(choices=TRANSACTION_STATUSES, max_length=9)),
                ("reason", models.TextField(blank=True, null=True)),
                ("actor", models.ForeignKey(on_delete=PROTECT, to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=PROTECT, to="projects.project")),
                (
                    "test_transaction",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="audit_events",
                        to="test_transactions.testtransaction",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            Q(event_type="CREATED", prior_state__isnull=True, next_state="DRAFT")
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
                ],
            },
        ),
        migrations.CreateModel(
            name="Attachment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("storage_key", models.CharField(max_length=64, unique=True)),
                ("original_name", models.CharField(max_length=255)),
                ("media_type", models.CharField(max_length=100)),
                ("size_bytes", models.PositiveIntegerField()),
                ("sha256", models.CharField(max_length=64)),
                ("uploaded_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("project", models.ForeignKey(on_delete=PROTECT, to="projects.project")),
                ("uploaded_by", models.ForeignKey(on_delete=PROTECT, to=settings.AUTH_USER_MODEL)),
                (
                    "transaction",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="attachments",
                        to="test_transactions.testtransaction",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=Q(size_bytes__gt=0), name="test_attachment_size_positive",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="TestTransactionLine",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID",
                    ),
                ),
                ("line_no", models.PositiveIntegerField()),
                ("description", models.CharField(blank=True, max_length=240)),
                ("qty", models.DecimalField(decimal_places=4, max_digits=14)),
                ("unit_price", models.DecimalField(decimal_places=4, max_digits=16)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=24)),
                ("project", models.ForeignKey(on_delete=PROTECT, to="projects.project")),
                (
                    "transaction",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="lines", to="test_transactions.testtransaction",
                    ),
                ),
                ("work_node", models.ForeignKey(on_delete=PROTECT, to="projects.worknode")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("transaction", "line_no"), name="test_line_transaction_number_unique",
                    ),
                    models.CheckConstraint(
                        condition=Q(line_no__gt=0), name="test_line_number_positive",
                    ),
                    models.CheckConstraint(condition=Q(qty__gt=0), name="test_line_qty_positive"),
                    models.CheckConstraint(
                        condition=Q(unit_price__gte=0), name="test_line_unit_price_nonnegative",
                    ),
                    models.CheckConstraint(
                        condition=Q(amount__gte=0), name="test_line_amount_nonnegative",
                    ),
                ],
            },
        ),
        # PostgreSQL numeric precision rejects infinities. NaN requires explicit checks.
        migrations.RunSQL(
            """
            ALTER TABLE test_transactions_testtransactionline
            ADD CONSTRAINT test_line_qty_not_nan CHECK (qty <> 'NaN'::numeric),
            ADD CONSTRAINT test_line_unit_price_not_nan CHECK (unit_price <> 'NaN'::numeric),
            ADD CONSTRAINT test_line_amount_not_nan CHECK (amount <> 'NaN'::numeric);
            """,
            """
            ALTER TABLE test_transactions_testtransactionline
            DROP CONSTRAINT test_line_amount_not_nan,
            DROP CONSTRAINT test_line_unit_price_not_nan,
            DROP CONSTRAINT test_line_qty_not_nan;
            """,
        ),
    ]
