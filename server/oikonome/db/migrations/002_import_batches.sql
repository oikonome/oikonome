-- import batches: every file import is tagged and one-click reversible.
CREATE TABLE IF NOT EXISTS import_batches (
    tenant_id  UUID NOT NULL DEFAULT current_setting('app.tenant_id', true)::uuid,
    id         TEXT NOT NULL,
    source     TEXT NOT NULL,           -- csv | ofx | qif | mint | ynab
    account_id TEXT,
    filename   TEXT,
    row_count  INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);
ALTER TABLE import_batches ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON import_batches;
CREATE POLICY tenant_isolation ON import_batches
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
