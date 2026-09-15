-- Native push: a mobile device may register the push token its platform
-- issued it (FCM on Android, APNs on iOS). The token is an opaque routing
-- handle for the platform's push service — it carries no account data —
-- and it rides on the device's own row so revoking the device also stops
-- its pushes. push_dead_at marks tokens the push service reported gone.
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS push_token    TEXT;
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS push_platform TEXT;  -- fcm | apns
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS push_updated  TIMESTAMPTZ;
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS push_dead_at  TIMESTAMPTZ;
