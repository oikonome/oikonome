-- Which passkey opened this session's "sudo" window.
--
-- A credential rotation (password or email change) evicts passkeys,
-- because a hijacked session could have enrolled one. On an account whose
-- only strong factor is a passkey, evicting all of them drops the
-- account to zero factors, so the key that proved the caller is present
-- has to survive. "Freshly used" was the first approximation of that, and
-- it is the wrong test: POST /api/stepup/passkey is reachable by any
-- authenticated session, so an attacker who plants a key and re-asserts it
-- every few minutes keeps a key that is every bit as fresh as the victim's
-- — and on the recovery-code road the victim's own key is the stale one,
-- so the rotation deleted the owner's key and kept the stranger's.
--
-- So the identity of the proving key is recorded rather than inferred:
-- the step-up ticket remembers which credential signed the assertion, and
-- redeeming it stamps that credential on the session (or device token)
-- next to elevated_at. An elevation proved by a password or a recovery
-- code stamps NULL, which means "keep none" — the lost-authenticator road
-- lands where the password-reset road lands, with no key left behind.

ALTER TABLE webauthn_challenges
    ADD COLUMN IF NOT EXISTS passkey_id UUID
        REFERENCES passkeys(id) ON DELETE CASCADE;

-- deliberately NOT a foreign key: a dangling id must degrade to "keep
-- nothing", which is the safe direction, rather than to a stamp that
-- silently became NULL when some other door deleted the key.
ALTER TABLE sessions      ADD COLUMN IF NOT EXISTS elevated_passkey_id UUID;
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS elevated_passkey_id UUID;
