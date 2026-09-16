-- multi-channel budget summaries (SMS / web push alongside email).
--
-- push_subscriptions: browser Web Push subscriptions, one row per
-- browser/device. Control-plane (no RLS) like sessions — a subscription
-- belongs to a USER; tenant_id is carried for the worker's per-tenant
-- fan-out without a join through users. endpoint is unique: re-subscribing
-- the same browser upserts instead of duplicating.
--
-- push_vapid: the instance's VAPID keypair (Web Push requires one), a
-- single row generated lazily on first use. The private key is encrypted
-- under OIKONOME_MASTER_KEY like other instance secrets.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL,
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- delivery failures flip this off instead of deleting (a 410 Gone
    -- endpoint is dead; anything else may be transient)
    dead_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS push_subs_tenant ON push_subscriptions (tenant_id)
    WHERE dead_at IS NULL;

CREATE TABLE IF NOT EXISTS push_vapid (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    private_key TEXT NOT NULL,      -- encrypted (crypto.encrypt_cp)
    public_key  TEXT NOT NULL,      -- b64url, served to the browser
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
