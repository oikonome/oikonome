-- Small-business management: the entity model.
-- A tenant can declare one or more business entities (their LLC / sole prop)
-- and assign whole accounts and/or individual transactions to an entity. The
-- HARD-SEPARATION filter (entity money excluded from personal budgets) is
-- applied in a SEPARATE step — this migration only records the assignment, so
-- it changes no money math on its own. EIN is stored encrypted (envelope
-- crypto, app layer) with a last-4 display column. (Mirrored in schema.sql.)

CREATE TABLE IF NOT EXISTS business_entity (
    tenant_id           UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    structure           TEXT NOT NULL,           -- single_member_llc | multi_member_llc | sole_prop | s_corp
    state               TEXT,                    -- formation state, e.g. 'DE'
    formation_date      DATE,                    -- date the entity was formed/registered
    business_start_date DATE,                    -- when it began operating (the §195 start-up hinge)
    ein_enc             TEXT,                    -- EIN ciphertext (per-tenant envelope crypto, crypto.encrypt)
    ein_last4           TEXT,                    -- display only (last 4 of the EIN)
    registered_agent    TEXT,
    fiscal_year_end     TEXT,                    -- 'MM-DD', e.g. '12-31'
    status              TEXT NOT NULL DEFAULT 'active',  -- active | archived (read-only on downgrade)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);

-- Members/owners (multi-member LLC, S-corp shareholders). A single-member LLC
-- has one row at 100%. Kept separate from the entity so ownership can change.
CREATE TABLE IF NOT EXISTS entity_membership (
    tenant_id     UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id            UUID NOT NULL DEFAULT gen_random_uuid(),
    entity_id     UUID NOT NULL,
    member_name   TEXT NOT NULL,
    ownership_pct NUMERIC(6,3),                  -- e.g. 100.000
    is_manager    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES business_entity(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entity_membership_entity
    ON entity_membership(tenant_id, entity_id);

-- Assignment columns. entity_id on an ACCOUNT = the whole account belongs to
-- the entity (all its transactions are business). entity_id on a TRANSACTION =
-- a per-row override (e.g. a business cost paid on a personal card). A
-- transaction is "business" if its own entity_id is set OR its account's is.
-- ON DELETE SET NULL: deleting an entity reverts its money to personal, never
-- destroys transactions.
ALTER TABLE accounts     ADD COLUMN IF NOT EXISTS entity_id UUID;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS entity_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'accounts_entity_fk') THEN
        ALTER TABLE accounts ADD CONSTRAINT accounts_entity_fk
            FOREIGN KEY (tenant_id, entity_id)
            REFERENCES business_entity(tenant_id, id) ON DELETE SET NULL (entity_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'transactions_entity_fk') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_entity_fk
            FOREIGN KEY (tenant_id, entity_id)
            REFERENCES business_entity(tenant_id, id) ON DELETE SET NULL (entity_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_accounts_entity     ON accounts(tenant_id, entity_id);
CREATE INDEX IF NOT EXISTS idx_txn_entity          ON transactions(tenant_id, entity_id);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['business_entity','entity_membership']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    END LOOP;
END $$;
