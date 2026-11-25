import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


# Keep the guards in the same migration as their tables and drop them before a rollback.
GUARD_APPROVED_TERMS = """
CREATE FUNCTION s103_guard_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('APPROVED', 'SUPERSEDED') THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Approved contract history cannot be deleted'
                USING ERRCODE = '23514', CONSTRAINT = 'contract_revision_immutable';
        END IF;
        IF (to_jsonb(NEW) - 'status') IS DISTINCT FROM (to_jsonb(OLD) - 'status')
            OR (NEW.status <> OLD.status AND NOT (OLD.status = 'APPROVED' AND NEW.status = 'SUPERSEDED'))
        THEN
            RAISE EXCEPTION 'Approved contract terms cannot be changed'
                USING ERRCODE = '23514', CONSTRAINT = 'contract_revision_immutable';
        END IF;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.status = 'SUPERSEDED' THEN
            RAISE EXCEPTION 'A draft cannot be superseded'
                USING ERRCODE = '23514', CONSTRAINT = 'contract_revision_transition';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER s103_revision_guard
BEFORE UPDATE OR DELETE ON contracts_contractrevision
FOR EACH ROW EXECUTE FUNCTION s103_guard_revision();

CREATE FUNCTION s103_guard_component() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    revision_key uuid;
    revision_state varchar;
BEGIN
    IF TG_OP = 'DELETE' THEN
        revision_key := OLD.revision_id;
    ELSE
        revision_key := NEW.revision_id;
        IF TG_OP = 'UPDATE' THEN
            IF NEW.revision_id <> OLD.revision_id THEN
                RAISE EXCEPTION 'Create a separate component for a new revision'
                    USING ERRCODE = '23514', CONSTRAINT = 'contract_component_revision_fixed';
            END IF;
        END IF;
    END IF;

    SELECT status INTO revision_state FROM contracts_contractrevision
        WHERE id = revision_key FOR UPDATE;
    IF revision_state IS DISTINCT FROM 'DRAFT' THEN
        RAISE EXCEPTION 'Components can only be written on a draft revision'
            USING ERRCODE = '23514', CONSTRAINT = 'contract_component_draft_only';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER s103_component_guard
BEFORE INSERT OR UPDATE OR DELETE ON contracts_contractcomponent
FOR EACH ROW EXECUTE FUNCTION s103_guard_component();
"""

REMOVE_GUARDS = """
DROP TRIGGER s103_component_guard ON contracts_contractcomponent;
DROP FUNCTION s103_guard_component();
DROP TRIGGER s103_revision_guard ON contracts_contractrevision;
DROP FUNCTION s103_guard_revision();
"""


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="OwnerContract",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("project", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to="projects.project")),
                ("code", models.CharField(max_length=40)),
                ("status", models.CharField(choices=[("ACTIVE", "Active"), ("ARCHIVED", "Archived")], default="ACTIVE", max_length=8)),
            ],
            options={"constraints": [
                models.CheckConstraint(condition=Q(status__in=("ACTIVE", "ARCHIVED")), name="ownercontract_status_valid"),
            ]},
        ),
        migrations.CreateModel(
            name="ContractRevision",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("contract", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name="revisions", to="contracts.ownercontract",
                )),
                ("revision_no", models.PositiveIntegerField()),
                ("valid_from", models.DateField()),
                ("status", models.CharField(
                    choices=[("DRAFT", "Draft"), ("APPROVED", "Approved"), ("SUPERSEDED", "Superseded")],
                    default="DRAFT", max_length=10,
                )),
                ("approved_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL,
                )),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"constraints": [
                models.UniqueConstraint(fields=("contract", "revision_no"), name="contract_revision_number_unique"),
                models.CheckConstraint(condition=Q(revision_no__gt=0), name="contract_revision_number_positive"),
                models.CheckConstraint(
                    condition=(
                        Q(status="DRAFT", approved_by__isnull=True, approved_at__isnull=True)
                        | Q(
                            status__in=("APPROVED", "SUPERSEDED"),
                            approved_by__isnull=False,
                            approved_at__isnull=False,
                        )
                    ),
                    name="contract_revision_approval_valid",
                ),
            ]},
        ),
        migrations.CreateModel(
            name="ContractComponent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("revision", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name="components", to="contracts.contractrevision",
                )),
                ("component_code", models.CharField(max_length=40)),
                ("name", models.CharField(max_length=160)),
                ("pricing_method", models.CharField(
                    choices=[("COST_PLUS", "Cost plus"), ("UNIT_PRICE", "Unit price"), ("LUMP_SUM", "Lump sum")],
                    max_length=10,
                )),
                ("fee_rate", models.DecimalField(blank=True, decimal_places=4, max_digits=7, null=True)),
                ("unit_price", models.DecimalField(blank=True, decimal_places=4, max_digits=16, null=True)),
                ("stage_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=16, null=True)),
            ],
            options={"constraints": [
                models.UniqueConstraint(fields=("revision", "component_code"), name="contract_component_code_unique"),
                models.CheckConstraint(
                    condition=(
                        Q(
                            pricing_method="COST_PLUS", fee_rate__isnull=False, fee_rate__gte=0,
                            unit_price__isnull=True, stage_amount__isnull=True,
                        )
                        | Q(
                            pricing_method="UNIT_PRICE", unit_price__isnull=False, unit_price__gt=0,
                            fee_rate__isnull=True, stage_amount__isnull=True,
                        )
                        | Q(
                            pricing_method="LUMP_SUM", stage_amount__isnull=False, stage_amount__gt=0,
                            fee_rate__isnull=True, unit_price__isnull=True,
                        )
                    ),
                    name="contract_component_pricing_valid",
                ),
            ]},
        ),
        migrations.RunSQL(GUARD_APPROVED_TERMS, REMOVE_GUARDS),
    ]
