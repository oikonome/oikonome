-- Import staging lives in Postgres, not in per-process dicts. An
-- in-memory store is invisible to the other workers (a bulk run 404s
-- when a different process answers), evicts silently at a fixed count,
-- and has no tenant binding of its own. One row per staged file, under
-- the standard RLS policy — tenant binding is the database's problem.
-- Eviction is explicit: a TTL sweep plus a per-tenant byte budget
-- enforced app-side. (Mirrored in schema.sql.)

CREATE TABLE IF NOT EXISTS import_staging (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    token      TEXT NOT NULL,
    idx        INTEGER NOT NULL DEFAULT 0,
    kind       TEXT NOT NULL,                    -- bulk | csv | taxdoc
    filename   TEXT,
    data       BYTEA,                            -- raw file bytes (may be empty)
    meta       JSONB,                            -- per-stage extras (mapping info, parsed rows)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, token, idx)
);

ALTER TABLE import_staging ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON import_staging;
CREATE POLICY tenant_isolation ON import_staging
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
