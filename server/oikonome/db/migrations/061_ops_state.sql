-- a tiny control-plane key/value for operator-facing background state:
-- a periodic job that alerts on a threshold must remember the level it
-- last alerted on, so it mails on TRANSITIONS rather than every run (an
-- alert every 15 minutes is an alert nobody reads).
--
-- Deliberately its own table: `broadcast` cannot stand in for it — that
-- table has CHECK (id = 1), so a second writer's row silently no-ops and
-- the watcher re-alerts forever.
CREATE TABLE IF NOT EXISTS ops_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
