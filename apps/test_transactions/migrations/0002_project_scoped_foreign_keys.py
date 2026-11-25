from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0003_worknode_project_target"),
        ("test_transactions", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            """
            ALTER TABLE test_transactions_testtransaction
            ADD CONSTRAINT test_transaction_corrects_project_fk
            FOREIGN KEY (corrects_id, project_id)
            REFERENCES test_transactions_testtransaction (id, project_id);

            ALTER TABLE test_transactions_testtransactionline
            ADD CONSTRAINT test_line_transaction_project_fk
            FOREIGN KEY (transaction_id, project_id)
            REFERENCES test_transactions_testtransaction (id, project_id),
            ADD CONSTRAINT test_line_work_node_project_fk
            FOREIGN KEY (work_node_id, project_id)
            REFERENCES projects_worknode (id, project_id);

            ALTER TABLE test_transactions_attachment
            ADD CONSTRAINT test_attachment_transaction_project_fk
            FOREIGN KEY (transaction_id, project_id)
            REFERENCES test_transactions_testtransaction (id, project_id);

            ALTER TABLE test_transactions_auditevent
            ADD CONSTRAINT test_audit_transaction_project_fk
            FOREIGN KEY (test_transaction_id, project_id)
            REFERENCES test_transactions_testtransaction (id, project_id);
            """,
            """
            ALTER TABLE test_transactions_auditevent
            DROP CONSTRAINT test_audit_transaction_project_fk;

            ALTER TABLE test_transactions_attachment
            DROP CONSTRAINT test_attachment_transaction_project_fk;

            ALTER TABLE test_transactions_testtransactionline
            DROP CONSTRAINT test_line_work_node_project_fk,
            DROP CONSTRAINT test_line_transaction_project_fk;

            ALTER TABLE test_transactions_testtransaction
            DROP CONSTRAINT test_transaction_corrects_project_fk;
            """,
        ),
        migrations.RunSQL(
            """
            CREATE FUNCTION test_transaction_protect_approval() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.status = 'APPROVED' THEN
                    RAISE EXCEPTION 'Approved test transactions are immutable'
                    USING ERRCODE = '23514', CONSTRAINT = 'test_transaction_approved_immutable';
                END IF;

                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $$;

            CREATE TRIGGER test_transaction_approved_immutable
            BEFORE UPDATE OR DELETE ON test_transactions_testtransaction
            FOR EACH ROW EXECUTE FUNCTION test_transaction_protect_approval();
            """,
            """
            DROP TRIGGER test_transaction_approved_immutable ON test_transactions_testtransaction;
            DROP FUNCTION test_transaction_protect_approval();
            """,
        ),
        migrations.RunSQL(
            """
            CREATE FUNCTION test_transaction_protect_approved_children() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE
                parent_ids uuid[];
                parent_status varchar;
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    parent_ids := ARRAY[NEW.transaction_id];
                ELSIF TG_OP = 'DELETE' THEN
                    parent_ids := ARRAY[OLD.transaction_id];
                ELSE
                    parent_ids := ARRAY[OLD.transaction_id, NEW.transaction_id];
                END IF;

                -- Use the trigger table's schema and serialize against parent approval.
                FOR parent_status IN EXECUTE pg_catalog.format(
                    'SELECT status FROM %I.test_transactions_testtransaction
                     WHERE id = ANY($1) ORDER BY id FOR SHARE',
                    TG_TABLE_SCHEMA
                ) USING parent_ids LOOP
                    IF parent_status = 'APPROVED' THEN
                        RAISE EXCEPTION 'Lines and attachments of approved test transactions are immutable'
                        USING ERRCODE = '23514', CONSTRAINT = 'test_transaction_approved_children_immutable';
                    END IF;
                END LOOP;

                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $$;

            CREATE TRIGGER test_transaction_line_approval_guard
            BEFORE INSERT OR UPDATE OR DELETE ON test_transactions_testtransactionline
            FOR EACH ROW EXECUTE FUNCTION test_transaction_protect_approved_children();

            CREATE TRIGGER test_transaction_attachment_approval_guard
            BEFORE INSERT OR UPDATE OR DELETE ON test_transactions_attachment
            FOR EACH ROW EXECUTE FUNCTION test_transaction_protect_approved_children();
            """,
            """
            DROP TRIGGER test_transaction_attachment_approval_guard ON test_transactions_attachment;
            DROP TRIGGER test_transaction_line_approval_guard ON test_transactions_testtransactionline;
            DROP FUNCTION test_transaction_protect_approved_children();
            """,
        ),
    ]
