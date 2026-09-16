"""Request-timing accumulator — the honest p95 source.

The infra-stress sampler runs in the WORKER process, but request
durations and psycopg_pool stats live in the WEB process's memory. The
cheapest honest cross-process channel already in the stack is Redis: the
timing middleware keeps a small in-process ring of recent /api request
durations and, at most every PUBLISH_EVERY seconds, publishes a compact
JSON summary (p95 over the ring, request count, pool stats including the
cumulative PoolTimeout-ish `requests_errors` counter) under a short TTL.
The sampler reads that key; a missing/expired key records NULL — a web
process that served nothing recently has no p95 to claim.

Everything here is best-effort: a Redis outage or a stats failure must
never slow or fail a request.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware

REDIS_KEY = "oikonome:req-metrics"
KEY_TTL = 180          # seconds — stale summaries expire into honest NULLs
PUBLISH_EVERY = 10     # seconds between Redis publishes (per web process)
RING_SIZE = 512        # recent /api requests kept for the percentile
WINDOW_S = 300         # only durations younger than this feed the p95

_ring: deque[tuple[float, float]] = deque(maxlen=RING_SIZE)  # (ts, ms)
_ring_lock = threading.Lock()
_last_publish = 0.0


def _redis():
    import redis as _r
    return _r.from_url(os.environ.get("REDIS_URL",
                                      "redis://localhost:6379/0"),
                       socket_connect_timeout=2, socket_timeout=2)


def _pool_stats() -> dict:
    """Aggregate psycopg_pool stats across this process's pools — the
    tenant pools AND the control-plane pool. `requests_errors` counts
    checkout failures — in this configuration that is PoolTimeout (the
    knee signal, and on the control pool the 503 load-shed trigger).
    Cumulative since process start; consumers take deltas.

    The pool_* columns are the tenant+control SUM (the infra_metrics
    schema predates the control pool and its columns stay as-is); the
    control pool's own numbers ride the summary as cpool_* keys — no DB
    column, but live consumers of the Redis summary can tell which pool
    is under pressure."""
    from ..db import tenancy
    out = {"pool_size": 0, "pool_available": 0, "pool_waiting": 0,
           "pool_timeouts": 0}
    pools = list(tenancy._pools.values())
    cpools = list(tenancy._control_pools.values())
    if not pools and not cpools:
        return {k: None for k in out}
    for p in pools + cpools:
        s = p.get_stats()
        out["pool_size"] += s.get("pool_size", 0)
        out["pool_available"] += s.get("pool_available", 0)
        out["pool_waiting"] += s.get("requests_waiting", 0)
        out["pool_timeouts"] += s.get("requests_errors", 0)
    if cpools:
        c = {"cpool_size": 0, "cpool_waiting": 0, "cpool_timeouts": 0}
        for p in cpools:
            s = p.get_stats()
            c["cpool_size"] += s.get("pool_size", 0)
            c["cpool_waiting"] += s.get("requests_waiting", 0)
            c["cpool_timeouts"] += s.get("requests_errors", 0)
        out.update(c)
    return out


def _p95(now: float) -> tuple[float | None, int]:
    with _ring_lock:
        vals = sorted(ms for ts, ms in _ring if now - ts <= WINDOW_S)
    if not vals:
        return None, 0
    idx = max(0, int(len(vals) * 0.95) - 1) if len(vals) > 1 else 0
    return vals[idx], len(vals)


def _publish(now: float) -> None:
    p95, n = _p95(now)
    payload = {"at": now, "p95_ms": p95, "count": n}
    payload.update(_pool_stats())
    _redis().set(REDIS_KEY, json.dumps(payload), ex=KEY_TTL)


def _publish_bg(now: float) -> None:
    try:
        _publish(now)
    except Exception:                                     # noqa: BLE001
        pass


def record(duration_ms: float) -> None:
    """Add one request duration and maybe publish. Never raises, never
    blocks. The publish issues a SYNCHRONOUS redis SET (up to ~4 s against
    a hung/degraded redis) and record() is called inline from the async
    request middleware, so doing it here would freeze the whole event loop
    for that window — exactly when the p95 telemetry exists to observe a
    Redis brown-out. The SET runs on a short-lived daemon thread so a
    degraded Redis never stalls the loop; at most one thread per
    PUBLISH_EVERY per process."""
    global _last_publish
    now = time.time()
    with _ring_lock:
        _ring.append((now, duration_ms))
    if now - _last_publish < PUBLISH_EVERY:
        return
    _last_publish = now             # even on failure: don't hammer a dead redis
    threading.Thread(target=_publish_bg, args=(now,), daemon=True).start()


def read_summary() -> dict | None:
    """The sampler's side: the latest published summary, or None (no web
    traffic recently / redis down). Never raises."""
    try:
        raw = _redis().get(REDIS_KEY)
        return json.loads(raw) if raw else None
    except Exception:                                     # noqa: BLE001
        return None


class RequestTimingMiddleware(BaseHTTPMiddleware):
    """Times /api requests only — the trigger is '/api p95 >2s', and
    static/SPA asset serving would only dilute the signal."""

    async def dispatch(self, request, call_next):
        if not request.url.path.startswith("/api"):
            return await call_next(request)
        t0 = time.perf_counter()
        try:
            return await call_next(request)
        finally:
            try:
                record((time.perf_counter() - t0) * 1000.0)
            except Exception:                             # noqa: BLE001
                pass
