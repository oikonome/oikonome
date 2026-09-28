-- A receipt can be snapped before its charge reaches the ledger. Until the
-- matcher (engine/receipt_match.py) finds the transaction it belongs to, the
-- receipt has no txn_id — a WAITING receipt. The foreign key to transactions
-- stays: with a NULL txn_id the composite key is simply not checked, and the
-- moment a receipt is attached the key holds it exactly as before.
--
--   matched_at      when the receipt was paired with its transaction
--                   (NULL on a receipt attached from the transaction itself,
--                   and on a waiting one)
--   match_method    'auto' — the matcher paired it; 'manual' — a person did.
--                   NULL for a receipt attached from the transaction itself.
--   unmatched_txn_ids
--                   transactions a person took this receipt OFF. The matcher
--                   never offers it to them again: without the memory the
--                   next sync would pair it straight back.
--
-- The partial index is the matcher's first question on every sync — "is
-- anything waiting at all?" — which almost always answers no.

ALTER TABLE receipts ALTER COLUMN txn_id DROP NOT NULL;
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS matched_at TIMESTAMPTZ;
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS match_method TEXT;
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS unmatched_txn_ids TEXT[]
    NOT NULL DEFAULT '{}';

ALTER TABLE receipts DROP CONSTRAINT IF EXISTS receipts_match_method_known;
ALTER TABLE receipts ADD CONSTRAINT receipts_match_method_known
    CHECK (match_method IS NULL OR match_method IN ('auto', 'manual'));

CREATE INDEX IF NOT EXISTS idx_receipts_waiting
    ON receipts (tenant_id, created_at) WHERE txn_id IS NULL;
