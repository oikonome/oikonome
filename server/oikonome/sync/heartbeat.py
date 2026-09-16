"""Push-heartbeats for host-side collector scripts.

Every product sync door stamps a row here on a successful push, so any
script that writes through a door is observable for free; raw-SQL
container-exec bridges call stamp themselves (one line). Doctor
renders staleness from these rows; the worker's nightly sweep sends the
opt-in edge-triggered stale email.

expected_hours: NULL = undecided, 0 = never warn, N = watched. A source
auto-watches at 24h the first time it pushes on a later calendar day
within 3 days of its previous push — daily-ish recurring behavior
observed — so nightly collectors get staleness tracking with zero
config while one-time and occasional manual imports stay quiet forever.
"""

from __future__ import annotations

import contextvars

# past-due factor: a watched source is stale once it has missed 2 full
# expected intervals (nightly script silent >48h)
STALE_FACTOR = 2


# Interactive UI imports go through the same doors scripts push through,
# so a couple of one-time CSV uploads within 3 days would auto-watch the
# source and leave Doctor warning about a "stale collector" forever. The
# import endpoints flip this off for session users; script-token and
# container-exec pushes keep watching.
AUTO_WATCH = contextvars.ContextVar("heartbeat_auto_watch", default=True)

# HOW the push arrived, shown next to each Doctor heartbeat —
# 'token' (script token over HTTP), 'app' (a signed-in upload), or the
# default 'exec' (a container-exec bridge calling stamp() directly,
# which is the only path that never goes through a route).
PUSH_VIA = contextvars.ContextVar("heartbeat_push_via", default="exec")

# the collection half's kind (api | scrape), as declared by the
# collector's payload — the server can't know how data was gathered
# host-side unless told. None = not declared (kept, never wiped).
PUSH_KIND = contextvars.ContextVar("heartbeat_push_kind", default=None)


def stamp(conn, source: str, rows: int | None = None,
          label: str | None = None) -> None:
    """Record a successful push from `source`. Upsert: refreshes
    last_push/last_rows, keeps the freshest non-null label, clears the
    alert edge, and auto-watches on the second-day push (script pushes
    only — see AUTO_WATCH)."""
    watch_case = ("""CASE
                   WHEN script_heartbeats.expected_hours IS NULL
                        AND script_heartbeats.last_push::date < now()::date
                        AND script_heartbeats.last_push > now() - interval '3 days'
                   THEN 24
                   ELSE script_heartbeats.expected_hours END"""
                  if AUTO_WATCH.get()
                  else "script_heartbeats.expected_hours")
    conn.execute(
        f"""INSERT INTO script_heartbeats (source, label, last_rows, via,
                                          kind)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (tenant_id, source) DO UPDATE SET
               label = COALESCE(EXCLUDED.label, script_heartbeats.label),
               last_rows = EXCLUDED.last_rows,
               expected_hours = {watch_case},
               last_push = now(),
               via = EXCLUDED.via,
               kind = COALESCE(EXCLUDED.kind, script_heartbeats.kind),
               alerted_at = NULL""",
        (source, label, rows, PUSH_VIA.get(), PUSH_KIND.get()))


def overdue(conn) -> list[dict]:
    """Watched sources past STALE_FACTOR × their expected interval,
    stalest first."""
    return conn.execute(
        """SELECT source, label, last_push, last_rows, expected_hours,
                  alerts, alerted_at,
                  EXTRACT(EPOCH FROM now() - last_push)/3600 AS age_hours
           FROM script_heartbeats
           WHERE COALESCE(expected_hours, 0) > 0
             AND last_push < now() - make_interval(
                     hours => expected_hours * %s)
           ORDER BY last_push""", (STALE_FACTOR,)).fetchall()


def revoked_tokens(conn) -> dict[str, "object"]:
    """{source: revoked_at} for every watched source whose last push is
    older than a revocation of a token named for it, with no live
    replacement — the predicate the Accounts page shows as a red dot.
    Shared here so Doctor and the daily email say the same thing: a
    credential change that revokes every script token is otherwise visible
    on Accounts only, and a household that does not open that page learns
    its collectors are dead from the unmatched charges."""
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]
    if not tid:
        return {}
    rows = conn.execute(
        r"""SELECT h.source,
                  (SELECT max(t.revoked_at) FROM api_tokens t
                    WHERE t.tenant_id = %s::uuid
                      AND t.revoked_at > h.last_push
                      AND (lower(t.name) = lower(h.source)
                           OR lower(t.name) LIKE
                              replace(replace(lower(h.source), '_', '\_'),
                                      '%%', '\%%') || '-%%' ESCAPE '\')
                      AND NOT EXISTS (
                        SELECT 1 FROM api_tokens l
                         WHERE l.tenant_id = t.tenant_id
                           AND l.revoked_at IS NULL
                           AND (lower(l.name) = lower(h.source)
                                OR lower(l.name) LIKE
                                   replace(replace(lower(h.source), '_', '\_'),
                                           '%%', '\%%') || '-%%' ESCAPE '\')))
                  AS revoked_at
             FROM script_heartbeats h
            WHERE COALESCE(h.expected_hours, 0) > 0""", (tid,)).fetchall()
    return {r["source"]: r["revoked_at"] for r in rows if r["revoked_at"]}


def panel(conn) -> list[dict]:
    """Every heartbeat row for the Doctor panel, freshest first, with a
    computed status: fresh | stale | off (unwatched)."""
    return conn.execute(
        """SELECT source, label, first_push, last_push, last_rows,
                  expected_hours, alerts, via, kind,
                  EXTRACT(EPOCH FROM now() - last_push)/3600 AS age_hours,
                  CASE
                      WHEN COALESCE(expected_hours, 0) = 0 THEN 'off'
                      WHEN last_push < now() - make_interval(
                               hours => expected_hours * %s) THEN 'stale'
                      ELSE 'fresh' END AS status
           FROM script_heartbeats
           ORDER BY last_push DESC""", (STALE_FACTOR,)).fetchall()
