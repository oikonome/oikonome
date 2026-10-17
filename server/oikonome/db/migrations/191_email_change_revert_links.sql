-- The way back from a login-email change the owner did not make.
--
-- The email is the login identifier, so a change made from a hijacked
-- session (a phished password plus a relayed one-time code is enough)
-- moves the account somewhere its owner can neither sign in to nor reset
-- from: the old address no longer names any account. So the change mails
-- the OLD address a one-shot link, and following it puts the address
-- back, signs every session and device out, and starts a password reset
-- to that mailbox.
--
-- The link restores the ADDRESS only. It strips no factor: the old
-- mailbox may be the very reason the owner moved away from it, and "I
-- read the old inbox" must not equal "I own the account". The reset it
-- starts keeps the usual rule that an account with a second factor also
-- needs a recovery code.
--
-- Bound to the account AND the address it was mailed to, never to the
-- address the account moved to: a second change made in a hurry must not
-- disarm the first link. Control plane, no RLS, the password_resets
-- discipline: 256-bit token in the link, sha256 here.

CREATE TABLE IF NOT EXISTS email_change_reverts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    old_email   TEXT NOT NULL,
    new_email   TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_email_change_reverts_user
  ON email_change_reverts (user_id) WHERE used_at IS NULL;
