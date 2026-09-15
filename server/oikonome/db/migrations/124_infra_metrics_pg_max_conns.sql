-- The sampler records the database's own connection cap next to the count
-- it holds. The breach rule scores connections as a share of that cap, so
-- 16 open connections reads as two-thirds of a small managed database's
-- connection limit and as nothing at all on a self-hosted container's 100.
-- Rows from before this column keep classifying against the absolute
-- 25-connection thresholds.
ALTER TABLE infra_metrics ADD COLUMN IF NOT EXISTS pg_max_conns INTEGER;
