"""Outbound integrations for a household running its own instance.

Three doors, one picture. A household that runs Oikonome on its own box
usually runs other things beside it — a dashboard, a home-automation
hub, an assistant — and wants the books in them without giving any of
them the password. So:

  * **The read endpoints** (`/api/integrations/*`): a bounded, read-only
    JSON view of the books — the verdict, balances, bills due, alerts,
    a transaction search, spending by category, the net-worth series.
    A session may read them (every role: they disclose nothing a viewer
    cannot already see on the pages), and so may a script token minted
    with the `read` scope, which can reach nothing else. The local MCP
    server (`integrations/mcp/`) is a client of exactly these.
  * **`/metrics`**: the same summary in the Prometheus text format, so a
    scraper (Prometheus, Grafana Agent, Home Assistant's Prometheus
    sensor…) polls it with the read token as a bearer.
  * **Webhooks** (`/api/webhooks`): the instance posting events out —
    `notify/webhooks.py` has the protocol; this file has the owner's
    management doors (create with the secret shown once, edit, test,
    rotate, delete, the delivery log).

The summary is computed ONCE (`summary()`) and both the JSON door and
the metrics door render it, so a sensor and a scraper can never disagree
about "left today"; the daily webhook event carries the same dict, built
by the same function, from the same gather the daily email renders.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response

from ..db import tenancy
from ..engine import budget, links, reporting
from ..engine.compat import as_date
from ..notify import webhooks
from . import data, demoguard
from .security import limit

router = APIRouter()

# one token's scrape or one sensor's poll, per instance, per minute — a
# bound rather than a budget, so a runaway poller cannot spend the
# gather on every request
_READ_LIMIT = [Depends(limit("integrations", 120, 60))]
_QUERY_LIMIT = [Depends(limit("integrations_query", 240, 60))]

# the one shadow predicate (linked non-primaries and hidden accounts), as
# the Transactions page's search applies it
_NOT_SHADOW = reporting._NOT_SHADOW.format(col="t.account_id")

BILLS_AHEAD_DAYS = 7
TXN_LIMIT_MAX = 200
SPENDING_MONTHS_MAX = 24
NETWORTH_MONTHS_MAX = 120


def _user():
    from .app import current_user
    return current_user


def _conn(user):
    return tenancy.tenant_connect(user["tenant_id"])


def _today(conn) -> dt.date:
    from .. import localtime
    return localtime.now_local(budget.load_config(conn)).date()


def _r2(v) -> float:
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(f) else round(f, 2)


# ---- the summary --------------------------------------------------------------

def summary(conn, today: dt.date | None = None, *, st: dict | None = None) -> dict:
    """Everything a sensor, a scraper or an assistant wants at a glance.
    `st` may be a status dict the caller already gathered (the daily
    email's sweep passes its own so the event and the mail agree)."""
    from . import report, todayview
    today = today or _today(conn)
    if st is None:
        st = report.gather(conn, today)
    ctx = todayview.build_context(st)
    day = ctx["day"]
    b = st["buckets"]
    cfg = st.get("config") or budget.load_config(conn)
    days_left = max(1, st["days_in_month"] - today.day + 1)
    # the verdict's own budget: the two variable buckets (a carve-out is
    # a slice of one of them, not an addition)
    month_budget = b["food"]["month_budget"] + b["other"]["month_budget"]

    # balances: the Accounts page's rows, minus what the page hides
    accounts = []
    shadow = set(links.shadow_ids(conn))
    for a in data.accounts_detail(conn):
        if a.get("user_removed_at"):
            continue
        typ, _, sub = (a.get("kind") or "/").partition("/")
        accounts.append({
            "id": a["id"], "name": a["name"], "institution": a["institution_name"],
            "type": typ, "subtype": sub, "mask": a.get("mask"),
            "balance": _r2(a["balance_current"]),
            "available": (_r2(a["balance_available"])
                          if a.get("balance_available") is not None else None),
            "status": a.get("status"), "aggregator": a.get("aggregator"),
            "counted": a["id"] not in shadow,
        })

    # bills due in the next week, unpaid
    end = today + dt.timedelta(days=BILLS_AHEAD_DAYS)
    due = []
    try:
        for o in budget.upcoming_bill_occurrences(conn, cfg, today, end):
            due.append({"payee": o["payee"], "amount": _r2(o["planned"]),
                        "due": o["due"].isoformat()})
    except Exception:                                        # noqa: BLE001
        due = []

    # connections and how fresh each one is
    conns = []
    for r in conn.execute(
            """SELECT i.id, i.institution_name, i.aggregator, i.status,
                      (SELECT MAX(ran_at) FROM sync_log s
                        WHERE s.item_id = i.id AND s.error IS NULL) AS last_ok
                 FROM items i
                WHERE COALESCE(i.status, '') <> 'archived'
                ORDER BY i.institution_name""").fetchall():
        age = None
        if r["last_ok"] is not None:
            age = int((dt.datetime.now(dt.timezone.utc) - r["last_ok"])
                      .total_seconds())
        conns.append({"id": r["id"], "name": r["institution_name"],
                      "aggregator": r["aggregator"], "status": r["status"],
                      "last_sync_at": (r["last_ok"].isoformat()
                                       if r["last_ok"] else None),
                      "last_sync_age_seconds": age})

    # the Transactions page's own counts: a linked card's second copy and
    # a hidden account's rows are not rows of this ledger, so they are not
    # counted here either (same shadow predicate the page's search applies).
    # A split row is categorized by its parts whatever its own column says,
    # and the "?" filter leaves it out, so "uncategorized" does too — else
    # the gauge counts rows its drill-down can never list.
    counts = conn.execute(
        f"""SELECT COUNT(*) AS n,
                  COUNT(*) FILTER (WHERE COALESCE(t.category_override,
                                            t.category_primary, '') = ''
                                     AND NOT EXISTS (
                                         SELECT 1 FROM transaction_splits sp
                                          WHERE sp.txn_id = t.id)) AS uncat,
                  COUNT(*) FILTER (WHERE t.pending = 1) AS pending
             FROM transactions t WHERE t.removed = 0 {_NOT_SHADOW}""").fetchone()

    nw = reporting.compute_networth(conn, today=today)
    alerts = [{"kind": a.get("kind"), "severity": a.get("severity"),
               "message": a.get("message")} for a in (st.get("alerts") or [])]
    rw = st.get("runway") or {}
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": today.isoformat(),
        "version": os.environ.get("OIKONOME_VERSION") or "dev",
        "verdict": st["verdict"],
        "pace": day["pace_line"],
        "today": {
            "left": _r2(day["simple"]["left_today"]),
            "allowance": _r2(day["day_allow"]),
            "spent": _r2(day["day_spent"]),
            "by_bucket": {label: {"left": _r2(a["left_today"]),
                                  "allowance": _r2(a["today_allowance"])}
                          for label, a in day["allow_tiles"]},
        },
        "month": {
            "budget": _r2(month_budget),
            "spent": _r2(st["variable_actual"]),
            "expected": _r2(st["variable_expected"]),
            "variance": _r2(st["variance"]),
            "fixed_spent": _r2(b["fixed"]["actual"]),
            "fixed_budget": _r2(b["fixed"]["month_budget"]),
            "days_in_month": int(st["days_in_month"]),
            "days_left": days_left,
            "headroom": _r2(day["headroom"]) if day.get("headroom") is not None else None,
        },
        "cash": {
            "checking": _r2(rw["checking"]) if rw.get("checking") is not None else None,
            "card_debt": _r2(rw.get("card_debt")),
            "bills_due_14d": _r2(rw.get("due_total")),
        } if rw else None,
        "net_worth": {"total": _r2(nw.get("current_total")),
                      "with_property": _r2(nw.get("full_total"))},
        "accounts": accounts,
        "bills_due": due,
        "alerts": alerts,
        "connections": conns,
        "transactions": {"total": int(counts["n"]),
                         "uncategorized": int(counts["uncat"]),
                         "pending": int(counts["pending"])},
    }


