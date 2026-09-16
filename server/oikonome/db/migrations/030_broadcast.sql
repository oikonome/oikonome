-- operator broadcast — one message every user of the instance sees as an
-- in-app banner ("maintenance tonight 9pm"). Single-row table; set/clear
-- from the admin console (admin-write-only; the app role reads it for
-- /api/me).
CREATE TABLE IF NOT EXISTS broadcast (
    id       INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    message  TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',   -- info | warn
    set_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
