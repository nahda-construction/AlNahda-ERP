import uuid

from django.db import models
from django.db.models import Q


class Party(models.Model):
    class Kind(models.TextChoices):
        COMPANY = "COMPANY", "Company"
        EXTERNAL = "EXTERNAL", "External"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    identity_kind = models.CharField(max_length=8, choices=Kind)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(identity_kind__in=("COMPANY", "EXTERNAL")),
                name="party_identity_kind_valid",
            ),
            models.UniqueConstraint(
                fields=("identity_kind",),
                condition=Q(identity_kind="COMPANY"),
                name="party_single_company",
            ),
            models.UniqueConstraint(
                fields=("id", "identity_kind"),
                name="party_id_identity_kind_unique",
            ),
        ]


class Project(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ARCHIVED = "ARCHIVED", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    owner_party = models.ForeignKey(Party, on_delete=models.PROTECT)
    status = models.CharField(max_length=8, choices=Status, default=Status.ACTIVE)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=("ACTIVE", "ARCHIVED")),
                name="project_status_valid",
            ),
        ]


class WorkNode(models.Model):
    class NodeType(models.TextChoices):
        SCOPE = "SCOPE", "Scope"
        ITEM = "ITEM", "Execution item"
        ACTIVITY_1 = "ACTIVITY_1", "First-level activity"
        ACTIVITY_2 = "ACTIVITY_2", "Second-level activity"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="children",
    )
    parent_type = models.CharField(max_length=10, choices=NodeType, null=True, blank=True)
    node_type = models.CharField(max_length=10, choices=NodeType)
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=160)
    status = models.CharField(max_length=8, choices=Project.Status, default=Project.Status.ACTIVE)
    unit = models.CharField(max_length=24, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("project", "code"), name="worknode_project_code_unique"),
            models.UniqueConstraint(
                fields=("id", "project", "node_type"), name="worknode_parent_target_unique",
            ),
            models.CheckConstraint(
                condition=(
                    Q(node_type="SCOPE", parent__isnull=True, parent_type__isnull=True)
                    | (
                        Q(parent__isnull=False, parent_type__isnull=False)
                        & (
                            Q(node_type="ITEM", parent_type="SCOPE")
                            | Q(node_type="ACTIVITY_1", parent_type="ITEM")
                            | Q(node_type="ACTIVITY_2", parent_type="ACTIVITY_1")
                        )
                    )
                ),
                name="worknode_valid_parent_level",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("ACTIVE", "ARCHIVED")), name="worknode_status_valid",
            ),
        ]
