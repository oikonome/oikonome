-- A console session opened with the operator token ends when the token is
-- rotated. The binding between the two used to ride inside the cookie, as
-- a keyed digest of the token, which handed anyone holding one cookie a
-- way to test guesses at the token offline. The binding now lives on the
-- session row instead and the cookie is random bytes with no relation to
-- the token. A row with no binding predates this and is ended like a
-- rotated one. Re-runnable.

ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS token_fp TEXT;
