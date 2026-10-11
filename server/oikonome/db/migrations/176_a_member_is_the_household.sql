-- A business can have several members, and nothing said which of them
-- is the household this instance keeps the books for: the estimated tax
-- covered every member and said so. One member may now be marked as the
-- household; the estimate and the capital account then also give that
-- member's share, by their ownership percentage. At most one per entity
-- (the engine clears the others when one is set). Re-runnable.

ALTER TABLE entity_membership
    ADD COLUMN IF NOT EXISTS is_household BOOLEAN NOT NULL DEFAULT FALSE;
