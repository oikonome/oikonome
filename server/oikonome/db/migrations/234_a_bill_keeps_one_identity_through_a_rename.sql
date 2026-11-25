-- A bill now has an identity that never changes: bills.uid, drawn once
-- when the row is made. Its id is the slug of its payee, and renaming the
-- bill rewrites that id, so nothing that remembers a bill across months
-- (a month's frozen schedule) can tell a renamed bill from a new one. A
-- rename, a re-save, the card-number cuts and every other in-place update
-- keep the uid; a restore lands the archived one when no other bill of the
-- household holds it. Each month's frozen schedule records it from now
-- on; nothing reads it to match a bill yet.
--
-- The column is added in three steps. Adding a nullable column with no
-- default is a catalogue change only. Each existing row then draws its own
-- uid: that UPDATE writes every row of the table once (a new version of
-- each bill, which the bills table, small in every household, takes
-- briefly). Only after that does the column take its default and NOT NULL,
-- which reads the table once under its lock to check no row is empty. A
-- build older than this one keeps working beside it: it never names the
-- column, and every bill it inserts takes the default; an insert that
-- arrives while the steps run waits for them and then lands. schema.sql
-- carries the column for a fresh install;
-- the index is here alone.

ALTER TABLE bills ADD COLUMN IF NOT EXISTS uid UUID;

UPDATE bills SET uid = gen_random_uuid() WHERE uid IS NULL;

ALTER TABLE bills
    ALTER COLUMN uid SET DEFAULT gen_random_uuid(),
    ALTER COLUMN uid SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS bills_by_uid ON bills (tenant_id, uid);
