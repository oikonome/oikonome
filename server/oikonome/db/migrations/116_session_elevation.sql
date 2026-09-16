-- Session elevation ("sudo mode"). Taking the password (+ live TOTP /
-- passkey step-up) on EVERY account-security request costs the settings
-- page a password field per card. Instead identity is re-proved ONCE —
-- passkey where the account has one, else password (+ code when
-- enrolled) — and the proof is stamped on THAT session row;
-- for ten minutes the guarded doors read the stamp instead of asking
-- again. The threat model does not move: re-auth exists so a SESSION
-- THIEF (cookie, no password/passkey) cannot change the security
-- posture, and the stamp is only ever written by the same proofs, so the
-- thief still cannot elevate — nor borrow the owner's other session's
-- stamp, because it lives on the row, not the user. Two doors (account
-- delete, the download-everything export) require the stamp to be
-- seconds old, not minutes. Mobile runs on device tokens, which are a
-- session by another name, so they carry the same column. Control-plane
-- tables, no RLS. (Mirrored in schema.sql.)

ALTER TABLE sessions      ADD COLUMN IF NOT EXISTS elevated_at TIMESTAMPTZ;
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS elevated_at TIMESTAMPTZ;
