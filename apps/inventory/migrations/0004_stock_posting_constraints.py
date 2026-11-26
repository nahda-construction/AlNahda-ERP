from django.db import migrations


APPROVAL_GUARDS = """
CREATE FUNCTION inventory_protect_stock_issue_approval() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'APPROVED' THEN
        RAISE EXCEPTION 'Approved stock issues are immutable'
        USING ERRCODE = '23514', CONSTRAINT = 'stock_issue_approved_immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER stock_issue_approved_immutable
BEFORE UPDATE OR DELETE ON inventory_stockissue
FOR EACH ROW EXECUTE FUNCTION inventory_protect_stock_issue_approval();

CREATE FUNCTION inventory_protect_approved_issue_lines() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    issue_ids uuid[];
    issue_status varchar;
BEGIN
    IF TG_OP = 'INSERT' THEN
        issue_ids := ARRAY[NEW.issue_id];
    ELSIF TG_OP = 'DELETE' THEN
        issue_ids := ARRAY[OLD.issue_id];
    ELSE
        issue_ids := ARRAY[OLD.issue_id, NEW.issue_id];
    END IF;

    -- Serialize line writes against approval, including moves between issues.
    FOR issue_status IN EXECUTE pg_catalog.format(
        'SELECT status FROM %I.inventory_stockissue
         WHERE id = ANY($1) ORDER BY id FOR SHARE',
        TG_TABLE_SCHEMA
    ) USING issue_ids LOOP
        IF issue_status = 'APPROVED' THEN
            RAISE EXCEPTION 'Lines of approved stock issues are immutable'
            USING ERRCODE = '23514', CONSTRAINT = 'stock_issue_approved_line_immutable';
        END IF;
    END LOOP;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER stock_issue_approved_line_immutable
BEFORE INSERT OR UPDATE OR DELETE ON inventory_stockissueline
FOR EACH ROW EXECUTE FUNCTION inventory_protect_approved_issue_lines();
"""


REMOVE_APPROVAL_GUARDS = """
DROP TRIGGER stock_issue_approved_line_immutable ON inventory_stockissueline;
DROP FUNCTION inventory_protect_approved_issue_lines();
DROP TRIGGER stock_issue_approved_immutable ON inventory_stockissue;
DROP FUNCTION inventory_protect_stock_issue_approval();
"""


