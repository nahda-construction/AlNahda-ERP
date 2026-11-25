import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0002_worknode_parent_fk"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectAccess",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID",
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[("OPERATOR", "Operator"), ("MANAGER", "Manager"), ("VIEWER", "Viewer")],
                        max_length=8,
                    ),
                ),
                ("active", models.BooleanField(default=True)),
                ("granted_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "granted_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="project_access_grants",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="projects.project",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="project_accesses",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "permissions": [
                    ("view_internal_api_docs", "Can view internal API documentation"),
                    ("manage_project_access", "Can manage project access"),
                    ("download_test_attachment", "Can download test attachments in scope"),
                    ("print_test_document", "Can print test documents in scope"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "project", "role"),
                        name="project_access_user_project_role_unique",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(role__in=("OPERATOR", "MANAGER", "VIEWER")),
                        name="project_access_role_valid",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(active=True, revoked_at__isnull=True)
                            | models.Q(
                                active=False, revoked_at__isnull=False,
                                revoked_at__gte=models.F("granted_at"),
                            )
                        ),
                        name="project_access_revocation_valid",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ProjectAccessEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID",
                    ),
                ),
                ("occurred_at", models.DateTimeField()),
                ("event_type", models.CharField(choices=[("GRANTED", "Granted"), ("REVOKED", "Revoked")], max_length=7)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="project_access_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "membership",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="access_control.projectaccess",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(event_type__in=("GRANTED", "REVOKED")),
                        name="project_access_event_type_valid",
                    ),
                ],
            },
        ),
    ]
