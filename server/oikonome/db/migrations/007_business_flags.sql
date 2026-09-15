-- Business-expense flagging: mirror of reimburse_flags — a one-click tag,
-- deliberately separate from categories (a Schedule C marker, not a spend
-- reclassification; verdict math unchanged).
CREATE TABLE IF NOT EXISTS business_flags (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    txn_id     TEXT NOT NULL,
    flagged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, txn_id)
);
ALTER TABLE business_flags ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON business_flags;
CREATE POLICY tenant_isolation ON business_flags
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
