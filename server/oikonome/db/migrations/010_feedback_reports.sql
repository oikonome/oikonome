-- In-app feedback pipeline. Each submission gets a
-- feedback number (FB-YYYYMMDD-xxxx); the package (message + checklist +
-- doctor bundle + recent app logs + optional screenshot) is emailed to
-- the maintainer when SMTP is configured, else downloaded by the tester
-- to send manually. (Mirrored in schema.sql for fresh installs.)

CREATE TABLE IF NOT EXISTS feedback_reports (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    number     TEXT NOT NULL,            -- FB-YYYYMMDD-xxxx
    message    TEXT NOT NULL DEFAULT '',
    delivery   TEXT NOT NULL,            -- emailed | downloaded
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, number)
);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['feedback_reports']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    END LOOP;
END $$;
