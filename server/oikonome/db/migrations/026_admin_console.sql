-- Operator console, minimum viable set. Control plane, no RLS.
-- Both tables are ADMIN-ROLE-ONLY: the console authenticates the operator
-- (OIKONOME_ADMIN_TOKEN) and does all its work on admin_connect(), so the
-- app role gets no grants at all — SQL injection through the tenant app
-- can neither forge an operator session nor scrub the audit trail.
-- (migrate.py's re-grant block re-asserts these REVOKEs every boot, since
-- its blanket GRANT ON ALL TABLES would otherwise re-open them.)
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,               -- sha256 of the cookie token
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    ip         TEXT
);

CREATE TABLE IF NOT EXISTS admin_audit (
    id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    action TEXT NOT NULL,                      -- login | invite | approve | …
    target TEXT,                               -- email / tenant id acted on
    detail JSONB,
    ip     TEXT
);

REVOKE ALL ON admin_sessions, admin_audit FROM oikonome_app;
