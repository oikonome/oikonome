-- A person's wages for a year are the sum of their W-2s whenever W-2s are
-- what set that figure (specs/w2-wages.md). Each wage figure gets a
-- companion holding the value the W-2s last wrote into it; the figure
-- belongs to the W-2s while it is empty or still equals that value, so any
-- other writer, including a build that predates this one, takes it back
-- just by writing it.
--
-- Migration 221's booleans (*_from_w2) stay and are not read: a flag
-- survives a writer that does not know about it, which is how a rollback
-- would have let a W-2 overwrite a return's figure.
--
-- Seeded from the one figure known to have come from a W-2: a row whose
-- source is exactly 'w2' was made by the W-2 path and nothing has merged
-- into it since. No wage figure changes in this file.

ALTER TABLE income_annual
    ADD COLUMN IF NOT EXISTS primary_wages_w2 DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS spouse_wages_w2  DOUBLE PRECISION;

UPDATE income_annual
   SET primary_wages_w2 = primary_wages
 WHERE source = 'w2' AND primary_wages IS NOT NULL
   AND primary_wages_w2 IS NULL;
