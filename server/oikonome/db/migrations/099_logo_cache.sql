-- Merchant logos, cached instance-wide (control plane, no tenant key, no
-- RLS): a logo is Plaid's public asset, not tenant data. The app serves
-- them from /api/logo so the browser and the phone never fetch from
-- plaid.com themselves — a self-hoster's ledger reveals nothing to a CDN,
-- and the CSP img-src goes back to 'self'.
CREATE TABLE IF NOT EXISTS logo_cache (
    url          TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    bytes        BYTEA NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
