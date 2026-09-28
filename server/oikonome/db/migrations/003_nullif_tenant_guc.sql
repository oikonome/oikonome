-- After a pooled connection RESET, app.tenant_id is '' (empty), and the
-- policies/defaults cast '' :: uuid → ERROR on every query instead of
-- returning zero rows. NULLIF makes empty == unset everywhere.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'tenant_settings','items','accounts','transactions','liabilities',
        -- 040 renamed recurring→bills; fresh installs run this AFTER
        -- schema.sql already created the new names, so this list must
        -- use them (already-migrated installs never re-run this file)
        'sync_log','bills','bill_proposals','reimbursements',
        'reimburse_flags','manual_categories','alerts_log','job_runs',
        'tenant_keys','import_batches']
    LOOP
        EXECUTE format(
            'ALTER TABLE %I ALTER COLUMN tenant_id SET DEFAULT '
            'NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    END LOOP;
END $$;
