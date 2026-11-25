from django.conf import settings
from django.db import models
from django.db.models import F, Q


class ProjectAccess(models.Model):
    class Role(models.TextChoices):
        OPERATOR = "OPERATOR", "Operator"
        MANAGER = "MANAGER", "Manager"
        VIEWER = "VIEWER", "Viewer"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="project_accesses",
    )
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    role = models.CharField(max_length=8, choices=Role)
    active = models.BooleanField(default=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="project_access_grants",
    )
    granted_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        permissions = [
            ("view_internal_api_docs", "Can view internal API documentation"),
            ("manage_project_access", "Can manage project access"),
            ("download_test_attachment", "Can download test attachments in scope"),
            ("print_test_document", "Can print test documents in scope"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("user", "project", "role"), name="project_access_user_project_role_unique",
            ),
            models.CheckConstraint(
                condition=Q(role__in=("OPERATOR", "MANAGER", "VIEWER")),
                name="project_access_role_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(active=True, revoked_at__isnull=True)
                    | Q(active=False, revoked_at__isnull=False, revoked_at__gte=F("granted_at"))
                ),
                name="project_access_revocation_valid",
            ),
        ]


class ProjectAccessEvent(models.Model):
    class EventType(models.TextChoices):
        GRANTED = "GRANTED", "Granted"
        REVOKED = "REVOKED", "Revoked"

    membership = models.ForeignKey(
        ProjectAccess, on_delete=models.PROTECT, related_name="events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="project_access_events",
    )
    occurred_at = models.DateTimeField()
    event_type = models.CharField(max_length=7, choices=EventType)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(event_type__in=("GRANTED", "REVOKED")),
                name="project_access_event_type_valid",
            ),
        ]
