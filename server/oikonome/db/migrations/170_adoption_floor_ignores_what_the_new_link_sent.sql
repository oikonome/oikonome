-- An open adoption's history floor counts only the rows the restore
-- brought back, never rows the new bank link has delivered since.
--
-- Migration 167 recomputed the floor of every open adoption from the
-- account's oldest feed row on or before the cutoff. By then the new link
-- may already have synced: the reconnect guard inserts the rows near the
-- cutoff that match nothing the restore holds, and keeps rows older than
-- the floor, all on the same account. Counted as restored history, those
-- moved the floor. An account the restore held only as file rows got a
-- floor a month before the cutoff instead of at the start of the file,
-- so the deep backfill was then inserted beside the file rows that
-- already hold it; and rows the guard had kept from below the floor
-- lowered it, so later rows above them were dropped on their date alone.
--
-- A Plaid row's raw document names the account it was delivered for. A
-- restored row was delivered for the account the restore brought back,
-- under its old id; a row the new link sent names the adopting account
-- itself, and is left out here. Otherwise this is migration 167's rule:
-- the oldest restored feed row, or the oldest file row when the account
-- holds nothing else. Keep the prefixes in step with
-- sync/base.FILE_IMPORT_ID_PREFIXES; a test holds the two lists equal.
-- Closed adoptions are left as they are. Re-runnable.

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
           AND (t.raw IS NULL
                OR t.raw->>'account_id' IS DISTINCT FROM a2.account_id)
         WHERE a2.reconciled_at IS NULL
           AND a2.cutoff_date IS NOT NULL
         GROUP BY a2.tenant_id, a2.account_id, a2.adopted_at) f
 WHERE a.tenant_id = f.tenant_id
   AND a.account_id = f.account_id
   AND a.adopted_at = f.adopted_at
   AND a.history_from IS DISTINCT FROM f.lo;
