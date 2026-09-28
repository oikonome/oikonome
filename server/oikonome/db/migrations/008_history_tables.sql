-- History / reference tables — investment & crypto positions, net-worth
-- history, the tax-return income spine, merchant display/category maps, and
-- the Amazon order pipeline's tables. Populated by sync/restore.py (export
-- ZIPs) and by the sync jobs that keep them live.
-- No FKs to accounts/transactions on purpose: snapshot/reference rows must
-- survive account re-links and removed transactions. (Also mirrored in
-- schema.sql for fresh installs; everything here is idempotent.)

CREATE TABLE IF NOT EXISTS holdings (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    account_id TEXT NOT NULL,
    symbol     TEXT NOT NULL,              -- ticker or CUSIP
    name       TEXT,                       -- fund name
    quantity   DOUBLE PRECISION,           -- net shares
    price      DOUBLE PRECISION,           -- last known share price
    value      DOUBLE PRECISION,           -- quantity * price
    as_of      TIMESTAMPTZ,
    raw        JSONB,
    PRIMARY KEY (tenant_id, account_id, symbol)
);

CREATE TABLE IF NOT EXISTS crypto_holdings (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    account_id TEXT NOT NULL,
    currency   TEXT NOT NULL,
    quantity   DOUBLE PRECISION,
    native_usd DOUBLE PRECISION,
    as_of      TIMESTAMPTZ,
    raw        JSONB,
    PRIMARY KEY (tenant_id, account_id, currency)
);

-- Human-recorded net-worth anchors; month stays TEXT 'YYYY-MM' (a label).
CREATE TABLE IF NOT EXISTS networth_recorded (
    tenant_id UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    month     TEXT NOT NULL,               -- 'YYYY-MM'
    total     DOUBLE PRECISION NOT NULL,
    source    TEXT,
    PRIMARY KEY (tenant_id, month)
);

-- Nightly live snapshots (the "real" trend accrues here, one point a night).
CREATE TABLE IF NOT EXISTS networth_snapshot (
    tenant_id UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    date      DATE NOT NULL,
    total     DOUBLE PRECISION,
    by_class  JSONB,
    PRIMARY KEY (tenant_id, date)
);

-- Authoritative yearly income from tax returns / W-2s / SSA. Reference
-- overlay, NOT transactions. joint=1 rows are MFJ totals incl. spouse.
CREATE TABLE IF NOT EXISTS income_annual (
    tenant_id         UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    year              INTEGER NOT NULL,
    total_income      DOUBLE PRECISION,    -- 1040 total income line (joint if MFJ)
    agi               DOUBLE PRECISION,
    taxable_income    DOUBLE PRECISION,
    tax_paid          DOUBLE PRECISION,
    wages             DOUBLE PRECISION,    -- 1040 wages line (joint if MFJ)
    primary_wages     DOUBLE PRECISION,    -- primary's own W-2 wages
    investment_income DOUBLE PRECISION,    -- taxable interest + ordinary dividends
    capital_gain      DOUBLE PRECISION,    -- may be negative
    spouse_wages      DOUBLE PRECISION,
    ss_earnings       DOUBLE PRECISION,    -- SSA SS-taxed earnings (wage-base capped)
    medicare_earnings DOUBLE PRECISION,    -- SSA Medicare-taxed (uncapped ~= gross)
    filing_status     TEXT,
    joint             INTEGER DEFAULT 0,
    source            TEXT,
    note              TEXT,
    PRIMARY KEY (tenant_id, year)
);

-- Itemized income documents (W-2 / 1099 / 5498 / 1098) — full box detail in
-- amounts (JSONB). Original integer ids travel in restores, so no identity.
CREATE TABLE IF NOT EXISTS income_documents (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id             BIGINT NOT NULL,
    year           INTEGER NOT NULL,
    form           TEXT NOT NULL,          -- W-2, 1099-INT, 1099-DIV, ...
    owner          TEXT DEFAULT 'primary',
    payer          TEXT,
    ein            TEXT,
    primary_amount DOUBLE PRECISION,       -- most-representative figure for the doc
    amounts        JSONB,                  -- all boxes/fields
    notes          TEXT,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS idx_income_docs_tenant_year
    ON income_documents(tenant_id, year);

-- Canonical merchant map for DISPLAY/grouping only. Keyed by the raw
-- descriptor COALESCE(merchant_name, name); display surfaces read
-- COALESCE(canonical, merchant_name, name). method='llm' beats 'layer1'.
CREATE TABLE IF NOT EXISTS merchant_canonical (
    tenant_id    UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    raw_merchant TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    method       TEXT,                     -- 'layer1' (deterministic) | 'llm'
    as_of        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, raw_merchant)
);

-- LLM-sharpened merchant → category map (refines vague aggregator buckets).
CREATE TABLE IF NOT EXISTS merchant_categories (
    tenant_id        UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    merchant         TEXT NOT NULL,
    category_primary TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'llm',
    classified_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, merchant)
);

CREATE TABLE IF NOT EXISTS amazon_orders (
    tenant_id       UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    dedup_key       TEXT NOT NULL,
    account         TEXT NOT NULL,
    date            DATE NOT NULL,
    amount          DOUBLE PRECISION NOT NULL,
    payee           TEXT NOT NULL DEFAULT 'Amazon',
    seller          TEXT NOT NULL DEFAULT '',
    memo            TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT '',
    category_source TEXT NOT NULL DEFAULT 'rules',
    order_number    TEXT NOT NULL DEFAULT '',
    is_refund       INTEGER NOT NULL,
    payment_method  TEXT NOT NULL DEFAULT '',
    items_json      JSONB NOT NULL DEFAULT '[]'::jsonb,
    inserted_at     TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_amazon_orders_tenant_date
    ON amazon_orders(tenant_id, date);

CREATE TABLE IF NOT EXISTS amazon_matches (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    transaction_id TEXT NOT NULL,
    dedup_key      TEXT,
    matched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS amazon_summaries (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    dedup_key  TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, dedup_key)
);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'holdings','crypto_holdings','networth_recorded','networth_snapshot',
        'income_annual','income_documents','merchant_canonical',
        'merchant_categories','amazon_orders','amazon_matches','amazon_summaries']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    END LOOP;
END $$;
