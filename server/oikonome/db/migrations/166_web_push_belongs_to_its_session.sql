-- A browser's web-push subscription belongs to the session that made it.
--
-- Without the link a subscription outlived its session: a browser whose
-- cookie expired, signed out or was revoked kept receiving the
-- household's verdict lines and alert text, because nothing tied the two.
-- With it, ending the session (sign-out, a revoke, a reset, the expiry
-- sweep) removes the channel too.
--
-- Rows made before this column existed have no session to name and stay
-- until the posture changes that already drop every subscription (a
-- password change, a reset, sign-out everywhere) or until their browser
-- subscribes again, which binds it.
ALTER TABLE push_subscriptions
    ADD COLUMN IF NOT EXISTS session_id UUID
        REFERENCES sessions(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS push_subs_session
    ON push_subscriptions (session_id);
