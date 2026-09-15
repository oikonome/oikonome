-- The scheduled-mail cadences fire at most once per LOCAL day, and the
-- once-per-day guard compared the heartbeat against local midnight in the
-- household's CURRENT zone. Changing the household's timezone to one
-- far enough ahead that "now" lands on the next calendar date moved that
-- midnight past the morning's send, and the next sweep sent the same
-- verdict again a few hours later. The heartbeat now records the zone it
-- was stamped in, so a send only counts as "yesterday's" when both the
-- zone it was sent in and the zone in effect now agree the day has turned.
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS zone TEXT;
