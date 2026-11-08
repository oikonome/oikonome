-- Every live door writes a number into transactions.amount: the upsert refuses
-- a row without one, the plan and wallet importers write 0, and the restore
-- keeps a blank-amount archive row at 0 with a marker in raw. Every ledger
-- reader turns the amount into cents, the duplicate matchers first, so one
-- blank row stored by an older restore fails every later import, the
-- reconnect guard, the bill history and the books that meet it. Such a row
-- can carry a person's note, split or receipt, so it is kept at 0 with the
-- same marker the restore writes, and the column refuses NULL from here on.
UPDATE transactions
   SET amount = 0,
       raw = COALESCE(raw, '{}'::jsonb) || '{"_amount_missing": true}'::jsonb
 WHERE amount IS NULL;
ALTER TABLE transactions ALTER COLUMN amount SET NOT NULL;
