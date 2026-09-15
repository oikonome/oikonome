-- The bill schedule as the month actually ran under it. The budget
-- snapshot froze the config, but the fixed-bill schedule still came from
-- the live bills table — edit a bill's amount (or an envelope's cap) and
-- every closed month was quietly re-judged against today's schedule, the
-- same lost-history bug the config snapshot was built to kill. The nightly
-- job now freezes the active bill rows (plus the schedule's monthly
-- bills/income load, for the history card) beside the config; closed
-- months load their schedule from here. NULL = the snapshot predates bill
-- freezing — those months keep the live-table behaviour, as before.
-- (Mirrored in schema.sql.)

ALTER TABLE budget_snapshots ADD COLUMN IF NOT EXISTS bills JSONB;
