-- One personally-paid business cost is explained once: capitalized (a
-- contribution) OR marked reimbursable. Migration 159 built the index that
-- holds the database to that only where no transaction already broke it,
-- so an upgraded install could be left without it while a fresh one has
-- it, and the two then answer a restore differently.
--
-- This brings every install to the same place. Where a transaction carries
-- more than one fronted movement, the earliest recorded is kept (the
-- engine's own rule: the first pick stands and a second is refused) and
-- the rest are removed; a restore applies the same choice to an archive.
-- Then the index is built. Re-runnable: a clean table deletes nothing and
-- the index is IF NOT EXISTS.

DELETE FROM equity_movement m
 USING (SELECT tenant_id, id,
               row_number() OVER (PARTITION BY tenant_id, txn_id
                                  ORDER BY created_at, id) AS n
          FROM equity_movement
         WHERE txn_id IS NOT NULL
           AND kind IN ('contribution', 'reimbursement')) d
 WHERE m.tenant_id = d.tenant_id AND m.id = d.id AND d.n > 1;

CREATE UNIQUE INDEX IF NOT EXISTS equity_movement_one_fronted_per_txn
    ON equity_movement (tenant_id, txn_id)
 WHERE txn_id IS NOT NULL
   AND kind IN ('contribution', 'reimbursement');
