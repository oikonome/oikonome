-- push-heartbeats for host-side collector scripts. Sync entry
-- points stamp a row per successful push (sync/heartbeat.py); Doctor
-- renders staleness from it; the worker's nightly sweep sends the
-- opt-in edge-triggered stale email. expected_hours: NULL = undecided
-- (auto-watches at 24 on a second-day push), 0 = never warn.
-- (Mirrored in schema.sql for fresh installs; idempotent.)

CREATE TABLE IF NOT EXISTS script_heartbeats (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    source         TEXT NOT NULL,          -- item_id or a script-chosen slug
    label          TEXT,                   -- display name for the panel
    first_push     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_push      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_rows      INTEGER,
    expected_hours INTEGER,                -- NULL undecided · 0 off · N watch
    alerts         INTEGER NOT NULL DEFAULT 0,
    alerted_at     TIMESTAMPTZ,            -- edge-trigger guard, cleared on push
    PRIMARY KEY (tenant_id, source)
);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['script_heartbeats']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    END LOOP;
END $$;
