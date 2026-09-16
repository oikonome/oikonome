-- The sampler records the database's own connection cap next to the count
-- it holds. The breach rule scores connections as a share of that cap, so
-- the same count reads differently on a small database and a large one.
-- Rows without a recorded cap classify against the absolute thresholds.
ALTER TABLE infra_metrics ADD COLUMN IF NOT EXISTS pg_max_conns INTEGER;
