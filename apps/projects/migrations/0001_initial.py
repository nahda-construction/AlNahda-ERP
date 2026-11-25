import uuid

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Party",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.CharField(max_length=40, unique=True)),
                ("name", models.CharField(max_length=160)),
                ("identity_kind", models.CharField(choices=[("COMPANY", "Company"), ("EXTERNAL", "External")], max_length=8)),
                ("active", models.BooleanField(default=True)),
            ],
            options={"constraints": [
                models.CheckConstraint(condition=Q(identity_kind__in=("COMPANY", "EXTERNAL")), name="party_identity_kind_valid"),
                models.UniqueConstraint(condition=Q(identity_kind="COMPANY"), fields=("identity_kind",), name="party_single_company"),
                models.UniqueConstraint(fields=("id", "identity_kind"), name="party_id_identity_kind_unique"),
            ]},
        ),
        migrations.CreateModel(
            name="Project",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.CharField(max_length=40, unique=True)),
                ("name", models.CharField(max_length=160)),
                ("owner_party", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="projects.party")),
                ("status", models.CharField(choices=[("ACTIVE", "Active"), ("ARCHIVED", "Archived")], default="ACTIVE", max_length=8)),
            ],
            options={"constraints": [
                models.CheckConstraint(condition=Q(status__in=("ACTIVE", "ARCHIVED")), name="project_status_valid"),
            ]},
        ),
        migrations.CreateModel(
            name="WorkNode",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="projects.project")),
                ("parent", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="children", to="projects.worknode",
                )),
                ("parent_type", models.CharField(
                    blank=True, choices=[("SCOPE", "Scope"), ("ITEM", "Execution item"),
                                         ("ACTIVITY_1", "First-level activity"), ("ACTIVITY_2", "Second-level activity")],
                    max_length=10, null=True,
                )),
                ("node_type", models.CharField(
                    choices=[("SCOPE", "Scope"), ("ITEM", "Execution item"),
                             ("ACTIVITY_1", "First-level activity"), ("ACTIVITY_2", "Second-level activity")],
                    max_length=10,
                )),
                ("code", models.CharField(max_length=40)),
                ("name", models.CharField(max_length=160)),
                ("status", models.CharField(choices=[("ACTIVE", "Active"), ("ARCHIVED", "Archived")], default="ACTIVE", max_length=8)),
                ("unit", models.CharField(blank=True, default="", max_length=24)),
            ],
            options={"constraints": [
                models.UniqueConstraint(fields=("project", "code"), name="worknode_project_code_unique"),
                models.UniqueConstraint(fields=("id", "project", "node_type"), name="worknode_parent_target_unique"),
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
                models.CheckConstraint(condition=Q(status__in=("ACTIVE", "ARCHIVED")), name="worknode_status_valid"),
            ]},
        ),
    ]
