-- Deduction-ready classification of business transactions.
-- The three-bucket split plain expense tracking gets wrong:
--   organizational — the cost of CREATING the entity (§248: filing fees, legal)
--   startup_195    — other pre-operating start-up costs (§195)
--   operating      — ordinary post-open operating expenses (Schedule C lines)
-- The entity's business_start_date is the hinge (costs before it are org/§195).
-- sched_c_line is the optional Schedule C line label for operating expenses.
-- Sparse: only classified business transactions get a row. (Mirrored in schema.sql.)

CREATE TABLE IF NOT EXISTS business_txn_class (
    tenant_id   UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    txn_id      TEXT NOT NULL,
    bucket      TEXT NOT NULL,                   -- organizational | startup_195 | operating
    sched_c_line TEXT,                           -- e.g. 'Advertising', 'Supplies'
    note        TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, txn_id)
);

ALTER TABLE business_txn_class ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON business_txn_class;
CREATE POLICY tenant_isolation ON business_txn_class
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
