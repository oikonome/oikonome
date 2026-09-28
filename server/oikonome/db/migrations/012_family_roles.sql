-- family members with view-only access. users.role gates every
-- write in current_user (owner = full control; viewer = read-only plus
-- own-account actions). invites are one-time share-links, control-plane
-- like users (admin-conn access, no RLS — they create users).
-- (Mirrored in schema.sql for fresh installs.)

ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'owner';

CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,                 -- sha256 of the URL token
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'viewer',
    label      TEXT NOT NULL DEFAULT '',         -- who it's for (display only)
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_invites_tenant ON invites(tenant_id);
