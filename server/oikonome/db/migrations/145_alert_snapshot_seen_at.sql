-- The instant of the newest view of the world a snapshot applied to an
-- alert row.
--
-- The alert log is written as whole snapshots, each built from reads taken
-- before the write, so a slow writer can land after a faster one that
-- looked later. `last_seen` already stops an older DAY from undoing a newer
-- one, but it is a date, and two writers in flight at once almost always
-- carry the same day: the older view then reactivated an alert the newer
-- one had just cleared, and reset the household's dismissal with it.
-- Writers now stamp the time they began reading; a writer whose view is
-- older than the row's stamp leaves the row alone.
--
-- NULL on existing rows: the first snapshot after this stamps them.

ALTER TABLE alerts_log ADD COLUMN IF NOT EXISTS seen_at TIMESTAMPTZ;
