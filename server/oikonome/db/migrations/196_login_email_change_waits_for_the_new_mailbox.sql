-- A login-email change on a hosted instance waits for the new mailbox.
--
-- The email is the login identifier and, on hosted, one namespace shared
-- by every household. A change that moved the account at once let anyone
-- with a verified account hold any address they could type: the row sat
-- on it unverified, a signup by the address's real owner met "already
-- has an account", and the reclaim door, the nightly sweep and the reset
-- all treated the squatting household as somebody's real one.
--
-- So the change is two steps. The request (re-authenticated, as before)
-- records the wanted address here and mails a one-shot link to IT; the
-- account moves only when that link is followed and its confirm button
-- pressed — a POST, so a mail scanner fetching the link moves nothing.
-- Until then the address belongs to nobody: a signup for it goes ahead,
-- and the link then finds it taken and refuses.
--
-- A request answers only while the account is still on the address it
-- was made from (`old_email`) and the session or device that made it is
-- still signed in, so a reset, a revert, a sign-out everywhere or a
-- later request each end it. Its newest request is the only live one.
-- Control plane, no RLS, admin-only, the password_resets discipline:
-- 256-bit token in the link, sha256 here.

CREATE TABLE IF NOT EXISTS email_change_requests (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    old_email   TEXT NOT NULL,
    new_email   TEXT NOT NULL,
    -- the credential that asked: a browser session or a phone's device
    -- token (their API-safe ids), at most one of them
    session_id  UUID,
    device_id   UUID,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_email_change_requests_user
  ON email_change_requests (user_id) WHERE used_at IS NULL;
