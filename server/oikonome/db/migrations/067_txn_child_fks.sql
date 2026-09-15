-- Rows keyed on a transaction id get real foreign keys.
--
-- Eight tables hang off a transaction — the user's own category corrections,
-- reimbursement pairings, business flags, notes, Schedule C classifications,
-- Amazon matches. Without a foreign key, every path that deletes transactions
-- (account purge, erasure, restore) has to remember all eight, and a row it
-- misses points at nothing until a later transaction id collision silently
-- re-attaches someone's old note to a new row.
--
-- Two different behaviours on purpose:
--
--   CASCADE  — annotations that mean nothing without their transaction.
--   SET NULL — `equity_movement`, which is a real accounting entry the user
--              may have created deliberately; a contribution recorded against
--              a transaction should survive that transaction being purged,
--              minus the link. Deleting someone's books as a side effect of
--              removing an account would be a worse bug than the one this
--              migration fixes.
--
-- Existing orphans are removed first, or the constraint cannot be created.

DELETE FROM manual_categories c WHERE NOT EXISTS (
    SELECT 1 FROM transactions t
     WHERE t.tenant_id = c.tenant_id AND t.id = c.transaction_id);
DELETE FROM reimbursements r WHERE NOT EXISTS (
    SELECT 1 FROM transactions t
     WHERE t.tenant_id = r.tenant_id AND t.id = r.expense_id)
   OR NOT EXISTS (
    SELECT 1 FROM transactions t
     WHERE t.tenant_id = r.tenant_id AND t.id = r.reimburse_id);
DELETE FROM reimburse_flags f WHERE NOT EXISTS (
    SELECT 1 FROM transactions t
     WHERE t.tenant_id = f.tenant_id AND t.id = f.txn_id);
DELETE FROM business_flags b WHERE NOT EXISTS (
    SELECT 1 FROM transactions t
     WHERE t.tenant_id = b.tenant_id AND t.id = b.txn_id);
DELETE FROM transaction_notes n WHERE NOT EXISTS (
    SELECT 1 FROM transactions t
     WHERE t.tenant_id = n.tenant_id AND t.id = n.txn_id);
DELETE FROM business_txn_class c WHERE NOT EXISTS (
    SELECT 1 FROM transactions t
     WHERE t.tenant_id = c.tenant_id AND t.id = c.txn_id);
DELETE FROM amazon_matches m WHERE NOT EXISTS (
    SELECT 1 FROM transactions t
     WHERE t.tenant_id = m.tenant_id AND t.id = m.transaction_id);
-- equity_movement keeps its rows; only the dangling link is cleared
UPDATE equity_movement e SET txn_id = NULL
 WHERE txn_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM transactions t
     WHERE t.tenant_id = e.tenant_id AND t.id = e.txn_id);

ALTER TABLE manual_categories
  DROP CONSTRAINT IF EXISTS manual_categories_txn_fk;
ALTER TABLE manual_categories
  ADD CONSTRAINT manual_categories_txn_fk
  FOREIGN KEY (tenant_id, transaction_id)
  REFERENCES transactions (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE reimbursements
  DROP CONSTRAINT IF EXISTS reimbursements_expense_fk;
ALTER TABLE reimbursements
  ADD CONSTRAINT reimbursements_expense_fk
  FOREIGN KEY (tenant_id, expense_id)
  REFERENCES transactions (tenant_id, id) ON DELETE CASCADE;
ALTER TABLE reimbursements
  DROP CONSTRAINT IF EXISTS reimbursements_reimburse_fk;
ALTER TABLE reimbursements
  ADD CONSTRAINT reimbursements_reimburse_fk
  FOREIGN KEY (tenant_id, reimburse_id)
  REFERENCES transactions (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE reimburse_flags
  DROP CONSTRAINT IF EXISTS reimburse_flags_txn_fk;
ALTER TABLE reimburse_flags
  ADD CONSTRAINT reimburse_flags_txn_fk
  FOREIGN KEY (tenant_id, txn_id)
  REFERENCES transactions (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE business_flags
  DROP CONSTRAINT IF EXISTS business_flags_txn_fk;
ALTER TABLE business_flags
  ADD CONSTRAINT business_flags_txn_fk
  FOREIGN KEY (tenant_id, txn_id)
  REFERENCES transactions (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE transaction_notes
  DROP CONSTRAINT IF EXISTS transaction_notes_txn_fk;
ALTER TABLE transaction_notes
  ADD CONSTRAINT transaction_notes_txn_fk
  FOREIGN KEY (tenant_id, txn_id)
  REFERENCES transactions (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE business_txn_class
  DROP CONSTRAINT IF EXISTS business_txn_class_txn_fk;
ALTER TABLE business_txn_class
  ADD CONSTRAINT business_txn_class_txn_fk
  FOREIGN KEY (tenant_id, txn_id)
  REFERENCES transactions (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE amazon_matches
  DROP CONSTRAINT IF EXISTS amazon_matches_txn_fk;
ALTER TABLE amazon_matches
  ADD CONSTRAINT amazon_matches_txn_fk
  FOREIGN KEY (tenant_id, transaction_id)
  REFERENCES transactions (tenant_id, id) ON DELETE CASCADE;

-- SET NULL must name its column: on a COMPOSITE key the unqualified form
-- nulls EVERY column in the key, including tenant_id, which is NOT NULL —
-- so the delete fails outright instead of clearing the link. Column-scoped
-- SET NULL is PostgreSQL 15+; this project requires 16.
ALTER TABLE equity_movement
  DROP CONSTRAINT IF EXISTS equity_movement_txn_fk;
ALTER TABLE equity_movement
  ADD CONSTRAINT equity_movement_txn_fk
  FOREIGN KEY (tenant_id, txn_id)
  REFERENCES transactions (tenant_id, id) ON DELETE SET NULL (txn_id);

-- idempotency: contribute/reimburse had no guard, so a double-submit
-- from the Transactions category dropdown recorded the same owner
-- contribution twice and silently doubled owner equity. One movement per
-- (transaction, kind); movements with no transaction are unconstrained.
--
-- De-dup FIRST. Any tenant who double-clicked before this index existed
-- already holds the duplicate pair — and CREATE UNIQUE INDEX on existing
-- duplicates raises, rolls back
-- the whole migration (migrate.py wraps each file in one transaction), never
-- stamps schema_migrations, and re-fails on every boot: the instance does not
-- start until an operator hand-deletes rows — the same reason the orphan
-- cleanups above run before their constraints.
--
-- Keep the EARLIEST of each group: the first click is the one the user meant,
-- the rest are the accident. Deleting rather than summing is deliberate —
-- these are duplicate records of ONE event, not two contributions.
DELETE FROM equity_movement e
 WHERE txn_id IS NOT NULL
   AND EXISTS (
       SELECT 1 FROM equity_movement k
        WHERE k.tenant_id = e.tenant_id
          AND k.txn_id    = e.txn_id
          AND k.kind      = e.kind
          AND (k.created_at, k.id) < (e.created_at, e.id));

CREATE UNIQUE INDEX IF NOT EXISTS equity_movement_txn_kind_uniq
    ON equity_movement (tenant_id, txn_id, kind)
    WHERE txn_id IS NOT NULL;
