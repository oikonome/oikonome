-- A W-2's EIN is kept as its last four digits only: the full number is
-- never stored in this table and never leaves in an export.
-- This matches business_entity, whose EIN is encrypted at rest, exposed
-- only as ein_last4, and excluded from the export.
--
-- Nothing reads the full number back, and last four is what a display
-- needs without being the identifier.
ALTER TABLE income_documents ADD COLUMN IF NOT EXISTS ein_last4 TEXT;

UPDATE income_documents
   SET ein_last4 = right(regexp_replace(ein, '\D', '', 'g'), 4)
 WHERE ein IS NOT NULL
   AND ein_last4 IS NULL
   AND length(regexp_replace(ein, '\D', '', 'g')) >= 4;

-- and clear the full number from existing rows
UPDATE income_documents SET ein = NULL WHERE ein IS NOT NULL;
