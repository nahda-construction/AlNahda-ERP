\set ON_ERROR_STOP on
BEGIN;

SELECT set_config('erp.runtime_role', :'runtime_role', true);

DO $$
DECLARE
    runtime_name text := current_setting('erp.runtime_role');
    runtime_role pg_roles%ROWTYPE;
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
            JOIN pg_namespace AS schema ON schema.oid = relation.relnamespace
            WHERE schema.nspname = 'public' AND relation.relowner = runtime_role.oid
        )
    THEN
        RAISE EXCEPTION 'The ERP runtime role must be an independent, restricted login';
    END IF;
END
$$;

SELECT current_database() AS database_name \gset
GRANT CONNECT ON DATABASE :"database_name" TO :"runtime_role";
GRANT USAGE ON SCHEMA public TO :"runtime_role";

REVOKE ALL ON TABLE
    public.auth_user, public.auth_group, public.auth_permission,
    public.auth_user_groups, public.auth_user_user_permissions, public.auth_group_permissions,
    public.django_content_type, public.django_session,
    public.projects_project, public.projects_worknode,
    public.access_control_projectaccess, public.access_control_projectaccessevent,
    public.test_transactions_testtransaction, public.test_transactions_testtransactionline,
    public.test_transactions_attachment, public.test_transactions_auditevent,
    public.test_transactions_idempotencyrecord
FROM :"runtime_role";

GRANT SELECT ON TABLE
    public.auth_user, public.auth_group, public.auth_permission,
    public.auth_user_groups, public.auth_user_user_permissions, public.auth_group_permissions,
    public.django_content_type, public.projects_project, public.projects_worknode
TO :"runtime_role";
-- Django records login time and may upgrade an older password hash on successful login.
GRANT UPDATE (last_login, password) ON public.auth_user TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.django_session TO :"runtime_role";

GRANT SELECT, INSERT ON TABLE
    public.access_control_projectaccess, public.access_control_projectaccessevent,
    public.test_transactions_testtransaction, public.test_transactions_testtransactionline,
    public.test_transactions_attachment, public.test_transactions_auditevent,
    public.test_transactions_idempotencyrecord
TO :"runtime_role";
GRANT UPDATE (active, granted_by_id, granted_at, revoked_at)
ON public.access_control_projectaccess TO :"runtime_role";
GRANT UPDATE (status, approved_by_id, approved_at)
ON public.test_transactions_testtransaction TO :"runtime_role";
GRANT UPDATE (status, test_transaction_id, response_status, response_body)
ON public.test_transactions_idempotencyrecord TO :"runtime_role";

REVOKE ALL ON SEQUENCE
    public.access_control_projectaccess_id_seq, public.access_control_projectaccessevent_id_seq,
    public.test_transactions_testtransactionline_id_seq, public.test_transactions_auditevent_id_seq,
    public.test_transactions_idempotencyrecord_id_seq
FROM :"runtime_role";
GRANT USAGE ON SEQUENCE
    public.access_control_projectaccess_id_seq, public.access_control_projectaccessevent_id_seq,
    public.test_transactions_testtransactionline_id_seq, public.test_transactions_auditevent_id_seq,
    public.test_transactions_idempotencyrecord_id_seq
TO :"runtime_role";

DO $$
DECLARE
    runtime_name text := current_setting('erp.runtime_role');
    audit_table text;
    idempotency_table regclass := 'public.test_transactions_idempotencyrecord'::regclass;
BEGIN
    FOREACH audit_table IN ARRAY ARRAY[
        'public.access_control_projectaccessevent',
        'public.test_transactions_auditevent'
    ] LOOP
        IF has_table_privilege(runtime_name, audit_table, 'UPDATE,DELETE,TRUNCATE')
            OR has_any_column_privilege(runtime_name, audit_table, 'UPDATE')
        THEN
            RAISE EXCEPTION 'Unexpected audit write privileges on %', audit_table;
        END IF;
    END LOOP;
    IF has_table_privilege(runtime_name, idempotency_table, 'UPDATE,DELETE,TRUNCATE')
        OR EXISTS (
            SELECT 1
            FROM pg_attribute
            WHERE attrelid = idempotency_table
                AND attnum > 0 AND NOT attisdropped
                AND attname NOT IN ('status', 'test_transaction_id', 'response_status', 'response_body')
                AND has_column_privilege(runtime_name, attrelid, attnum, 'UPDATE')
        )
    THEN
        RAISE EXCEPTION 'Unexpected write privileges on the idempotency record';
    END IF;
END
$$;

COMMIT;
