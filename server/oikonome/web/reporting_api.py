"""JSON API for the analytics reports (Net Worth / Spending / Cash Flow /
investment fees) — the surface the SPA report pages consume. Session-cookie
auth (current_user); every handler runs on an RLS-scoped tenant connection.

Mount with one line in web/app.py:  app.include_router(reporting_api.router)

Routes (all GET, auth-gated):
  /api/reports/networth   engine.reporting.compute_networth
  /api/reports/spending   engine.reporting.compute_spending
  /api/reports/cashflow   engine.reporting.compute_cashflow
  /api/reports/cashflow/flow?range=…   engine.reporting.flow_breakdown
  /api/reports/fees       engine.reporting.compute_fees

Each payload carries a `_built_at` key. Reports are not materialized: they
compute on demand (Postgres is fast enough), so `_built_at` is the compute
time.

Nightly job (jobs/worker wiring, one line in nightly sweep):
  snapshot_tenant(tenant_id)  — records today's exact net worth into
  `networth_snapshot` (the REAL trend accrues one point per night, so it
  must run from day one or the recorded series gets a hole).
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException

from ..db import tenancy
from ..engine import reporting
from .security import limit

router = APIRouter(prefix="/api/reports")

# Every one of these four recomputes its whole report from the ledger on
# each GET — there is no report_cache to serve from here — and the handlers
# are sync, so each request holds a shared threadpool worker for the length
# of that scan. The pages behind them are navigated, not polled (one fetch
# per visit, plus one per owner lens on Net Worth), so a shared per-IP
# budget an order of magnitude above that is invisible to a household and
# still bounds what a scripted loop can make one worker do.
_REPORT_LIMIT = [Depends(limit("reports", 60, 60))]


def _user():
    # late import: avoids app<->router circularity (same pattern as web/api.py)
    from .app import current_user
    return current_user


def _report(name: str, user: dict) -> dict:
    fn = reporting.REPORTS.get(name)
    if fn is None:
        raise HTTPException(404, "unknown report")
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        data = fn(conn, today=_household_today(conn))
    finally:
        conn.close()
    data["_built_at"] = dt.datetime.now().isoformat(timespec="seconds")
    return data


def _household_today(conn) -> dt.date:
    """The household's own day, like every other live surface: a report
    built at 6pm Pacific on the 31st must not think it is the 1st because
    the container's clock says so."""
    from .. import localtime
    from ..engine import budget
    return localtime.now_local(budget.load_config(conn)).date()


@router.get("/networth", dependencies=_REPORT_LIMIT)
def networth(user: dict = Depends(_user()), owner: str = ""):
    # an owner lens shows only that owner's live accounts (property +
    # historical trend are household-level and omitted for an owner view).
    if not owner:
        return _report("networth", user)
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        data = reporting.compute_networth(conn, owner=owner,
                                          today=_household_today(conn))
    finally:
        conn.close()
    data["_built_at"] = dt.datetime.now().isoformat(timespec="seconds")
    return data


@router.get("/spending", dependencies=_REPORT_LIMIT)
def spending(user: dict = Depends(_user())):
    return _report("spending", user)


@router.get("/cashflow", dependencies=_REPORT_LIMIT)
def cashflow(user: dict = Depends(_user())):
    return _report("cashflow", user)


@router.get("/cashflow/flow", dependencies=_REPORT_LIMIT)
def cashflow_flow(range: str = "1y", user: dict = Depends(_user())):
    """One timeframe of the Cash Flow flow picture. Served per range —
    not inside the report — because the fixed split walks month_status
    month by month and 'all' over a decade costs seconds nobody should
    pay to see the default window. The client caches each range it asks
    for, so a toggle is one fetch ever."""
    return _windowed(reporting.flow_breakdown, range, user)


def _windowed(fn, range_key: str, user: dict) -> dict:
    """spending_window / income_window over one validated site-wide range."""
    if range_key not in reporting.FLOW_RANGES:
        raise HTTPException(400, "unknown range")
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        data = fn(conn, _household_today(conn), range_key)
    finally:
        conn.close()
    data["_built_at"] = dt.datetime.now().isoformat(timespec="seconds")
    return data


@router.get("/spending/window", dependencies=_REPORT_LIMIT)
def spending_window(range: str = "1y", user: dict = Depends(_user())):
    return _windowed(reporting.spending_window, range, user)


@router.get("/income/window", dependencies=_REPORT_LIMIT)
def income_window(range: str = "1y", user: dict = Depends(_user())):
    return _windowed(reporting.income_window, range, user)


@router.get("/fees", dependencies=_REPORT_LIMIT)
def fees(user: dict = Depends(_user())):
    return _report("fees", user)


# ---- nightly snapshot job (jobs-callable; sync body, worker-thread safe) ----


def snapshot_tenant(tenant_id: str) -> dict:
    """Record today's exact net worth for one tenant. Call from the
    worker's nightly sweep: `_sweep(snapshot_tenant, …)`
    or inside nightly_tenant."""
    from ..engine import alerts
    conn = tenancy.tenant_connect(tenant_id)
    try:
        # two doors reach this (the nightly cron and the console's
        # run-job); the single-flight try-lock the nightly sweep itself
        # takes keeps a second entrant from recording the same night twice.
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:snapshot:{tenant_id}",)).fetchone()["ok"]
        if not locked:
            return {"snapshot": False, "_status": "already-running"}
        try:
            reporting.snapshot_networth(conn)
            alerts.heartbeat(conn, "networth-snapshot", "recorded")
            return {"snapshot": True}
        finally:
            tenancy.release_lock(conn, f"oikonome:snapshot:{tenant_id}")
    finally:
        conn.close()
