import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.projects.models import Project


class OwnerContract(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField(Project, on_delete=models.PROTECT)
    code = models.CharField(max_length=40)
    status = models.CharField(max_length=8, choices=Project.Status, default=Project.Status.ACTIVE)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=("ACTIVE", "ARCHIVED")), name="ownercontract_status_valid",
            ),
        ]


class ContractRevision(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        SUPERSEDED = "SUPERSEDED", "Superseded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(OwnerContract, on_delete=models.PROTECT, related_name="revisions")
    revision_no = models.PositiveIntegerField()
    valid_from = models.DateField()
    status = models.CharField(max_length=10, choices=Status, default=Status.DRAFT)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("contract", "revision_no"), name="contract_revision_number_unique",
            ),
            models.CheckConstraint(
                condition=Q(revision_no__gt=0), name="contract_revision_number_positive",
            ),
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
        ]


class ContractComponent(models.Model):
    class PricingMethod(models.TextChoices):
        COST_PLUS = "COST_PLUS", "Cost plus"
        UNIT_PRICE = "UNIT_PRICE", "Unit price"
        LUMP_SUM = "LUMP_SUM", "Lump sum"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.ForeignKey(ContractRevision, on_delete=models.PROTECT, related_name="components")
    component_code = models.CharField(max_length=40)
    name = models.CharField(max_length=160)
    pricing_method = models.CharField(max_length=10, choices=PricingMethod)
    fee_rate = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    unit_price = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    stage_amount = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("revision", "component_code"), name="contract_component_code_unique",
            ),
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
        ]
