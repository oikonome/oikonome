-- script tokens — long-lived bearer credentials for host-side
-- collector scripts (community scripts). Control-plane like sessions:
-- the token→tenant lookup happens before any tenant scope exists, so no
-- RLS. Server restricts them to the import doors; the owner mints and
-- revokes them in Settings → Connections. (Mirrored in schema.sql.)

CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash   TEXT PRIMARY KEY,               -- sha256 of the oik_ token
    id           UUID NOT NULL DEFAULT gen_random_uuid(),  -- API-safe handle
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    name         TEXT NOT NULL DEFAULT '',       -- which script (display only)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_tenant ON api_tokens(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_tokens_id ON api_tokens(id);
