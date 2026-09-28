-- The scheduled-mail cadences fire at most once per LOCAL day. Each
-- heartbeat records the zone it was stamped in, so a send only counts as
-- "yesterday's" when both the zone it was sent in and the zone in effect
-- now agree the day has turned: a household moving to a zone far enough
-- ahead that "now" lands on the next calendar date must not get the same
-- verdict twice.
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS zone TEXT;
