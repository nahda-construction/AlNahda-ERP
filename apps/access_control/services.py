from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import ProjectAccess, ProjectAccessEvent
from .policies import has_internal_permission, is_internal_employee


def _require_membership_manager(actor):
    if not has_internal_permission(actor, "manage_project_access"):
        raise PermissionDenied


@transaction.atomic
def grant_project_access(*, actor, user, project, role):
    _require_membership_manager(actor)
    if not is_internal_employee(user):
        raise ValidationError("Project access requires an active employee account.")
    if role not in ProjectAccess.Role.values:
        raise ValidationError("Unknown project role.")

    membership, created = ProjectAccess.objects.select_for_update().get_or_create(
        user=user,
        project=project,
        role=role,
        defaults={"granted_by": actor, "granted_at": timezone.now},
    )
    if not created:
        if membership.active:
            return membership
        membership.active = True
        membership.granted_by = actor
        membership.granted_at = timezone.now()
        membership.revoked_at = None
        membership.save(update_fields=("active", "granted_by", "granted_at", "revoked_at"))

    ProjectAccessEvent.objects.create(
        membership=membership,
        actor=actor,
        occurred_at=membership.granted_at,
        event_type=ProjectAccessEvent.EventType.GRANTED,
    )
    return membership


@transaction.atomic
def revoke_project_access(*, actor, membership_id):
    _require_membership_manager(actor)
    membership = ProjectAccess.objects.select_for_update().get(pk=membership_id)
    if not membership.active:
        return membership

    membership.active = False
    membership.revoked_at = timezone.now()
    membership.save(update_fields=("active", "revoked_at"))
    ProjectAccessEvent.objects.create(
        membership=membership,
        actor=actor,
        occurred_at=membership.revoked_at,
        event_type=ProjectAccessEvent.EventType.REVOKED,
    )
    return membership
