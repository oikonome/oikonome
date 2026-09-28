-- multi-source accounts. Rows in one group_id are the SAME
-- real-world account fed by different sources; home_rank is the user's
-- preference order (0 = favorite). The EFFECTIVE primary is computed at
-- read time (lowest home_rank whose source is healthy), so failover is
-- automatic and stateless; failover_alerted edge-guards the email.
-- (Mirrored in schema.sql for fresh installs.)

CREATE TABLE IF NOT EXISTS account_links (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    group_id   UUID NOT NULL,
    account_id TEXT NOT NULL,
    home_rank  INTEGER NOT NULL DEFAULT 0,
    failover_alerted TIMESTAMPTZ,      -- set on the group's rank-0 row
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_account_links_group
    ON account_links(tenant_id, group_id);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['account_links']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    END LOOP;
END $$;
