\set ON_ERROR_STOP on
\if :{?revoke_inventory_runtime}
\else
\set revoke_inventory_runtime off
\endif

BEGIN;
SELECT set_config('erp.runtime_role', :'runtime_role', true),
       set_config('erp.revoke_inventory_runtime', :'revoke_inventory_runtime', true);

-- Inventory grant policy.
DO $inventory_grants$
DECLARE
    runtime_name text := current_setting('erp.runtime_role');
    revoke_inventory boolean := current_setting('erp.revoke_inventory_runtime')::boolean;
    runtime_role pg_roles%ROWTYPE;
    inventory_relation regclass;
    column_list text;
    policy record;
    column_name text;
    privilege text;
    allowed boolean;
BEGIN
    SELECT * INTO STRICT runtime_role FROM pg_roles WHERE rolname = runtime_name;
    IF runtime_name = current_user
        OR NOT runtime_role.rolcanlogin
        OR runtime_role.rolsuper
        OR runtime_role.rolcreaterole
        OR runtime_role.rolcreatedb
        OR runtime_role.rolreplication
        OR runtime_role.rolbypassrls
        OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member = runtime_role.oid)
        OR has_database_privilege(runtime_name, current_database(), 'CREATE')
        OR has_schema_privilege(runtime_name, 'public', 'CREATE')
        OR EXISTS (
            SELECT 1
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public' AND relation.relowner = runtime_role.oid
        )
    THEN
        RAISE EXCEPTION 'The ERP runtime role must be an independent, restricted login';
    END IF;

    -- Clear both table and column grants in the inventory delta before reapplying it.
    FOREACH inventory_relation IN ARRAY ARRAY[
        'public.inventory_material'::regclass,
        'public.inventory_warehouse'::regclass,
        'public.inventory_valuationpool'::regclass,
        'public.inventory_stocklocationbalance'::regclass,
        'public.inventory_stockissue'::regclass,
        'public.inventory_stockissueline'::regclass,
        'public.inventory_stockmovement'::regclass
    ] LOOP
        SELECT string_agg(quote_ident(attname), ', ' ORDER BY attnum) INTO column_list
        FROM pg_attribute WHERE attrelid = inventory_relation AND attnum > 0 AND NOT attisdropped;
        EXECUTE format('REVOKE ALL ON TABLE %s FROM %I', inventory_relation, runtime_name);
        EXECUTE format('REVOKE ALL (%s) ON TABLE %s FROM %I', column_list, inventory_relation, runtime_name);
    END LOOP;
    EXECUTE format('REVOKE SELECT (id, active) ON public.projects_party FROM %I', runtime_name);

    IF NOT revoke_inventory THEN
        EXECUTE format('GRANT SELECT ON TABLE public.inventory_material, public.inventory_warehouse,
            public.inventory_valuationpool, public.inventory_stocklocationbalance,
            public.inventory_stockissue, public.inventory_stockissueline TO %I', runtime_name);
        EXECUTE format('GRANT SELECT, INSERT ON TABLE public.inventory_stockmovement TO %I', runtime_name);
        EXECUTE format('GRANT UPDATE (quantity) ON public.inventory_stocklocationbalance TO %I', runtime_name);
        EXECUTE format('GRANT UPDATE (status, approved_by_id, approved_at) ON public.inventory_stockissue TO %I', runtime_name);
        EXECUTE format('GRANT SELECT (id, active) ON public.projects_party TO %I', runtime_name);
    END IF;

    -- Check effective privileges so PUBLIC, ownership, and grants from other grantors cannot widen access.
    -- The existing audit grants must already have been applied by ERP-02 and remain unchanged.
    FOR policy IN
        SELECT * FROM (VALUES
            ('public.inventory_material'::regclass, true, ARRAY['SELECT'], '{}'::jsonb),
            ('public.inventory_warehouse'::regclass, true, ARRAY['SELECT'], '{}'::jsonb),
            ('public.inventory_valuationpool'::regclass, true, ARRAY['SELECT'], '{}'::jsonb),
            ('public.inventory_stocklocationbalance'::regclass, true, ARRAY['SELECT'], '{"UPDATE":["quantity"]}'::jsonb),
            ('public.inventory_stockissue'::regclass, true, ARRAY['SELECT'], '{"UPDATE":["status","approved_by_id","approved_at"]}'::jsonb),
            ('public.inventory_stockissueline'::regclass, true, ARRAY['SELECT'], '{}'::jsonb),
            ('public.inventory_stockmovement'::regclass, true, ARRAY['SELECT', 'INSERT'], '{}'::jsonb),
            ('public.projects_party'::regclass, true, ARRAY[]::text[], '{"SELECT":["id","active"]}'::jsonb),
            ('public.access_control_projectaccessevent'::regclass, false, ARRAY['SELECT', 'INSERT'], '{}'::jsonb),
            ('public.test_transactions_auditevent'::regclass, false, ARRAY['SELECT', 'INSERT'], '{}'::jsonb)
        ) AS rules(relation_id, inventory_delta, table_privileges, column_privileges)
    LOOP
        FOREACH privilege IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER', 'MAINTAIN'] LOOP
            allowed := (NOT revoke_inventory OR NOT policy.inventory_delta) AND privilege = ANY(policy.table_privileges);
            IF has_table_privilege(runtime_name, policy.relation_id, privilege) <> allowed
                OR has_table_privilege(runtime_name, policy.relation_id, privilege || ' WITH GRANT OPTION')
            THEN
                RAISE EXCEPTION 'Unexpected effective % privilege on %', privilege, policy.relation_id;
            END IF;
        END LOOP;

        FOR column_name IN
            SELECT attname FROM pg_attribute WHERE attrelid = policy.relation_id AND attnum > 0 AND NOT attisdropped
        LOOP
            FOREACH privilege IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'REFERENCES'] LOOP
                allowed := (NOT revoke_inventory OR NOT policy.inventory_delta) AND (
                    privilege = ANY(policy.table_privileges)
                    OR COALESCE((policy.column_privileges -> privilege) ? column_name, false)
                );
                IF has_column_privilege(runtime_name, policy.relation_id, column_name, privilege) <> allowed
                    OR has_column_privilege(runtime_name, policy.relation_id, column_name, privilege || ' WITH GRANT OPTION')
                THEN
                    RAISE EXCEPTION 'Unexpected effective % privilege on %.%', privilege, policy.relation_id, column_name;
                END IF;
            END LOOP;
        END LOOP;
    END LOOP;
END
$inventory_grants$;

COMMIT;
