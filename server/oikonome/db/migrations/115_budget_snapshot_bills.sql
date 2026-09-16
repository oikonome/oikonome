-- The bill schedule as the month actually ran under it. The budget
-- snapshot freezes the config; without the bills too, editing a bill's
-- amount (or an envelope's cap) would quietly re-judge every closed month
-- against today's schedule. The nightly job freezes the active bill rows (plus the schedule's monthly
-- bills/income load, for the history card) beside the config; closed
-- months load their schedule from here. NULL = the snapshot predates bill
-- freezing — those months keep the live-table behaviour.
-- (Mirrored in schema.sql.)

ALTER TABLE budget_snapshots ADD COLUMN IF NOT EXISTS bills JSONB;
