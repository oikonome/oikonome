-- An adoption's history floor is where the bank feed's own restored rows
-- begin, not the oldest row of any source.
--
-- The floor bounds the reconnect guard's date-only drop from below. The
-- column's first backfill took the oldest live row on or before the
-- cutoff whatever made it, so an account that also held file-imported
-- history from years before the link got a floor at the start of the
-- file: the months between that file and the first feed row were then
-- dropped as "certainly held" when the restore holds no copy of them.
-- Adopting an account now takes the oldest feed row, and the oldest file
-- row only when the account holds nothing else; this brings the
-- adoptions still open to the same answer.
--
-- The file-import id prefixes are the ones the sync code names
-- (sync/base.FILE_IMPORT_ID_PREFIXES); a test holds the two lists equal.
-- Closed adoptions are left as they are: their guard has already run.
-- Re-runnable: a floor that already agrees is not touched.

UPDATE account_adoptions a
   SET history_from = f.lo
  FROM (SELECT a2.tenant_id, a2.account_id, a2.adopted_at,
               COALESCE(
                   min(t.date) FILTER (WHERE NOT (t.id LIKE ANY (ARRAY[
                       'pdf:%', 'csv:%', 'ofx:%', 'qif:%', 'mint:%',
                       'ynab:%', 'monarch:%', 'copilot:%', 'simplifi:%']))),
                   min(t.date)) AS lo
          FROM account_adoptions a2
          JOIN transactions t
            ON t.tenant_id = a2.tenant_id
           AND t.account_id = a2.account_id
           AND t.removed = 0
           AND t.date <= a2.cutoff_date
         WHERE a2.reconciled_at IS NULL
           AND a2.cutoff_date IS NOT NULL
         GROUP BY a2.tenant_id, a2.account_id, a2.adopted_at) f
 WHERE a.tenant_id = f.tenant_id
   AND a.account_id = f.account_id
   AND a.adopted_at = f.adopted_at
   AND a.history_from IS DISTINCT FROM f.lo;
