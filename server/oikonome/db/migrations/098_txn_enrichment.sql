-- Everything the aggregator tells us about a transaction that a person
-- would want to see becomes a COLUMN — written on ingest, backfilled here
-- from the raw record every row already carries, round-tripped by
-- export/restore, shown in the row detail and searchable, rather than
-- sitting unread in transactions.raw.
--
--   check_number       the bank's check number (Plaid check_number)
--   payment_channel    (existed, never written) in store | online | other
--   payment_processor  a payment app / terminal in front of the merchant
--                      (PayPal, Venmo, Apple Pay, Square) — context, not
--                      identity; from the Plaid counterparties
--   location_*         where it happened (Plaid location)
--   mcc                the merchant's registered line of business
--                      (merchant_category_code) — a category signal
--   authorized_date    (existed, only restore wrote it)
--   category_source    who decided category_primary — plaid | import |
--                      flow | fuel | mcc | rule_user | rule_llm |
--                      rule_seed | rule_model — so the row can say WHY.
--                      (An override's provenance is derivable: manual_
--                      categories.bill_id, the Amazon prefix.)
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS check_number      TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS payment_processor TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_city     TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_region   TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_address  TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_postal   TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_lat      DOUBLE PRECISION;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_lon      DOUBLE PRECISION;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS location_store    TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS mcc               TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_source   TEXT;

-- Backfill from raw (Plaid shape). Idempotent: only rows still NULL.
UPDATE transactions SET
    check_number      = COALESCE(check_number, NULLIF(raw->>'check_number','')),
    payment_channel   = COALESCE(payment_channel, NULLIF(raw->>'payment_channel','')),
    location_city     = COALESCE(location_city, NULLIF(raw->'location'->>'city','')),
    location_region   = COALESCE(location_region, NULLIF(raw->'location'->>'region','')),
    location_address  = COALESCE(location_address, NULLIF(raw->'location'->>'address','')),
    location_postal   = COALESCE(location_postal, NULLIF(raw->'location'->>'postal_code','')),
    location_lat      = COALESCE(location_lat, (NULLIF(raw->'location'->>'lat',''))::double precision),
    location_lon      = COALESCE(location_lon, (NULLIF(raw->'location'->>'lon',''))::double precision),
    location_store    = COALESCE(location_store, NULLIF(raw->'location'->>'store_number','')),
    mcc               = COALESCE(mcc, NULLIF(raw->>'merchant_category_code','')),
    authorized_date   = COALESCE(authorized_date, (NULLIF(raw->>'authorized_date',''))::date)
WHERE raw IS NOT NULL AND jsonb_typeof(raw) = 'object'
  AND (raw ? 'personal_finance_category' OR raw ? 'check_number' OR raw ? 'location');

-- payment_processor: the payment_app / payment_terminal counterparty
UPDATE transactions t SET payment_processor = sub.p
FROM (SELECT t2.tenant_id, t2.id,
             (SELECT c->>'name' FROM jsonb_array_elements(t2.raw->'counterparties') c
               WHERE c->>'type' IN ('payment_app','payment_terminal') LIMIT 1) AS p
        FROM transactions t2
       WHERE t2.payment_processor IS NULL
         AND jsonb_typeof(t2.raw->'counterparties') = 'array') sub
WHERE sub.tenant_id = t.tenant_id AND sub.id = t.id AND sub.p IS NOT NULL;

-- category_source for the primary layer, best effort from what we know:
--   a Plaid row whose primary still equals Plaid's PFC → 'plaid'
--   a Plaid row whose primary differs               → 'rule' (some machine
--                                                     or user rule; refined
--                                                     to rule_<src> by the
--                                                     next apply() pass)
--   no PFC, a flow category                         → 'flow'
--   otherwise                                       → 'import'
UPDATE transactions SET category_source = CASE
    WHEN category_source IS NOT NULL THEN category_source
    WHEN category_primary IS NULL THEN NULL
    WHEN category_plaid IS NOT NULL AND category_primary = category_plaid THEN 'plaid'
    WHEN category_plaid IS NOT NULL THEN 'rule'
    WHEN category_primary IN ('TRANSFER_IN','TRANSFER_OUT','LOAN_PAYMENTS','INCOME') THEN 'flow'
    ELSE 'import' END
WHERE category_source IS NULL;

CREATE INDEX IF NOT EXISTS transactions_check_number
    ON transactions (tenant_id, check_number) WHERE check_number IS NOT NULL;
