-- Mobile device tokens: long-lived bearer credentials for the native
-- apps, minted by an authenticated session and revocable from the web
-- sessions panel. Control-plane (no RLS) like sessions/api_tokens —
-- reads/writes go through the app role directly.
CREATE TABLE IF NOT EXISTS device_tokens (
    token_hash   TEXT PRIMARY KEY,               -- sha256 of the oikd_ token
    id           UUID NOT NULL DEFAULT gen_random_uuid(),  -- API-safe handle
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_name  TEXT NOT NULL DEFAULT '',       -- e.g. "Test Phone" (display only)
    platform     TEXT NOT NULL DEFAULT '',       -- ios | android
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,           -- sliding idle deadline
    last_seen    TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_user ON device_tokens(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_device_tokens_id ON device_tokens(id);
