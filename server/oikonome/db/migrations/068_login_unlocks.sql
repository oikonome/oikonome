-- an emailed way to ANSWER the human check, for users the
-- Turnstile script can never reach (JS off, ad blocker, corporate MITM
-- proxy, privacy browser). Proving control of the account's mailbox is a
-- stronger answer to "is this the account's owner" than a CAPTCHA, and it
-- needs no JavaScript.
--
-- Control-plane table, no RLS — same world as password_resets, and the same
-- at-rest discipline: opaque 256-bit token in the link, sha256 here, so a DB
-- leak alone unlocks nothing.
--
-- unlocked_until is what the login door reads. It is set when the link is
-- FOLLOWED, not when it is minted, and it exempts THAT ACCOUNT only — never
-- the IP and never the instance. Clearing the per-IP bucket here would hand
-- back a laundering hole: anyone holding one account they
-- control could unlock their own, wipe the IP counter, and resume guessing
-- at someone else's.

CREATE TABLE IF NOT EXISTS login_unlocks (
    id             BIGSERIAL PRIMARY KEY,
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash     TEXT NOT NULL UNIQUE,
    expires_at     TIMESTAMPTZ NOT NULL,      -- link validity
    used_at        TIMESTAMPTZ,               -- link followed
    unlocked_until TIMESTAMPTZ,               -- exemption window, set on use
    consumed_at    TIMESTAMPTZ,               -- exemption spent on a sign-in
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- the login door's hot path: "is there a live exemption for this user"
CREATE INDEX IF NOT EXISTS idx_login_unlocks_active
  ON login_unlocks (user_id, unlocked_until)
  WHERE consumed_at IS NULL;
