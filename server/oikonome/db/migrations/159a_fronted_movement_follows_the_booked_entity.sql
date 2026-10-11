-- Where a transaction carries more than one fronted movement (a
-- contribution and a reimbursement written before the one-per-transaction
-- rule), keep the one that agrees with where the cost is booked now.
--
-- Before that rule, a second pick naming another business moved the
-- transaction to it while the first business kept its contribution. The
-- transaction's entity (its own assignment, else its account's) is the
-- owner's latest decision, so the movement for that entity is the real
-- one; the earliest recorded wins only when neither matches. Keeping the
-- earliest regardless would credit one business's capital for a cost
-- another business's books carry.
--
-- Nothing is removed silently: the kept movement's note says what was
-- removed (kind, amount, business, date recorded), and a notice gives the
-- count. Named to run before the migration that removes all but the
-- earliest, so on an install that has not taken that one yet it finds
-- nothing left to choose. Re-runnable: a clean table changes nothing.

DO $$
DECLARE
    dropped integer;
BEGIN
    DROP TABLE IF EXISTS pg_temp.fronted_choice;
    CREATE TEMP TABLE fronted_choice AS
    SELECT m.tenant_id, m.id, m.txn_id,
           row_number() OVER (
               PARTITION BY m.tenant_id, m.txn_id
               ORDER BY (m.entity_id IS NOT DISTINCT FROM
                         COALESCE(t.entity_id, a.entity_id)) DESC,
                        m.created_at, m.id) AS n
      FROM equity_movement m
      LEFT JOIN transactions t
             ON t.tenant_id = m.tenant_id AND t.id = m.txn_id
      LEFT JOIN accounts a
             ON a.tenant_id = t.tenant_id AND a.id = t.account_id
     WHERE m.txn_id IS NOT NULL
       AND m.kind IN ('contribution', 'reimbursement')
       AND (m.tenant_id, m.txn_id) IN (
           SELECT tenant_id, txn_id FROM equity_movement
            WHERE txn_id IS NOT NULL
              AND kind IN ('contribution', 'reimbursement')
            GROUP BY tenant_id, txn_id HAVING count(*) > 1);

    UPDATE equity_movement k
       SET note = concat_ws(E'\n', NULLIF(k.note, ''), d.said)
      FROM (SELECT c.tenant_id, c.txn_id,
                   'An upgrade removed another movement on this charge '
                   || '(one per charge): '
                   || string_agg(
                          m.kind || ' of ' || to_char(m.amount, 'FM999999999990.00')
                          || ' for ' || COALESCE(e.name, 'another business')
                          || ', recorded ' || to_char(m.created_at, 'YYYY-MM-DD'),
                          '; ' ORDER BY m.created_at, m.id) AS said
              FROM fronted_choice c
              JOIN equity_movement m
                ON m.tenant_id = c.tenant_id AND m.id = c.id
              LEFT JOIN business_entity e
                ON e.tenant_id = m.tenant_id AND e.id = m.entity_id
             WHERE c.n > 1
             GROUP BY c.tenant_id, c.txn_id) d,
           fronted_choice keep
     WHERE keep.n = 1
       AND keep.tenant_id = d.tenant_id AND keep.txn_id = d.txn_id
       AND k.tenant_id = keep.tenant_id AND k.id = keep.id;

    DELETE FROM equity_movement m
     USING fronted_choice c
     WHERE m.tenant_id = c.tenant_id AND m.id = c.id AND c.n > 1;
    GET DIAGNOSTICS dropped = ROW_COUNT;
    DROP TABLE fronted_choice;
    IF dropped > 0 THEN
        RAISE NOTICE 'equity_movement: removed % second fronted movement(s); '
                     'each kept movement''s note records what it replaced',
                     dropped;
    END IF;
END $$;
