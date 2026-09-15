-- Costco receipts, pushed by a host-side collector the way Amazon orders
-- are: one row per warehouse, gas-station or costco.com receipt with its
-- line items (item number, the register's abbreviated description,
-- department, amount) and a per-item category, plus the receipt's
-- dominant category and a one-line breakdown. The engine matches each
-- receipt to the card charge that paid for it by amount and date, so a
-- $300 warehouse run reads "groceries $180 · household $60 · apparel $60"
-- instead of one GENERAL_MERCHANDISE row. Same sign convention as
-- amazon_orders: negative = purchase, positive = refund.
CREATE TABLE IF NOT EXISTS costco_receipts (
    tenant_id       UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    dedup_key       TEXT NOT NULL,
    account         TEXT NOT NULL DEFAULT 'main',
    date            DATE NOT NULL,
    amount          DOUBLE PRECISION NOT NULL,
    receipt_type    TEXT NOT NULL DEFAULT 'warehouse',  -- warehouse | gas | online
    warehouse       TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT '',            -- dominant by amount
    category_source TEXT NOT NULL DEFAULT 'rules',
    summary         TEXT NOT NULL DEFAULT '',            -- "groceries $180 · household $60"
    is_refund       INTEGER NOT NULL DEFAULT 0,
    payment_method  TEXT NOT NULL DEFAULT '',
    items_json      JSONB NOT NULL DEFAULT '[]'::jsonb,
    inserted_at     TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_costco_receipts_tenant_date
    ON costco_receipts(tenant_id, date);

CREATE TABLE IF NOT EXISTS costco_matches (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    transaction_id TEXT NOT NULL,
    dedup_key      TEXT,
    matched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, transaction_id),
    CONSTRAINT costco_matches_txn_fk FOREIGN KEY (tenant_id, transaction_id)
        REFERENCES transactions (tenant_id, id) ON DELETE CASCADE
);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['costco_receipts','costco_matches']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = (SELECT NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)) '
            'WITH CHECK (tenant_id = (SELECT NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid))', t);
    END LOOP;
END $$;
