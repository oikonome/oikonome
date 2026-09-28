-- in-app guided testing: per-step tester feedback
CREATE TABLE IF NOT EXISTS testing_feedback (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    step       TEXT NOT NULL,
    status     TEXT NOT NULL,            -- pass | issue | skip
    note       TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, step)
);
ALTER TABLE testing_feedback ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON testing_feedback;
CREATE POLICY tenant_isolation ON testing_feedback
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
