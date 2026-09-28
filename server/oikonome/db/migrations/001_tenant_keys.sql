-- per-tenant envelope keys: each tenant's data key, wrapped by the master
-- key (env OIKONOME_MASTER_KEY). RLS like every domain table.
CREATE TABLE IF NOT EXISTS tenant_keys (
    tenant_id   UUID NOT NULL DEFAULT current_setting('app.tenant_id', true)::uuid,
    wrapped_key TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id)
);
ALTER TABLE tenant_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenant_keys;
CREATE POLICY tenant_isolation ON tenant_keys
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
