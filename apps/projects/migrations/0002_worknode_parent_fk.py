from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("projects", "0001_initial")]

    operations = [
        migrations.RunSQL(
            """
            ALTER TABLE projects_worknode
            ADD CONSTRAINT worknode_parent_project_type_fk
            FOREIGN KEY (parent_id, project_id, parent_type)
            REFERENCES projects_worknode (id, project_id, node_type);
            """,
            """
            ALTER TABLE projects_worknode
            DROP CONSTRAINT worknode_parent_project_type_fk;
            """,
        ),
    ]
