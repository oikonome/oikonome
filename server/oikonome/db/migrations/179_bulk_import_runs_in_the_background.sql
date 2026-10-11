-- A folder import runs as a background job instead of inside the request
-- that submits it.
--
-- Run inside one POST, two hundred statements held a web worker, a
-- database connection and the household's sync lock for as long as they
-- took, which on PDFs is hours; the edge gave up long before, so the
-- person read a failure while the import went on landing, and a retry
-- found the upload already spent. Now the submit freezes the plan in
-- bulk_import_jobs, moves the staged bytes into bulk_import_files (one row
-- per file, keyed by the file's position in the original upload, never
-- renumbered), and returns. The worker runs the files one at a time and
-- writes each file's outcome back; the clients poll it.
--
-- The bytes are the files as uploaded, like import_staging, and are
-- cleared from each row the moment that file has run (and from every row
-- when a job is cancelled, stopped or fails), so a job holds only the
-- files it has not reached yet. A household has at most one live job.
-- Both tables are import scratch and job bookkeeping: they never travel in
-- an export. Mirrored in schema.sql.

CREATE TABLE IF NOT EXISTS bulk_import_jobs (
    tenant_id   UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id          UUID NOT NULL DEFAULT gen_random_uuid(),
    stage_token_hash TEXT NOT NULL,             -- sha256 of the analyze token
    state       TEXT NOT NULL DEFAULT 'queued'
        CONSTRAINT bulk_import_jobs_state_known CHECK (state IN
            ('queued', 'running', 'cancelling', 'cancelled', 'stopped',
             'done', 'error')),
    stop_reason TEXT,
    starter_user_id UUID,
    starter_cred    TEXT,                       -- the starting credential's fingerprint
    starter_via     TEXT,                       -- app | token
    decisions   JSONB NOT NULL,                 -- the plan, frozen at submit
    notes       JSONB NOT NULL DEFAULT '[]'::jsonb,  -- outcomes settled at submit (archives)
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    rows_total  BIGINT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    cancel_requested_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, stage_token_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS bulk_import_jobs_one_live
    ON bulk_import_jobs (tenant_id)
    WHERE state IN ('queued', 'running', 'cancelling');

CREATE TABLE IF NOT EXISTS bulk_import_files (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    job_id     UUID NOT NULL,
    file_id    INTEGER NOT NULL,                -- the staged file's idx
    filename   TEXT,
    data       BYTEA,                           -- NULL once the file has run
    size       INTEGER NOT NULL DEFAULT 0,
    state      TEXT NOT NULL DEFAULT 'pending'
        CONSTRAINT bulk_import_files_state_known CHECK (state IN
            ('pending', 'running', 'done', 'failed', 'skipped')),
    result     JSONB,
    PRIMARY KEY (tenant_id, job_id, file_id),
    FOREIGN KEY (tenant_id, job_id) REFERENCES bulk_import_jobs (tenant_id, id)
        ON DELETE CASCADE
);

ALTER TABLE bulk_import_jobs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON bulk_import_jobs;
CREATE POLICY tenant_isolation ON bulk_import_jobs
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));

ALTER TABLE bulk_import_files ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON bulk_import_files;
CREATE POLICY tenant_isolation ON bulk_import_files
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));
