-- Free-text notes on a transaction. A per-transaction user annotation,
-- like business_flags / reimburse_flags — a separate table (sparse, RLS,
-- never touched by sync) rather than a column on the synced transactions
-- table. Shown everywhere the ledger is (Transactions, Today, merchant
-- history, business books). (Mirrored in schema.sql.)

CREATE TABLE IF NOT EXISTS transaction_notes (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    txn_id     TEXT NOT NULL,
    note       TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, txn_id)
);

ALTER TABLE transaction_notes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON transaction_notes;
CREATE POLICY tenant_isolation ON transaction_notes
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
