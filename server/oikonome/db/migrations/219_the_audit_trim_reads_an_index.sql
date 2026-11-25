-- The nightly retention pass (jobs/retention.py) trims the audit log by
-- age alone, in bounded batches. The two indexes it already had lead on
-- the action and on the target, so every batch read the whole table to
-- find the rows older than the floor. Keyed on the time, each batch reads
-- only the slice it deletes.
CREATE INDEX IF NOT EXISTS admin_audit_at_idx ON admin_audit (at);
