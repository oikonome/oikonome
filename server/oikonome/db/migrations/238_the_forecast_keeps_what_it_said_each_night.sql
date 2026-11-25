-- What the cash forecast said each night: forecast_nights, one row per
-- household per household-local day, written once by the nightly forecast
-- step (engine/forecast_score.record) and never back-filled. It holds the
-- checking account the walk started from and its balance, each
-- scenario's predicted balances 7 to 60 days ahead, its low point and the
-- card payments it assumed (`walks`, whole cents), the pace (`pace`), and
-- for 120 nights the inputs that cannot be rebuilt later (`inputs`, set
-- to NULL after that). `fresh` says the checking balance had been pulled
-- in the 36 hours before; `restored` marks a row a restore landed; `score`
-- is the grading as of that night, recomputed when absent. A later night's
-- starting balance is the real balance an earlier prediction is graded
-- against (specs/forecast-grading.md). Rows go after 400 nights.
--
-- The household's own record: exported and restored (restored rows are
-- graded only against each other), gone with the household. Every read is
-- the household's rows by day, which the primary key serves. Mirrored in
-- schema.sql.

CREATE TABLE IF NOT EXISTS forecast_nights (
    tenant_id      UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    as_of          DATE NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    model          TEXT NOT NULL,
    checking_id    TEXT NOT NULL,
    start_checking DOUBLE PRECISION NOT NULL,
    fresh          BOOLEAN NOT NULL,
    restored       BOOLEAN NOT NULL DEFAULT false,
    walks          JSONB NOT NULL
        CONSTRAINT forecast_nights_walks_is_an_object
            CHECK (jsonb_typeof(walks) = 'object'),
    pace           JSONB NOT NULL,
    inputs         JSONB,
    score          JSONB,
    PRIMARY KEY (tenant_id, as_of)
);

ALTER TABLE forecast_nights ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON forecast_nights;
CREATE POLICY tenant_isolation ON forecast_nights
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));

GRANT SELECT, INSERT, UPDATE, DELETE ON forecast_nights
    TO oikonome_app, oikonome_admin;
