"""Infra-stress sampler — one row per minute into infra_metrics.

Records the pressure signals the console's thresholds classify
(connections, pool timeouts, /api p95), so a spike is visible live in
the console AND reconstructable the next day.

Sources, each individually guarded (a missing source records NULL and
NEVER crashes the sample — the row itself is the heartbeat, and a gap in
rows is what the console renders as "sampler down"):

  * pg_stat_activity  — connection count by state (admin connection)
  * redis summary     — the web process's request p95 + psycopg_pool
                        stats incl. the cumulative PoolTimeout counter
                        (published by web/reqmetrics.py; the worker can't
                        see the web process's memory)
  * /proc             — host load + memory (shared kernel: /proc in the
                        container reflects the host)
  * host-health.json  — disk % + per-container cpu/mem (host timer runs
                        scripts/host-health.sh into /state/ops — the
                        existing root channel, reused, not duplicated)
  * redis INFO        — redis memory

Retention rides the same job: rows older than RETENTION_DAYS are deleted
after each insert, so the table is a self-trimming ring buffer.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os

from ..db import tenancy

log = logging.getLogger("oikonome.jobs")

RETENTION_DAYS = 14
SAMPLE_EVERY_S = 60          # the cron cadence; the console's gap threshold
#                              derives from this (3 missed samples = gap)

# ---- trigger thresholds (single source of truth: the console's
# gauge colors and the history spike markers both classify through these) --
THRESHOLDS = {
    # metric: (amber, red) — value >= level trips it
    "pg_total_conns": (16, 21),      # absolute fallback — only for rows
                                     # that never recorded the server's
                                     # own max_connections
    "pg_conn_pct": (64.0, 84.0),     # share of the server's own
                                     # max_connections
    "api_p95_ms": (1000.0, 2000.0),  # >1s amber, >2s red
    "pool_timeout_delta": (1, 1),    # ANY new PoolTimeout since last sample
    "mem_pct": (80.0, 90.0),
    "load_per_cpu": (0.7, 1.0),
    "disk_used_pct": (80, 90),
}


# A p95 is only a capacity signal when there is a crowd behind it. The web
# process publishes the p95 of the last 5 minutes, so one 3-second export
# sits in five consecutive samples as
# a "sustained" >2s p95 with a request count of 1 — and the operator gets an
# amber mail about a box nobody was using. Under fewer requests than this
# per window the p95 is a slow endpoint, not pressure, and the chart still
# shows it; it just does not colour the verdict.
P95_MIN_REQS = 20


def pg_conn_lines(max_conns) -> tuple[float, float]:
    """The absolute (amber, red) connection counts for a server whose cap
    is `max_conns` — what the chart draws as guide lines. Falls back to the
    absolute constants when the cap was never sampled."""
    if not max_conns:
        return THRESHOLDS["pg_total_conns"]
    a, r = THRESHOLDS["pg_conn_pct"]
    return (max_conns * a / 100.0, max_conns * r / 100.0)


def _level(metric: str, value) -> str | None:
    """'red' / 'amber' / None for one metric value (None value = None)."""
    if value is None:
        return None
    amber, red = THRESHOLDS[metric]
    if value >= red:
        return "red"
    if value >= amber:
        return "amber"
    return None


def breaches(row: dict, prev_timeouts: int | None = None) -> dict[str, str]:
    """Which triggers this sample trips, at what level. `row` is an
    infra_metrics row (dict); `prev_timeouts` is the previous sample's
    cumulative pool_timeouts (for the delta — a web-process restart makes
    the counter drop, which clamps to 0, never a phantom breach)."""
    out: dict[str, str] = {}
    # Connections are scored against the server's OWN cap when the sample
    # carries it, so the same count reads differently on a small database
    # and a large one. Rows without a sampled cap use the absolute fallback.
    total = row.get("pg_total_conns")
    if total is not None:
        mx = row.get("pg_max_conns")
        lv = (_level("pg_conn_pct", 100.0 * total / mx) if mx
              else _level("pg_total_conns", total))
        if lv:
            out["pg_total_conns"] = lv
    p95 = row.get("api_p95_ms")
    n = row.get("api_req_count")
    if p95 is not None and (n is None or n >= P95_MIN_REQS):
        lv = _level("api_p95_ms", p95)
        if lv:
            out["api_p95_ms"] = lv
    lv = _level("disk_used_pct", row.get("disk_used_pct"))
    if lv:
        out["disk_used_pct"] = lv
    if row.get("pool_timeouts") is not None and prev_timeouts is not None:
        delta = max(row["pool_timeouts"] - prev_timeouts, 0)
        lv = _level("pool_timeout_delta", delta)
        if lv:
            out["pool_timeout_delta"] = lv
    if row.get("mem_used_gb") is not None and row.get("mem_total_gb"):
        lv = _level("mem_pct",
                    100.0 * row["mem_used_gb"] / row["mem_total_gb"])
        if lv:
            out["mem_pct"] = lv
    if row.get("host_load1") is not None and row.get("host_cpus"):
        lv = _level("load_per_cpu", row["host_load1"] / row["host_cpus"])
        if lv:
            out["load_per_cpu"] = lv
    return out


# ---- sources (module attributes so tests can monkeypatch each one) --------


def _pg_stats() -> dict:
    admin = tenancy.admin_connect()
    try:
        row = admin.execute(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE state = 'active') AS active,
                      count(*) FILTER (WHERE state = 'idle') AS idle,
                      current_setting('max_connections')::int AS max_conns
               FROM pg_stat_activity
               WHERE datname = current_database()""").fetchone()
        return {"pg_total_conns": row["total"], "pg_active": row["active"],
                "pg_idle": row["idle"], "pg_max_conns": row["max_conns"]}
    finally:
        admin.close()