# ---- Prometheus text ------------------------------------------------------------

# every control character, \r included: a consumer that splits on universal
# newlines would otherwise cut one sample into two unparseable lines
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _lbl(v) -> str:
    return _CTRL.sub(" ", str(v or "").replace("\\", "\\\\")
                     .replace('"', '\\"').replace("\n", " "))


def metrics_text(s: dict) -> str:
    """The summary in the Prometheus exposition format (text/plain 0.0.4).
    Money is in dollars as a float; ages in seconds."""
    out: list[str] = []

    def g(name: str, help_: str, rows):
        out.append(f"# HELP oikonome_{name} {help_}")
        out.append(f"# TYPE oikonome_{name} gauge")
        for labels, value in rows:
            if value is None:
                continue
            lab = ("{" + ",".join(f'{k}="{_lbl(v)}"' for k, v in labels.items())
                   + "}") if labels else ""
            out.append(f"oikonome_{name}{lab} {float(value)!r}")

    verdicts = ("ON BUDGET", "OVER BUDGET", "UNDER BUDGET")
    g("info", "The instance.", [({"version": s["version"]}, 1)])
    g("verdict", "The month verdict: 1 for the one that applies.",
      [({"verdict": v}, 1 if s["verdict"] == v else 0) for v in verdicts])
    t, m = s["today"], s["month"]
    g("left_today_dollars", "Variable money left to spend today.", [({}, t["left"])])
    g("today_allowance_dollars", "Today's variable allowance.", [({}, t["allowance"])])
    g("spent_today_dollars", "Variable spend posted today.", [({}, t["spent"])])
    g("bucket_left_today_dollars", "Left today, per bucket.",
      [({"bucket": k}, v["left"]) for k, v in t["by_bucket"].items()])
    g("month_budget_dollars", "The month's variable budget.", [({}, m["budget"])])
    g("month_spent_dollars", "Variable spend month to date.", [({}, m["spent"])])
    g("month_expected_dollars", "Variable spend expected by today at plan pace.",
      [({}, m["expected"])])
    g("month_variance_dollars", "Spent minus expected; positive is over pace.",
      [({}, m["variance"])])
    g("month_fixed_spent_dollars", "Bills paid month to date.", [({}, m["fixed_spent"])])
    g("month_days_left", "Days left in the month, today included.", [({}, m["days_left"])])
    if m.get("headroom") is not None:
        g("headroom_dollars", "Checking minus card debt minus bills due.",
          [({}, m["headroom"])])
    c = s.get("cash") or {}
    if c.get("checking") is not None:
        g("checking_dollars", "Checking balance.", [({}, c["checking"])])
        g("card_debt_dollars", "Card balances outstanding.", [({}, c["card_debt"])])
    nw = s["net_worth"]
    g("net_worth_dollars", "Net worth of financial accounts.", [({}, nw["total"])])
    g("net_worth_with_property_dollars", "Net worth including property and vehicles.",
      [({}, nw["with_property"])])
    # `counted` marks the copy of a linked account the money math reads, so
    # a sum over the gauge can count one card reached through two
    # aggregators once
    g("account_balance_dollars", "Balance per account (a card's is what is owed); "
      "counted=\"0\" is the second copy of a linked account.",
      [({"account": a["name"], "institution": a["institution"] or "",
         "type": a["type"], "id": a["id"],
         "counted": "1" if a.get("counted", True) else "0"}, a["balance"])
       for a in s["accounts"]])
    g("bills_due_7d_dollars", "Unpaid bills due in the next seven days.",
      [({}, sum(b["amount"] for b in s["bills_due"]))])
    g("bills_due_7d_count", "How many.", [({}, len(s["bills_due"]))])
    sev: dict[str, int] = {}
    for a in s["alerts"]:
        sev[a["severity"] or "info"] = sev.get(a["severity"] or "info", 0) + 1
    g("alerts_active", "Active alerts on the Today page, by severity.",
      [({"severity": k}, v) for k, v in sorted(sev.items())] or [({"severity": "warn"}, 0)])
    g("connection_sync_age_seconds", "Seconds since each connection last synced cleanly.",
      [({"connection": cn["name"] or cn["id"], "aggregator": cn["aggregator"] or ""},
        cn["last_sync_age_seconds"]) for cn in s["connections"]])
    g("connection_ok", "1 when the connection's status is ok.",
      [({"connection": cn["name"] or cn["id"]}, 1 if (cn["status"] or "ok") == "ok" else 0)
       for cn in s["connections"]])
    tx = s["transactions"]
    g("transactions_total", "Rows in the ledger.", [({}, tx["total"])])
    g("transactions_uncategorized", "Rows with no category.", [({}, tx["uncategorized"])])
    g("transactions_pending", "Rows still pending at the bank.", [({}, tx["pending"])])
    return "\n".join(out) + "\n"


