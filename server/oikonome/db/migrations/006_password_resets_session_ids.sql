-- Auth hardening:
--   * password_resets — one-time, 1-hour reset tokens (sha256 at rest, like
--     sessions); control-plane table, no RLS.
--   * sessions.id — a UUID surrogate the session-management API can expose
--     (the PK is the token hash, which must never leave the server).
--   * sessions.last_seen — coarse activity stamp for the sessions list.
CREATE TABLE IF NOT EXISTS password_resets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,             -- sha256 of the link token
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id);

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS id UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_id ON sessions(id);
