-- Home-screen widget tokens: the credential a phone's widget polls with.
--
-- A widget draws on the lock screen and the launcher, refreshed by the
-- OS every half hour with no person present — so it cannot use the
-- device token, which sits in the biometric enclave precisely so that
-- nothing reads it without a finger or a face. It gets its own, lesser
-- credential instead: a child of the device row that can reach exactly
-- one door (the glance payload: today's verdict and allowance, never a
-- balance or a transaction) and dies with its parent — revoking the
-- device, changing the password, or the device idling out all end the
-- widget too, because the lookup joins through the live parent row.
-- Control-plane, no RLS, like device_tokens. Mirrored in schema.sql.

CREATE TABLE IF NOT EXISTS widget_tokens (
    token_hash   TEXT PRIMARY KEY,               -- sha256 of the oikw_ token
    device_id    UUID NOT NULL,                  -- device_tokens.id (the parent)
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_widget_tokens_device ON widget_tokens(device_id);
