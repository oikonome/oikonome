-- A Coinbase row stored Coinbase's own sign: + for value arriving in the
-- wallet. The ledger's sign is the other way round (+ is money out), so a
-- withdrawal leaving Coinbase listed as money arriving, a deposit as money
-- leaving, and a sale's legs read as the same sum arriving twice.
--
-- Every door now takes a Coinbase row's amount from its own record, the
-- native_amount it stores in raw, negated (sync.coinbase_push.
-- ledger_amount), and its transfer label from that sign. This rewrites the
-- rows already stored by the same rule: a row whose id is coinbase:…,
-- whose raw native_amount is a plain decimal (the only shape the rule
-- reads), and whose amount is not already that value negated. The label is
-- rewritten only when it is TRANSFER_IN or TRANSFER_OUT; category_override
-- is a person's word and is never touched. A restore applies the same rule
-- to an archived row (restore.upgrade_archived_txn).
--
-- The amount is read only from a decimal of at most 13 integer and 12
-- fraction digits, within the 1e12 the Python doors accept (engine.compat.
-- MAX_AMOUNT); anything else is left as it was, as ledger_amount leaves it.
-- A row's raw is the household's own data, so an unbounded digit string
-- reaching the cast would abort the whole migration. The cast sits inside
-- a CASE because the planner may evaluate a predicate on the subquery's
-- column before the filter that guards it (and need not
-- evaluate an AND's arms in order).
--
-- Bounded and idempotent: a written row no longer matches.

UPDATE transactions t
   SET amount = x.ledger,
       category_primary = CASE
           WHEN t.category_primary IN ('TRANSFER_IN', 'TRANSFER_OUT')
           THEN CASE WHEN x.ledger < 0 THEN 'TRANSFER_IN'
                     ELSE 'TRANSFER_OUT' END
           ELSE t.category_primary END
  FROM (SELECT id, tenant_id,
               CASE WHEN n.amt ~ '^-?[0-9]{1,13}(\.[0-9]{1,12})?$'
                    THEN CASE WHEN abs(n.amt::numeric) <= 1e12
                              THEN -(n.amt::float8) END
               END AS ledger
          FROM transactions,
               LATERAL (SELECT raw->'native_amount'->>'amount' AS amt) n
         WHERE id LIKE 'coinbase:%'
           AND jsonb_typeof(raw->'native_amount') = 'object'
           AND jsonb_typeof(raw->'native_amount'->'amount')
               IN ('string', 'number')
       ) x
 WHERE t.id = x.id AND t.tenant_id = x.tenant_id
   AND x.ledger IS NOT NULL
   AND t.amount <> x.ledger;
