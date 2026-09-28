-- A device-mint ticket remembers the login session it was issued with.
--
-- Every sign-in opens a session and hands back a one-shot ticket; a phone
-- spends the ticket for a device token and keeps only that token. The
-- phone closed the session itself by posting a logout with the cookie, but
-- a platform whose fetch never sees the Set-Cookie header sends that
-- logout with no cookie, so the session stayed open for its whole
-- lifetime. Recording the session's hash on the ticket lets the exchange
-- close it server-side. Hashed like the ticket itself: a read of this
-- table must not yield a usable session.

ALTER TABLE webauthn_challenges ADD COLUMN IF NOT EXISTS session_hash TEXT;
