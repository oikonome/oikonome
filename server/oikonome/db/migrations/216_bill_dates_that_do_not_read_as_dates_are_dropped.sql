-- A bill's raw document holds its schedule's dates: dueOn, lastDueOn, and
-- the recurrence's endOn and dueOn. A restore drops one that does not read
-- as a date (restore.clean_bill_raw) and takes dueOn back from the due_on
-- column, and every reader guards each one (bills.stored_date), but rows
-- written before the restore validated them still hold an impossible day
-- ("2026-02-30"), a blank, a number. Each reader that forgets the guard
-- turns one such row into a Bills screen that does not load.
--
-- The step, db/migrate.py's PYTHON_STEPS entry for this file
-- (drop_unreadable_bill_dates), runs in this file's transaction: it drops
-- every such key and, where raw dueOn is then missing and due_on is set,
-- writes raw dueOn from the column, as a restore does. A null is left: it
-- is how a bill says it has no floor or no end yet. Idempotent: a row is
-- written only if it changes, and only if it still reads as it was read.
-- So this file changes no schema and no row; it is the record that the
-- step ran.

SELECT 1;
