from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import ContractRevision, OwnerContract


@transaction.atomic
def approve_revision(revision_id, *, approved_by):
    """Approve the draft and preserve the terms of the previous approved revision."""
    contract_id = ContractRevision.objects.values_list("contract_id", flat=True).get(pk=revision_id)
    OwnerContract.objects.select_for_update().get(pk=contract_id)
    revision = ContractRevision.objects.select_for_update().get(pk=revision_id, contract_id=contract_id)

    if revision.status != ContractRevision.Status.DRAFT:
        raise ValidationError("Only a draft revision can be approved.")

    ContractRevision.objects.filter(
        contract_id=contract_id, status=ContractRevision.Status.APPROVED,
    ).update(status=ContractRevision.Status.SUPERSEDED)
    revision.status = ContractRevision.Status.APPROVED
    revision.approved_by = approved_by
    revision.approved_at = timezone.now()
    revision.save(update_fields=("status", "approved_by", "approved_at"))
    return revision
