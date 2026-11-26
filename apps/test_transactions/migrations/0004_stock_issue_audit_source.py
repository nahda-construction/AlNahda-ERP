from django.db import migrations, models
from django.db.models import PROTECT, Q


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0004_stock_posting_constraints"),
        ("test_transactions", "0003_idempotency_record"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="auditevent", name="test_audit_transition_valid",
        ),
        migrations.AddField(
            model_name="auditevent",
            name="stock_issue",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=PROTECT, related_name="audit_events", to="inventory.stockissue",
            ),
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="test_transaction",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=PROTECT, related_name="audit_events", to="test_transactions.testtransaction",
            ),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=(
                    Q(test_transaction__isnull=False, stock_issue__isnull=True)
                    | Q(test_transaction__isnull=True, stock_issue__isnull=False)
                ),
                name="audit_single_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=(
                    Q(
                        event_type="CREATED", test_transaction__isnull=False,
                        prior_state__isnull=True, next_state="DRAFT",
                    )
                    | Q(
                        event_type="APPROVED", prior_state__isnull=False,
                        prior_state="DRAFT", next_state="APPROVED",
                    )
                ),
                name="test_audit_transition_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.UniqueConstraint(
                fields=("stock_issue",), condition=Q(stock_issue__isnull=False),
                name="uq_stock_issue_audit_event",
            ),
        ),
        migrations.RunSQL(
            """
            ALTER TABLE test_transactions_auditevent
            ADD CONSTRAINT stock_audit_issue_project_fk
            FOREIGN KEY (stock_issue_id, project_id)
            REFERENCES inventory_stockissue (id, project_id) MATCH SIMPLE;
            """,
            """
            ALTER TABLE test_transactions_auditevent DROP CONSTRAINT stock_audit_issue_project_fk;
            """,
        ),
    ]
