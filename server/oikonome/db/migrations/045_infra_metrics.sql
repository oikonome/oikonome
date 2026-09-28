-- Infra-stress metric history for the admin console's load panel.
-- One row per worker sample (~60s); ring-buffer retention (the
-- sampler DELETEs rows older than 14 days). Control plane, no RLS — the
-- metrics describe the BOX, not a tenant.
--
-- Every metric column is nullable ON PURPOSE: a missing source (redis
-- down, host-health.json absent, no web traffic yet) records NULL, never
-- crashes the sampler — and the console renders NULL as "unknown".
CREATE TABLE IF NOT EXISTS infra_metrics (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- postgres (pg_stat_activity, current database; a managed database
    -- caps connections, so this is read against that ceiling)
    pg_total_conns  INTEGER,
    pg_active       INTEGER,
    pg_idle         INTEGER,
    -- web-process psycopg_pool stats (published to redis by the request
    -- middleware; the worker can't see the web process's memory directly)
    pool_size       INTEGER,
    pool_available  INTEGER,
    pool_waiting    INTEGER,
    pool_timeouts   BIGINT,      -- CUMULATIVE since web-process start; the
                                 -- console takes deltas (reset ⇒ clamp to 0)
    -- request timings (recent /api p95 from the middleware's ring buffer)
    api_p95_ms      REAL,
    api_req_count   INTEGER,
    -- host (shared kernel /proc + host-health.json via /state/ops)
    host_load1      REAL,
    host_cpus       INTEGER,
    mem_used_gb     REAL,
    mem_total_gb    REAL,
    disk_used_pct   INTEGER,
    containers      JSONB,       -- per-container cpu/mem from host-health.sh
    redis_mem_mb    REAL
);

CREATE INDEX IF NOT EXISTS infra_metrics_ts ON infra_metrics (ts);

-- sampler writes and console reads both run on the admin connection; the
-- app role only ever needs to read (nothing does today) — an injected
-- app-role write must not be able to fake or scrub the stress history
REVOKE INSERT, UPDATE, DELETE ON infra_metrics FROM oikonome_app;
