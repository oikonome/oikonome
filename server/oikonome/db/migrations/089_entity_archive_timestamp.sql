-- Removing a business entity is archive-first: the entity goes
-- read-only and hidden from active use, and every dependent record —
-- equity ledger, mileage log, 1099 vendors, compliance calendar, members —
-- survives, because those are tax-relevant records with retention
-- obligations. The ON DELETE CASCADE FKs on those tables stay as they are:
-- they only ever fire on an explicit, name-confirmed "delete forever",
-- which is their legitimate case (an entity created by mistake).
-- archived_at records WHEN the business was closed, next to the existing
-- status column. (Mirrored in schema.sql.)

ALTER TABLE business_entity ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

-- entities archived before this column existed: their updated_at was last
-- touched by the archive itself, so it is the best available timestamp.
UPDATE business_entity
   SET archived_at = updated_at
 WHERE status = 'archived' AND archived_at IS NULL;
