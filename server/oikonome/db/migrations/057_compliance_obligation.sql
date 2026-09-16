-- Compliance calendar — recurring state/federal obligations.
-- A state's annual report is DERIVED in code from the entity's state +
-- formation date (no storage). This table holds USER-ADDED custom obligations
-- (a franchise tax, a federal filing, a local license, a state the code does
-- not know) so the calendar isn't limited to the built-in rules. (Mirrored in
-- schema.sql.)

CREATE TABLE IF NOT EXISTS compliance_obligation (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         UUID NOT NULL DEFAULT gen_random_uuid(),
    entity_id  UUID NOT NULL,
    title      TEXT NOT NULL,
    due_date   DATE NOT NULL,                    -- anchor (first/next occurrence)
    recurrence TEXT NOT NULL DEFAULT 'yearly',   -- yearly | once
    fee        NUMERIC(10,2),
    url        TEXT,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES business_entity(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_compliance_entity ON compliance_obligation(tenant_id, entity_id);

ALTER TABLE compliance_obligation ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON compliance_obligation;
CREATE POLICY tenant_isolation ON compliance_obligation
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
