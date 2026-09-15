-- Receipts were the one transaction-child table migration 067 missed.
--
-- 067 gave manual_categories, reimbursements, reimburse_flags, business_flags,
-- transaction_notes, business_txn_class, amazon_matches and equity_movement a
-- real FK to transactions, precisely so a transaction delete could not leave
-- orphans that a later id collision would silently re-attach. `receipts.txn_id`
-- was left as a bare TEXT column with only an index.
--
-- That gap is worse than the ones 067 closed, for two reasons:
--
--   * every delete path — purge_account_data (Accounts → remove), the coinbase
--     and plan-CSV full-resync deletes, and batch rollback — issues a bare
--     DELETE FROM transactions with no receipts cleanup, so the orphan is the
--     normal outcome, not an edge case;
--   * collector transaction ids are CONTENT HASHES (plan CSV: sha1 of
--     plan|date|symbol|type|amount|shares|price|occ), so a purge followed by a
--     resync reproduces the SAME id — and the orphaned receipt, which still
--     holds the original image bytes, re-attaches to a transaction the user
--     never uploaded it to. Someone else's receipt image appearing on a row is
--     a privacy failure, not a tidiness one.
--
-- receipt_items already cascades off receipts(tenant_id, id), so it inherits
-- this fix once receipts itself is constrained.
--
-- Orphans are deleted first — the constraint cannot be created over them, and
-- a failed CREATE rolls back the whole file, never stamps schema_migrations,
-- and re-fails on every boot (067's own unique index taught this).

DELETE FROM receipts r
 WHERE NOT EXISTS (
     SELECT 1 FROM transactions t
      WHERE t.tenant_id = r.tenant_id
        AND t.id        = r.txn_id);

ALTER TABLE receipts
    DROP CONSTRAINT IF EXISTS receipts_txn_fk;
ALTER TABLE receipts
    ADD CONSTRAINT receipts_txn_fk
    FOREIGN KEY (tenant_id, txn_id)
    REFERENCES transactions (tenant_id, id)
    ON DELETE CASCADE;
