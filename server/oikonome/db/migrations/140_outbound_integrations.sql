-- Outbound integrations for a household running its own instance:
-- webhooks, a metrics exporter, a read door for a local MCP server.
--
-- Two things change.
--
-- 1. A script token gets a SCOPE. Until now every token was a push
--    credential — it could put rows in through the import doors and
--    nothing else. A metrics scraper, a Home Assistant sensor and an MCP
--    server need the opposite: read a summary, never write. `scope` says
--    which of the two a token is; the auth layer allows each scope exactly
--    its own doors, so a leaked read token cannot import and a leaked push
--    token cannot read. Existing rows keep 'push', which is what they were.
--
-- 2. Webhooks and their deliveries. A webhook is a URL the household asked
--    to be told at, with the events it wants and a signing secret (stored
--    encrypted under the tenant key, like an aggregator credential — it has
--    to be recoverable to sign with). A delivery is one event for one
--    webhook: an outbox row, attempted with backoff by the worker, kept a
--    week for the Settings card to show what happened. Both are tenant
--    tables under RLS; neither is exported (a restore into another instance
--    must not silently start posting that household's ledger to a URL the
--    new operator never saw, and the secret would not decrypt there
--    anyway). Mirrored in schema.sql.

ALTER TABLE api_tokens ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'push';
DO $$ BEGIN
    ALTER TABLE api_tokens ADD CONSTRAINT api_tokens_scope_known
        CHECK (scope IN ('push', 'read'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS webhooks (
    tenant_id       UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id              UUID NOT NULL DEFAULT gen_random_uuid(),
    url             TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    secret          TEXT NOT NULL,               -- encrypted under the tenant key
    events          TEXT[] NOT NULL DEFAULT '{}',-- event names, or '*'
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT NOT NULL DEFAULT '',    -- the owner's address at creation
    last_attempt_at TIMESTAMPTZ,
    last_status     INTEGER,                     -- HTTP status of the last attempt
    last_error      TEXT,
    failures        INTEGER NOT NULL DEFAULT 0,  -- consecutive failed deliveries
    disabled_reason TEXT,                        -- set when the app switched it off
    PRIMARY KEY (tenant_id, id)
);
ALTER TABLE webhooks ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON webhooks;
CREATE POLICY tenant_isolation ON webhooks
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    tenant_id       UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id              UUID NOT NULL DEFAULT gen_random_uuid(),
    webhook_id      UUID NOT NULL,
    event           TEXT NOT NULL,
    payload         JSONB NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending', -- pending | delivered | failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at    TIMESTAMPTZ,
    response_status INTEGER,
    last_error      TEXT,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS webhook_deliveries_due
    ON webhook_deliveries (tenant_id, next_attempt_at) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS webhook_deliveries_hook
    ON webhook_deliveries (tenant_id, webhook_id, created_at DESC);
ALTER TABLE webhook_deliveries ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON webhook_deliveries;
CREATE POLICY tenant_isolation ON webhook_deliveries
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));
