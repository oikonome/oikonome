-- passkeys (WebAuthn) as an optional per-user sign-in upgrade.
-- Control-plane like users/sessions. webauthn_challenges holds the
-- short-lived server-side challenge between the options call and the
-- browser's response (stateless tokens can't be replay-safe).
-- (Mirrored in schema.sql for fresh installs.)

CREATE TABLE IF NOT EXISTS passkeys (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id TEXT NOT NULL UNIQUE,          -- base64url
    public_key    TEXT NOT NULL,                 -- base64url COSE
    sign_count    BIGINT NOT NULL DEFAULT 0,
    transports    TEXT NOT NULL DEFAULT '',      -- comma-joined hints
    label         TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_passkeys_user ON passkeys(user_id);

CREATE TABLE IF NOT EXISTS webauthn_challenges (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purpose    TEXT NOT NULL,                    -- register | login
    user_id    UUID,                             -- register: whose; login: NULL
    challenge  TEXT NOT NULL,                    -- base64url
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
