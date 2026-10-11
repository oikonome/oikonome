-- At most one member of a business is the household. The engine clears
-- the others when it sets one, but two writers marking two different
-- members at the same moment each clear what the other has not committed
-- yet, and both marks land — the estimate then reads one member's share
-- and the capital account another's. The database now refuses the
-- second mark. Any business already carrying more than one keeps its
-- earliest marked member, the one the estimate was already reading.
-- Re-runnable.

UPDATE entity_membership m
   SET is_household = FALSE
 WHERE m.is_household
   AND EXISTS (SELECT 1 FROM entity_membership o
                WHERE o.tenant_id = m.tenant_id
                  AND o.entity_id = m.entity_id
                  AND o.is_household
                  AND (o.created_at, o.id) < (m.created_at, m.id));

CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_membership_one_household
    ON entity_membership (tenant_id, entity_id) WHERE is_household;
