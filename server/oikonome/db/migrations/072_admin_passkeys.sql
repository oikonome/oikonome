-- Operator passkeys for the admin console.
--
-- A shared token (OIKONOME_ADMIN_TOKEN) is unattributable, un-rotatable
-- without an edit + restart, phishable, and plaintext on the host — and the
-- console reaches the root host-agent (restore, reset, uninstall, reboot).
-- These tables give it a real credential: a
-- hardware-bound WebAuthn key per operator, and a NAMED actor on every
-- audit row.
--
-- Deliberately NOT the tenant `passkeys` table: that one FKs to users, and
-- the console is outside the tenant auth world by design (see the module
-- docstring in server/oikonome/web/adminconsole.py).
--
-- Same posture as admin_sessions/admin_audit (migration 026): ADMIN-ROLE
-- ONLY. The app role gets no grants at all, so injected SQL through the
-- tenant app can neither enrol an operator key nor mint a step-up ticket.
-- (migrate.py's re-grant block re-asserts the REVOKE every boot.)

CREATE TABLE IF NOT EXISTS admin_credentials (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label         TEXT NOT NULL DEFAULT '',      -- "yubikey-5c", "laptop"…
    credential_id TEXT NOT NULL UNIQUE,          -- base64url
    public_key    TEXT NOT NULL,                 -- base64url COSE
    sign_count    BIGINT NOT NULL DEFAULT 0,
    transports    TEXT NOT NULL DEFAULT '',      -- comma-joined hints
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used     TIMESTAMPTZ
);

-- Short-lived server-side state between an options call and the browser's
-- response (register/login/stepup), plus the minted step-up TICKETS —
-- stored as sha256, like every other bearer credential here.
--
-- session_hash binds a step-up to the admin session that started it: a
-- ticket minted in one browser must not complete a destructive command
-- issued from another.
CREATE TABLE IF NOT EXISTS admin_challenges (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purpose       TEXT NOT NULL,                 -- register|login|stepup|ticket
    challenge     TEXT NOT NULL,                 -- base64url (ticket: sha256)
    session_hash  TEXT,                          -- stepup/ticket: whose session
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_challenges_expires
    ON admin_challenges (expires_at);

-- Which credential signed this session in; NULL = the break-glass token.
ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS credential_id UUID
    REFERENCES admin_credentials(id) ON DELETE SET NULL;

-- Who acted: "passkey:<label>" or "token". Nullable — rows written before
-- this migration genuinely do not know.
ALTER TABLE admin_audit ADD COLUMN IF NOT EXISTS actor TEXT;

REVOKE ALL ON admin_credentials, admin_challenges FROM oikonome_app;
