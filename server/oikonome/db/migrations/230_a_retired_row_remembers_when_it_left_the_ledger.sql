-- A retired transaction now remembers WHEN it left the ledger:
-- transactions.removed_at, NULL while the row is live. A receipt or a pair
-- left on a retired row waits a reconcile window before it is let go, and
-- that wait has to be counted from the retirement; the charge's own date
-- and the day the receipt was attached both come before it, so an old
-- charge retired today was treated as long gone.
--
-- The column, and the trigger that writes it on every path that retires
-- a row or lands one already retired (transactions_stamp_removed, BEFORE
-- INSERT OR UPDATE), are in schema.sql,
-- which migrate applies before any migration and under its lock-wait
-- guard: an ADD COLUMN here would take ACCESS EXCLUSIVE on the ledger for
-- the whole of this file, without that guard. A build that predates this
-- one keeps working beside them: it never names the column, and every
-- retirement it writes, an UPDATE of `removed` or an insert of a retired
-- row, is stamped by the trigger. See specs/receipts.md, "When a row left
-- the ledger".
--
-- This file stamps the rows already retired. The time is the feed's own
-- withdrawal stamp where it reads as a time, else the moment a person
-- or the twin dedup decided it, else now: a row whose retirement time is
-- unknown waits one fresh window, so nothing is let go early. A live row
-- and a row already stamped are not written, so a second run changes
-- nothing. A restore applies the same rule to an archived row that has no
-- removed_at of its own (landers._txn_values).

UPDATE transactions t
   SET removed_at = COALESCE(
           CASE WHEN pg_input_is_valid(t.raw->>'feed_removed_at',
                                       'timestamptz')
                THEN (t.raw->>'feed_removed_at')::timestamptz END,
           t.retired_at,
           now())
 WHERE t.removed <> 0
   AND t.removed_at IS NULL;
