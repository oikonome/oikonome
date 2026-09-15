-- Merchant identity becomes a ROW, not a string.
--
-- Until now a transaction's merchant was re-derived at read time from its
-- raw descriptor through merchant_canonical (raw string → display name),
-- and every surface spelled that lookup itself — differently. This table
-- makes the merchant a thing with an id: resolved ONCE per transaction
-- (engine/merchant_identity.py), stored on transactions.merchant_id, and
-- read by joining the id. Plaid's stable merchant entity id, logo, website
-- and kind live here; non-Plaid rows get a merchant named by the layer-1
-- canonical (and attach to a Plaid merchant when the names match exactly).
--
-- merchant_canonical stays as the alias table (raw string → merchant): its
-- `canonical` column is kept as a mirror of merchants.name so readers that
-- have not moved to the join keep working; merchant_id is the truth.
CREATE TABLE IF NOT EXISTS merchants (
    tenant_id       UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id              UUID NOT NULL DEFAULT gen_random_uuid(),
    plaid_entity_id TEXT,
    name            TEXT NOT NULL,
    name_source     TEXT NOT NULL DEFAULT 'layer1',   -- manual | plaid | llm | layer1
    kind            TEXT NOT NULL DEFAULT 'merchant',  -- merchant | marketplace | payment_app | delivery_service | income_source | financial_institution | other
    logo_url        TEXT,
    website         TEXT,
    phone           TEXT,
    mcc             TEXT,
    parent_id       UUID,                              -- an outlet's brand (Costco Gas → Costco); populated later
    merged_into     UUID,                              -- a merged-away merchant points at its survivor
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS merchants_plaid_entity
    ON merchants (tenant_id, plaid_entity_id) WHERE plaid_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS merchants_lower_name
    ON merchants (tenant_id, lower(name));

ALTER TABLE merchant_canonical ADD COLUMN IF NOT EXISTS merchant_id UUID;
ALTER TABLE transactions       ADD COLUMN IF NOT EXISTS merchant_id UUID;
CREATE INDEX IF NOT EXISTS transactions_merchant
    ON transactions (tenant_id, merchant_id);

-- institution branding (Plaid /institutions/get_by_id?include_optional_metadata)
ALTER TABLE items ADD COLUMN IF NOT EXISTS logo TEXT;          -- base64 PNG as Plaid returns it
ALTER TABLE items ADD COLUMN IF NOT EXISTS brand_color TEXT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS url TEXT;

DO $$
BEGIN
    EXECUTE 'ALTER TABLE merchants ENABLE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON merchants';
    EXECUTE
        'CREATE POLICY tenant_isolation ON merchants '
        'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
        'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)';
END $$;

-- The backfill (resolve every existing row) is a Python job — engine/
-- merchant_identity.resolve_all — run by the nightly worker on first sight
-- and by `oikonome merchants resolve`; it needs the counterparty logic, not
-- SQL. Rows stay readable meanwhile: DISPLAY_MERCHANT falls back to the
-- canonical string until merchant_id is set.
