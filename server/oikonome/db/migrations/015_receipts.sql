-- receipts on transactions. Images live IN Postgres (5 MB cap,
-- enforced app-side) so every backup path carries them; line items are
-- taggable (business/personal/custom) and feed the expense report.
-- Budget math never reads these tables. (Mirrored in schema.sql.)

CREATE TABLE IF NOT EXISTS receipts (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         UUID NOT NULL DEFAULT gen_random_uuid(),
    txn_id     TEXT NOT NULL,
    image      BYTEA NOT NULL,
    mime       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'uploaded', -- uploaded | parsed | failed
    parsed     JSONB,                            -- merchant/date/total/tax/tip
    error      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    parsed_at  TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS idx_receipts_txn ON receipts(tenant_id, txn_id);

CREATE TABLE IF NOT EXISTS receipt_items (
    tenant_id   UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    receipt_id  UUID NOT NULL,
    line        INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    qty         DOUBLE PRECISION,
    amount      DOUBLE PRECISION,
    tag         TEXT NOT NULL DEFAULT '',        -- '' | business | personal | custom
    PRIMARY KEY (tenant_id, receipt_id, line)
);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['receipts','receipt_items']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    END LOOP;
END $$;
