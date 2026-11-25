-- A W-2 stored before a W-2 named whose it is carries the column default,
-- the primary earner, whoever's form it was. This reconciles those years.
--
-- Where a W-2-owned primary figure disagrees with what its primary W-2s
-- add up to, only the W-2 the figure stands for (its box 1 is the figure;
-- failing that, the first stored) stays the primary earner's; the rest
-- become 'unassigned' — listed, counted for nobody until named. A figure
-- that still disagrees was never the W-2s' sum and stops belonging to
-- them. A year where the figure already equals its primary W-2s' sum (one
-- W-2, or W-2s stored by this design) is untouched. No figure changes.
-- Same rule as taxdocs.reconcile_unmarked, which a restore of an older
-- archive applies per year.

WITH sums AS (
    SELECT a.tenant_id, a.year, a.primary_wages
      FROM income_annual a
     WHERE a.primary_wages_w2 IS NOT NULL
       AND a.primary_wages_w2 IS DISTINCT FROM (
           SELECT SUM(d.primary_amount) FROM income_documents d
            WHERE d.tenant_id = a.tenant_id AND d.year = a.year
              AND d.form = 'W-2' AND d.owner = 'primary'
              AND d.primary_amount IS NOT NULL AND d.primary_amount <> 0)
), keep AS (
    SELECT DISTINCT ON (s.tenant_id, s.year) s.tenant_id, s.year, d.id
      FROM sums s
      JOIN income_documents d
        ON d.tenant_id = s.tenant_id AND d.year = s.year
       AND d.form = 'W-2' AND d.owner = 'primary'
     ORDER BY s.tenant_id, s.year,
              (d.primary_amount IS NOT DISTINCT FROM s.primary_wages) DESC,
              d.id
)
UPDATE income_documents d
   SET owner = 'unassigned'
  FROM keep k
 WHERE d.tenant_id = k.tenant_id AND d.year = k.year
   AND d.form = 'W-2' AND d.owner = 'primary' AND d.id <> k.id;

UPDATE income_annual a
   SET primary_wages_w2 = NULL
 WHERE a.primary_wages_w2 IS NOT NULL
   AND a.primary_wages_w2 IS DISTINCT FROM (
       SELECT SUM(d.primary_amount) FROM income_documents d
        WHERE d.tenant_id = a.tenant_id AND d.year = a.year
          AND d.form = 'W-2' AND d.owner = 'primary'
          AND d.primary_amount IS NOT NULL AND d.primary_amount <> 0);