# ---- the read doors ---------------------------------------------------------------

@router.get("/api/integrations/events")
def events_api(user: dict = Depends(_user())):
    """The webhook event catalogue — what a receiver can subscribe to."""
    return {"events": [{"name": k, "description": v}
                       for k, v in webhooks.EVENTS.items()]}


@router.get("/api/integrations/summary", dependencies=_READ_LIMIT)
def summary_api(user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        return summary(conn)
    finally:
        conn.close()


@router.get("/metrics", dependencies=_READ_LIMIT)
def metrics_api(user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        text = metrics_text(summary(conn))
    finally:
        conn.close()
    return Response(text, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/api/integrations/accounts", dependencies=_QUERY_LIMIT)
def accounts_api(user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        rows = []
        # both halves of a linked account stay listed, as on the Accounts
        # page; `counted` says which one the money math reads, so a
        # dashboard that sums balances does not count the same card twice
        shadow = set(links.shadow_ids(conn))
        for a in data.accounts_detail(conn):
            if a.get("user_removed_at"):
                continue
            typ, _, sub = (a.get("kind") or "/").partition("/")
            rows.append({"id": a["id"], "name": a["name"],
                         "institution": a["institution_name"],
                         "type": typ, "subtype": sub, "mask": a.get("mask"),
                         "balance": _r2(a["balance_current"]),
                         "available": (_r2(a["balance_available"])
                                       if a.get("balance_available") is not None
                                       else None),
                         "transactions": int(a.get("txns") or 0),
                         "status": a.get("status"),
                         "counted": a["id"] not in shadow})
        return {"accounts": rows}
    finally:
        conn.close()


_TXN_KEYS = ("id", "date", "amount", "payee", "category", "account", "mask",
             "pending", "counts_as_spend", "note", "entity", "bank_text",
             "payment_channel", "location_city", "location_region",
             "recurring_bill", "item_summary")


def _txn(r) -> dict:
    d = dict(r)
    out = {k: d.get(k) for k in _TXN_KEYS if k in d}
    for k, v in list(out.items()):
        if isinstance(v, (dt.date, dt.datetime)):
            out[k] = v.isoformat()
    return out


@router.get("/api/integrations/transactions", dependencies=_QUERY_LIMIT)
def transactions_api(user: dict = Depends(_user()), q: str = "",
                     since: str = "", until: str = "", account: str = "",
                     category: str = "",
                     limit_: int = Query(50, alias="limit", ge=1,
                                         le=TXN_LIMIT_MAX),
                     page: int = Query(1, ge=1, le=10_000)):
    """A ledger search: the same universal search the Transactions page
    runs, newest first, paged. `since`/`until` are ISO dates."""
    for label, v in (("since", since), ("until", until)):
        if v:
            try:
                as_date(v[:10])
            except ValueError:
                raise HTTPException(400, f"{label} must be YYYY-MM-DD")
    conn = _conn(user)
    try:
        rows, total, amount_sum, _amz, spend, _hits = data.search_transactions(
            conn, q, account_id=account or None, date_from=since or None,
            date_to=until or None, category=category or None,
            page=page, per_page=limit_)
        return {"transactions": [_txn(r) for r in rows], "total": total,
                "amount_sum": _r2(amount_sum),
                "spend_sum": _r2((spend or {}).get("sum")),
                "page": page, "limit": limit_}
    finally:
        conn.close()


@router.get("/api/integrations/spending", dependencies=_QUERY_LIMIT)
def spending_api(user: dict = Depends(_user()),
                 months: int = Query(3, ge=1, le=SPENDING_MONTHS_MAX)):
    """Spend by category and by month over the last `months` complete
    months plus the current one — what the Spending page counts, figure
    for figure.

    Built from the page's own pieces rather than a query of its own:
    reporting.SPEND_WHERE carries the scope (money out, no transfers or
    card payments, business money kept apart, and a card reached through
    two aggregators or an account the household hid counted once or not
    at all), and the split join fans a hand-split charge out into its
    parts, so a warehouse run split into groceries and household lands in
    both at its share. The category key is the STORED key ('?' for none)
    because the transactions door filters on exactly that — an assistant
    that asks for the rows behind a figure gets them."""
    from ..engine import categories
    conn = _conn(user)
    try:
        today = _today(conn)
        first = today.replace(day=1)
        y, m = first.year, first.month - (months - 1)
        while m < 1:
            m += 12
            y -= 1
        start = dt.date(y, m, 1)
        excl, ex_p = reporting.excluded_accounts_sql(conn)
        rows = conn.execute(
            f"""SELECT to_char(t.date, 'YYYY-MM') AS ym,
                       COALESCE(NULLIF({categories.PART_CAT}, ''),
                                '{data.UNCATEGORIZED}') AS cat,
                       ROUND(SUM({reporting.PART_NET_JOINED})::numeric, 2) AS amt,
                       COUNT(DISTINCT t.id) AS n
                  FROM transactions t {reporting.REIMB_PARTIAL_JOIN}
                       {reporting.SPLIT_JOIN}
                 WHERE {reporting.SPEND_WHERE}
                       AND t.date >= %s AND t.date <= %s {excl}
                 GROUP BY 1, 2 ORDER BY 1, 3 DESC""",
            (start, today, *ex_p)).fetchall()
        by_month: dict[str, dict] = {}
        by_cat: dict[str, float] = {}
        for r in rows:
            mo = by_month.setdefault(r["ym"], {"month": r["ym"], "total": 0.0,
                                               "categories": {}})
            amt = _r2(r["amt"])
            mo["total"] = _r2(mo["total"] + amt)
            mo["categories"][r["cat"]] = {"amount": amt, "count": int(r["n"])}
            by_cat[r["cat"]] = _r2(by_cat.get(r["cat"], 0.0) + amt)
        return {"from": start.isoformat(), "to": today.isoformat(),
                "months": list(by_month.values()),
                "categories": [{"category": k, "amount": v}
                               for k, v in sorted(by_cat.items(),
                                                  key=lambda kv: -kv[1])]}
    finally:
        conn.close()


@router.get("/api/integrations/bills", dependencies=_QUERY_LIMIT)
def bills_api(user: dict = Depends(_user()),
              days: int = Query(30, ge=1, le=366)):
    """Unpaid bill occurrences due in the next `days` days, and the bill
    catalogue behind them."""
    conn = _conn(user)
    try:
        today = _today(conn)
        cfg = budget.load_config(conn)
        end = today + dt.timedelta(days=days)
        due = [{"payee": o["payee"], "amount": _r2(o["planned"]),
                "due": o["due"].isoformat()}
               for o in budget.upcoming_bill_occurrences(conn, cfg, today, end)]
        bills = []
        for r in conn.execute(
                """SELECT payee, amount, frequency, due_on, type, category, raw
                     FROM bills WHERE active = 1
                    ORDER BY due_on NULLS LAST, payee""").fetchall():
            raw = r["raw"] if isinstance(r["raw"], dict) else {}
            rec = raw.get("recurrence") if isinstance(
                raw.get("recurrence"), dict) else {}
            bills.append({
                "payee": r["payee"], "amount": _r2(abs(r["amount"] or 0)),
                "frequency": r["frequency"],
                "interval": int(rec.get("interval") or 1),
                "next_due": r["due_on"].isoformat() if r["due_on"] else None,
                "income": (r["type"] or "").upper() == "INCOME",
                "category": r["category"],
                "type": raw.get("bill_type") or "occurrence"})
        return {"from": today.isoformat(), "to": end.isoformat(),
                "due": due, "bills": bills}
    finally:
        conn.close()


@router.get("/api/integrations/alerts", dependencies=_QUERY_LIMIT)
def alerts_api(user: dict = Depends(_user())):
    """Active, undismissed alerts as the Today page shows them."""
    conn = _conn(user)
    try:
        rows = conn.execute(
            """SELECT kind, severity, message, first_seen, last_seen
                 FROM alerts_log
                WHERE active = 1 AND dismissed = 0
                ORDER BY first_seen DESC""").fetchall()
        return {"alerts": [{"kind": r["kind"], "severity": r["severity"],
                            "message": r["message"],
                            "since": r["first_seen"].isoformat()}
                           for r in rows]}
    finally:
        conn.close()


@router.get("/api/integrations/networth", dependencies=_QUERY_LIMIT)
def networth_api(user: dict = Depends(_user()),
                 months: int = Query(12, ge=1, le=NETWORTH_MONTHS_MAX)):
    """Today's net worth and the recorded nightly series over the last
    `months` months (one point per recorded day)."""
    conn = _conn(user)
    try:
        today = _today(conn)
        nw = reporting.compute_networth(conn, today=today)
        since = today - dt.timedelta(days=31 * months)
        series = [{"date": r["date"].isoformat(), "total": _r2(r["total"])}
                  for r in conn.execute(
                      "SELECT date, total FROM networth_snapshot "
                      "WHERE date >= %s ORDER BY date", (since,)).fetchall()]
        return {"total": _r2(nw.get("current_total")),
                "with_property": _r2(nw.get("full_total")),
                "by_institution": [{"institution": k, "total": _r2(v)}
                                   for k, v in nw.get("by_institution") or []],
                "by_asset_class": [{"asset_class": k, "total": _r2(v)}
                                   for k, v in nw.get("by_asset_class") or []],
                "series": series}
    finally:
        conn.close()


# ---- webhooks: the owner's management doors ---------------------------------------

def _owner_session(user: dict) -> None:
    if user.get("script_token") or user.get("role") != "owner":
        raise HTTPException(403, "owner only")


def _hook_id(raw) -> str:
    try:
        return str(uuid.UUID(str(raw or "")))
    except ValueError:
        raise HTTPException(404, "no such webhook")


@router.get("/api/webhooks")
def webhooks_list_api(user: dict = Depends(_user())):
    _owner_session(user)
    conn = _conn(user)
    try:
        return {"webhooks": webhooks.list_hooks(conn),
                "events": [{"name": k, "description": v}
                           for k, v in webhooks.EVENTS.items()]}
    finally:
        conn.close()


@router.post("/api/webhooks", dependencies=[Depends(limit("webhooks", 20, 3600))])
def webhooks_create_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """Create one; the response carries the signing secret exactly once.
    A webhook sends the household's balances and rows to a URL, so it is
    a step-up act like minting a token: a hijacked cookie alone must not
    be able to point the ledger at a stranger's server."""
    demoguard.deny(user)
    _owner_session(user)
    from .app import _require_elevation
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    conn = _conn(user)
    try:
        try:
            row, secret = webhooks.create(
                conn, url=str(body.get("url") or ""),
                events=body.get("events") or [],
                name=str(body.get("name") or ""),
                created_by=str(user.get("email") or ""))
        except webhooks.WebhookError as e:
            raise HTTPException(400, str(e))
        return {**row, "secret": secret}
    finally:
        conn.close()


@router.post("/api/webhooks/{hook_id}")
def webhooks_update_api(hook_id: str, user: dict = Depends(_user()),
                        body: dict = Body(...)):
    """Edit: url, events, name, enabled. A new URL is a new destination
    for the ledger, so that one field steps up; the rest do not."""
    demoguard.deny(user)
    _owner_session(user)
    hid = _hook_id(hook_id)
    url = body.get("url")
    if url is not None:
        from .app import _require_elevation
        _require_elevation(user, password=str(body.get("password") or ""),
                           totp_code=str(body.get("totp_code") or ""),
                           recovery_code=str(body.get("recovery_code") or ""))
    conn = _conn(user)
    try:
        try:
            row = webhooks.update(
                conn, hid, url=(str(url) if url is not None else None),
                events=body.get("events"),
                name=(str(body["name"]) if "name" in body else None),
                enabled=(bool(body["enabled"]) if "enabled" in body else None))
        except webhooks.WebhookError as e:
            raise HTTPException(404 if "no such" in str(e) else 400, str(e))
        return row
    finally:
        conn.close()


@router.post("/api/webhooks/{hook_id}/delete")
def webhooks_delete_api(hook_id: str, user: dict = Depends(_user())):
    demoguard.deny(user)
    _owner_session(user)
    hid = _hook_id(hook_id)
    conn = _conn(user)
    try:
        if not webhooks.delete(conn, hid):
            raise HTTPException(404, "no such webhook")
        return {"ok": True}
    finally:
        conn.close()


@router.post("/api/webhooks/{hook_id}/rotate")
def webhooks_rotate_api(hook_id: str, user: dict = Depends(_user()),
                        body: dict = Body(default={})):
    """A fresh signing secret, shown once. Step-up, like creation."""
    demoguard.deny(user)
    _owner_session(user)
    hid = _hook_id(hook_id)
    from .app import _require_elevation
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    conn = _conn(user)
    try:
        try:
            row, secret = webhooks.rotate_secret(conn, hid)
        except webhooks.WebhookError as e:
            raise HTTPException(404, str(e))
        return {**row, "secret": secret}
    finally:
        conn.close()


@router.post("/api/webhooks/{hook_id}/test",
             dependencies=[Depends(limit("webhooks_test", 30, 3600))])
def webhooks_test_api(hook_id: str, user: dict = Depends(_user())):
    """Send a test.ping now and report what the receiver said."""
    demoguard.deny(user)
    _owner_session(user)
    hid = _hook_id(hook_id)
    # The attempt waits on a stranger's server for up to the send deadline;
    # the tenant pool is small, so the connection goes back to it for that
    # wait — queue and read the secret, release, send, then reconnect to
    # settle.
    conn = _conn(user)
    try:
        try:
            attempt = webhooks.ping_prepare(conn, hid)
        except webhooks.WebhookError as e:
            raise HTTPException(404, str(e))
    finally:
        conn.close()
    if "done" in attempt:
        return attempt["done"]
    status, error = webhooks.ping_send(attempt)
    conn = _conn(user)
    try:
        return webhooks.ping_settle(conn, attempt, status, error)
    finally:
        conn.close()


@router.get("/api/webhooks/{hook_id}/deliveries")
def webhooks_deliveries_api(hook_id: str, user: dict = Depends(_user()),
                            limit_: int = Query(20, alias="limit", ge=1,
                                                le=100)):
    _owner_session(user)
    hid = _hook_id(hook_id)
    conn = _conn(user)
    try:
        return {"deliveries": webhooks.deliveries(conn, hid, limit_)}
    finally:
        conn.close()
