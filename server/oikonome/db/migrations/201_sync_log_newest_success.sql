-- The header's "synced Xm ago" chip, and the stale-sync alert, ask for the
-- household's newest successful sync of anything at all — including the
-- log's bookkeeping rows (an import undone, an account purged) and items
-- the connections list does not show. The per-item index cannot answer
-- that without reading every successful row the household ever logged,
-- and the log is kept forever. Ordered by time alone, this one answers it
-- from the first leaf entry.
CREATE INDEX IF NOT EXISTS idx_sync_log_last_ok
    ON sync_log (tenant_id, ran_at DESC)
    WHERE error IS NULL;
