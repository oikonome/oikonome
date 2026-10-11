-- One personally-paid business cost is explained once: capitalized (a
-- contribution) OR marked reimbursable, for one entity. The engine already
-- refuses a second pick under a row lock (engine/equity.py,
-- _front_expense); this index makes the database refuse it too. Draws,
-- distributions and movements with no transaction stay unconstrained.
--
-- Rows written before that rule may already break it, and which of two
-- movements is the real one is the owner's decision, not a migration's.
-- So the index is built only when no transaction carries two fronted
-- movements; otherwise nothing is changed, a notice says how many
-- transactions are affected, and the engine's check keeps new rows clean.
-- Re-runnable: IF NOT EXISTS, and the check reads before it writes.

DO $$
DECLARE
    clashing integer;
BEGIN
    SELECT count(*) INTO clashing FROM (
        SELECT 1 FROM equity_movement
         WHERE txn_id IS NOT NULL
           AND kind IN ('contribution', 'reimbursement')
         GROUP BY tenant_id, txn_id
        HAVING count(*) > 1) d;
    IF clashing = 0 THEN
        CREATE UNIQUE INDEX IF NOT EXISTS equity_movement_one_fronted_per_txn
            ON equity_movement (tenant_id, txn_id)
         WHERE txn_id IS NOT NULL
           AND kind IN ('contribution', 'reimbursement');
    ELSE
        RAISE NOTICE 'equity_movement: % transaction(s) carry more than one '
                     'fronted movement; one-per-transaction index not built '
                     '(data left unchanged)', clashing;
    END IF;
END $$;
