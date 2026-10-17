-- The reconnect guard remembers, on the adoption itself, what each page of
-- a backfill has already accounted for.
--
-- A backfill arrives in pages and each page is matched on its own, so what
-- one page learned has to be written somewhere the next can read it: which
-- incoming ids it dropped as copies and which restored row each one used
-- up, which retired holds a settlement has claimed, and which holds are
-- still waiting for theirs. Kept in the rows' raw records it did not hold:
-- the bank's `modified` restates raw wholesale, and a copy dropped without
-- an id to carry left no row to write on at all — so the next page's
-- matcher offered the same restored row again, and a hold's settlement
-- went as a second copy of it.
--
-- {"absorbed": {incoming id: restored row id}, "settled": {hold id:
--  settlement id}, "awaiting": [hold id, ...]}; NULL is an adoption whose
-- guard has noted nothing yet.
ALTER TABLE account_adoptions ADD COLUMN IF NOT EXISTS guard_ledger JSONB;

-- A guard open while this lands keeps what its earlier pages wrote in raw
-- (the keys stay on the rows, unread, until the next adoption of the
-- account clears them).
UPDATE account_adoptions a
   SET guard_ledger = jsonb_build_object(
       'absorbed', COALESCE((
           SELECT jsonb_object_agg(t.id, t.id) FROM transactions t
            WHERE t.tenant_id = a.tenant_id AND t.account_id = a.account_id
              AND t.raw ? '_carried_from'), '{}'::jsonb),
       'settled', COALESCE((
           SELECT jsonb_object_agg(t.id, t.raw->>'_settled_by')
             FROM transactions t
            WHERE t.tenant_id = a.tenant_id AND t.account_id = a.account_id
              AND t.raw ? '_settled_by'), '{}'::jsonb),
       'awaiting', COALESCE((
           SELECT jsonb_agg(t.id) FROM transactions t
            WHERE t.tenant_id = a.tenant_id AND t.account_id = a.account_id
              AND t.raw ? '_awaits_settlement'), '[]'::jsonb))
 WHERE a.reconciled_at IS NULL AND a.guard_ledger IS NULL
   AND EXISTS (SELECT 1 FROM transactions t
                WHERE t.tenant_id = a.tenant_id
                  AND t.account_id = a.account_id
                  AND t.raw ?| ARRAY['_carried_from', '_settled_by',
                                     '_awaits_settlement']);
