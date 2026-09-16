-- A month's budget, frozen as the month closes. The settings blob is one
-- mutable document, so without a snapshot every past month is re-judged
-- against TODAY's budget — raise the budget in April and March's verdict
-- quietly flips. The nightly job upserts the CURRENT month's config here and never
-- touches earlier months, so the last write before the month turns is the
-- budget that month actually ran under; the lenses judge closed months
-- against it. A closed month with no row predates the product (or predates
-- this table) and is shown as net income vs spending, not a budget verdict.
-- (Mirrored in schema.sql.)

CREATE TABLE IF NOT EXISTS budget_snapshots (
    tenant_id   UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    year        INT NOT NULL,
    month       INT NOT NULL CHECK (month BETWEEN 1 AND 12),
    config      JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, year, month)
);

DO $$
BEGIN
    EXECUTE 'ALTER TABLE budget_snapshots ENABLE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON budget_snapshots';
    EXECUTE
        'CREATE POLICY tenant_isolation ON budget_snapshots '
        'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
        'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)';
END $$;
