-- The per-connection questions that do not filter on the error column
-- read one connection's rows from an index, not the whole log: has this
-- connection ever been pulled, was it tried in the last few seconds (the
-- onboarding poll asks both for every connection, every few seconds),
-- what did its last pull say, and which failures came after its newest
-- success (the reaper, nightly, per connection). The two partial indexes
-- beside this one cover only the rows without an error, so each of these
-- read every row the household ever logged. Unfiltered, over (tenant,
-- item, ran_at desc), it answers all of them from one connection's
-- slice, newest first. The nightly retention pass keeps the log itself
-- bounded (jobs/retention.py), so the build stays small.
CREATE INDEX IF NOT EXISTS idx_sync_log_item_ran
    ON sync_log (tenant_id, item_id, ran_at DESC);
