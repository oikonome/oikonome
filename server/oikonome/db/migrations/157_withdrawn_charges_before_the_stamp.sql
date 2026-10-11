-- A posted charge the bank feed takes back is stamped as the feed's own
-- withdrawal (sync/base.py mark_removed writes raw.feed_removed_at), and
-- the bills readers (engine/budget.py _spend_rows withdrawn=True) count
-- only stamped rows, because merges, file restates, full-replace pushes
-- and a person's retirement also set removed=1 and none of those is the
-- bank taking a charge back.
--
-- Rows the removal delta retired before the stamp existed carry none, so
-- their bill reads "overdue, never posted" on an upgraded instance while
-- a fresh one reads "withdrawn". This stamps them once. A row qualifies
-- only when every other way of being retired is ruled out:
--
--   * posted: a retired pending row is the ordinary hold-to-posted swap
--   * retired_at IS NULL: no person or dedup decided it
--   * an aggregator id (no "<source>:" prefix): file imports, exchange
--     and plan pushes and the full-pull connectors all prefix theirs, and
--     restate or retire their own rows
--   * on a connection whose aggregator sends removal deltas (plaid), the
--     only caller of mark_removed
--   * no live row of the same account, date and amount beside it: a
--     merge's retired twin always has one, and a withdrawn charge that
--     lives on under another id is already paid by that live row, so
--     stamping it would add nothing
--
-- Idempotent: a stamped row is skipped, and only rows that change are
-- written. The stamp's value is the time it was written, as mark_removed
-- writes it; no reader uses the value.

UPDATE transactions t
   SET raw = COALESCE(t.raw, '{}'::jsonb)
             || jsonb_build_object('feed_removed_at', now())
 WHERE t.removed = 1
   AND t.pending = 0
   AND t.retired_at IS NULL
   AND (t.raw IS NULL OR jsonb_typeof(t.raw) = 'object')
   AND NOT (COALESCE(t.raw, '{}'::jsonb) ? 'feed_removed_at')
   AND position(':' IN t.id) = 0
   AND EXISTS (SELECT 1
                 FROM accounts a
                 JOIN items i ON i.tenant_id = a.tenant_id
                             AND i.id = a.item_id
                WHERE a.tenant_id = t.tenant_id
                  AND a.id = t.account_id
                  AND i.aggregator = 'plaid')
   AND NOT EXISTS (SELECT 1
                     FROM transactions l
                    WHERE l.tenant_id = t.tenant_id
                      AND l.account_id = t.account_id
                      AND l.removed = 0
                      AND l.date = t.date
                      AND l.amount = t.amount);
