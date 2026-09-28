-- Delete-with-grace: a tenant scheduled for deletion carries
-- status='pending_delete' + delete_after; the nightly worker purges it
-- once the grace window lapses, and an operator can restore it (→ active)
-- any time before then. Immediate delete still exists for the abuse case.
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS delete_after TIMESTAMPTZ;

-- Consented data access: an operator sees NO tenant data by default
-- (the doctrine). A tenant can GRANT time-boxed support access
-- from Settings; the operator's data-touching actions (export, and any
-- future data view) check for a live grant and are always audited.
-- Control-plane, no RLS (like sessions) — an operator reads it on the
-- admin connection; the granting write goes through the tenant's own
-- authenticated session.
CREATE TABLE IF NOT EXISTS support_consents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL,
    granted_by  UUID,                          -- the user who granted it
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS support_consents_live
    ON support_consents (tenant_id)
    WHERE revoked_at IS NULL;
