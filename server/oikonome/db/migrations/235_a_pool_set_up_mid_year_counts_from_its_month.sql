-- The day a bill was set up: bills.created_on. A yearly envelope set up
-- during the year counts the year's spending at its merchant from the
-- month it was set up, not from January 1 (budget._envelope_prior_used):
-- spending before it was everyday spending in its own months.
--
-- Added nullable with no default, so the table is not rewritten and
-- every bill already stored reads NULL — "not known", which counts from
-- January 1 as before. Then a default for the rows written from now on,
-- which leaves the stored rows NULL. This build stores the household's
-- day. A build older than this one never names the column: a bill it
-- saves during a rolling deploy takes the default, the database's own
-- (UTC) day — late in the evening west of UTC that is tomorrow, and the
-- pool is then not in the plan for the rest of that household day.

ALTER TABLE bills ADD COLUMN IF NOT EXISTS created_on DATE;
ALTER TABLE bills ALTER COLUMN created_on SET DEFAULT CURRENT_DATE;
