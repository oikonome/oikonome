-- Spam defense for the public email doors (signup, and any intake form an
-- add-on adds). All control-plane (no RLS, no tenant_id): a public door
-- has no tenant behind it.
-- Writes are admin-role only (see migrate.py grant block); the app role
-- never touches these — the check runs on an admin connection.
-- Mirrored in schema.sql.

-- The merged public feed (disposable-email-domains + StopForumSpam toxic),
-- refreshed wholesale by the daily job. `sources` records which feed(s)
-- carried each domain.
CREATE TABLE IF NOT EXISTS spam_domains_public (
    domain     TEXT PRIMARY KEY,
    sources    TEXT[] NOT NULL DEFAULT '{}',
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Operator/decline/auto-added domain blocks (small, hand-curated).
-- source: 'manual' (operator added) | 'decline' (decline-as-spam) |
-- 'auto' (live check flagged at submission / re-scan).
CREATE TABLE IF NOT EXISTS spam_domains_blocked (
    domain   TEXT PRIMARY KEY,
    reason   TEXT,
    source   TEXT NOT NULL DEFAULT 'manual',
    added_by TEXT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-address blocks — used when the domain is a major free provider
-- (safelisted) so we never blocklist the whole of gmail/outlook/etc.
CREATE TABLE IF NOT EXISTS spam_addresses_blocked (
    email    TEXT PRIMARY KEY,
    reason   TEXT,
    source   TEXT NOT NULL DEFAULT 'manual',
    added_by TEXT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Live-reputation cache (StopForumSpam email lookup + DNS flags), keyed by
-- domain with a TTL via checked_at, so repeat domains never re-hit the API.
CREATE TABLE IF NOT EXISTS spam_reputation_cache (
    domain     TEXT PRIMARY KEY,
    verdict    TEXT NOT NULL,          -- 'spam' | 'clean'
    score      JSONB NOT NULL DEFAULT '{}'::jsonb,
    source     TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One-row metadata for the public-feed refresh (last run, size, per-source
-- ETag/Last-Modified so an unchanged source is skipped).
CREATE TABLE IF NOT EXISTS blocklist_meta (
    id             BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    last_fetched_at TIMESTAMPTZ,
    public_count   INTEGER NOT NULL DEFAULT 0,
    source_state   JSONB NOT NULL DEFAULT '{}'::jsonb
);
INSERT INTO blocklist_meta (id) VALUES (true) ON CONFLICT DO NOTHING;

-- Silently-dropped submissions — the count + a viewable log for the
-- console. surface is the intake point; reason is why it was dropped.
CREATE TABLE IF NOT EXISTS spam_drops (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain     TEXT,
    surface    TEXT NOT NULL,          -- 'request' | 'signup'
    reason     TEXT NOT NULL,          -- 'public_list'|'blocked'|'address'|'live'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_spam_drops_created ON spam_drops(created_at DESC);
