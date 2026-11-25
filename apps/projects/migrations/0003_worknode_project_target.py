from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0002_worknode_parent_fk"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="worknode",
            constraint=models.UniqueConstraint(
                fields=("id", "project"), name="worknode_project_target_unique",
            ),
        ),
    ]
