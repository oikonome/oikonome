-- Deployment history for the admin console: one row per version the
-- migrate step first sees (migrate runs on every install/upgrade, on the
-- admin connection, and knows OIKONOME_VERSION — so "a migrate run with a
-- new version" IS a deployment). Admin-write-only like the audit trail.
CREATE TABLE IF NOT EXISTS deployments (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version     TEXT NOT NULL,
    deployed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    migrations  INT NOT NULL DEFAULT 0     -- migrations applied in that run
);
