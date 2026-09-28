-- The integrations summary and the connections list ask, per item, for
-- its newest successful sync. Without an index that is a scan of the
-- whole log per item; a partial index over (tenant, item, ran_at desc)
-- on the rows without an error answers it from one leaf page.
CREATE INDEX IF NOT EXISTS idx_sync_log_item_last_ok
    ON sync_log (tenant_id, item_id, ran_at DESC)
    WHERE error IS NULL;
