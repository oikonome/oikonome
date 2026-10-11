-- How far back the history an adopted account brought with it reaches.
--
-- The reconnect guard drops a backfilled row on its date alone when it is
-- well before the cutoff, on the grounds that the restore already holds
-- it. That only holds inside the span the restore actually covers: a
-- connection linked a few months before the backup carries a few months of
-- history, while the fresh link replays up to two years, and a row older
-- than anything the account held is a charge the ledger has no copy of.
-- Dropping it loses it for good, because the cursor moves past it.
--
-- The guard reads this column to bound the date-only drop from below.
-- Adoptions still open get it from the oldest live row on or before their
-- cutoff, which is the restored history: the guard has never inserted a
-- row older than the cutoff minus its deep window, so nothing below that
-- edge can be the backfill's own.
ALTER TABLE account_adoptions ADD COLUMN IF NOT EXISTS history_from DATE;

UPDATE account_adoptions a
   SET history_from = (SELECT min(t.date) FROM transactions t
                        WHERE t.tenant_id = a.tenant_id
                          AND t.account_id = a.account_id
                          AND t.removed = 0
                          AND t.date <= a.cutoff_date)
 WHERE a.history_from IS NULL
   AND a.reconciled_at IS NULL
   AND a.cutoff_date IS NOT NULL;
