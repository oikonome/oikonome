-- signup gate — both tables are CONTROL PLANE (no
-- RLS; they exist before any tenant does).
--
-- signup_invites: operator-minted (`oikonome invite <email>` — admin role
-- only; the app role can only read and burn, so injected SQL can't mint
-- itself an invite), single-use, expiring, bound to one email address.
CREATE TABLE IF NOT EXISTS signup_invites (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash     TEXT NOT NULL UNIQUE,      -- sha256 of the link token
    email          TEXT NOT NULL,             -- the ONLY address it admits
    note           TEXT,                      -- free-text operator memo
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    used_at        TIMESTAMPTZ,               -- one-time: set on signup
    used_by_tenant UUID                       -- audit: which tenant it minted
);
CREATE INDEX IF NOT EXISTS idx_signup_invites_email ON signup_invites(email);

-- email_verifications: same shape and at-rest discipline as
-- password_resets (opaque token in the link, sha256 in the DB).
CREATE TABLE IF NOT EXISTS email_verifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,          -- 7 days from creation
    used_at     TIMESTAMPTZ                    -- one-time: set on verify
);
CREATE INDEX IF NOT EXISTS idx_email_verifications_user
    ON email_verifications(user_id);
