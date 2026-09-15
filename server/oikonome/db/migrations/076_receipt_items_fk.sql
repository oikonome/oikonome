-- The cascade migration 071 said it inherited does not exist.
--
-- 071 constrained receipts → transactions ON DELETE CASCADE and skipped
-- receipt_items with "receipt_items already cascades off receipts(tenant_id,
-- id), so it inherits this fix once receipts itself is constrained." No such
-- FK was ever declared: migration 015 created receipt_items bare, schema.sql
-- mirrors it bare, and no later migration added one. So since 071, every
-- transaction-delete path it itself enumerates — purge_account_data, the
-- coinbase/plan-CSV full-resync deletes, import-batch rollback — cascades the
-- receipts row away and permanently strands its receipt_items: unreachable
-- (every reader INNER JOINs receipts) and undeletable through the app.
--
-- Strays are deleted first — the constraint cannot be created over them, and
-- a failed ALTER rolls back the whole file, never stamps schema_migrations,
-- and re-fails on every boot (071's own lesson, inherited for real this time).

DELETE FROM receipt_items ri
 WHERE NOT EXISTS (
     SELECT 1 FROM receipts r
      WHERE r.tenant_id = ri.tenant_id
        AND r.id        = ri.receipt_id);

ALTER TABLE receipt_items
    DROP CONSTRAINT IF EXISTS receipt_items_receipt_fk;
ALTER TABLE receipt_items
    ADD CONSTRAINT receipt_items_receipt_fk
    FOREIGN KEY (tenant_id, receipt_id)
    REFERENCES receipts (tenant_id, id)
    ON DELETE CASCADE;
