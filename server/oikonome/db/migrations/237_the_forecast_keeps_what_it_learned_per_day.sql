-- What the cash forecast learned from history, kept per household per
-- day: forecast_learned. The forecast's learned facts (the usual pace, the
-- heavier-month factor, each card's timing and the payday habit, every
-- bill's past payments and paying account) are a pure function of the
-- ledger as it stood at the end of yesterday and the day itself
-- (specs/today-figures.md, "What the forecast learned is yesterday's").
-- They were kept only in each worker's memory, so a cold worker or a new
-- day rebuilt them, at the cost of seconds on a long ledger, and each
-- worker held its own copy. The nightly forecast step writes the day's row
-- for every household; the first build of a day writes it when it is
-- missing or its stamp no longer matches the history (a write dated
-- before today), and every build reads that one row.
--
-- `stamp` is forecast.history_stamp as the facts were computed under;
-- `rule_version` is forecast.LEARNED_RULE_VERSION, so a row a build with
-- other rules wrote is never read; `written_by` names the process that
-- wrote it. `facts` is the forecast's own encoding of each named fact
-- (forecast.encode_learned), stored as json, not jsonb, so its order and
-- every number come back exactly as written.
--
-- Derived data: never exported, never restored, not mirrored between
-- instances, gone with the household; a household keeps its newest few
-- days only (the writer deletes the older ones). Mirrored in schema.sql.

CREATE TABLE IF NOT EXISTS forecast_learned (
    tenant_id    UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    as_of        DATE NOT NULL,
    rule_version INTEGER NOT NULL,
    stamp        TEXT NOT NULL,
    written_by   TEXT NOT NULL DEFAULT '',
    facts        JSON NOT NULL
        CONSTRAINT forecast_learned_facts_is_an_object
            CHECK (json_typeof(facts) = 'object'),
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, as_of)
);

ALTER TABLE forecast_learned ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON forecast_learned;
CREATE POLICY tenant_isolation ON forecast_learned
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));

GRANT SELECT, INSERT, UPDATE, DELETE ON forecast_learned
    TO oikonome_app, oikonome_admin;
