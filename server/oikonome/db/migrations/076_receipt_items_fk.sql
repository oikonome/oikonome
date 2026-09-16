-- Give receipt_items its own cascade off receipts(tenant_id, id).
--
-- Every transaction-delete path — purge_account_data, the coinbase/plan-CSV
-- full-resync deletes, import-batch rollback — cascades the receipts row
-- away; without this FK its receipt_items would be stranded: unreachable
-- (every reader INNER JOINs receipts) and undeletable through the app.
--
-- Strays are deleted first — the constraint cannot be created over them, and
-- a failed ALTER rolls back the whole file, never stamps schema_migrations,
-- and re-fails on every boot.

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
