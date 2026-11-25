-- Every tax or income document stored is now a row of its own, with the
-- figures it wrote, so the Import page can list each one and remove it
-- (specs/tax-documents.md, "Stored documents and what each one wrote").
-- Until now an upload merged its columns into income_annual and left
-- nothing behind but a free-text `source`: a later document overwrote a
-- column silently, and nothing could be taken back.
--
--   income_uploads        one stored document: its kind, file name, when
--                         and by whom; removed_at marks one taken back
--                         (it can be put back for 30 days). A removed
--                         W-2 upload holds its forms in held_documents.
--   income_upload_values  per year, the columns THIS document wrote and
--                         the values it wrote (fields, a JSON object).
--   income_documents.upload_id  the upload a W-2 or other form came in.
--
-- An upload is known by `uid`, set when it is created and carried by an
-- export: a restore finds an upload already here by it and nothing else
-- (two uploads can share their kind, name and moment).
--
-- The figures already stored are given to uploads so that every figure
-- resolves to exactly what it is now (taxdocs.resolve_year; checked by
-- test_backfill_resolves_to_every_stored_figure). They are dated at the
-- epoch plus their id, so any document stored later outranks them and
-- the moment this file ran decides nothing. The column families are
-- disjoint, so they split cleanly:
--   * the earnings record's columns (ss_earnings, medicare_earnings) of
--     every year → one 'legacy_ssa' upload;
--   * a year's return columns → one 'legacy_return' upload for that year,
--     with no file name (the old `source` names no file). A wage figure
--     that still equals what the W-2s last wrote is the W-2s'
--     (specs/w2-wages.md) and is not given to the return;
--   * every stored form → an upload of its own ('w2' for a W-2,
--     'document' for any other form).
-- No figure changes in this file.

CREATE TABLE IF NOT EXISTS income_uploads (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id             BIGINT NOT NULL,
    uid            UUID NOT NULL DEFAULT gen_random_uuid(),
    kind           TEXT NOT NULL CHECK (kind IN ('ssa', 'transcript', '1040',
                       'w2', 'document', 'legacy_ssa', 'legacy_return')),
    filename       TEXT,
    stored_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    stored_by      UUID,
    removed_at     TIMESTAMPTZ,
    removed_by     UUID,
    held_documents JSONB,
    PRIMARY KEY (tenant_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS income_uploads_by_uid
    ON income_uploads (tenant_id, uid);

CREATE TABLE IF NOT EXISTS income_upload_values (
    tenant_id UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    upload_id BIGINT NOT NULL,
    year      INTEGER NOT NULL,
    fields    JSONB NOT NULL,
    PRIMARY KEY (tenant_id, upload_id, year)
);
CREATE INDEX IF NOT EXISTS income_upload_values_by_year
    ON income_upload_values (tenant_id, year);

ALTER TABLE income_documents ADD COLUMN IF NOT EXISTS upload_id BIGINT;
CREATE INDEX IF NOT EXISTS income_documents_by_upload
    ON income_documents (tenant_id, upload_id);

ALTER TABLE income_uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE income_upload_values ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON income_uploads;
CREATE POLICY tenant_isolation ON income_uploads
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));
DROP POLICY IF EXISTS tenant_isolation ON income_upload_values;
CREATE POLICY tenant_isolation ON income_upload_values
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));
GRANT SELECT, INSERT, UPDATE, DELETE ON income_uploads, income_upload_values
    TO oikonome_app, oikonome_admin;

-- a household that already has uploads (this file run again by hand) is
-- left alone: the backfill is for households stored before the list
CREATE TEMP TABLE _bf_tenants ON COMMIT DROP AS
SELECT t.tenant_id FROM (
    SELECT tenant_id FROM income_annual
    UNION SELECT tenant_id FROM income_documents) t
 WHERE NOT EXISTS (SELECT 1 FROM income_uploads u
                    WHERE u.tenant_id = t.tenant_id);

-- the earnings record: one upload (id 1) over every year it filled
INSERT INTO income_uploads (tenant_id, id, kind, stored_at)
SELECT DISTINCT a.tenant_id, 1, 'legacy_ssa',
       'epoch'::timestamptz + interval '1 second'
  FROM income_annual a JOIN _bf_tenants b USING (tenant_id)
 WHERE a.ss_earnings IS NOT NULL OR a.medicare_earnings IS NOT NULL;
INSERT INTO income_upload_values (tenant_id, upload_id, year, fields)
SELECT a.tenant_id, 1, a.year, jsonb_strip_nulls(jsonb_build_object(
           'ss_earnings', a.ss_earnings,
           'medicare_earnings', a.medicare_earnings))
  FROM income_annual a JOIN _bf_tenants b USING (tenant_id)
 WHERE a.ss_earnings IS NOT NULL OR a.medicare_earnings IS NOT NULL;

-- the return columns: one upload per year, numbered after the record's
CREATE TEMP TABLE _bf_returns ON COMMIT DROP AS
SELECT a.tenant_id, a.year,
       1 + row_number() OVER (PARTITION BY a.tenant_id ORDER BY a.year) AS id,
       jsonb_strip_nulls(jsonb_build_object(
           'total_income', a.total_income, 'agi', a.agi,
           'taxable_income', a.taxable_income, 'tax_paid', a.tax_paid,
           'wages', a.wages, 'investment_income', a.investment_income,
           'capital_gain', a.capital_gain,
           'primary_wages', CASE WHEN a.primary_wages IS DISTINCT FROM
                                 a.primary_wages_w2 THEN a.primary_wages END,
           'spouse_wages', CASE WHEN a.spouse_wages IS DISTINCT FROM
                                a.spouse_wages_w2 THEN a.spouse_wages END))
           AS fields
  FROM income_annual a JOIN _bf_tenants b USING (tenant_id)
 WHERE COALESCE(a.total_income, a.agi, a.taxable_income, a.tax_paid,
                a.wages, a.investment_income, a.capital_gain) IS NOT NULL
    OR (a.primary_wages IS NOT NULL
        AND a.primary_wages IS DISTINCT FROM a.primary_wages_w2)
    OR (a.spouse_wages IS NOT NULL
        AND a.spouse_wages IS DISTINCT FROM a.spouse_wages_w2);
INSERT INTO income_uploads (tenant_id, id, kind, stored_at)
SELECT tenant_id, id, 'legacy_return',
       'epoch'::timestamptz + id * interval '1 second' FROM _bf_returns;
INSERT INTO income_upload_values (tenant_id, upload_id, year, fields)
SELECT tenant_id, id, year, fields FROM _bf_returns;

-- every stored form: an upload of its own, numbered after the returns
CREATE TEMP TABLE _bf_forms ON COMMIT DROP AS
SELECT d.tenant_id, d.id AS doc_id,
       COALESCE((SELECT max(id) FROM income_uploads u
                  WHERE u.tenant_id = d.tenant_id), 0)
       + row_number() OVER (PARTITION BY d.tenant_id ORDER BY d.id) AS id,
       CASE WHEN d.form = 'W-2' THEN 'w2' ELSE 'document' END AS kind
  FROM income_documents d JOIN _bf_tenants b USING (tenant_id)
 WHERE d.upload_id IS NULL;
INSERT INTO income_uploads (tenant_id, id, kind, stored_at)
SELECT tenant_id, id, kind, 'epoch'::timestamptz + id * interval '1 second'
  FROM _bf_forms;
UPDATE income_documents d SET upload_id = f.id
  FROM _bf_forms f
 WHERE d.tenant_id = f.tenant_id AND d.id = f.doc_id;