MOVEMENT_GUARDS = """
CREATE FUNCTION inventory_protect_stock_movements() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Stock movements are append-only'
    USING ERRCODE = '23514', CONSTRAINT = 'stock_movement_append_only';
END;
$$;

CREATE TRIGGER stock_movement_append_only
BEFORE UPDATE OR DELETE ON inventory_stockmovement
FOR EACH ROW EXECUTE FUNCTION inventory_protect_stock_movements();

CREATE FUNCTION inventory_protect_movement_balance() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    has_movements boolean;
BEGIN
    IF (NEW.id, NEW.pool_id, NEW.warehouse_id, NEW.stock_state)
        IS NOT DISTINCT FROM (OLD.id, OLD.pool_id, OLD.warehouse_id, OLD.stock_state)
    THEN
        RETURN NEW;
    END IF;

    EXECUTE pg_catalog.format(
        'SELECT EXISTS (SELECT 1 FROM %I.inventory_stockmovement WHERE balance_id = $1)',
        TG_TABLE_SCHEMA
    ) INTO has_movements USING OLD.id;
    IF has_movements THEN
        RAISE EXCEPTION 'The identity of a balance used by stock movements is immutable'
        USING ERRCODE = '23514', CONSTRAINT = 'stock_balance_identity_immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER stock_balance_identity_immutable
BEFORE UPDATE OF id, pool_id, warehouse_id, stock_state ON inventory_stocklocationbalance
FOR EACH ROW EXECUTE FUNCTION inventory_protect_movement_balance();

CREATE FUNCTION inventory_protect_movement_pool() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    has_movements boolean;
BEGIN
    IF (NEW.id, NEW.scope_type, NEW.project_id, NEW.owner_id, NEW.material_id, NEW.general_owner_kind)
        IS NOT DISTINCT FROM
        (OLD.id, OLD.scope_type, OLD.project_id, OLD.owner_id, OLD.material_id, OLD.general_owner_kind)
    THEN
        RETURN NEW;
    END IF;

    -- Posting holds these same balance locks. Re-read movements after any wait.
    EXECUTE pg_catalog.format(
        'SELECT id FROM %I.inventory_stocklocationbalance WHERE pool_id = $1 ORDER BY id FOR UPDATE',
        TG_TABLE_SCHEMA
    ) USING OLD.id;

    EXECUTE pg_catalog.format(
        'SELECT EXISTS (
            SELECT 1 FROM %I.inventory_stocklocationbalance AS balance
            JOIN %I.inventory_stockmovement AS movement ON movement.balance_id = balance.id
            WHERE balance.pool_id = $1
        )',
        TG_TABLE_SCHEMA, TG_TABLE_SCHEMA
    ) INTO has_movements USING OLD.id;
    IF has_movements THEN
        RAISE EXCEPTION 'The identity of a valuation pool used by stock movements is immutable'
        USING ERRCODE = '23514', CONSTRAINT = 'stock_pool_identity_immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER stock_pool_identity_immutable
BEFORE UPDATE OF id, scope_type, project_id, owner_id, material_id, general_owner_kind ON inventory_valuationpool
FOR EACH ROW EXECUTE FUNCTION inventory_protect_movement_pool();
"""


REMOVE_MOVEMENT_GUARDS = """
DROP TRIGGER stock_pool_identity_immutable ON inventory_valuationpool;
DROP FUNCTION inventory_protect_movement_pool();
DROP TRIGGER stock_balance_identity_immutable ON inventory_stocklocationbalance;
DROP FUNCTION inventory_protect_movement_balance();
DROP TRIGGER stock_movement_append_only ON inventory_stockmovement;
DROP FUNCTION inventory_protect_stock_movements();
"""


class Migration(migrations.Migration):
    dependencies = [("inventory", "0003_stock_posting_models")]

    operations = [
        migrations.RunSQL(
            """
            ALTER TABLE inventory_stockissue
            ADD CONSTRAINT stock_issue_work_node_project_fk
            FOREIGN KEY (work_node_id, project_id) REFERENCES projects_worknode (id, project_id);

            -- Precision-limited numeric columns reject infinities. NaN needs an explicit check.
            ALTER TABLE inventory_stockissueline
            ADD CONSTRAINT stock_issue_line_qty_not_nan CHECK (qty <> 'NaN'::numeric);

            ALTER TABLE inventory_stockmovement
            ADD CONSTRAINT stock_movement_signed_qty_not_nan CHECK (signed_qty <> 'NaN'::numeric),
            ADD CONSTRAINT stock_movement_issue_line_balance_fk
            FOREIGN KEY (issue_line_id, balance_id) REFERENCES inventory_stockissueline (id, balance_id)
            MATCH SIMPLE;
            """,
            """
            ALTER TABLE inventory_stockmovement
            DROP CONSTRAINT stock_movement_issue_line_balance_fk,
            DROP CONSTRAINT stock_movement_signed_qty_not_nan;

            ALTER TABLE inventory_stockissueline DROP CONSTRAINT stock_issue_line_qty_not_nan;
            ALTER TABLE inventory_stockissue DROP CONSTRAINT stock_issue_work_node_project_fk;
            """,
        ),
        migrations.RunSQL(APPROVAL_GUARDS, REMOVE_APPROVAL_GUARDS),
        migrations.RunSQL(MOVEMENT_GUARDS, REMOVE_MOVEMENT_GUARDS),
    ]
