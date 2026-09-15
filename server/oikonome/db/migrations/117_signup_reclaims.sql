-- Taking back a squatted address, with the mailbox's proof FIRST.
--
-- Signup inserts the user row unverified, so with open signup anyone
-- can register an address they do not own and enrol a second factor on it:
-- the real owner's signup then answers "already exists", and the reset asks
-- for the squatter's recovery codes. Two obvious answers — resetting the
-- row's credentials on signup, and wiping the row's tenant and starting
-- over — are both takeovers: either one
-- hands whatever that account holds (banks connected, 2FA enrolled — the
-- normal state of a household that has not clicked its link yet) to anyone
-- who merely knows the address.
--
-- So a signup for a taken-but-unverified address creates NOTHING. It parks
-- what the signup carried — the chosen password's hash, the invite, the
-- referral — under an opaque token, and mails the address a link. Only the
-- click, which proves the mailbox, performs the reclaim: an unverified
-- owner's tenant is wiped (accomplices included), an unverified member is
-- moved out of the stranger's household, and the fresh account is created
-- already verified. A scanner prefetching the link reaches a confirm page;
-- the POST behind it is what spends the token.
--
-- Control-plane table, no RLS — the same world as password_resets, the same
-- at-rest discipline: 256-bit token in the link, sha256 here. Admin-only:
-- signup already runs on the admin connection (it creates tenants).

CREATE TABLE IF NOT EXISTS signup_reclaims (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email         TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    invite        TEXT,                         -- raw signup-invite token, re-looked-up on the click
    ref           TEXT,                         -- the signup's referrer string, kept for the record
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_signup_reclaims_user
  ON signup_reclaims (user_id) WHERE used_at IS NULL;
