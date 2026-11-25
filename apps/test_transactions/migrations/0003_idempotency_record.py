import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import PROTECT, Q


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("test_transactions", "0002_project_scoped_foreign_keys"),
    ]

    operations = [
        migrations.CreateModel(
            name="IdempotencyRecord",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID",
                    ),
                ),
                (
                    "operation",
                    models.CharField(
                        choices=[("CREATE_TEST_DRAFT", "Create test draft")], max_length=32,
                    ),
                ),
                ("key", models.UUIDField()),
                ("payload_sha256", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[("IN_PROGRESS", "In progress"), ("COMPLETED", "Completed")],
                        default="IN_PROGRESS", max_length=11,
                    ),
                ),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("response_status", models.PositiveSmallIntegerField(null=True, blank=True)),
                ("response_body", models.JSONField(null=True, blank=True)),
                ("actor", models.ForeignKey(on_delete=PROTECT, to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=PROTECT, to="projects.project")),
                (
                    "test_transaction",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=PROTECT, to="test_transactions.testtransaction",
                    ),
                ),
            ],
            options={
                "constraints": [
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
                ],
            },
        ),
        migrations.RunSQL(
            """
            ALTER TABLE test_transactions_idempotencyrecord
            ADD CONSTRAINT idem_transaction_project_fk
            FOREIGN KEY (test_transaction_id, project_id)
            REFERENCES test_transactions_testtransaction (id, project_id);
            """,
            """
            ALTER TABLE test_transactions_idempotencyrecord
            DROP CONSTRAINT idem_transaction_project_fk;
            """,
        ),
        migrations.RunSQL(
            """
            CREATE FUNCTION idem_protect_completed_receipt() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.status = 'COMPLETED' THEN
                    RAISE EXCEPTION 'Completed idempotency receipts are immutable'
                    USING ERRCODE = '23514', CONSTRAINT = 'idem_completed_receipt_immutable';
                END IF;
                RETURN NEW;
            END;
            $$;

            CREATE TRIGGER idem_completed_receipt_immutable
            BEFORE UPDATE ON test_transactions_idempotencyrecord
            FOR EACH ROW EXECUTE FUNCTION idem_protect_completed_receipt();
            """,
            """
            DROP TRIGGER idem_completed_receipt_immutable ON test_transactions_idempotencyrecord;
            DROP FUNCTION idem_protect_completed_receipt();
            """,
        ),
    ]
