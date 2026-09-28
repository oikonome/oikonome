-- The way back in for someone who has lost the password, the authenticator
-- AND the recovery codes.
--
-- The mailed reset strips every factor, so on an account with a strong
-- factor enrolled it also demands a recovery code — unconditionally, or
-- "I control the inbox" would equal "I own the account". A person missing
-- all three therefore needs another door. This table is that door: an
-- email-proven request that a factor-clearing reset will land after a
-- cooling-off period. The account is told on every channel it has, one
-- click on the mailed link cancels it, and so does any live sign-in. The
-- delay plus the cancel is what makes inbox control insufficient on its
-- own — the objection the unconditional rule encodes still holds.
--
-- Control plane, no RLS: keyed on the user like password_resets. The
-- cancel link is a 256-bit token, stored hashed like every other link.

CREATE TABLE IF NOT EXISTS factor_resets (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    lands_at          TIMESTAMPTZ NOT NULL,
    cancel_token_hash TEXT NOT NULL UNIQUE,
    cancelled_at      TIMESTAMPTZ,
    cancel_reason     TEXT,                      -- link | sign-in | superseded
    consumed_at       TIMESTAMPTZ,               -- the reset that used it
    requested_ip      TEXT
);
CREATE INDEX IF NOT EXISTS idx_factor_resets_user ON factor_resets(user_id);
