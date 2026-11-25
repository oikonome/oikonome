-- A W-2 does not print which member of the household it belongs to, and
-- until now a stored one did not record it either: every W-2 was the
-- primary earner's, the first one stored for a year filled that year's
-- primary_wages, and a second was kept and never counted. From this build
-- the person says whose each W-2 is, and a person's wages for a year are
-- the sum of their W-2s whenever W-2s are what set the figure.
--
-- Three things make that sum safe to take:
--
-- 1. income_annual records, per wage figure, that W-2s set it
--    (primary_wages_from_w2, spouse_wages_from_w2). A figure from a return,
--    an earnings record or a hand edit is never moved by a W-2. A row whose
--    source is exactly 'w2' was created by the W-2 path and nothing has
--    merged into it since, so its primary_wages is a W-2's box 1.
--
-- 2. No stored W-2 is deleted. Two W-2s of one year with the same
--    employer and the same box 1 can be two people's forms (spouses at one
--    employer), so nothing here decides they are one upload, and no index
--    refuses a second (225 drops such an index where an install has one).
--
-- 3. Nobody is assumed. In a year that holds several W-2s, the one the
--    year's figure already stands for (its box 1 is the year's
--    primary_wages; failing that, the first stored) stays the primary
--    earner's. The others become 'unassigned': listed, counted for nobody,
--    until their person is named. No wage figure changes in this file.

ALTER TABLE income_annual
    ADD COLUMN IF NOT EXISTS primary_wages_from_w2 BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS spouse_wages_from_w2  BOOLEAN NOT NULL DEFAULT false;

UPDATE income_annual
   SET primary_wages_from_w2 = true
 WHERE source = 'w2' AND primary_wages IS NOT NULL
   AND NOT primary_wages_from_w2;

UPDATE income_documents SET owner = 'primary' WHERE owner IS NULL;
ALTER TABLE income_documents ALTER COLUMN owner SET NOT NULL;

UPDATE income_documents d
   SET owner = 'unassigned'
 WHERE d.form = 'W-2' AND d.owner = 'primary'
   AND d.id <> (
        SELECT k.id
          FROM income_documents k
          LEFT JOIN income_annual a
            ON a.tenant_id = k.tenant_id AND a.year = k.year
         WHERE k.tenant_id = d.tenant_id AND k.year = d.year
           AND k.form = 'W-2' AND k.owner = 'primary'
         ORDER BY (k.primary_amount IS NOT DISTINCT FROM a.primary_wages) DESC,
                  k.id
         LIMIT 1);
