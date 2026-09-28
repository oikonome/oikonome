-- The categorizer's learned overlay: a small per-tenant model retrained
-- nightly from this household's OWN corrections, so a fixed merchant
-- teaches the classifier about the next lookalike merchant instead of
-- only that one name. The artifact lives in the database on purpose:
-- the /models mount is deployment-provided and read-only, a tenant's
-- corrections are tenant data (RLS applies, backups and restore carry
-- it), and the artifact is small — TF-IDF n-grams over a few hundred
-- merchant strings, hundreds of KB. The shipped /models artifact stays
-- the floor; this overlay answers first and abstains the same way.
-- (Mirrored in schema.sql.)

CREATE TABLE IF NOT EXISTS categorizer_model (
    tenant_id   UUID PRIMARY KEY DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    artifact    BYTEA NOT NULL,
    trained_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    samples     INT NOT NULL,
    classes     INT NOT NULL,
    -- the exact library that wrote the pickle: a version-skewed artifact
    -- can load and still fail at predict, and this names the culprit
    sklearn_version TEXT NOT NULL DEFAULT ''
);

DO $$
BEGIN
    EXECUTE 'ALTER TABLE categorizer_model ENABLE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON categorizer_model';
    EXECUTE
        'CREATE POLICY tenant_isolation ON categorizer_model '
        'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
        'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)';
END $$;
