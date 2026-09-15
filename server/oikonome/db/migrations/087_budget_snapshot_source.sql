-- How a month's budget snapshot came to exist: 'close' (frozen by the
-- nightly job as the month ran) or 'backfill' (the owner chose to apply
-- the then-current budget to older months that predate snapshots). The
-- distinction is display truth for the Budget page's history card — a
-- backfilled month is a verdict the owner opted into retroactively, and
-- the UI says so rather than passing it off as history. (Mirrored in
-- schema.sql.)

ALTER TABLE budget_snapshots
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'close';
