-- One ledger row, several categories: a hand split of a single charge.
--
-- A warehouse-club charge is groceries AND a lawn chair; a payment to a housemate is
-- rent AND the power bill. Until now a row carried exactly one effective
-- category, and the only finer grain was a receipt's line items, which
-- explain a charge but never re-bucket it. This table holds the parts a
-- person wrote: category + amount, in line order, summing to the row's
-- amount to the cent (the server refuses anything else).
--
-- Readers that roll spend up BY CATEGORY LEFT JOIN this table and read
-- COALESCE(sp.category, <effective category>) / COALESCE(sp.amount,
-- t.amount): a split row fans out into its parts, an unsplit row is its own
-- single part, and every rollup that joins agrees with every other one.
-- Readers that list ledger rows one per transaction do not join; the row
-- carries its parts as a JSON column instead. Parts are spend categories
-- only — a transfer or income part would make the whole-row spend test
-- (SPEND_WHERE / SPEND_ONLY_SQL, which never join) disagree with the
-- category rollups, so it is refused at the door rather than reconciled in
-- every reader.
--
-- The row's own category_override / category_primary stay as they were:
-- the split is an overlay that wins while it exists and vanishes without a
-- trace when removed, so the sync, the bill pass and the store matchers
-- need no knowledge of it.
CREATE TABLE IF NOT EXISTS transaction_splits (
    tenant_id UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    txn_id    TEXT NOT NULL,
    line      INTEGER NOT NULL,              -- 1-based, display order
    category  TEXT NOT NULL,                 -- stored key (FOOD_AND_DRINK / a custom name)
    amount    DOUBLE PRECISION NOT NULL,     -- same sign as the row; parts sum to it
    PRIMARY KEY (tenant_id, txn_id, line),
    CONSTRAINT transaction_splits_txn_fk FOREIGN KEY (tenant_id, txn_id)
        REFERENCES transactions (tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE transaction_splits ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON transaction_splits;
CREATE POLICY tenant_isolation ON transaction_splits
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));
