from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            """
            ALTER TABLE inventory_valuationpool
            ADD CONSTRAINT inventory_general_pool_owner_kind_fk
            FOREIGN KEY (owner_id, general_owner_kind)
            REFERENCES projects_party (id, identity_kind) MATCH SIMPLE;
            """,
            """
            ALTER TABLE inventory_valuationpool
            DROP CONSTRAINT inventory_general_pool_owner_kind_fk;
            """,
        ),
    ]
