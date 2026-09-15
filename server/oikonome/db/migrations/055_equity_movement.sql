-- Owner-equity movements. Capital contributions, owner draws/
-- distributions, and reimbursements are their OWN classes that hit the entity's
-- capital account — never personal/business spend or income. Optionally linked
-- to a transaction (the personally-paid expense being reimbursed/contributed)
-- and to a member. (Mirrored in schema.sql.)
--
-- Capital account balance = Σ contribution − Σ (draw + distribution).
-- Reimbursements are the business settling a member-fronted cost; tracked here,
-- they don't change the capital balance (they're a cash settlement, not equity).

CREATE TABLE IF NOT EXISTS equity_movement (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         UUID NOT NULL DEFAULT gen_random_uuid(),
    entity_id  UUID NOT NULL,
    kind       TEXT NOT NULL,                    -- contribution | draw | distribution | reimbursement
    amount     NUMERIC(14,2) NOT NULL,           -- always positive; kind carries direction
    date       DATE NOT NULL,
    member_id  UUID,                             -- which member (nullable)
    txn_id     TEXT,                             -- linked transaction (nullable)
    form       TEXT,                             -- free text: 'Cash', 'ACH transfer', …
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES business_entity(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_equity_entity ON equity_movement(tenant_id, entity_id);

ALTER TABLE equity_movement ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON equity_movement;
CREATE POLICY tenant_isolation ON equity_movement
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
