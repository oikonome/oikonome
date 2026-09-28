-- self-employed tax hygiene (QuickBooks Self-Employed lane, kept lean).
-- Quarterly estimated taxes, mileage, home-office, a simple balance sheet, and
-- 1099 vendor tracking. Estimated tax + home office are CALCULATIONS driven by
-- a few per-entity settings; mileage + 1099 vendors need storage. All feed the
-- existing P&L. Everything is an estimate — not tax advice. (Mirrored in schema.sql.)

-- per-entity tax settings (drive the estimated-tax + home-office calcs)
ALTER TABLE business_entity ADD COLUMN IF NOT EXISTS home_office_sqft INTEGER;
ALTER TABLE business_entity ADD COLUMN IF NOT EXISTS income_tax_rate NUMERIC(5,2);  -- assumed effective %, e.g. 22.00
ALTER TABLE business_entity ADD COLUMN IF NOT EXISTS filing_status TEXT;            -- single | married_joint | …

CREATE TABLE IF NOT EXISTS mileage_log (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         UUID NOT NULL DEFAULT gen_random_uuid(),
    entity_id  UUID NOT NULL,
    date       DATE NOT NULL,
    miles      NUMERIC(10,1) NOT NULL,
    purpose    TEXT,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES business_entity(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mileage_entity ON mileage_log(tenant_id, entity_id);

-- 1099-NEC vendor marks: which payees are contractors (reportable at $600+)
CREATE TABLE IF NOT EXISTS vendor_1099 (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id         UUID NOT NULL DEFAULT gen_random_uuid(),
    entity_id  UUID NOT NULL,
    merchant   TEXT NOT NULL,                    -- payee (canonical display name)
    reportable BOOLEAN NOT NULL DEFAULT TRUE,
    tin_last4  TEXT,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, entity_id, merchant),
    FOREIGN KEY (tenant_id, entity_id) REFERENCES business_entity(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vendor1099_entity ON vendor_1099(tenant_id, entity_id);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['mileage_log','vendor_1099']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    END LOOP;
END $$;