def _req_stats() -> dict:
    """The web process's published summary (reqmetrics). Absent/expired
    key = NULLs: no recent web traffic is not an error."""
    from ..web import reqmetrics
    s = reqmetrics.read_summary()
    if not s:
        return {}
    return {"api_p95_ms": s.get("p95_ms"), "api_req_count": s.get("count"),
            "pool_size": s.get("pool_size"),
            "pool_available": s.get("pool_available"),
            "pool_waiting": s.get("pool_waiting"),
            "pool_timeouts": s.get("pool_timeouts")}


def _host_stats() -> dict:
    out: dict = {}
    with open("/proc/loadavg") as f:
        out["host_load1"] = float(f.read().split()[0])
    out["host_cpus"] = os.cpu_count()
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            mem[k] = v.strip()
    total = int(mem["MemTotal"].split()[0]) / 1024 / 1024
    avail = int(mem["MemAvailable"].split()[0]) / 1024 / 1024
    out["mem_used_gb"] = round(total - avail, 2)
    out["mem_total_gb"] = round(total, 2)
    return out


def _host_health() -> dict:
    """disk % + per-container cpu/mem from the host-health.sh snapshot
    (the console's existing root channel — /state/ops, read-only bind)."""
    from pathlib import Path
    hh = (Path(os.environ.get("OIKONOME_STATE_DIR", "/state"))
          / "ops" / "host-health.json")
    if not hh.is_file():
        return {}
    health = json.loads(hh.read_text())
    out: dict = {}
    if health.get("disk_used_pct") is not None:
        out["disk_used_pct"] = int(health["disk_used_pct"])
    if health.get("container_stats"):
        out["containers"] = health["container_stats"]
    return out


def _redis_mem() -> dict:
    import redis as _r
    r = _r.from_url(os.environ.get("REDIS_URL",
                                   "redis://localhost:6379/0"),
                    socket_connect_timeout=2, socket_timeout=2)
    return {"redis_mem_mb": round(r.info("memory")["used_memory"] / 1e6, 2)}


_SOURCES = ("_pg_stats", "_req_stats", "_host_stats", "_host_health",
            "_redis_mem")

_COLUMNS = ("pg_total_conns", "pg_active", "pg_idle", "pg_max_conns",
            "pool_size",
            "pool_available", "pool_waiting", "pool_timeouts", "api_p95_ms",
            "api_req_count", "host_load1", "host_cpus", "mem_used_gb",
            "mem_total_gb", "disk_used_pct", "containers", "redis_mem_mb")


def sample() -> dict:
    """Collect one sample, insert the row, trim the ring buffer. Sync +
    idempotent-ish (a duplicate minute is just a second row). Returns the
    collected values (tests read them)."""
    import sys
    mod = sys.modules[__name__]
    vals: dict = {}
    for name in _SOURCES:
        try:
            vals.update(getattr(mod, name)() or {})
        except Exception as e:                            # noqa: BLE001
            log.warning("infra sampler source %s failed: %s", name, e)
    row = {c: vals.get(c) for c in _COLUMNS}
    from ..engine.compat import jsonb
    if row["containers"] is not None:
        row["containers"] = jsonb(row["containers"])
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            f"INSERT INTO infra_metrics ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join(['%s'] * len(_COLUMNS))})",
            tuple(row[c] for c in _COLUMNS))
        admin.execute(
            "DELETE FROM infra_metrics WHERE ts < now() - %s",
            (dt.timedelta(days=RETENTION_DAYS),))
    finally:
        admin.close()
    return row
