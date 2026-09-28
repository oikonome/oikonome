-- Recovery codes: the safety net for FORCED second factors.
-- When an instance requires a second factor, an account must enrol TOTP or
-- a passkey before using the app; a TOTP user who loses their authenticator
-- would be locked out, so enrolment issues one-time recovery codes (shown
-- once). Control plane, no RLS
-- (like every other auth secret) — sha256 at rest, single-use.
CREATE TABLE IF NOT EXISTS recovery_codes (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash  TEXT NOT NULL UNIQUE,      -- sha256 of the printed code
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at    TIMESTAMPTZ                -- one-time: set when redeemed
);
CREATE INDEX IF NOT EXISTS idx_recovery_codes_user ON recovery_codes(user_id);

-- The app role may read + burn (used_at) but never mint (issuance is a
-- server-side admin-connection act at enrollment, like signup_invites) —
-- the migrate re-grant block column-scopes this.
