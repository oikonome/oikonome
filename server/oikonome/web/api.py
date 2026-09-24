"""JSON API over the management data layer (web/data.py) — the surface the
React SPA and mobile app consume. Session-cookie auth (current_user);
every handler runs on an RLS-scoped tenant connection.

The routes:
  GET  /api/transactions        month browse OR universal search (q/acct/
                                cat/date_from/date_to/page)
  POST /api/transactions/{id}/category   {"category": X} · X="" clears
  GET  /api/categories · /api/accounts
  GET  /api/reimburse/pending             the awaiting list
  GET  /api/reimburse/{txn}/candidates    opposite-direction matches
  POST /api/reimburse/link                {txn_id, other_ids: [...]}
  POST /api/reimburse/unlink              {expense_id, reimburse_id}
  POST /api/reimburse/{txn}/flag · /unflag
  GET  /api/retirement            engine/retirement.project wrapper
                                  (what-if query params, config birthdate/
                                  ss_estimates supply the defaults)
  GET  /api/bills/bill · /api/bills/history   per-bill prefill /
                                  per-payee merchant history + ledger fit
  POST /api/bills/{save,delete,restore,toggle,hint}   bill CRUD
                                  (the /bills/* form actions as JSON)
"""

from __future__ import annotations

import calendar
import datetime as dt
import os
from ..envnum import env_flag
from .. import ext
import re
import uuid
from typing import Annotated

import psycopg

from fastapi import (APIRouter, Body, Depends, File, Form, HTTPException,
                     Query, Request, UploadFile)
from pydantic import BeforeValidator
from starlette.concurrency import run_in_threadpool

from ..db import tenancy
from ..engine import activity as _activity
from . import data, demoguard, mailguard, permissions
from .security import limit


def _no_byo_on_hosted() -> None:
    """In hosted mode the aggregator runs under the operator's
    credentials — a tenant must not save its own Plaid/MX/
    SimpleFIN keys; the SPA hides them, and a crafted request that got past
    the UI would shadow the operator's connection. Server-enforced 403, not
    just UI. Self-host is BYO-by-design and unaffected."""
    if env_flag("OIKONOME_HOSTED"):
        raise HTTPException(
            403, "in hosted mode bank connections run under the operator's "
                 "provider keys — you don't add your own")


# The self-host fat-finger guard on plaid_enrich_cap: an extra zero must
# not authorise a spend nobody meant to approve, but a self-hoster is
# spending against THEIR OWN Plaid account, so the number is theirs.
ENRICH_CAP_SELFHOST_MAX = 100000


def _enrich_cap_ceiling() -> int:
    """Highest monthly Plaid Enrich row cap a TENANT may write here.

    Enrich is billed per row to whoever owns the Plaid credentials. On hosted
    that is the operator — `_no_byo_on_hosted` makes sure a tenant cannot
    bring its own keys — so the cap is not a preference there, it is the
    operator's spend ceiling, and a tenant that can raise its own ceiling has
    no ceiling. Hosted tenants may lower it (0 = off) and never raise it past
    the ceiling the installed gate reports; self-host keeps the full
    range. The installed gate may lift a whole deployment without a code
    change — except that 0 here means OFF, never "uncapped": the whole
    point of this number is that money cannot be spent without a ceiling."""
    if not env_flag("OIKONOME_HOSTED"):
        return ENRICH_CAP_SELFHOST_MAX
    cap = ext.gate.enrich_rows_cap()
    return ENRICH_CAP_SELFHOST_MAX if cap is None else cap


def _no_smtp_byo_on_hosted() -> None:
    """In hosted mode the operator's mail server sends the mail, so a tenant
    does not point the app at its own relay. The reason is not tidiness: a
    tenant-set relay WINS over the operator's env in resolve_smtp, so the
    daily verdict — the product — would leave through a server the operator
    does not run, cannot see, and cannot debug when it silently stops. Blocked
    server-side and not merely hidden, for the same reason as above.
    Self-host configures its own mail by design and is unaffected."""
    if env_flag("OIKONOME_HOSTED"):
        raise HTTPException(
            403, "in hosted mode the operator's mail server sends your email "
                 "— there is no mail server to configure here")

router = APIRouter(prefix="/api")


# The first characters a spreadsheet reads as "this cell is a formula".
_CSV_FORMULA_LEAD = ("=", "+", "-", "@")
# Characters that can sit in front of one of those and still leave it first
# as far as the reader is concerned: they are invisible, several importers
# trim them, and a byte-order mark at the head of a file is consumed as an
# encoding marker rather than as content. Tab, CR, line feed and a BOM
# are all this class.
_CSV_INVISIBLE_LEAD = ("\t", "\r", "\n", "﻿", " ")


def _csv_safe(v):
    """Neutralize spreadsheet formula injection: a cell beginning = + - @
    — with or without invisible characters in front of it — is prefixed
    with a quote so Excel/Calc treat it as text, not a formula. Untrusted
    sources: LLM-parsed receipt lines, bank-feed merchant names, the user's
    own free-text category on a file an accountant opens.

    `pages.py:_csv_cell` carries the same rule for the full-data export."""
    if not isinstance(v, str) or not v:
        return v
    head = v.lstrip("".join(_CSV_INVISIBLE_LEAD))
    if v[:1] in _CSV_INVISIBLE_LEAD + _CSV_FORMULA_LEAD \
            and head[:1] in _CSV_FORMULA_LEAD:
        return "'" + v
    if v[:1] in ("\t", "\r"):
        # a leading tab/CR is neutralized whatever follows it: it splits or
        # shifts a cell in some readers on its own
        return "'" + v
    return v


def _filename_safe(v: str, fallback: str = "export") -> str:
    """Make a user-supplied string safe inside `filename="..."`.

    Two ways raw text breaks a Content-Disposition header (it reaches this
    header via the entity name in the books CSV and `tag` in the
    expenses CSV):

    * a double quote or newline terminates/splits the quoted value — header
      injection, and at best a mangled download name;
    * a non-ASCII character raises UnicodeEncodeError when the header is
      latin-1 encoded, turning a download into a 500.

    Keep an unambiguous ASCII subset and collapse the rest, rather than
    trying to escape — the filename is cosmetic, so degrading it is free.
    """
    # isascii() FIRST: str.isalnum() is Unicode-aware, so "Ω", "日" and "Я"
    # are alphanumeric and would sail straight through, still raising the
    # latin-1 UnicodeEncodeError this exists to prevent. An entity named in
    # a non-Latin script is entirely ordinary.
    out = "".join(c if (c.isascii() and (c.isalnum() or c in "-_. ")) else "-"
                  for c in (v or "")).strip(" .-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:60] or fallback


def _money_float(raw, field: str) -> float:
    """Parse a user money string; reject NaN/Infinity — they parse fine
    but poison DOUBLE PRECISION columns and then permanently 500 every
    JSON response containing the row (allow_nan=False)."""
    import math
    try:
        v = float(str(raw).replace(",", "").replace("$", "").strip() or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be a number")
    if not math.isfinite(v):
        raise HTTPException(400, f"{field} must be a real number")
    return v


def _user():
    # late import: avoids app<->api circularity
    from .app import current_user
    return current_user


def _conn(user):
    return tenancy.tenant_connect(user["tenant_id"])


# ---- the household activity log: the doors below write one row per act ----

def _log_bulk(conn, user, action: str, category: str, ids: list[str],
              out: dict) -> None:
    """One row for a bulk act over a hand-picked set — the count, the
    category, and the first row named so the entry reads as something."""
    n = int(out.get("applied") or 0)
    first = ids[0] if ids else ""
    label = _activity.txn_label(conn, first) if first else ""
    if action == "category":
        _activity.record(conn, user, "category", "bulk", target=None,
                         label=label,
                         detail={"after": category, "count": n, "ids": ids[:50]})
    elif action in ("biz_on", "biz_off"):
        _activity.record(conn, user, "business",
                         "flagged" if action == "biz_on" else "unflagged",
                         target=first if n == 1 else None,
                         label=label if n == 1 else f"{n} transactions")
    elif action == "reimb_flag":
        _activity.record(conn, user, "reimbursement", "flagged",
                         target=first if n == 1 else None,
                         label=label if n == 1 else f"{n} transactions")


def _log_proposal(conn, user, pid: str, action: str, r: dict) -> None:
    """A decided bill offer. `r` is apply_proposal's answer."""
    payee = r.get("approved") or r.get("rejected") or pid
    p = conn.execute("SELECT kind, bill_type FROM bill_proposals WHERE id=%s",
                     (pid,)).fetchone() or {}
    kind = p.get("kind") or r.get("kind") or "add"
    if kind in ("add", "income"):
        act = "confirmed" if action == "approve" else "dismissed"
    else:
        act = "applied" if action == "approve" else "kept"
    _activity.record(conn, user, "bill", act, target=payee, label=payee,
                     detail={"proposal": kind, "income": kind == "income",
                             "pid": pid})


def _log_merge(conn, user, pid: str, action: str, r: dict) -> None:
    if action == "approve":
        _activity.record(conn, user, "merchant", "merged",
                         target=r.get("into"), label=str(r.get("from") or pid),
                         detail={"before": r.get("from"), "after": r.get("into"),
                                 "rows": r.get("rows")})
        return
    names = conn.execute(
        """SELECT (SELECT name FROM merchants WHERE id = p.from_merchant_id) AS a,
                  (SELECT name FROM merchants WHERE id = p.into_merchant_id) AS b
             FROM merchant_merge_proposals p WHERE p.id=%s""", (pid,)).fetchone()
    pair = " and ".join(x for x in ((names or {}).get("a"), (names or {}).get("b")) if x)
    _activity.record(conn, user, "merchant", "kept_apart", target=pid,
                     label=pair or "two merchants")


def _today(conn) -> dt.date:
    """The household's OWN calendar day, for every live request that does
    day/month math. The instance clock may be UTC while
    a household sets its timezone in settings; the nightly email already
    resolves that (worker.py), and so must the live Today page, lenses,
    bills and send-now: on the server clock a US household watches its
    budget month roll over mid-afternoon while that evening's email still
    says the 31st."""
    from .. import localtime
    from ..engine import budget
    return localtime.now_local(budget.load_config(conn)).date()


def _today_for(user: dict) -> dt.date:
    """_today for a handler that has not opened its tenant connection yet."""
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        return _today(conn)
    finally:
        conn.close()



def _same_site_download(request: Request) -> None:
    """Refuse a cookie-authenticated download that a cross-site page
    started. The session cookie is SameSite=Lax, and Lax cookies DO ride a
    top-level cross-site GET, so `<a href="https://<instance>/api/…csv">`
    on any page a signed-in person visits would drop the file into their
    Downloads; the origin middleware wraps writes only. The household ZIP
    takes the ticket route for this; the smaller downloads (worksheets,
    packages, calendars, receipt images) take the browser's own word:
    fetch metadata names a cross-site initiator, and a Referer from
    another origin says the same on browsers without it. The native app
    and same-origin pages send neither and pass."""
    site = request.headers.get("sec-fetch-site", "").lower()
    if site == "cross-site":
        raise HTTPException(403, "cross-site download refused — open it "
                                 "from inside the app")
    ref = request.headers.get("referer", "")
    if ref and not site:
        from urllib.parse import urlsplit
        r, h = urlsplit(ref), request.headers.get("host", "")
        if r.netloc and h and r.netloc.lower() != h.lower():
            raise HTTPException(403, "cross-site download refused — open "
                                     "it from inside the app")


def _ser(row: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v)
            for k, v in dict(row).items()}


_SOURCE_WORDS = {
    "plaid": "your bank's aggregator (Plaid)", "import": "the file it was imported from",
    "flow": "the ledger's own transfer / payment rules", "fuel": "the fuel-arm outlet",
    "mcc": "the merchant's registered line of business (MCC)",
    "rule_user": "your rule for this merchant", "rule_llm": "the AI's merchant rule",
    "rule_seed": "the built-in merchant rule", "rule_model": "the learned merchant rule",
    "rule": "a merchant rule",
}


def _category_why(r: dict) -> str | None:
    """One sentence on where the row's category came from — the detail
    panel's 'why'. Override first (a person / a bill / Amazon matching),
    else the primary layer's category_source, with Plaid's own confidence
    named when it is the deciding layer."""
    if r.get("category_override"):
        # a person's pin first, then an item match, then a bill — the rank
        # the writers keep, so the sentence names the hand that won even
        # where a bill's mark still sits under an item match
        if r.get("override_manual"):
            return "you set it on this transaction"
        if str(r["category_override"]).startswith("Amazon - "):
            return "matched to an Amazon order"
        if str(r["category_override"]).startswith("Costco - "):
            return "matched to a Costco receipt"
        if r.get("override_bill"):
            return f"the bill \"{r['override_bill']}\" categorizes the charges it matches"
        return "set on this transaction"
    src = r.get("category_source")
    if src == "plaid":
        conf = (r.get("category_plaid_confidence") or "").replace("_", " ").lower()
        return f"Plaid, {conf} confidence" if conf else "Plaid"
    words = _SOURCE_WORDS.get(src or "")
    return words or ("no category yet" if not r.get("category_key") else None)


@router.get("/today/full")
def today_full(user: dict = Depends(_user()), date: str = ""):
    """Everything the SPA Today page renders — the same gather()+context
    the Jinja page and email use, serialized. Single source preserved.

    `date=YYYY-MM-DD` (optional) renders a PAST day: same verdict/plan/
    bucket math as-of that day (gather's historical notion, not a fork),
    with the now-facts — cash forecast, runway, alerts — suppressed
    (live=False). Out-of-range dates clamp to [first transaction, today]."""
    from . import report, todayview
    if date:
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, "date must be an ISO date (YYYY-MM-DD)")
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        real_today = _today(conn)
        if not date:
            day = real_today
        first = conn.execute("SELECT MIN(date) AS d FROM transactions "
                             "WHERE removed = 0").fetchone()["d"]
        # clamp: no earlier than the ledger, no later than the real today
        day = min(max(day, first or real_today), real_today)
        live = day == real_today
        st = report.gather(conn, day, live=live)
        # goal progress rides the Today payload (tile + pace) —
        # paced as of the viewed day. Computed in gather() so the email
        # renders the same numbers.
        goals = st["savings_goals"]
        ctx = todayview.build_context(st)
        # the recent pane renders the SAME table the Transactions page
        # does — full ledger-row shape, same ids the variable-spend
        # window picked
        recent = [_ser(r) for r in data.transactions_by_ids(
            conn, [r["txn_id"] for r in ctx["day"]["recent_var"]])]
        for o in recent:
            o["category_why"] = _category_why(o)
        _stamp_recurring(conn, recent)
        # "After paying cards and remaining bills" tile:
        # unpaid bill occurrences from now through month end plus the
        # envelope drip the walk would charge — computed in gather()
        # (report.bills_remaining_month) so the SPA tile and the email
        # can never disagree. A now-fact, so live-only (None on a
        # past-day view, like the runway itself).
        bills_remaining = st["bills_remaining_month"]
    finally:
        conn.close()
    day, fc = ctx["day"], ctx["fc"]
    b = st["buckets"]

    def bucket(k):
        v = b[k]
        out = {"actual": round(v["actual"], 2),
               "expected": round(v["expected"], 2),
               "month_budget": round(v["month_budget"], 2)}
        if "children" in v:     # food/other: custom-bucket carve-outs
            out["children"] = [{"name": c["name"],
                                "actual": round(c["actual"], 2),
                                "expected": round(c["expected"], 2),
                                "month_budget": round(c["month_budget"], 2)}
                               for c in v["children"]]
            out["display_actual"] = round(v["display_actual"], 2)
            out["display_expected"] = round(v["display_expected"], 2)
            out["remaining_budget"] = round(v["remaining_budget"], 2)
        return out

    scale = st.get("variable_scale")
    return {
        "date": st["today"].isoformat(),
        # back-nav state: is this the live today, where "now" actually is,
        # and how far back the ledger goes (the ‹ button's floor)
        "is_today": live,
        "real_today": real_today.isoformat(),
        "min_date": first.isoformat() if first else None,
        # start of the "recent transactions" window — the header renders
        # "({{ st.yesterday|d }} – {{ st.today|d }} · …)"
        "yesterday": st["yesterday"].isoformat(),
        "day_of_month": st["today"].day,
        "days_in_month": st["days_in_month"],
        "verdict": st["verdict"],
        # which hero face this account shows (simple = the one number;
        # advanced = the full tile/goal anatomy) — the page's toggle
        # writes it back through /api/settings. Page-only: the daily
        # email's face is the separate email_schedule.daily.summary
        # setting, NOT this toggle.
        "today_view": st["today_view"],
        # the simple face's composed pieces (one number + chips), shared
        # with the email renders so the surfaces cannot drift
        "simple": ctx["day"]["simple"],
        "variance": round(st["variance"], 2),
        "tolerance": round(st["tolerance"], 2),
        "variable_actual": round(st["variable_actual"], 2),
        "variable_budget": round(b["food"]["month_budget"]
                                 + b["other"]["month_budget"], 2),
        # "This month's plan" card (today_body.html) — all straight
        # from budget.month_status
        "income": (round(st["income"], 2)
                   if st.get("income") is not None else None),
        "plan_surplus": (round(st["plan_surplus"], 2)
                         if st.get("plan_surplus") is not None else None),
        "avg_bills": round(st.get("avg_bills") or 0, 2),
        "savings_plan": round(st.get("savings_plan") or 0, 2),
        # sweep goals: the plan cap, month-to-date availability, and the
        # server-composed sentence (identical in the email HTML and plain)
        "sweep_plan": round(st.get("sweep_plan") or 0, 2),
        "sweep_available": (round(st["sweep_available"], 2)
                            if st.get("sweep_available") is not None
                            else None),
        "sweep_line": day.get("sweep_line"),
        "variable_scale": ({"factor": round(scale["factor"], 4),
                            "static_total": round(scale["static_total"], 2),
                            "available": round(scale["available"], 2)}
                           if scale else None),
        "irregular_bills": [dict(i, amount=round(i["amount"], 2))
                            for i in st.get("irregular_bills") or []],
        # "Scheduled but not yet posted" line: [payee, amount, due] rows
        "overdue_unpaid": [[p, round(a, 2), due]
                           for p, a, due in st.get("overdue_unpaid") or []],
        # month-to-date by category (Amazon subcategories clustered)
        "mtd": [[cat, round(amt, 2)] for cat, amt in day["mtd"]],
        "amazon_subs": [[s, round(a, 2)] for s, a in day["amazon_subs"]],
        "amazon_total": round(day["amazon_total"], 2),
        "allow": {k: {"rate": round(v["rate"], 2),
                      "plan": round(v["plan"], 2),
                      "spent_today": v["spent_today"],
                      "today_allowance": v["today_allowance"],
                      "left_today": v["left_today"],
                      "month_budget": v["month_budget"],
                      "month_spent": v["month_spent"],
                      "daily_note": v["daily_note"],
                      "recover_days": v["recover_days"],
                      "recover_note": v["recover_note"]}
                  for k, v in day["allow"].items()},
        # custom buckets' today-ledger tiles, in config order
        "allow_kids": day["allow_kids"],
        # bills pinned to Today (bill setting) — server-composed cards,
        # identical wording in the email HTML and plain text
        "bill_cards": day["bill_cards"],
        "days_left": day["days_left"],
        # hero sentences + why chips: composed once in build_context so
        # this page, the email HTML and the plain text cannot drift
        "pace_line": day["pace_line"],
        "why_chips": day["why_chips"],
        "headroom": day["headroom"],
        "buckets": {k: bucket(k) for k in ("fixed", "food", "other")},
        "fixed_unpaid_due": round(b["fixed"].get("unpaid_due") or 0, 2),
        "reasons": st["reasons"],
        # the Why narrative: server-composed once, rendered identically by
        # this page, the email HTML and the plain text
        "why": st.get("why"),
        "alerts": st.get("alerts") or [],
        "runway": ({"checking": st["runway"]["checking"],
                    "card_debt": round(st["runway"]["card_debt"], 2),
                    "due_total": round(st["runway"]["due_total"], 2),
                    "balance_unreliable":
                        bool(st["runway"].get("balance_unreliable"))}
                   if st.get("runway") else None),
        # unpaid bill occurrences through month end — the
        # "after paying cards and remaining bills" tile; null on past days
        "bills_remaining_month": bills_remaining,
        "next_paycheck": st.get("next_paycheck"),
        "recent": recent,
        # two user-facing card scenarios — pay all cards now and
        # autopay statement balance. The engine still computes the full-
        # balance-at-due-dates series internally; the product doesn't show it.
        "forecast": ({"days": fc["days"],
                      "checking": fc["checking"],
                      "card_debt": fc["card_debt"],
                      "pace_now": fc["pace_now"],
                      "pace_stmt": fc["pace_stmt"],
                      # the dated card autopays the chart draws as lines:
                      # [date, amount, card name], amount negative = leaving
                      "card_autopay": fc["card_autopay"]}
                     if fc else None),
        "forecast_rows": [{"date": d0, "amount": a, "label": p2,
                           "bal_now": bn, "bal_stmt": bs}
                          for d0, a, p2, bn, bs in ctx["fc_rows"]],
        "savings_goals": goals,
    }


# ---- timeframe lenses ----------------------------------------
# Week/Month/Year summaries composed from the frozen engine by web/lenses.py
# — the same functions the weekly/monthly emails render, so API and email
# cannot diverge.

def _ck_ym(y: int, m: int) -> None:
    """Bound a caller-supplied year/month BEFORE it reaches dt.date().

    Out of range they would reach the engine as an unhandled ValueError
    from `dt.date(y, m, 1)` — a 500 instead of a 400. Written inline, the
    guard would be re-implemented per endpoint and every later endpoint that
    takes a y/m would re-open the same hole. One spelling, shared."""
    if not (1900 <= y <= 2100 and 1 <= m <= 12):
        raise HTTPException(400, "y/m out of range")


def _ck_year(y: int | None, field: str = "y") -> None:
    """Same, for endpoints that take a bare year.

    EVERY endpoint taking a `year` has to call this, not just the ones that
    obviously build a date: a year eventually reaches `dt.date(year, 1, 1)`,
    `year + 1` (the 1040-ES Q4 deadline), or a bigint bind, and each of those
    turns year=0 or year=99999 into an opaque 500 instead of a 400."""
    if y is not None and not 1900 <= y <= 2100:
        raise HTTPException(400, f"{field} out of range")


def _body_text(body: dict, *keys: str, default: str = "") -> str:
    """Read a short human-text field (payee, category, name…) from a JSON
    body, safely.

    Clients send whatever JSON they like: `{"display": 123}` would reach
    `.strip()` on an int and surface as an opaque 500. A number or composite
    value is never a usable name here, and str()-coercing one would write
    "123" or "{'a': 1}" into the ledger — so anything that is not a string
    (or null) is a 400. Keys are tried in order (some endpoints accept
    aliases like old/from); blank and missing values fall through to
    `default`."""
    for key in keys:
        v = body.get(key)
        if v is None:
            continue
        if not isinstance(v, str):
            raise HTTPException(400, f"{key} must be a string")
        v = v.strip()
        if v:
            return v
    return default


@router.get("/today/glance")
def today_glance(request: Request, user: dict = Depends(_user())):
    """The home-screen widget's payload: the Today hero's simple face, and
    nothing past it. One number (what is still spendable today across
    every variable bucket), the verdict word, the day bar, the bucket
    chips and the pace phrase — all lifted from the SAME context the page
    and the daily email render, so the widget can never show a number the
    app would not. No balances, no transactions, no account names: this
    is what a lock screen may show.

    The one door a widget token opens; a device token or a browser
    session reads it too (the app refreshes its own widget from here).
    Whole dollars, like every aggregate on the hero. Weak ETag so a
    widget polling on the OS's clock costs nothing when nothing moved."""
    import hashlib
    import json
    from fastapi.responses import Response
    from ..engine import budget
    from . import report, todayview
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        today = _today(conn)
        cfg = budget.load_config(conn)
        budgets_set = bool(cfg.get("food_monthly") or cfg.get("other_monthly"))
        if budgets_set:
            st = report.gather(conn, today, live=True)
            day = todayview.build_context(st)["day"]
        else:
            st = day = None
    finally:
        conn.close()
    if st is None:
        # the app shows no hero before a budget exists (a verdict on a
        # plan the household has not made is arithmetic, not news); the
        # widget says so instead of a $0 that would read as "nothing left"
        body = {"date": today.isoformat(), "budgets_set": False}
    else:
        body = {
            "date": st["today"].isoformat(),
            "budgets_set": True,
            "verdict": st["verdict"],
            "left_today": round(day["simple"]["left_today"]),
            "day_spent": round(day["day_spent"]),
            "day_allow": round(day["day_allow"]),
            "days_left": day["days_left"],
            "pace_line": day["pace_line"] or "",
            "chips": day["simple"]["chips"],
        }
    body["as_of"] = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds")
    payload = json.dumps({k: v for k, v in body.items() if k != "as_of"},
                         sort_keys=True)
    etag = 'W/"' + hashlib.sha256(payload.encode()).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(json.dumps(body), media_type="application/json",
                    headers={"ETag": etag, "Cache-Control": "private, no-cache"})


@router.get("/lens/month")
def lens_month(user: dict = Depends(_user()), y: int = 0, m: int = 0):
    """Month report card — final for past months, projected for the
    current one (default: this month)."""
    from . import lenses
    conn = _conn(user)
    try:
        # the household's day decides BOTH the default month and whether
        # the month is still open — on the server clock the summary calls
        # a live month final for the hours after UTC midnight
        today = _today(conn)
        y, m = y or today.year, m or today.month
        _ck_ym(y, m)
        return lenses.month_summary(conn, y, m, today=today)
    finally:
        conn.close()


@router.get("/lens/year")
def lens_year(user: dict = Depends(_user()), y: int = 0):
    """12-cell verdict grid + annual totals + category YoY (default: this
    year)."""
    from . import lenses
    conn = _conn(user)
    try:
        today = _today(conn)
        y = y or today.year
        _ck_year(y)
        return lenses.year_summary(conn, y, today=today)
    finally:
        conn.close()


@router.get("/budget/snapshots")
def budget_snapshots(user: dict = Depends(_user())):
    """Budget history for the Budget page's card: the frozen months
    (newest first, each month's variable budgets + how it was captured)
    and how many closed months since the ledger began have no snapshot —
    the months the backfill button would freeze."""
    from ..engine import budget
    conn = _conn(user)
    try:
        rows = conn.execute(
            "SELECT year, month, config, bills, captured_at, source "
            "FROM budget_snapshots ORDER BY year DESC, month DESC").fetchall()
        first = conn.execute("SELECT MIN(date) AS d FROM transactions "
                             "WHERE removed = 0").fetchone()
        cfg = budget.load_config(conn)
    finally:
        conn.close()
    from .. import localtime
    today = localtime.now_local(cfg).date()
    have = {(r["year"], r["month"]) for r in rows}
    missing = 0
    if first and first["d"]:
        y, m = first["d"].year, first["d"].month
        while (y, m) < (today.year, today.month):
            if (y, m) not in have:
                missing += 1
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    months = []
    for r in rows:
        c = dict(r["config"])
        # a malformed row (pre-validation restore) lists as absent
        # rather than 500ing the whole history card
        if not budget._snapshot_budgets_numeric(c):
            continue
        food = float(c.get("food_monthly") or 0)
        other = float(c.get("other_monthly") or 0)
        entry = {"y": r["year"], "m": r["month"],
                 "food_monthly": round(food, 2),
                 "other_monthly": round(other, 2),
                 "variable_budget": round(food + other, 2),
                 "source": r["source"],
                 "captured_at": r["captured_at"].isoformat()}
        # the schedule frozen with the budget — absent on snapshots that
        # predate bill freezing, and the card says "—" rather than back-
        # deriving a number the month never ran under
        sched = r["bills"] if isinstance(r["bills"], dict) else {}
        for k in ("bills_monthly", "income_monthly"):
            v = sched.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                entry[k] = round(float(v), 2)
        months.append(entry)
    return {"months": months, "missing": missing,
            "budget_set": bool((cfg.get("food_monthly") or 0) > 0
                               or (cfg.get("other_monthly") or 0) > 0)}


@router.post("/budget/snapshots/backfill")
def budget_snapshots_backfill(user: dict = Depends(_user())):
    """Freeze the CURRENT budget for every closed no-snapshot month since
    the ledger began — the owner's explicit opt-in to the retroactive
    judgment the lenses otherwise refuse to fabricate. Marked
    source='backfill'; existing snapshots and the current month are never
    touched. (Viewers are stopped by the session write gate.)"""
    demoguard.deny(user)
    from ..engine import budget
    conn = _conn(user)
    try:
        n = budget.backfill_snapshots(conn, _today(conn))
    finally:
        conn.close()
    return {"backfilled": n}


def _stamp_recurring(conn, rows: list[dict]) -> None:
    """Stamp each ledger row with `recurring_bill` — the active
    bill whose matcher counts it (the ⟳ indicator). Same semantics as the
    history page's ✓: the row's merchant is one the bill pays (its text
    tokens only for a bill with no identity) AND the amount is inside the
    bill's tolerance (envelopes take any spend row on their merchant)."""
    from ..engine import budget
    from ..engine.compat import as_dict
    bills = []
    active = conn.execute("SELECT payee, merchant, amount, raw "
                          "FROM bills WHERE active=1").fetchall()
    # each bill's identity, so the pill follows a renamed payee
    ids = budget.bill_merchant_ids(conn, active) if active else {}
    for b in active:
        raw = as_dict(b["raw"])
        ref = abs(b["amount"] or 0)
        bills.append((b["payee"],
                      budget.merchant_matcher(b["merchant"],
                                              budget._key_token(b["payee"]),
                                              ids.get(b["payee"] or "")),
                      raw.get("bill_type", "occurrence"),
                      (b["amount"] or 0) > 0,          # income series
                      ref, budget.bill_tolerance(ref)))
    for r in rows:
        r["recurring_bill"] = None
        if not bills:
            continue
        text = (r.get("payee") or "").lower()
        amt = r.get("amount") or 0
        for payee, match, btype, income, ref, tol in bills:
            if not match(text, None, r.get("merchant_id")):
                continue
            if btype == "envelope":
                if amt > 0:
                    r["recurring_bill"] = payee
                    break
            elif ((amt < 0 if income else amt > 0)
                  and abs(abs(amt) - ref) <= tol):
                r["recurring_bill"] = payee
                break


def _per_day(total, start, end, today: dt.date):
    """Average daily spend over a window: from `start` to `end` or today,
    whichever comes first — a month in progress divides by the days
    elapsed, a closed month by all of its days. (None, None) when there is
    no start to count from, the window has not begun, or nothing was spent.
    """
    if not total or not start:
        return None, None
    s = start if isinstance(start, dt.date) else dt.date.fromisoformat(str(start)[:10])
    e = end if isinstance(end, dt.date) else (
        dt.date.fromisoformat(str(end)[:10]) if end else today)
    days = (min(e, today) - s).days + 1
    if days <= 0:
        return None, None
    return round(total / days, 2), days


@router.get("/transactions")
def transactions(user: dict = Depends(_user()), y: int = 0, m: int = 0,
                 q: str = "", acct: str = "", cat: str = "",
                 date_from: str = "", date_to: str = "", page: int = 1,
                 reimb: int = 0, owner: str = "", scope: str = "all",
                 channel: str = "", checks: int = 0, bucket: str = "",
                 as_of: str = "", counted: int = 0):
    # bound like _ck_ym bounds y/m: an unbounded page multiplies into an
    # OFFSET past bigint inside the driver (NumericValueOutOfRange → 500)
    page = min(max(1, page), 10_000_000)
    # which rows the ledger lists: household + business (default, business
    # rows labelled by entity), household only, or business only
    if scope not in ("all", "personal", "business"):
        raise HTTPException(400, "scope must be all, personal or business")
    if channel and channel not in ("in store", "online", "other"):
        raise HTTPException(400, "channel must be in store, online or other")
    conn = _conn(user)
    try:
        today = _today(conn)
        _ck_ym(y or today.year, m or today.month)
        for _label, _v in (("date_from", date_from), ("date_to", date_to)):
            if _v:
                # as_date, not fromisoformat: a well-formed year the ledger
                # cannot read (0001, 9999) must be a 400 here, not a 500 in
                # the search below
                from ..engine.compat import as_date as _as_date
                try:
                    _as_date(_v[:10])
                except ValueError:
                    raise HTTPException(400, f"{_label} must be YYYY-MM-DD")
        yy, mm = y or today.year, m or today.month
        # `bucket` opens the rows behind ONE of the month verdict's own
        # numbers — Food, Everything else, or a custom carve-out. The set
        # cannot be expressed as a category filter (Food is a category OR
        # two override strings, minus every bill-shaped row, plus envelope
        # overflow; a carve-out is a plan label matched by merchant AND
        # category rules), so the engine names the rows and the search
        # surface renders them. Every other filter stands down: this is a
        # listing of a number, not a query.
        if bucket:
            from ..engine import budget as _budget
            from . import lenses as _lenses
            # a year's bucket — the year lens's money map — is `y` with no
            # `m`: the union of that bucket over the year's elapsed budgeted
            # months, the months the map summed. Its rows run to today.
            year_scope = bool(y) and not m
            read_on = today
            if year_scope:
                if as_of:
                    raise HTTPException(400, "as_of belongs to a month's bucket")
                info = _lenses.year_bucket_ledger(conn, bucket, yy, today)
            else:
                # the month lens' own state: a closed month is judged against
                # its frozen snapshot there, so the listing matches the tile
                # that month actually shows, not today's budget applied back.
                # `as_of` is the day a Today page was read on: a past day of
                # the month shows that day's figures, so its rows stop there.
                if as_of:
                    try:
                        read_on = dt.date.fromisoformat(as_of[:10])
                    except ValueError:
                        raise HTTPException(400, "as_of must be YYYY-MM-DD")
                    if (read_on.year, read_on.month) != (yy, mm) or read_on > today:
                        raise HTTPException(
                            400, "as_of must be a day of that month, not after today")
                info = _budget.bucket_ledger(
                    _lenses.month_state(conn, yy, mm, read_on), bucket)
            if info is None:
                raise HTTPException(400, "unknown bucket")
            rows, total, amount_sum, amazon_items, spend, account_hits = \
                data.search_transactions(conn, "", ids=info["txn_ids"],
                                         page=max(1, page), scope="all")
            out = [_ser(r) for r in rows]
            for o in out:
                o["category_why"] = _category_why(o)
            _stamp_recurring(conn, out)
            avg = (round(spend["sum"] / spend["count"], 2)
                   if spend["count"] else None)
            # a bucket is one month's (or one year's) number: its daily pace
            # is over that period's days up to the day it was read on
            per_day, per_day_days = (
                _per_day(spend["sum"], dt.date(yy, 1, 1), dt.date(yy, 12, 31),
                         read_on)
                if year_scope else
                _per_day(spend["sum"], dt.date(yy, mm, 1),
                         dt.date(yy, mm, calendar.monthrange(yy, mm)[1]),
                         read_on))
            return {"mode": "search", "rows": out,
                    "total": total, "amount_sum": amount_sum,
                    "amazon_items": amazon_items, "page": page,
                    "spend_count": spend["count"], "spend_sum": spend["sum"],
                    "spend_avg": avg, "biz_count": spend["biz_count"],
                    "per_day": per_day, "per_day_days": per_day_days,
                    "account_hits": account_hits,
                    # what the chip over the list says it is showing, and
                    # the figure the label wore: reimbursements netted and
                    # envelope overflow counted the way the plan counts
                    # them, which the listed rows' raw sum is not
                    "bucket": info["bucket"], "bucket_label": info["label"],
                    "bucket_amount": info["amount"],
                    "bucket_scope": "year" if year_scope else "month"}
        # `counted` is a lens CATEGORY label's link: the rows that label
        # summed (personal spend, money out, reimbursements netted) for one
        # month or one whole year — not every row filed under the category.
        # Like a bucket, the engine names the rows and the other lenses
        # stand down, and the total is the label's own number.
        if counted and cat:
            from . import lenses as _lenses
            if date_from or date_to:
                yr = date_from[:4]
                if not (yr.isdigit() and date_from[:10] == f"{yr}-01-01"
                        and date_to[:10] == f"{yr}-12-31"):
                    raise HTTPException(
                        400, "a counted category is one month or one whole year")
                info = _lenses.year_category_ledger(conn, cat, int(yr))
            else:
                info = _lenses.month_category_ledger(
                    _lenses.month_state(conn, yy, mm, today), cat)
            rows, total, _gross, amazon_items, spend, account_hits = \
                data.search_transactions(conn, "", ids=info["txn_ids"],
                                         page=max(1, page), scope="all")
            out = [_ser(r) for r in rows]
            for o in out:
                o["category_why"] = _category_why(o)
            _stamp_recurring(conn, out)
            avg = (round(spend["sum"] / spend["count"], 2)
                   if spend["count"] else None)
            # the label's own number per day over the month or year it counts
            if date_from:
                window = (dt.date(int(yr), 1, 1), dt.date(int(yr), 12, 31))
            else:
                window = (dt.date(yy, mm, 1),
                          dt.date(yy, mm, calendar.monthrange(yy, mm)[1]))
            per_day, per_day_days = _per_day(info["amount"], *window, today)
            return {"mode": "search", "rows": out,
                    "total": total, "amount_sum": info["amount"],
                    "amazon_items": amazon_items, "page": page,
                    "spend_count": spend["count"], "spend_sum": spend["sum"],
                    "spend_avg": avg, "biz_count": spend["biz_count"],
                    "per_day": per_day, "per_day_days": per_day_days,
                    "account_hits": account_hits, "counted": True}
        # a channel or checks-only lens is a search (the month browse has
        # no way to express it), like a category or an account is
        searching = bool(q.strip() or acct or cat or date_from or date_to
                         or channel or checks)
        # A month link ("Food, July") sends y/m beside the filter, but a
        # search reads neither, so the same link would open a category's
        # ALL-TIME history. The month's span is derived server-side, so every
        # client gets the month it asked for. An explicit range
        # still wins: it is the more specific instruction.
        if searching and (y or m) and not (date_from or date_to):
            date_from = dt.date(yy, mm, 1).isoformat()
            date_to = dt.date(yy, mm,
                              calendar.monthrange(yy, mm)[1]).isoformat()
        if searching:
            rows, total, amount_sum, amazon_items, spend, account_hits = \
                data.search_transactions(
                    conn, q, account_id=acct or None, category=cat or None,
                    date_from=date_from or None, date_to=date_to or None,
                    reimb_only=bool(reimb), page=max(1, page), scope=scope,
                    channel=channel or None, checks_only=bool(checks))
            out = [_ser(r) for r in rows]
            for o in out:
                o["category_why"] = _category_why(o)
            _stamp_recurring(conn, out)
            # A total answers "how much have I given them"; the average
            # answers "what does a visit cost", which is the question a
            # merchant search is usually really asking. Sent only when
            # there is spending to average — a search matching nothing but
            # income or transfers has no average, and 0 would read as one.
            avg = round(spend["sum"] / spend["count"], 2) if spend["count"] else None
            # the average per DAY answers "what is this costing me" over the
            # window asked for — a month's category, a date range. A search
            # with no start date has no window to divide by.
            per_day, per_day_days = _per_day(spend["sum"], date_from or None,
                                             date_to or None, today)
            return {"mode": "search", "rows": out,
                    "total": total, "amount_sum": amount_sum,
                    "amazon_items": amazon_items, "page": page,
                    "spend_count": spend["count"], "spend_sum": spend["sum"],
                    "spend_avg": avg,
                    "per_day": per_day, "per_day_days": per_day_days,
                    # matched rows that are a business's (not in amount_sum
                    # unless scope=business)
                    "biz_count": spend["biz_count"],
                    # accounts the term matched by name — via_bank marks a
                    # renamed account found through the bank's own name, so
                    # the page can explain a result that shows neither word
                    "account_hits": account_hits}
        rows = data.transactions(conn, y or today.year, m or today.month,
                                 reimb_only=bool(reimb), owner=owner or None,
                                 scope=scope)
        out = [_ser(r) for r in rows]
        for o in out:
            o["category_why"] = _category_why(o)
        _stamp_recurring(conn, out)
        # The month header states the month's CASH FLOW — income in, spend
        # out, net — by the same definitions the Cash Flow page uses, not a
        # raw sum of the listed rows. Raw sums would count a credit-card
        # payment as money out on top of the card swipes it paid for, and
        # count transfers between own accounts on both sides, so the
        # header would show an "overspent" month that was actually
        # positive. Transfers, card payments, linked
        # shadow rows and business-entity money are all excluded; the
        # owner/reimb quick-filters narrow the LIST but the headline stays
        # the month's — a display filter must not change what the month
        # cost. `biz_count` says how many listed rows are a business's.
        from ..engine import entities as _ent, reporting as _reporting
        combined = _ent.is_combined(conn)
        flows = _reporting.month_flows(conn, y or today.year,
                                       m or today.month)
        per_day, per_day_days = _per_day(
            flows["out"], dt.date(yy, mm, 1),
            dt.date(yy, mm, calendar.monthrange(yy, mm)[1]), today)
        return {"mode": "month", "year": y or today.year,
                "month": m or today.month, "rows": out,
                "out_sum": flows["out"], "in_sum": flows["in"],
                "net_sum": flows["net"],
                # the month's spending per day so far (every day, once closed)
                "per_day": per_day, "per_day_days": per_day_days,
                # zero under the combined toggle: the rows ARE household
                # money then, and the label the client hangs on this says
                # otherwise
                "biz_count": 0 if combined else
                             sum(1 for r in out if r.get("entity"))}
    finally:
        conn.close()


@router.post("/transactions/{txn_id}/category")
def set_category(txn_id: str, user: dict = Depends(_user()),
                 body: dict = Body(...)):
    category = _body_text(body, "category")
    # "one" (default) corrects THIS row only and writes no rule; "all" also
    # teaches the merchant, which is what the Rules page then shows. An
    # unconditional "all" would let one correction on a generic payee
    # ("Check Paid") claim every row sharing it.
    scope = _body_text(body, "scope", default="one").lower()
    if scope not in ("one", "all"):
        raise HTTPException(400, "scope must be 'one' or 'all'")
    if scope == "all":
        # "all" teaches the merchant — the same class of merchant-wide
        # rewrite as the history page's bulk set and the rules editor, and
        # demo-denied with them (shared tenant, hourly reset; a write
        # reshaping a whole merchant's history for every visitor in
        # between is the kind the instance does not host). The single-row
        # correction stays open: demo is fully editable row by row.
        demoguard.deny(user)
    conn = _conn(user)
    try:
        exists = conn.execute(
            "SELECT COALESCE(category_override, category_primary) AS cat "
            "FROM transactions WHERE id=%s AND removed=0",
            (txn_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "transaction not found")
        res = None
        if category:
            res = data.set_category(conn, txn_id, category, scope=scope)
        else:
            data.clear_category(conn, txn_id)
        _activity.record(
            conn, user, "category", "set" if category else "cleared",
            target=txn_id, label=_activity.txn_label(conn, txn_id),
            detail={"before": exists["cat"], "after": category or None,
                    "scope": scope,
                    "merchant": (res or {}).get("merchant") if res else None})
        # `rule_written` reports what happened: an "all" on a flow category or
        # a merchant-less row writes nothing, so `scope == "all"` alone would
        # claim a write that never happened. When a rule IS
        # written, `undo` carries the same snapshot the history page's
        # bulk write returns — POST it back to
        # /bills/merchant-category/undo to reverse the teach (the per-row
        # override itself stays; it was the user's direct answer).
        return {"ok": True, "category": category or None,
                "scope": scope, "rule_written": bool(res),
                "undo": (res or {}).get("undo")}
    finally:
        conn.close()


# one charge, several categories — the parts a person wrote for a single
# ledger row (engine/splits.py holds the rules). PUT replaces, DELETE
# removes; the ledger row carries the result as `split`, so a client that
# refetches its list sees it, and the category rollups read the parts.
@router.put("/transactions/{txn_id}/split")
def set_split_api(txn_id: str, user: dict = Depends(_user()),
                  body: dict = Body(...)):
    _may_edit(user)
    from ..engine import splits as _splits
    parts = body.get("parts")
    conn = _conn(user)
    try:
        try:
            clean = _splits.set_split(conn, txn_id, parts)
        except LookupError:
            raise HTTPException(404, "transaction not found")
        except _splits.SplitError as e:
            raise HTTPException(400, str(e))
        _activity.record(conn, user, "split", "set", target=txn_id,
                         label=_activity.txn_label(conn, txn_id),
                         detail={"parts": clean})
        return {"ok": True, "split": clean}
    finally:
        conn.close()


@router.delete("/transactions/{txn_id}/split")
def clear_split_api(txn_id: str, user: dict = Depends(_user())):
    _may_edit(user)
    from ..engine import splits as _splits
    conn = _conn(user)
    try:
        exists = conn.execute(
            "SELECT 1 FROM transactions WHERE id=%s AND removed=0",
            (txn_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "transaction not found")
        had = conn.execute("SELECT 1 FROM transaction_splits WHERE txn_id=%s "
                           "LIMIT 1", (txn_id,)).fetchone() is not None
        _splits.clear_split(conn, txn_id)
        if had:
            _activity.record(conn, user, "split", "cleared", target=txn_id,
                             label=_activity.txn_label(conn, txn_id))
        return {"ok": True, "split": None}
    finally:
        conn.close()


# household ownership attribution — set an account's default owner or
# a per-transaction override, and list the owners in use for the filter axis.
# Owner-only (a write). Attribution only — never touches the verdict.

@router.get("/owners")
def owners_api(user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        return {"owners": data.list_owners(conn)}
    finally:
        conn.close()


@router.post("/accounts/{account_id}/owner")
def set_account_owner_api(account_id: str, user: dict = Depends(_user()),
                          body: dict = Body(...)):
    _may_edit(user)
    owner = _body_text(body, "owner")
    conn = _conn(user)
    try:
        n = data.set_account_owner(conn, account_id, owner or None)
        if not n:
            raise HTTPException(404, "account not found")
        return {"ok": True, "owner": owner or None}
    finally:
        conn.close()


@router.post("/transactions/bulk",
             dependencies=[Depends(limit("txn_bulk", 60, 3600))])
def transactions_bulk(user: dict = Depends(_user()), body: dict = Body(...)):
    """One action across an explicitly-selected set of transactions.

    Distinct from the merchant-wide write on purpose. That one infers a rule
    from a single correction and therefore refuses to touch transfers and
    income — a rule that reclassified money movement as spending would
    double-count it everywhere. Here the user picked the rows, which is
    consent to exactly those rows, and no rule is written, so nothing
    propagates forward or onto the Rules page.

    The response says what actually happened rather than reporting success:
    how many moved, how many ids no longer exist, and how many were flow rows
    — that last so the caller can point out that a transfer just became
    spending, which is a real consequence of a legitimate request.
    """
    demoguard.deny(user)
    _may_edit(user)
    ids = body.get("ids") or []
    if not isinstance(ids, list) or len(ids) > 1000:
        raise HTTPException(400, "ids must be a list of at most 1000")
    action = str(body.get("action") or "")
    conn = _conn(user)
    try:
        try:
            out = data.bulk_apply(conn, [str(i) for i in ids], action,
                                  str(body.get("category") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if out.get("applied"):
            _log_bulk(conn, user, action, str(body.get("category") or ""),
                      [str(i) for i in ids], out)
        return {"ok": True, **out}
    finally:
        conn.close()


@router.post("/transactions/retire-pending")
def retire_pending(user: dict = Depends(_user()), body: dict = Body(...)):
    """Retire a pending authorization the bank abandoned.

    The connectors retire the holds their own source stops carrying; a hold
    on a file-imported account, or on a connection that has since died, has
    nobody to do that and counts as spend forever. This is the person's
    answer to that row — and only to that row: it refuses anything already
    posted, and anything still inside the two-week window, because a hold
    that is merely slow would come back as a duplicate once it settled.
    """
    demoguard.deny(user)
    _may_edit(user)
    txn_id = _body_text(body, "id")
    if not txn_id:
        raise HTTPException(400, "id is required")
    conn = _conn(user)
    try:
        try:
            data.retire_pending(conn, txn_id)
        except LookupError:
            raise HTTPException(404, "transaction not found")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "id": txn_id}
    finally:
        conn.close()


# ---- merchants: the one place a person can correct merchant identity ----
# Every naming rule in the app is deliberately conservative, because a wrong
# answer the person looking at it cannot fix is worse than a missed merge.
# These three endpoints are what let the rules relax: a correction here
# outranks every automatic layer and is itself undoable.


@router.get("/merchants/catalog")
def merchants_catalog(user: dict = Depends(_user()), q: str = "",
                      limit: int = 200, order: str = "count"):
    """Merchants as the ledger shows them, with the raw strings underneath.

    Distinct from GET /merchants, which is the bill picker's flat list of
    known names — this one carries the counts, totals and variants the
    merchants screen needs.

    The variants are the point: a merchant that looks wrong is usually
    several source strings that should be one, or one that should be
    several.

    `order` is "count" (busiest first) or "alpha". The list is capped, and
    under count-order the cap cuts exactly the long-tail merchants most
    likely to need correction, so the screen needs a way to walk the tail."""
    from ..engine import merchant_dedup, merchant_merge
    # Validated here rather than letting the engine's ValueError escape: an
    # unknown sort is a bad request, and unhandled it would have been a 500.
    if order not in ("count", "alpha"):
        raise HTTPException(400, "order must be 'count' or 'alpha'")
    conn = _conn(user)
    try:
        rows = merchant_dedup.listing(conn, q, min(500, max(1, limit)),
                                      order=order)
        recent = conn.execute(
            "SELECT id, raw_merchant, from_canonical, to_canonical, at "
            "FROM merchant_renames ORDER BY at DESC LIMIT 20").fetchall()
        return {"merchants": [dict(r) for r in rows],
                "recent": [dict(r) for r in recent],
                # merchants the ledger thinks are one business, for the
                # person to merge or keep apart — never merged unasked
                "merges": merchant_merge.pending(conn)}
    finally:
        conn.close()


@router.post("/merchants/merge")
def merchant_merge_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """Decide a merge proposal: approve merges through the same journalled
    rename the Merchants page uses (so it shows under Recent changes and
    undo works unchanged); reject keeps the two apart and the pair is never
    offered again."""
    from ..engine import merchant_merge
    demoguard.deny(user)
    _may_edit(user)
    action = _body_text(body, "action")
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    pid = _body_text(body, "pid")
    if not pid:
        raise HTTPException(400, "pid required")
    conn = _conn(user)
    try:
        r = merchant_merge.decide(conn, pid, action)
        if not r.get("error"):
            _log_merge(conn, user, pid, action, r)
    finally:
        conn.close()
    if r.get("error"):
        raise HTTPException(400, r["error"])
    return {"ok": True, **r}


def _merchant_name_or_400(field: str, val: str,
                          max_len: int | None = 200) -> None:
    """A merchant name from a client: bounded, and free of control
    characters (C0/C1), which only ever arrive from a broken or hostile
    client. Shared by every merchant endpoint that takes one. Writes keep
    the cap (rename journals the name once per variant and renders it
    everywhere); the detail READ passes max_len=None — an imported
    descriptor longer than 200 chars is a real display name the catalog
    lists, and capping the read would make exactly those rows unopenable."""
    if max_len is not None and len(val) > max_len:
        raise HTTPException(400, f"{field} too long ({max_len} chars max)")
    if any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in val):
        raise HTTPException(400, f"{field} contains control characters")


@router.get("/merchants/detail")
def merchants_detail(display: str, user: dict = Depends(_user())):
    """Everything about one merchant as the ledger shows it — the
    expandable row on the merchants screen (facts, category, places)."""
    from ..engine import merchant_dedup
    # read path: no length cap — see _merchant_name_or_400
    _merchant_name_or_400("display", display, max_len=None)
    conn = _conn(user)
    try:
        d = merchant_dedup.detail(conn, display)
        if d is None:
            raise HTTPException(404, f"no merchant displaying as {display!r}")
        return d
    finally:
        conn.close()


@router.post("/merchants/rename")
def merchant_rename(user: dict = Depends(_user()), body: dict = Body(...)):
    """Rename a merchant, or merge it into another by naming that one.

    One endpoint for both because they are one act: pointing the source
    strings at a name. Whether that name already exists is a fact about the
    rest of the ledger, not a different operation."""
    from ..engine import merchant_dedup
    demoguard.deny(user)
    _may_edit(user)
    display = _body_text(body, "display")
    to = _body_text(body, "to")
    if not display or not to:
        raise HTTPException(400, "display and to are both required")
    # the target name is written into merchant_canonical for every variant,
    # journalled once per variant, and rendered on every surface — so cap it
    for _field, _val in (("display", display), ("to", to)):
        _merchant_name_or_400(_field, _val)
    conn = _conn(user)
    try:
        # a rename onto a name the ledger already shows is a merge: read
        # that before the act, for the sentence
        _into_exists = conn.execute(
            "SELECT 1 FROM merchants WHERE name=%s AND merged_into IS NULL "
            "LIMIT 1", (to,)).fetchone() is not None
        with conn.transaction():
            result = merchant_dedup.rename(conn, display, to)
        if not result["raws"]:
            raise HTTPException(404, f"no merchant displaying as {display!r}")
        _activity.record(conn, user, "merchant",
                         "merged" if _into_exists and to != display else "renamed",
                         target=to, label=display,
                         detail={"before": display, "after": to,
                                 "rows": result.get("rows")})
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/merchants/undo")
def merchant_undo(user: dict = Depends(_user()), body: dict = Body(...)):
    """Replay one journalled naming change backwards."""
    from ..engine import merchant_dedup
    demoguard.deny(user)
    _may_edit(user)
    try:
        change_id = int(body.get("id"))
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, "id is required")
    # journal ids are bigserial; a JSON number past bigint would only blow
    # up inside the driver as an out-of-range bind — an opaque 500 — so
    # bound it here where it can be a 400
    if not 1 <= change_id <= 2 ** 62:
        raise HTTPException(400, "id out of range")
    conn = _conn(user)
    try:
        with conn.transaction():
            out = merchant_dedup.undo(conn, change_id)
        _activity.record(conn, user, "merchant", "undone",
                         target=str(out.get("restored") or ""),
                         label=str(out.get("restored") or out.get("raw_merchant")
                                   or f"change {change_id}"))
        return out
    except LookupError as e:
        raise HTTPException(404, str(e))
    finally:
        conn.close()


@router.post("/transactions/{txn_id}/note")
def set_txn_note(txn_id: str, user: dict = Depends(_user()),
                 body: dict = Body(...)):
    """Set (or clear, when empty) a free-text note on a transaction."""
    demoguard.deny(user)
    _may_edit(user)
    conn = _conn(user)
    try:
        if not conn.execute("SELECT 1 FROM transactions WHERE id=%s AND "
                            "removed=0", (txn_id,)).fetchone():
            raise HTTPException(404, "transaction not found")
        prev = conn.execute("SELECT note FROM transaction_notes WHERE txn_id=%s",
                            (txn_id,)).fetchone()
        prev = prev["note"] if prev else ""
        note = data.set_note(conn, txn_id, str(body.get("note") or ""))
        if note != prev:
            _activity.record(
                conn, user, "note",
                "cleared" if not note else ("edited" if prev else "added"),
                target=txn_id, label=_activity.txn_label(conn, txn_id),
                detail={"before": prev or None, "after": note or None})
        return {"ok": True, "note": note}
    finally:
        conn.close()


@router.post("/transactions/{txn_id}/owner")
def set_txn_owner_api(txn_id: str, user: dict = Depends(_user()),
                      body: dict = Body(...)):
    _may_edit(user)
    owner = _body_text(body, "owner")
    conn = _conn(user)
    try:
        n = data.set_txn_owner(conn, txn_id, owner or None)
        if not n:
            raise HTTPException(404, "transaction not found")
        return {"ok": True, "owner": owner or None}
    finally:
        conn.close()


@router.get("/categories")
def categories(user: dict = Depends(_user())):
    """``categories`` = what this tenant actually uses (filter dropdowns);
    ``plaid_spend``/``plaid_flow`` = the standard Plaid primaries, for the
    pickers that SET a category rather than filter by one."""
    from ..engine import categories as cat
    conn = _conn(user)
    try:
        return {"categories": data.all_categories(conn),
                "plaid_spend": list(cat.PLAID_SPEND),
                "plaid_flow": list(cat.FLOW)}
    finally:
        conn.close()


@router.get("/accounts")
def accounts(user: dict = Depends(_user())):
    from ..engine import links
    conn = _conn(user)
    try:
        rows = [_ser(r) for r in data.accounts_detail(conn)]
        # which linked account is serving, which are shadows
        link_info: dict[str, dict] = {}
        for g in links.groups(conn):
            for m in g["members"]:
                link_info[m["account_id"]] = {
                    "group_id": g["group_id"], "home_rank": m["home_rank"],
                    "primary": m["primary"], "healthy": m["healthy"]}
        # items fed by a collector script carry their heartbeat —
        # the Accounts page badges them "Script · api/scrape", not "Import".
        # Live freshness for the status dot: ok = a
        # successful push within 26h, the same threshold Doctor applies to
        # aggregator connections. Heartbeats stamp only on SUCCESS, so
        # last_push IS the last good run; there is no stored error to show.
        # One cause the server CAN name: a password change or reset
        # revokes every script token, and the collector keeps running on
        # its host, pushing into a 401 it has no way to report here. When
        # a token named for this source was revoked after the last good
        # push, say so — otherwise a red dot sends the person to check a
        # collector that is perfectly healthy. "Named for" = the source
        # itself or "<source>-<anything>", a delimiter on purpose: a plain
        # prefix would let "amazon-orders-key" stand in for "amazon", and a live
        # token for one collector must never clear another's revocation.
        script_info = {r["source"]: {"label": r["label"], "via": r["via"],
                                     "kind": r["kind"], "source": r["source"],
                                     "last_push": r["last_push"].isoformat(),
                                     "ok": float(r["age_hours"]) <= 26,
                                     "token_revoked_at":
                                         r["token_revoked_at"].isoformat()
                                         if r["token_revoked_at"] else None}
                       for r in conn.execute(
                           # raw string: the LIKE-escape backslashes (\_,
                           # \%) are SQL's, not Python's — unprefixed they
                           # are invalid Python escapes (SyntaxWarning
                           # today, an error in a future Python)
                           r"""SELECT h.source, h.label, h.via, h.kind,
                                     h.last_push,
                                     EXTRACT(EPOCH FROM now() - h.last_push)
                                       /3600 AS age_hours,
                                     (SELECT max(t.revoked_at)
                                        FROM api_tokens t
                                       WHERE t.tenant_id = %s
                                         AND t.revoked_at > h.last_push
                                         AND (lower(t.name) = lower(h.source)
                                              OR lower(t.name) LIKE
                                                 replace(replace(lower(h.source),
                                                   '_', '\_'), '%%', '\%%')
                                                 || '-%%' ESCAPE '\')
                                         AND NOT EXISTS (
                                           SELECT 1 FROM api_tokens l
                                            WHERE l.tenant_id = t.tenant_id
                                              AND l.revoked_at IS NULL
                                              AND (lower(l.name) = lower(h.source)
                                                   OR lower(l.name) LIKE
                                                      replace(replace(lower(h.source),
                                                        '_', '\_'), '%%', '\%%')
                                                      || '-%%' ESCAPE '\')))
                                       AS token_revoked_at
                                FROM script_heartbeats h""",
                           (user["tenant_id"],)).fetchall()}
        for r in rows:
            r["link"] = link_info.get(r["id"])
            r["script"] = script_info.get(r["item_id"])
        return {"accounts": rows}
    finally:
        conn.close()


# ---- provider setup: MX + live key validation ----------

@router.post("/accounts/mx/keys")
def mx_keys(user: dict = Depends(_user()), body: dict = Body(...)):
    """Save BYO MX credentials — validated LIVE first (rule: you
    can't finish a setup flow with broken keys). api_key encrypted."""
    demoguard.deny(user)
    _no_byo_on_hosted()
    from ..db import crypto
    from ..engine import budget
    from ..sync import mx as mx_mod
    client_id = str(body.get("client_id") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    env = str(body.get("env") or "production")
    if env not in mx_mod.BASES:
        raise HTTPException(400, "env must be production or sandbox")
    if not client_id or not api_key:
        raise HTTPException(400, "client_id and api_key are required")
    try:
        if not mx_mod.validate(client_id, api_key, env):
            raise HTTPException(400, "MX rejected these keys (401) — "
                                     "check client_id/api_key and env")
    except HTTPException:
        raise
    except Exception as e:                        # noqa: BLE001
        raise HTTPException(400, f"couldn't reach MX: {type(e).__name__}")
    conn = _conn(user)
    try:
        with budget.config_txn(conn) as cfg:
            cfg["mx_client_id"] = client_id
            cfg["mx_api_key"] = crypto.encrypt(conn, api_key)
            cfg["mx_env"] = env
            cfg.pop("mx_user_guid", None)  # new keys → new MX user next use
    finally:
        conn.close()
    return {"ok": True, "env": env}


@router.post("/accounts/mx/connect")
def mx_connect(user: dict = Depends(_user())):
    """A fresh MX Connect-widget URL to link (or repair) banks."""
    demoguard.deny(user)
    from ..sync import mx as mx_mod
    conn = _conn(user)
    try:
        try:
            return {"url": mx_mod.connect_widget_url(conn)}
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:                    # noqa: BLE001
            raise HTTPException(400, f"MX error: {type(e).__name__}")
    finally:
        conn.close()


@router.post("/accounts/mx/sync")
def mx_sync_now(user: dict = Depends(_user())):
    demoguard.deny(user)
    from ..sync import mx as mx_mod
    from ..sync.base import tenant_sync_lock
    conn = _conn(user)
    try:
        with tenant_sync_lock(conn, user["tenant_id"]) as held:
            if not held:
                # the holder runs one more pass for this request (worker's
                # nudge) — the tenant does not wait for the hourly catch-up
                from ..jobs.worker import _nudge_sync
                _nudge_sync(conn)
                raise HTTPException(409, "a sync is already running — "
                                         "give it a moment")
            try:
                return {"ok": True, **mx_mod.sync(conn)}
            except ValueError as e:
                raise HTTPException(400, str(e))
            except Exception as e:                # noqa: BLE001
                raise HTTPException(
                    400, f"MX sync failed: {type(e).__name__}")
    finally:
        conn.close()


@router.post("/accounts/plaid/keys")
def plaid_keys(user: dict = Depends(_user()), body: dict = Body(...)):
    """Save BYO Plaid credentials after a LIVE validation —
    the guided flow's final step. Secret encrypted at rest."""
    demoguard.deny(user)
    _no_byo_on_hosted()
    from ..db import crypto
    from ..engine import budget
    r = plaid_validate(user=user, body=body)     # raises 400 on bad keys
    conn = _conn(user)
    try:
        with budget.config_txn(conn) as cfg:
            cfg["plaid_client_id"] = str(body["client_id"]).strip()
            cfg["plaid_secret"] = crypto.encrypt(conn,
                                                 str(body["secret"]).strip())
            cfg["plaid_env"] = r["env"]
    finally:
        conn.close()
    return {"ok": True, "env": r["env"]}


@router.delete("/accounts/plaid/keys")
def plaid_keys_clear(user: dict = Depends(_user())):
    """Forget the tenant's BYO Plaid credentials.

    Refused while live Plaid connections still hold an access token. An
    access token is only meaningful to the client_id that minted it, so
    once these keys are gone nothing can ever call /item/remove for those
    Items again: they stay subscribed (and billing) at Plaid with no
    handle left to release them by. Disconnecting the connection is the
    door that releases it upstream, so the refusal names the institutions
    to disconnect first — the same shape as refusing to purge an account
    on a live connection.

    Connections already archived while still holding a token (a
    disconnect whose release call failed) get one last best-effort
    release here, since clearing the keys is the reaper's last chance too.
    """
    demoguard.deny(user)
    from ..engine import budget
    conn = _conn(user)
    try:
        live = conn.execute(
            "SELECT id, institution_name FROM items WHERE aggregator='plaid' "
            "AND access_token IS NOT NULL "
            "AND COALESCE(status,'ok') <> 'archived' ORDER BY id").fetchall()
        if live:
            names = ", ".join(r["institution_name"] or r["id"] for r in live)
            raise HTTPException(
                400,
                f"Disconnect these Plaid connections first: {names}. Their "
                f"access tokens only work with these keys — clearing the keys "
                f"now would leave the connections billing at Plaid with no "
                f"way to release them.")
        released, stranded = _plaid_release_stragglers(conn)
        with budget.config_txn(conn) as cfg:
            for k in ("plaid_client_id", "plaid_secret", "plaid_env"):
                cfg.pop(k, None)
    finally:
        conn.close()
    return {"ok": True, "released": released, "stranded": stranded}


def _plaid_release_stragglers(conn) -> tuple[int, int]:
    """Best-effort /item/remove for every Plaid Item still holding a token
    (the caller has already refused if any of them is live), clearing the
    token on the ones that release. Returns (released, stranded). Never
    raises: the keys are going either way, and a Plaid outage must not wedge
    the door shut — the same trade erasure makes.
    """
    import logging

    from ..sync import base as sync_base
    from ..sync import plaid as plaid_mod
    rows = conn.execute(
        "SELECT id FROM items WHERE aggregator='plaid' "
        "AND access_token IS NOT NULL ORDER BY id").fetchall()
    if not rows:
        return 0, 0
    try:
        client = plaid_mod.Client.for_tenant(conn)
    except Exception:                                     # noqa: BLE001
        return 0, len(rows)                               # no usable creds
    released = 0
    for r in rows:
        try:
            token = sync_base.get_access_token(conn, r["id"])
            if token:
                try:
                    client.remove_item(token)
                except plaid_mod.PlaidError as e:
                    # already gone at Plaid counts as released — the same
                    # tolerance every other release path applies
                    if e.code != "ITEM_NOT_FOUND":
                        raise
            conn.execute("UPDATE items SET access_token=NULL WHERE id=%s",
                         (r["id"],))
            plaid_mod.stamp_ledger_removed(r["id"], "keys-cleared")
            released += 1
        except Exception as e:                            # noqa: BLE001
            logging.getLogger("oikonome.api").warning(
                "plaid release before key clear failed item=%s: %s",
                r["id"], e)
    return released, len(rows) - released


@router.delete("/accounts/mx/keys")
def mx_keys_clear(user: dict = Depends(_user())):
    demoguard.deny(user)
    from ..engine import budget
    conn = _conn(user)
    try:
        with budget.config_txn(conn) as cfg:
            for k in ("mx_client_id", "mx_api_key", "mx_env", "mx_user_guid"):
                cfg.pop(k, None)
    finally:
        conn.close()
    return {"ok": True}


@router.post("/connections/export",
             dependencies=[Depends(limit("connections_bundle", 5, 3600))])
def connections_export(user: dict = Depends(_user()), body: dict = Body(...)):
    """Passphrase-sealed full-config bundle — provider keys, LLM,
    SMTP, and live bank links — master-key-independent so it survives a move
    to a fresh environment. Owner only: these are instance-wide credentials,
    not one member's view."""
    demoguard.deny(user)
    from fastapi.responses import Response

    from ..sync import connections_bundle as cb
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    # This door decrypts EVERY tenant credential (provider
    # keys, LLM/SMTP secrets, live bank access tokens) and seals them under
    # a passphrase the CALLER chooses — encryption protects nothing when the
    # requester supplies the key. A bare stolen cookie must not reach it:
    # password step-up (+ passkey/recovery for passkey-only accounts), the
    # same rule every other durable-credential door already follows.
    # And, on a TOTP account, a live code — the same rule the script-token
    # and support-access doors follow, for a strictly larger prize: what
    # leaves here is every credential the tenant has, in a file whose only
    # lock is a passphrase the caller picked. A session thief who also has
    # the password must not walk past the second factor to get it. The
    # elevation must be FRESH (seconds, not minutes): the prize is too
    # large to ride a proof given ten minutes ago for something else.
    from .app import _require_elevation
    _require_elevation(user, fresh_seconds=60,
                       password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    passphrase = str(body.get("passphrase") or "")
    conn = _conn(user)
    try:
        try:
            blob = cb.export_bytes(conn, passphrase)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()
    stamp = f"{dt.datetime.now():%Y%m%d}"
    return Response(
        blob, media_type="application/octet-stream",
        headers={"Content-Disposition":
                 f'attachment; filename="oikonome-config-{stamp}.oikx"'})


@router.post("/connections/import",
             dependencies=[Depends(limit("connections_bundle", 5, 3600))])
def connections_import(user: dict = Depends(_user()),
                       passphrase: str = Form(...),
                       file: UploadFile = File(...),
                       password: str = Form(""),
                       recovery_code: str = Form(""),
                       totp_code: str = Form("")):
    """Merge a sealed config bundle into this instance, re-encrypting every
    secret under the local master key. Idempotent (items upsert)."""
    demoguard.deny(user)
    from ..sync import connections_bundle as cb
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    # The export door's twin: importing plants credentials —
    # aggregator items whose tokens sync money data. Same step-up, and the
    # same live code on a TOTP account: symmetric doors, one rule — down
    # to the FRESH window the export demands.
    from .app import _require_elevation
    _require_elevation(user, fresh_seconds=60, password=password,
                       totp_code=totp_code, recovery_code=recovery_code)
    data = file.file.read(4 * 1024 * 1024)        # a bundle is KBs; cap sanely
    conn = _conn(user)
    try:
        try:
            summary = cb.import_bytes(conn, data, passphrase)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()
    return {"ok": True, **summary}


@router.post("/accounts/plaid/validate")
def plaid_validate(user: dict = Depends(_user()), body: dict = Body(...)):
    """Live Plaid key check for the guided setup flow: one
    cheap authenticated call; nothing is saved here."""
    demoguard.deny(user)
    _no_byo_on_hosted()
    import httpx as _httpx

    from ..sync import plaid as plaid_mod
    client_id = str(body.get("client_id") or "").strip()
    secret = str(body.get("secret") or "").strip()
    env = str(body.get("env") or "production")
    base = plaid_mod.ENV_URLS.get(env)
    if base is None:
        raise HTTPException(400, "env must be production or sandbox")
    if not client_id or not secret:
        raise HTTPException(400, "client_id and secret are required")
    try:
        r = _httpx.post(base + "/institutions/get",
                        json={"client_id": client_id, "secret": secret,
                              "count": 1, "offset": 0,
                              "country_codes": ["US"]}, timeout=30)
    except Exception as e:                        # noqa: BLE001
        raise HTTPException(400, f"couldn't reach Plaid: {type(e).__name__}")
    if r.status_code != 200:
        code = (r.json().get("error_code")
                if "json" in r.headers.get("content-type", "") else r.text[:80])
        raise HTTPException(400, f"Plaid rejected these keys: {code}")
    return {"ok": True, "env": env}


# ---- multi-source account links -----------------------------------

@router.get("/accounts/links")
def account_links_get(user: dict = Depends(_user())):
    """Current link groups + auto-suggested pairs awaiting the owner's
    confirmation (same institution-mask-type heuristic as the spec)."""
    from ..engine import budget, links
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
        return {"groups": links.groups(conn),
                "suggestions": links.suggestions(
                    conn, dismissed=cfg.get("link_dismissed")),
                # the standing "who serves, who backs up" card is status,
                # not a decision; once read it may be put away — the client
                # brings it back on its own when a suggestion or a downed
                # source needs the owner
                "card_dismissed": bool(cfg.get("link_card_dismissed"))}
    finally:
        conn.close()


@router.post("/accounts/links")
def account_links_create(user: dict = Depends(_user()),
                         body: dict = Body(...)):
    from ..engine import links
    conn = _conn(user)
    try:
        raw = body.get("account_ids") or []
        if not isinstance(raw, list):
            raise HTTPException(400, "account_ids must be a list")
        ids = [str(i) for i in raw]
        if body.get("auto_order"):
            ids = links.default_order(conn, ids)
        try:
            gid = links.create(conn, ids)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"group_id": gid}
    finally:
        conn.close()


@router.delete("/accounts/links/{group_id}")
def account_links_delete(group_id: str, user: dict = Depends(_user())):
    from ..engine import links
    conn = _conn(user)
    try:
        if not links.unlink(conn, group_id):
            raise HTTPException(404, "no such link")
        return {"ok": True}
    finally:
        conn.close()


@router.post("/accounts/links/{group_id}/order")
def account_links_order(group_id: str, user: dict = Depends(_user()),
                        body: dict = Body(...)):
    from ..engine import links
    conn = _conn(user)
    try:
        try:
            # str() keeps a nested list/dict/number element from reaching
            # set()/SQL as an unhashable or raw parameter (same guard as
            # /api/reimburse/link); a non-list scalar is refused outright
            raw = body.get("account_ids") or []
            if not isinstance(raw, list):
                raise HTTPException(400, "account_ids must be a list")
            links.reorder(conn, group_id, [str(i) for i in raw])
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}
    finally:
        conn.close()


@router.post("/accounts/links/dismiss")
def account_links_dismiss(user: dict = Depends(_user()),
                          body: dict = Body(...)):
    """Remember a rejected suggestion so it stops nagging — or, with
    `card`, put the standing linked-sources card away (false shows it
    again). Linking and unlinking live in each account's editor, so the
    card carries no action the owner would lose by hiding it."""
    from ..engine import budget
    card = body.get("card")
    key = str(body.get("key") or "")
    if card is None and "|" not in key:
        raise HTTPException(422, "key required")
    conn = _conn(user)
    try:
        with budget.config_txn(conn) as cfg:
            if card is not None:
                cfg["link_card_dismissed"] = bool(card)
            else:
                dismissed = set(cfg.get("link_dismissed") or [])
                dismissed.add(key)
                cfg["link_dismissed"] = sorted(dismissed)
        return {"ok": True}
    finally:
        conn.close()


@router.get("/reimburse/pending")
def reimburse_pending(user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        return {"pending": [_ser(r) for r in data.pending_reimbursements(conn)]}
    finally:
        conn.close()


@router.get("/reimburse/pairs")
def reimburse_pairs(user: dict = Depends(_user())):
    """Linked pairs, newest first — what the undo button acts on."""
    conn = _conn(user)
    try:
        return {"pairs": [_ser(p)
                          for p in data.recent_reimbursement_pairs(conn)]}
    finally:
        conn.close()


@router.get("/reimburse/{txn_id}/candidates")
def reimburse_candidates(txn_id: str, user: dict = Depends(_user()),
                         q: str = ""):
    conn = _conn(user)
    try:
        anchor, cands = data.reimbursement_candidates(conn, txn_id, q=q)
        if anchor is None:
            raise HTTPException(404, "transaction not found")
        pairs = data.reimbursement_pairs(conn, txn_id)
        paired = {p["other_id"] for p in pairs}
        return {"anchor": _ser(anchor),
                "pairs": [_ser(p) for p in pairs],
                "candidates": [_ser(c) for c in cands
                               if c["id"] not in paired]}
    finally:
        conn.close()


@router.post("/reimburse/link",
             dependencies=[Depends(limit("reimburse_link", 60, 3600))])
def reimburse_link(user: dict = Depends(_user()), body: dict = Body(...)):
    txn_id = str(body.get("txn_id") or "")
    other_ids = body.get("other_ids") or []
    if not txn_id or not other_ids:
        raise HTTPException(400, "txn_id and other_ids are required")
    # every id fans out into per-row DB work on this one held connection, so
    # the client-controlled list gets the same cap and throttle as the
    # sibling bulk endpoint; str() keeps a nested list/dict/number element
    # from reaching the SQL adapter as a raw parameter
    if not isinstance(other_ids, list) or len(other_ids) > 1000:
        raise HTTPException(400, "other_ids must be a list of at most 1000")
    conn = _conn(user)
    try:
        out = data.link_reimbursements(conn, txn_id,
                                       [str(i) for i in other_ids],
                                       partial=bool(body.get("partial")))
        # the log says what happened, not what was asked: a request whose
        # every pair was refused linked nothing, and a full link the server
        # made partial is recorded as partial
        if out["linked"] > 0:
            _activity.record(conn, user, "reimbursement", "linked",
                             target=txn_id,
                             label=_activity.txn_label(conn, txn_id),
                             detail={"count": out["linked"],
                                     "partial": bool(body.get("partial"))
                                     or bool(out.get("partial"))})
        return out
    finally:
        conn.close()


@router.post("/reimburse/unlink")
def reimburse_unlink(user: dict = Depends(_user()), body: dict = Body(...)):
    conn = _conn(user)
    try:
        expense_id = str(body.get("expense_id") or "")
        data.unlink_reimbursement(conn, expense_id,
                                  str(body.get("reimburse_id") or ""))
        if expense_id:
            _activity.record(conn, user, "reimbursement", "unlinked",
                             target=expense_id,
                             label=_activity.txn_label(conn, expense_id))
        return {"ok": True}
    finally:
        conn.close()


@router.post("/reimburse/{txn_id}/flag")
def reimburse_flag(txn_id: str, user: dict = Depends(_user()),
                   body: dict | None = Body(default=None)):
    body = body or {}
    expected = body.get("expected")
    try:
        expected = float(expected) if expected not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "expected must be a number")
    # NaN/Infinity parse fine and slip past the <=0 check (all NaN comparisons
    # are False), then poison the DOUBLE column and 500 every later
    # /reimburse/pending response (allow_nan=False) — reject them.
    import math
    if expected is not None and not math.isfinite(expected):
        raise HTTPException(400, "expected must be a finite number")
    if expected is not None and expected <= 0:
        raise HTTPException(400, "expected must be positive")
    conn = _conn(user)
    try:
        data.flag_reimbursement(conn, txn_id,
                                partial=bool(body.get("partial")),
                                expected=expected)
        _activity.record(conn, user, "reimbursement", "flagged", target=txn_id,
                         label=_activity.txn_label(conn, txn_id),
                         detail={"expected": expected,
                                 "partial": bool(body.get("partial"))})
        return {"ok": True}
    finally:
        conn.close()


@router.post("/reimburse/{txn_id}/unflag")
def reimburse_unflag(txn_id: str, user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        data.unflag_reimbursement(conn, txn_id)
        _activity.record(conn, user, "reimbursement", "unflagged",
                         target=txn_id,
                         label=_activity.txn_label(conn, txn_id))
        return {"ok": True}
    finally:
        conn.close()


# a bill with no occurrence/envelope activity this month (income bills, or a
# fresh tenant) still carries the stats keys — the SPA renders "·" from zeros
_EMPTY_BILL_STATS = {"occurrences": 0, "paid": 0, "paid_amount": 0.0,
                     "used": None, "overflow": None, "next_unpaid": None,
                     "pool": None, "period_months": None,
                     "period_used": None, "period_left": None}


# moved to report.bills_remaining_month (gather computes it for both the
# SPA payload and the email); alias kept for callers and tests. F401 is
# suppressed because this IS the re-export — nothing in this module uses it.
from .report import bills_remaining_month as _bills_remaining_month  # noqa: E402,F401


def _month_bill_stats(conn, cfg: dict, today: dt.date) -> dict[str, dict]:
    """Per-bill occurrence/envelope detail for the current month — the Bills
    page's columns (Paid x/y; envelope Used/Chgs/Paid), computed once per
    request from the same engine passes month_status runs. Caps apply;
    DISABLED bills still get stats — they are marked, not counted, on the
    page.

    Every number here has a twin on Today and in the email, so this reads
    the ledger the way month_status reads it — same candidate occurrences,
    same row ownership, same cash basis. Where the two would differ the
    household is told two things about one payment, and neither page can be
    trusted."""
    from ..engine import budget
    from ..engine.compat import as_date
    month_start = today.replace(day=1)
    # Two schedules, on purpose. The DISPLAY set is every active bill, so a
    # disabled one still shows its own Paid x/y. The BUDGET set is the one
    # month_status expands — caps, `disabled_bills`, and the card-payment
    # filter — and that is the set allowed to decide who OWNS a ledger row.
    # Ownership has to come from the budget set or the two pages contradict
    # each other: a bill the household took out of the budget would hide
    # its charges from the envelope that category-matches them here, while
    # Today hands the envelope exactly those rows.
    bills = budget._recurring_bills(conn, caps=cfg.get("occurrence_caps"))
    budget_bills = budget.drop_card_pay_bills(conn, budget._recurring_bills(
        conn, caps=cfg.get("occurrence_caps"),
        disabled=cfg.get("disabled_bills")))
    # by payee, the bills table's own identity — the two loads return
    # different dicts for the same bill
    in_budget = {b["payee"] for b in budget_bills}
    # Envelopes the same way, and here the ORDER carries the rule: the
    # claimer is first-match-wins, so the budget's envelopes get first
    # refusal on every row and land on exactly month_status's numbers,
    # while a disabled envelope draws on what is left — enough for the
    # informational row it renders, and never at an enabled pool's expense.
    all_env = budget._envelope_bills(conn)
    in_budget_env = {e["payee"] for e in
                     budget._envelope_bills(
                         conn, disabled=cfg.get("disabled_bills"))}
    envelopes = ([e for e in all_env if e["payee"] in in_budget_env]
                 + [e for e in all_env if e["payee"] not in in_budget_env])
    # Matching reaches back PREPAY_MATCH_DAYS before the month and forward
    # into next month's occurrences, exactly as month_status does: rent due
    # the 1st is autopaid on the 28th before, and a page that only looks
    # inside the month calls that rent overdue while Today calls it settled
    # — one ledger, one schedule, two answers. The wider row set is for
    # MATCHING ONLY; `rows` (the in-month slice) is what the envelope pass
    # and the paid dollars account against, so last month's cash can never
    # land in this month's totals.
    match_rows = budget._spend_rows(
        conn, month_start - dt.timedelta(days=budget.PREPAY_MATCH_DAYS),
        today + dt.timedelta(days=1),
        excluded=cfg.get("excluded_accounts"))
    in_month = [i for i, r in enumerate(match_rows)
                if as_date(r["date"]) >= month_start]
    _pos = {src: dst for dst, src in enumerate(in_month)}
    rows = [match_rows[i] for i in in_month]
    ny, nm = ((today.year, today.month + 1) if today.month < 12
              else (today.year + 1, 1))

    def _match(bill_list, candidate_rows):
        occs = budget.month_occurrences(bill_list, today.year, today.month)
        n_this = len(occs)
        occs += budget.month_occurrences(bill_list, ny, nm)
        matched, occs = budget.match_occurrences(candidate_rows, occs)
        # A next-month occurrence nobody has paid yet was a matching
        # candidate and nothing more — keeping it would put two rents in
        # one month's Paid x/y. One that WAS paid here stays, on the same
        # cash basis the verdict uses: the money left in this month, so
        # this is the month that counts it.
        return matched, (occs[:n_this]
                         + [o for o in occs[n_this:] if o["txn"] is not None])

    # The budget's bills match FIRST, over every candidate row — the
    # matcher is first-come by date gap and amount across all occurrences,
    # so a disabled twin of an enabled bill could otherwise win the row
    # Today hands the enabled one, and the page would call it unpaid while
    # Today calls it settled. The bills the household took out of the
    # budget then match over what is left: enough for their informational
    # row, never at an enabled bill's expense.
    matched_all, occs = _match(budget_bills, match_rows)
    extra = [b for b in bills if b["payee"] not in in_budget]
    if extra:
        leftover = [r for i, r in enumerate(match_rows) if i not in matched_all]
        _, extra_occs = _match(extra, leftover)
        occs += extra_occs
    # Rows the BUDGET occurrences claimed (payment plus the fees that rode
    # with it), translated to `rows` indices for the envelope pass. A match
    # on a prior-month payment has no in-month row and simply drops out.
    _at = {id(r): i for i, r in enumerate(match_rows)}
    claimed = {_at[id(r)] for occ in occs
               if occ["txn"] is not None and occ["bill"]["payee"] in in_budget
               for r in [occ["txn"], *(occ.get("companions") or [])]
               if id(r) in _at}
    matched_idx = {_pos[i] for i in claimed if i in _pos}
    # income series: paychecks are not in the bill occurrence pass —
    # _recurring_bills is amount<0 only and _spend_rows is outflows — so
    # the Bills page's received column needs its own. Same engine, deposit
    # side: match this month's income occurrences against posted
    # non-transfer inflows.
    income_bills = budget._recurring_bills(conn, income=True)
    if income_bills:
        # a paycheck for an occurrence due on/after the 1st can
        # post in the closing days of the PREVIOUS month — a month-start-only
        # inflow window would leave that occurrence "·" forever. Look back
        # PREPAY_MATCH_DAYS before the month start (the same lookback
        # prepaid bills get) and add LAST month's income occurrences as
        # decoys — never emitted — so a boundary deposit attaches to its own
        # occurrence instead of eating this month's first one.
        inflows = budget._inflow_rows(
            conn,
            month_start - dt.timedelta(days=budget.PREPAY_MATCH_DAYS),
            today + dt.timedelta(days=1),
            excluded=cfg.get("excluded_accounts"))
        py, pm = ((today.year, today.month - 1) if today.month > 1
                  else (today.year - 1, 12))
        decoys = budget.month_occurrences(income_bills, py, pm)
        income_occs = budget.month_occurrences(income_bills,
                                               today.year, today.month)
        budget.match_occurrences(inflows, decoys + income_occs)
        occs += income_occs
    _, env_states = budget.match_envelopes(
        rows, envelopes, matched_idx,
        # the year-to-date pool must be drawn down by the same money the
        # verdict draws it down by, so the lookback gets the budget
        # schedule: a row an occurrence claims is the bill's there exactly
        # as it is in the current month. Without it a category-matched
        # annual envelope absorbs every earlier month's bill charges in
        # its category and the Bills page reports a pool spent by bills
        # that were never its, while Today reports it untouched.
        prior_used=budget._envelope_prior_used(
            conn, envelopes, today, excluded=cfg.get("excluded_accounts"),
            bills=budget_bills))
    per: dict[str, dict] = {}
    for occ in occs:
        e = per.setdefault(occ["bill"]["payee"], dict(_EMPTY_BILL_STATS))
        e["occurrences"] += 1
        if occ["txn"] is not None:
            e["paid"] += 1
            # Cash basis, the verdict's rule: an occurrence prepaid LAST
            # month is settled here (so it is never overdue) but its money
            # left in the month that counted it, and adds no dollars to
            # this month's paid total. Sum these across the page and you
            # get the verdict's fixed actual; count last month's cash again
            # and the two pages disagree about what the month cost.
            if as_date(occ["txn"]["date"]) >= month_start:
                e["paid_amount"] += occ["txn"]["amount"]
                # the fees that rode with it are part of what was paid
                e["paid_amount"] += sum(r["amount"]
                                        for r in occ.get("companions") or [])
        elif (not occ["floored"]
              and (e["next_unpaid"] is None or occ["due"] < e["next_unpaid"])):
            e["next_unpaid"] = occ["due"]
    for e in per.values():
        e["paid_amount"] = round(e["paid_amount"], 2)
        if e["next_unpaid"] is not None:
            e["next_unpaid"] = e["next_unpaid"].isoformat()
    for st in env_states:
        # envelope columns: Used (of the pool), Chgs (charge count, carried
        # in the `paid` key), Paid $ (used + overflow). an annual
        # envelope draws on a calendar-year pool, so `used` alone understates
        # it — carry the pool, what the year already spent, and what's left.
        drawn = st["prior_used"] + st["used"]
        per[st["bill"]["payee"]] = {
            "occurrences": 0, "paid": len(st["rows"]),
            "paid_amount": round(st["used"] + st["overflow"], 2),
            "used": round(st["used"], 2),
            "overflow": round(st["overflow"], 2),
            "pool": round(st["bill"]["pool"], 2),
            "period_months": st["bill"]["period_months"],
            "period_used": round(drawn, 2),
            "period_left": round(max(0.0, st["bill"]["pool"] - drawn), 2),
            "next_unpaid": None}
    return per


@router.get("/bills")
def bills_list(user: dict = Depends(_user())):
    """Proposals + bills — the SPA Bills page's data. Each bill row
    carries health, this month's occurrence/envelope stats (Paid x/y,
    envelope Used/Chgs/Paid), and the ledger-fit hint (💡): the fit summary,
    whether it diverges from the config, and its dismissed flag. `archived`
    lists soft-removed bills (active=0) for the archived section."""
    from ..engine import bills, budget
    from ..engine.compat import as_dict
    from .pages import _cadence_label, _proposal_summary
    conn = _conn(user)
    try:
        # payday roll: a received paycheck's Next date
        # advances the moment the page loads, not at the nightly pass —
        # idempotent and income-rows-only, so cheap on a read path.
        # viewers are read-only everywhere else; their page load
        # must not mutate due_on (the nightly detect still rolls for them).
        if user["role"] != "viewer":
            try:
                bills.advance_received_income(conn)
            except Exception:           # noqa: BLE001 — display must render
                import logging
                logging.getLogger("oikonome.bills").exception(
                    "payday roll failed on /api/bills")
        props = [{"id": p["id"], "kind": p["kind"],
                  "bill_type": p["bill_type"], "payee": p["payee"],
                  "amount": p["amount"],
                  # income proposals carry income=True in evidence;
                  # lets the Recurring UI show an INCOME section vs bills
                  "income": bool((as_dict(p["evidence"]) or {}).get("income")),
                  # an "attach" (fee) or "merchant" proposal names the
                  # bill it would join
                  "bill_payee": (as_dict(p["evidence"]) or {}).get("bill_payee"),
                  "cadence": _cadence_label(p["frequency"], p["interval"]),
                  # proposal extras: raw cadence pieces for the
                  # "freq ×N · next date" cell, and the "Looks like" LLM tag
                  "frequency": p["frequency"], "interval": p["interval"],
                  "next_due": (p["next_due"].isoformat()
                               if p["next_due"] else None),
                  "llm_tag": p["llm_tag"],
                  "summary": (bills.merchant_offer_summary(conn, p)
                              if p["kind"] == "merchant"
                              else _proposal_summary(p))}
                 for p in bills.pending_proposals(conn)]
        cfg = budget.load_config(conn)
        from .. import localtime
        today = localtime.now_local(cfg).date()
        health = bills.analyze_bills(conn, today)
        dismissed = cfg.get("dismissed_hints") or {}
        dismissed_health = cfg.get("dismissed_health") or {}
        disabled_bills = set(cfg.get("disabled_bills") or [])
        stats = _month_bill_stats(conn, cfg, today)
        caps = cfg.get("occurrence_caps") or {}
        rows = []          # response rows ("bills" key) — `bills` is the module
        for b in conn.execute(
                "SELECT payee, amount, frequency, due_on, raw, category, "
                "merchant FROM bills "
                "WHERE active=1 ORDER BY (amount>0), payee").fetchall():
            raw = as_dict(b["raw"])
            rec = raw.get("recurrence")
            if not isinstance(rec, dict):
                rec = {}
            h = health.get(b["payee"]) or {}
            fit = h.get("fit")
            rows.append({"payee": b["payee"],
                         "amount": abs(b["amount"] or 0),
                         "income": (b["amount"] or 0) > 0,
                         "cadence": _cadence_label(
                             rec.get("frequency") or b["frequency"],
                             # envelopes carry their period here, not in
                             # `recurrence` (they have no due dates)
                             budget.envelope_months(raw)
                             if raw.get("bill_type") == "envelope"
                             else rec.get("interval")),
                         "due_on": (b["due_on"].isoformat()
                                    if b["due_on"] else None),
                         "health": h.get("health"),
                         # what the ledger last showed for this merchant —
                         # a mismatch with no fit still gets to say why
                         "last_seen": _ser(h["last_seen"]) if h.get("last_seen") else None,
                         # the attention queue's raw material: when the
                         # bill last matched, its cycle, and the ONE action
                         # the evidence supports (see bills._suggest) — the
                         # clients render this verbatim, never invent one
                         "last_match": (h["last_match"].isoformat()
                                        if isinstance(h.get("last_match"),
                                                      (dt.date, dt.datetime))
                                        else h.get("last_match")),
                         "cycle_days": h.get("cycle_days"),
                         "suggestion": h.get("suggestion"),
                         # a dismissed flag stays hidden until the health
                         # itself changes to a different problem
                         # …and to a different ACTION: dismissing "stale"
                         # at one missed cycle must not also hide the
                         # two-cycle Disable offer when it later appears
                         "health_dismissed": (
                             (dismissed_health.get(b["payee"]) or {})
                             .get("health") == h.get("health")
                             and (dismissed_health.get(b["payee"]) or {})
                             .get("action", (h.get("suggestion") or {})
                                  .get("action"))
                             == (h.get("suggestion") or {}).get("action")),
                         # budget-disabled: the row shows no health pill and
                         # no dismiss control, so it must not be counted or
                         # listed as needing attention either
                         "disabled": b["payee"] in disabled_bills,
                         # 💡 pill: show when fit exists, not dismissed, and
                         # health is off or the fit diverges
                         "fit": fit,
                         "fit_diverges": bool(h.get("fit_diverges")),
                         "hint_dismissed": bills.fit_matches_dismissal(
                             fit, dismissed.get(b["payee"])),
                         # the edit-form prefill (/api/bills/bill), carried
                         # on every row so a page of N bills is one request
                         # — the per-bill route stays for single lookups
                         "cadence_value": _bill_cadence(raw),
                         "bill_type": raw.get("bill_type", "occurrence"),
                         "source": raw.get("source", "seed"),
                         "category": b["category"],
                         "merchant": b["merchant"],
                         "cap": caps.get(b["payee"]),
                         "show_today": bool(raw.get("show_today")),
                         "match_category": bool(raw.get("match_category")),
                         "txn_category": raw.get("txn_category") or "",
                         # the merchants the bill pays, by display name
                         "merchants": [x["name"] for x in budget.bill_refs(raw)],
                         # the fees that ride with the payment — shown as
                         # "incl. $1 fee" beside the amount
                         "companions": [{"tokens": c["label"],
                                         "amount": c["amount"],
                                         "window_days": c["window_days"]}
                                        for c in bills.companions_of(raw)],
                         "fee_total": bills.fee_total(raw),
                         **(stats.get(b["payee"]) or _EMPTY_BILL_STATS)})
        archived = [{"payee": r["payee"], "amount": r["amount"],
                     "frequency": r["frequency"],
                     "synced_at": (r["synced_at"].isoformat()
                                   if r["synced_at"] else None)}
                    for r in conn.execute(
                        "SELECT payee, amount, frequency, synced_at "
                        "FROM bills WHERE active=0 "
                        "ORDER BY synced_at DESC NULLS LAST, payee").fetchall()]
        # The headline states what the schedule costs; without it a person
        # would sum thirteen cadence groups by eye. `attention` counts
        # the bills whose health is actually wrong, so the headline can point
        # at the only rows that want a decision.
        totals = budget.recurring_load(conn, today)
        totals["attention"] = sum(
            1 for r in rows
            if (r.get("health") or "") in ("stale", "mismatch", "misdated",
                                           "drifting")
            and not r.get("health_dismissed") and not r.get("disabled"))
        return {"proposals": props, "bills": rows, "archived": archived,
                "totals": totals, "cadences": CADENCES}
    finally:
        conn.close()


@router.post("/bills/detect")
def bills_detect_api(user: dict = Depends(_user())):
    from ..engine import bills
    conn = _conn(user)
    try:
        stats = bills.run(conn)
    finally:
        conn.close()
    return {"proposed_add": stats["proposed_add"],
            "proposed_income": stats.get("proposed_income", 0),
            "drift_applied": stats["drift_applied"]}


@router.post("/bills/proposal")
def bills_proposal_api(user: dict = Depends(_user()),
                           body: dict = Body(...)):
    from ..engine import bills
    action = body.get("action") or ""
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    conn = _conn(user)
    try:
        pid = body.get("pid") or ""
        r = bills.apply_proposal(conn, pid, action)
        if not r.get("error"):
            _log_proposal(conn, user, pid, action, r)
    finally:
        conn.close()
    if r.get("error"):
        raise HTTPException(400, r["error"])
    return {"ok": True,
            "payee": r.get("approved") or r.get("rejected")}


@router.post("/bills/proposals/approve-all")
def bills_proposals_approve_all(user: dict = Depends(_user())):
    """Approve every pending proposal in one click — the setup wizard's
    bills step, where the finder's first pass over a fresh history is
    usually right and a person should not have to click approve twenty
    times. Each proposal goes through the same locked
    apply as a single approve, so a twin click or a concurrent single
    approve cannot double-create; one that fails to apply (slug
    collision, bad amount) stays pending and is reported, the rest still
    land."""
    from ..engine import bills
    conn = _conn(user)
    try:
        pids = [r["id"] for r in conn.execute(
            "SELECT id FROM bill_proposals WHERE status='pending' "
            "ORDER BY created_at, id").fetchall()]
        approved, failed = [], []
        for pid in pids:
            r = bills.apply_proposal(conn, pid, "approve")
            if r.get("error"):
                failed.append({"pid": pid, "error": r["error"]})
            else:
                approved.append(r.get("approved"))
                _log_proposal(conn, user, pid, "approve", r)
    finally:
        conn.close()
    return {"ok": True, "approved": len(approved), "payees": approved,
            "failed": failed}


# ---- recurring management (bill CRUD — /bills/{save,delete,restore,
#      toggle,hint} + /bills/history as JSON) --------------------------------

# cadence select values: "<FREQ>:<interval>" | "ONE_TIME:1" | "ENVELOPE:1"
CADENCES = [("DAILY:1", "daily"),
            ("WEEKLY:1", "weekly"), ("WEEKLY:2", "every 2 weeks"),
            ("DAILY:14", "every 14 days"), ("MONTHLY:1", "monthly"),
            ("WEEKLY:8", "every 8 weeks"), ("MONTHLY:2", "every 2 months"),
            ("MONTHLY:3", "every 3 months"), ("MONTHLY:6", "every 6 months"),
            ("YEARLY:1", "yearly"), ("YEARLY:2", "every 2 years"),
            ("ONE_TIME:1", "one-time"),
            ("ENVELOPE:1", "envelope (monthly pool)"),
            ("ENVELOPE:12", "envelope (annual pool)")]


def _bill_cadence(raw: dict) -> str:
    """A bill's raw config → the CADENCES select value (edit-form prefill)."""
    from ..engine.budget import envelope_months
    if raw.get("bill_type") == "envelope":
        return f"ENVELOPE:{envelope_months(raw)}"
    rec = raw.get("recurrence")
    if not isinstance(rec, dict) or not rec.get("frequency"):
        return "ONE_TIME:1"
    return f"{str(rec['frequency']).upper()}:{rec.get('interval') or 1}"


def _save_payee_config(conn, mutate) -> None:
    """Load config, apply `mutate(cfg) -> changed`, save if changed.

    Under the row lock: every caller here is a
    read-modify-write of the shared settings blob, which is the exact shape
    that silently discards a concurrent save."""
    from ..engine import budget
    with budget.config_txn(conn) as cfg:
        mutate(cfg)


@router.get("/bills/bill")
def bills_bill_api(user: dict = Depends(_user()), payee: str = ""):
    """One bill's editable fields (the edit-row prefill), plus its
    payee-keyed config: occurrence cap + budget-disabled flag."""
    from ..engine import bills, budget
    from ..engine.compat import as_dict
    conn = _conn(user)
    try:
        row = bills.get_bill(conn, payee)
        if row is None:
            raise HTTPException(404, "no such bill")
        raw = as_dict(row["raw"])
        cfg = budget.load_config(conn)
    finally:
        conn.close()
    return {"payee": row["payee"],
            "amount": abs(row["amount"] or 0),
            "income": (row["amount"] or 0) > 0,
            "cadence": _bill_cadence(raw),
            "next_due": row["due_on"].isoformat() if row["due_on"] else "",
            "bill_type": raw.get("bill_type", "occurrence"),
            "source": raw.get("source", "seed"),
            "category": row["category"],
            "merchant": row["merchant"],
            "active": bool(row["active"]),
            "cap": (cfg.get("occurrence_caps") or {}).get(row["payee"]),
            "disabled": row["payee"] in (cfg.get("disabled_bills") or []),
            "show_today": bool(raw.get("show_today")),
            "match_category": bool(raw.get("match_category")),
            # the transaction category stamped on the rows this bill
            # matches ("" = none — rows keep the merchant rule)
            "txn_category": raw.get("txn_category") or "",
            "merchants": [x["name"] for x in budget.bill_refs(raw)],
            "companions": [{"tokens": c["label"], "amount": c["amount"],
                            "window_days": c["window_days"]}
                           for c in bills.companions_of(raw)],
            "fee_total": bills.fee_total(raw),
            "cadences": CADENCES}


@router.post("/bills/save")
def bills_save_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """Add or edit a bill.

    Body: payee, amount (positive $), cadence "<FREQ>:<interval>" (see
    CADENCES), next_due (ISO date or ""), income (bool), orig_payee (set on
    rename), cap (int occurrence cap; ""/null clears; only touched when the
    key is present), merchant (match key for NEW bills only), merchants
    (the display names of the merchants the bill pays — its identity),
    show_today / match_category (bools) and category (string) — each only
    touched when its key is present."""
    from ..engine import bills, budget
    from ..engine.compat import as_dict
    payee = _body_text(body, "payee")
    if not payee:
        raise HTTPException(400, "payee required")
    amount = _money_float(body.get("amount", ""), "amount")
    # _money_float only rejects NaN/Inf; save_bill separately requires a
    # POSITIVE amount, but it runs AFTER the rename/config migration commits
    # (autocommit connection, no transaction). Reject a non-positive amount
    # up front so a failing edit never leaves a half-applied rename behind.
    if not amount > 0:
        raise HTTPException(400, "amount must be a positive dollar value")
    freq, _, iv = _body_text(body, "cadence",
                             default="MONTHLY:1").partition(":")
    freq = freq.upper()
    # Allowlist the frequency BEFORE any mutation: an unknown token (e.g.
    # "HOURLY") is not ONE_TIME/ENVELOPE, so it reaches PER_YEAR[freq] in
    # recurring._monthly and raises KeyError → a 500 that lands AFTER the
    # rename/config migration below has already committed (partial mutation).
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY",
                    "ONE_TIME", "ENVELOPE"):
        raise HTTPException(400, "bad cadence frequency")
    try:
        interval = int(iv or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "bad cadence interval")
    # Bound the interval: an unbounded value later overflows dt.timedelta in
    # the occurrence expansion (recurring._next_due / plan render), turning
    # every Today-page and daily-email render into a persistent 500 for the
    # tenant. 240 = every 20 years, well past any real bill cadence.
    if not 1 <= interval <= 240:
        raise HTTPException(400, "cadence interval out of range")
    orig_payee = _body_text(body, "orig_payee")
    cap_n = None
    if "cap" in body:                   # validate before any mutation
        cap_s = str(body["cap"]).strip() if body["cap"] is not None else ""
        if cap_s:
            try:
                cap_n = int(cap_s)
            except (TypeError, ValueError):
                raise HTTPException(400, "cap must be an integer")
            # a cap is an occurrence COUNT; a non-positive value is used as a
            # Python slice bound in budget._occurrences (expanded[:cap]), so a
            # negative silently drops occurrences and can flip the verdict.
            if cap_n < 1:
                raise HTTPException(400, "cap must be a positive integer")
    # pin-to-Today / category-pool flags: only touched when the key is
    # present (absent = keep the bill's current setting, same contract as
    # `cap`); category likewise, so a plain edit never strips detection's
    # category from the row
    show_today_v = bool(body["show_today"]) if "show_today" in body else None
    match_cat_v = (bool(body["match_category"])
                   if "match_category" in body else None)
    category_v = None
    if "category" in body:
        cat_s = str(body["category"] or "").strip()
        if len(cat_s) > 64:
            raise HTTPException(400, "category too long")
        category_v = cat_s or None
    # the TRANSACTION category the bill stamps on matched rows: only touched
    # when the key is present; "" clears it (and reverts the rows)
    txn_cat_v = None
    companions_v = None
    if "companions" in body:
        companions_v = []
        if not isinstance(body["companions"], list) \
                or len(body["companions"]) > 8:
            raise HTTPException(400, "companions must be a list of up to 8")
        for c in body["companions"]:
            if not isinstance(c, dict):
                raise HTTPException(400, "bad companion")
            label = str(c.get("tokens") or "").strip()
            if not label or len(label) > 80:
                raise HTTPException(400, "companion needs a short description")
            famt = _money_float(c.get("amount", ""), "companion amount")
            if not 0 < famt <= bills.COMPANION_MAX_AMOUNT:
                raise HTTPException(
                    400, f"a companion fee is between $0.01 and "
                         f"${bills.COMPANION_MAX_AMOUNT:.0f}")
            try:
                win = int(c.get("window_days", bills.COMPANION_WINDOW_DAYS))
            except (TypeError, ValueError):
                raise HTTPException(400, "companion window must be days")
            if not 0 <= win <= 14:
                raise HTTPException(400, "companion window is 0–14 days")
            companions_v.append({"tokens": label, "amount": famt,
                                 "window_days": win})
    if "txn_category" in body:
        txn_cat_v = str(body["txn_category"] or "").strip()
        if len(txn_cat_v) > 64:
            raise HTTPException(400, "txn_category too long")
        # never a FLOW category: stamping TRANSFER_OUT / INCOME / LOAN
        # PAYMENTS on a bill's charges would silently drop them from spend
        # (the verdict reads the override) — the spend-exclusion machinery
        # decides flow, not a bill; the row picker refuses these too
        from ..engine import categories as _cats
        if txn_cat_v in _cats.FLOW:
            raise HTTPException(
                400, "a bill cannot categorize its charges as a transfer, "
                     "income or loan payment")
    # the merchants the bill pays, by display name — only touched when the
    # key is present (absent = keep the bill's identity), as for `cap`
    merchants_v = None
    if "merchants" in body:
        if not isinstance(body["merchants"], list) or len(body["merchants"]) > 20:
            raise HTTPException(400, "merchants must be a list of up to 20 names")
        merchants_v = []
        for m in body["merchants"]:
            if not isinstance(m, str):
                raise HTTPException(400, "bad merchant name")
            _merchant_name_or_400("merchant", m)
            if m.strip():
                merchants_v.append(m.strip())
    # Validate next_due up front too: save_bill parses it via as_date only
    # AFTER the rename commits, so a malformed date would otherwise leave a
    # half-applied rename behind (same partial-mutation window as amount).
    next_due_in = _body_text(body, "next_due")
    if next_due_in:
        try:
            dt.date.fromisoformat(next_due_in)
        except ValueError:
            raise HTTPException(400, "next_due must be an ISO date")
    conn = _conn(user)
    try:
        # what the bill was before this edit, for the log's sentence — read
        # under the name the client edited (a rename moves it below)
        _was = _activity.bill_snapshot(
            bills.get_bill(conn, orig_payee or payee))
        if merchants_v:
            # A chip must name a merchant this ledger HAS. A name nothing
            # answers to is stored as a ref with no id, and an identity
            # whose refs resolve to nothing matches NOTHING: the bill stops
            # being paid — no history, no stamped rows, an unpaid month —
            # while the form shows the chip accepted. Resolved exactly as
            # the write path resolves it (budget.resolve_refs), so any
            # casing the picker offers passes. A name the bill ALREADY
            # carries is let through: a bill that arrived from another
            # instance can name a merchant this instance has never seen,
            # and an edit to the
            # amount or the date must not be refused because of it.
            prev = bills.get_bill(conn, orig_payee or payee)
            have = {r["name"].lower()
                    for r in budget.bill_refs(prev["raw"] if prev else None)}
            added = [m for m in merchants_v if m.lower() not in have]
            known = {r["name"].lower() for r in budget.resolve_refs(
                conn, [{"id": None, "name": m} for m in added]) if r["id"]}
            for m in added:
                if m.lower() not in known:
                    raise HTTPException(400, f"no merchant named {m!r}")
        # rename: move the row + migrate the payee-keyed config entries.
        # A rename onto an EXISTING different bill aborts the whole save —
        # falling through would overwrite the unrelated target.
        if orig_payee and orig_payee != payee:
            # the existence check and the UPDATE are two statements; two
            # renames racing onto one name both pass the check and the
            # loser trips the (tenant_id, id) primary key — same answer
            try:
                taken = (bills.get_bill(conn, payee) is not None
                         or bills.rename_bill(conn, orig_payee, payee) == 0)
            except psycopg.errors.UniqueViolation:
                taken = True
            if taken:
                raise HTTPException(
                    409, f"a bill named {payee!r} already exists")

            def _migrate(cfg):
                changed = False
                dis = cfg.get("disabled_bills") or []
                if orig_payee in dis:
                    cfg["disabled_bills"] = [payee if p == orig_payee else p
                                             for p in dis]
                    changed = True
                caps = cfg.get("occurrence_caps") or {}
                if orig_payee in caps:
                    caps[payee] = caps.pop(orig_payee)
                    cfg["occurrence_caps"] = caps
                    changed = True
                return changed
            _save_payee_config(conn, _migrate)
        old = bills.get_bill(conn, payee)
        if "cadence" not in body and old is not None:
            # an amount-only edit (the attention queue's Accept from a
            # client whose row lacks cadence_value) must not silently turn
            # a yearly bill monthly through the default — keep its own
            _f, _, _i = _bill_cadence(as_dict(old["raw"])).partition(":")
            # raw can arrive verbatim from a restore ZIP: only a cadence
            # that would have passed the explicit path's own checks is
            # carried forward, else the parsed default stands
            try:
                _n = int(_i or 1)
            except (TypeError, ValueError):
                _n = 0
            if (_f.upper() in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY",
                               "ENVELOPE", "ONE_TIME")
                    and 1 <= _n <= 240):
                freq, interval = _f.upper(), _n
        src, last_seen = "manual", None
        if old is not None:
            old_raw = as_dict(old["raw"])
            src = old_raw.get("source", "manual")
            # keep the series' history floor — editing must not re-floor the
            # bill as brand-new
            last_seen = old_raw.get("lastDueOn")
        # New bill: pin its match key from the merchant it was added from or,
        # failing that, the payee itself — so it matches the ledger
        # immediately. Edits pass None so save_bill keeps the derived merchant.
        merchant_val = (None if old is not None
                        else _body_text(body, "merchant", default=payee))
        try:
            bills.save_bill(
                conn, payee=payee, amount=amount,
                bill_type="envelope" if freq == "ENVELOPE" else "occurrence",
                frequency=None if freq in ("ONE_TIME", "ENVELOPE") else freq,
                interval=interval,
                next_due=next_due_in or None,
                source=src,
                category=(category_v if "category" in body
                          else old["category"] if old is not None else None),
                last_seen=last_seen, income=bool(body.get("income")),
                merchant=merchant_val,
                manual_touch=True,  # human edit → drift grace
                show_today=show_today_v, match_category=match_cat_v,
                txn_category=txn_cat_v, companions=companions_v,
                merchants=merchants_v)
        except ValueError as e:     # $0 amount / slug collision / bad date
            raise HTTPException(400, str(e))
        # (re)stamp this bill's rows right away — an edit that sets or
        # changes the transaction category should show on the ledger on
        # the next load, not after the nightly pass. Also on a plain edit:
        # a changed amount changes what the bill matches.
        bills.apply_txn_categories(conn, bill_id=bills._slug(payee))
        if "cap" in body:
            def _set_cap(cfg):
                caps = cfg.get("occurrence_caps") or {}
                if cap_n is not None:
                    caps[payee] = cap_n
                elif payee in caps:
                    caps.pop(payee)
                else:
                    return False
                if caps:
                    cfg["occurrence_caps"] = caps
                else:
                    cfg.pop("occurrence_caps", None)
                return True
            _save_payee_config(conn, _set_cap)
        _now = _activity.bill_snapshot(bills.get_bill(conn, payee))
        _activity.record(conn, user, "bill",
                         "edited" if _was is not None else "added",
                         target=payee, label=payee,
                         detail={"before": _was, "after": _now})
        return {"ok": True, "payee": payee}
    finally:
        conn.close()


@router.get("/merchants")
def merchants_list_api(user: dict = Depends(_user()), limit: int = 1000):
    """Distinct display-merchant names the tenant has transactions for
    (canonical-resolved via merchant_canonical), most-frequent first. Powers
    the recurring "Add a bill" merchant picker so a manual bill's match key
    lines up with the ledger's real merchants and matches future txns.
    Tenant-scoped by RLS on the _conn."""
    from ..engine import merchant_sql
    limit = max(1, min(int(limit or 1000), 2000))
    conn = _conn(user)
    try:
        rows = conn.execute(
            # The ledger's own display names, through the shared display
            # join: the bill door checks a chip against the merchants
            # table, so the picker must offer the same names, never a raw
            # string the resolver has cleaned. Keyed on the identity key
            # (outlet first) like every other surface — retyped from
            # merchant_name it offered a fuel arm's rows under the parent
            # brand, a name the bill would then never match.
            f"""SELECT {merchant_sql.DISPLAY_MERCHANT} AS merchant,
                       count(*) AS n
                 FROM transactions t
                 {merchant_sql.MC_JOIN}
                WHERE t.removed = 0
                  AND {merchant_sql.RAW_KEY} IS NOT NULL
                  AND {merchant_sql.RAW_KEY} <> ''
                GROUP BY 1
                ORDER BY n DESC, merchant
                LIMIT %s""", (limit,)).fetchall()
        return {"merchants": [r["merchant"] for r in rows]}
    finally:
        conn.close()


@router.post("/bills/delete")
def bills_delete_api(user: dict = Depends(_user()),
                         body: dict = Body(...)):
    """mode=archive (default): soft-remove, restorable, keeps payee config.
    mode=purge: hard delete (from the archived list) + drop payee-keyed
    config leftovers (occurrence cap, disabled flag)."""
    from ..engine import bills
    payee = _body_text(body, "payee")
    if not payee:
        raise HTTPException(400, "payee required")
    mode = body.get("mode") or "archive"
    conn = _conn(user)
    try:
        if mode == "purge":
            n = bills.delete_bill(conn, payee)

            def _cleanup(cfg):
                changed = False
                caps = cfg.get("occurrence_caps") or {}
                if payee in caps:
                    caps.pop(payee)
                    if caps:
                        cfg["occurrence_caps"] = caps
                    else:
                        cfg.pop("occurrence_caps", None)
                    changed = True
                dis = cfg.get("disabled_bills") or []
                if payee in dis:
                    dis = [p for p in dis if p != payee]
                    if dis:
                        cfg["disabled_bills"] = dis
                    else:
                        cfg.pop("disabled_bills", None)
                    changed = True
                return changed
            _save_payee_config(conn, _cleanup)
        else:
            n = bills.archive_bill(conn, payee)
        # a bill that is gone (or resting) lets go of the rows it categorized
        bills.apply_txn_categories(conn, bill_id=bills._slug(payee))
        if n:
            _activity.record(conn, user, "bill",
                             "deleted" if mode == "purge" else "archived",
                             target=payee, label=payee)
        return {"ok": True, "mode": mode, "affected": n}
    finally:
        conn.close()


@router.post("/bills/restore")
def bills_restore_api(user: dict = Depends(_user()),
                          body: dict = Body(...)):
    from ..engine import bills
    payee = _body_text(body, "payee")
    if not payee:
        raise HTTPException(400, "payee required")
    conn = _conn(user)
    try:
        n = bills.restore_bill(conn, payee)
        # back in service: its transaction category applies again
        bills.apply_txn_categories(conn, bill_id=bills._slug(payee))
        if n:
            _activity.record(conn, user, "bill", "restored",
                             target=payee, label=payee)
        return {"ok": True, "affected": n}
    finally:
        conn.close()


@router.post("/bills/toggle")
def bills_toggle_api(user: dict = Depends(_user()),
                         body: dict = Body(...)):
    """Flip a bill's budget-disabled flag (config `disabled_bills`) — the
    bill row stays; it's just excluded from the budget math."""
    payee = _body_text(body, "payee")
    if not payee:
        raise HTTPException(400, "payee required")
    state = {}
    conn = _conn(user)
    try:
        # an explicit target state makes a retried or duplicated request
        # idempotent — a bare flip would net a double-click to no change
        want = body.get("disabled")
        if want is not None and not isinstance(want, bool):
            raise HTTPException(400, "disabled must be true or false")

        def _flip(cfg):
            dis = list(cfg.get("disabled_bills") or [])
            to_disabled = (payee not in dis) if want is None else want
            state["changed"] = to_disabled != (payee in dis)
            if not to_disabled:
                dis = [p for p in dis if p != payee]
                state["disabled"] = False
            elif payee not in dis:
                dis.append(payee)
                state["disabled"] = True
            else:
                state["disabled"] = True
            if dis:
                cfg["disabled_bills"] = dis
            else:
                cfg.pop("disabled_bills", None)
            return True
        _save_payee_config(conn, _flip)
        if state.get("changed"):
            _activity.record(conn, user, "bill",
                             "paused" if state["disabled"] else "resumed",
                             target=payee, label=payee)
        return {"ok": True, "payee": payee, "disabled": state["disabled"]}
    finally:
        conn.close()


@router.post("/bills/hint")
def bills_hint_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """Dismiss (or restore) a bill's ledger-fit hint — or, with
    target="health", its health pill. Dismissal stores a signature —
    rejection memory: the hint stays hidden until the underlying finding
    itself changes (a different fit, or a different health problem)."""
    from ..engine import bills
    payee = _body_text(body, "payee")
    if not payee:
        raise HTTPException(400, "payee required")
    action = body.get("action") or "dismiss"
    target = body.get("target") or "fit"
    conn = _conn(user)
    try:
        # the SAME calendar day the list judged the row on — a UTC "today"
        # here could cross a threshold the household's day had not, and
        # store a signature the list then refuses to honour
        today = _today(conn)

        def _apply(cfg):
            key = "dismissed_health" if target == "health" \
                else "dismissed_hints"
            dismissed = cfg.get(key, {})
            if action == "restore":
                dismissed.pop(payee, None)
            elif target == "health":
                # ONE bill's analysis — a whole-table pass would run inside
                # the settings row lock and block every concurrent
                # settings write for as long as the ledger took
                h = bills.analyze_bills(conn, today, payee=payee
                                        ).get(payee) or {}
                if (h.get("health") or "") in ("stale", "mismatch",
                                               "misdated", "drifting"):
                    dismissed[payee] = {"health": h["health"],
                                        "action": (h.get("suggestion")
                                                   or {}).get("action"),
                                        "dismissed": today.isoformat()}
            else:
                h = bills.analyze_bills(conn, today, payee=payee).get(payee)
                fit = h.get("fit") if h else None
                if fit:
                    dismissed[payee] = {"kind": fit.get("kind"),
                                        "cadence": fit.get("cadence"),
                                        "amount": fit.get("amount"),
                                        "dismissed": today.isoformat()}
            if dismissed:
                cfg[key] = dismissed
            else:
                cfg.pop(key, None)
            return True
        _save_payee_config(conn, _apply)
        return {"ok": True, "payee": payee,
                "dismissed": action != "restore"}
    finally:
        conn.close()


# Two ledger scans and a fuzzy match per row, on a payee the caller names —
# the same class of expense as the analytics reports next door, so the same
# budget. Both clients link here from every merchant name they render, so the
# ceiling has to sit far above a person reading their spending and still well
# under a loop.
@router.get("/bills/history",
            dependencies=[Depends(limit("bills_history", 60, 60))])
def bills_history_api(user: dict = Depends(_user()), payee: str = "",
                      window: str = "all"):
    """Everything the per-payee history page renders, as JSON: matched
    txns (with `hit` = the
    bill/proposal matcher would count it), monthly totals, lifetime
    aggregate, ledger fit, the bill's config (or `covered_by` when another
    bill's matcher already covers this merchant), health, and the
    payee-scoped proposals.

    `window` is the site-wide timeframe vocabulary (This month · 1m · 3m
    6m 1y 3y 5y all; bills.HISTORY_WINDOWS) and bounds the transaction
    list only; the lifetime aggregate is lifetime by definition. An unknown
    value reads as all."""
    from ..engine import bills, budget
    from ..engine.compat import as_dict
    from .pages import _cadence_label, _proposal_summary
    conn = _conn(user)
    try:
        hist = bills.merchant_history(
            conn, payee, window=window if window in bills.HISTORY_WINDOWS else "all")
        bill = bills.get_bill(conn, payee)
        # a merchant clicked from Transactions may already be covered by a
        # bill under a different display name — link to it instead of
        # offering a duplicate add. Uses the bill's REAL matcher.
        covered_by = None
        if bill is None and payee.strip():
            for b in conn.execute(
                    "SELECT payee, merchant FROM bills "
                    "WHERE active=1 AND amount<0").fetchall():
                if b["merchant"] and budget.text_matches_merchant(
                        payee.lower(), b["merchant"]):
                    covered_by = b["payee"]
                    break
        bill_info = health = None
        if bill is not None and bill["active"]:
            raw = as_dict(bill["raw"])
            health = bills.analyze_bills(conn, payee=payee).get(payee)
            bill_info = {"amount": abs(bill["amount"] or 0),
                         "bill_type": raw.get("bill_type", "occurrence"),
                         "source": raw.get("source", "seed"),
                         "cadence": _bill_cadence(raw),
                         "due_on": (bill["due_on"].isoformat()
                                    if bill["due_on"] else None),
                         "income": (bill["amount"] or 0) > 0,
                         "tol": budget.bill_tolerance(abs(bill["amount"] or 0))}
        props = [p for p in bills.pending_proposals(conn)
                 if (p["payee"] or "").lower() == payee.lower()]
        summaries = {p["id"]: (bills.merchant_offer_summary(conn, p)
                               if p["kind"] == "merchant"
                               else _proposal_summary(p)) for p in props}
        # highlight: rows the bill/proposal matcher would count
        ref = (abs(bill["amount"]) if bill is not None else
               props[0]["amount"] if props else None)
        tol = budget.bill_tolerance(ref) if ref else None
        want_inflow = bool(bill_info and bill_info["income"])
        hint_dismissed = bills.fit_matches_dismissal(
            hist["fit"],
            (budget.load_config(conn).get("dismissed_hints") or {}).get(payee))
        # the page renders the ledger through the SAME row builder as the
        # Transactions page — full pills/flags/receipt
        # affordances, stamped with the ⟳ bill indicator, so the two
        # surfaces cannot drift
        txns = [_ser(r) for r in data.transactions_by_ids(
            conn, [t["txn_id"] for t in hist["txns"]])]
        for o in txns:
            o["category_why"] = _category_why(o)
        _stamp_recurring(conn, txns)
    finally:
        conn.close()
    for row in txns:
        row["txn_id"] = row["id"]  # the payload key both clients read
        row["hit"] = bool(
            (bill_info is not None and bill_info["bill_type"] == "envelope"
             and row["amount"] > 0)
            or (tol is not None
                and (row["amount"] < 0 if want_inflow else row["amount"] > 0)
                and abs(abs(row["amount"]) - ref) <= tol))
    return {"payee": payee, "txns": txns, "monthly": hist["monthly"],
            "logo": hist.get("logo"),
            "fit": hist["fit"], "lifetime": _ser(hist["lifetime"]),
            "inflow": hist["inflow"], "bill": bill_info,
            "health": _ser(health) if health else None,
            "proposals": [{"id": p["id"], "kind": p["kind"],
                           "bill_type": p["bill_type"], "payee": p["payee"],
                           "amount": p["amount"],
                           "cadence": _cadence_label(p["frequency"],
                                                     p["interval"]),
                           "summary": summaries[p["id"]]}
                          for p in props],
            "covered_by": covered_by, "hint_dismissed": hint_dismissed,
            "cadences": CADENCES}


# bulk category override for a whole merchant, from its history page.
# Owner-only (highest-blast-radius write). preview → confirm-with-count →
# apply (returns an undo snapshot) → undo. Canonical-merchant scoped.

# Demo-denied like the other merchant-wide rewrites (merchant rename,
# category rename): the demo tenant is shared and resets hourly, and a
# write that reshapes a whole merchant's history and rules for every
# visitor in between is the kind the instance does not host.

@router.post("/bills/merchant-category/preview")
def merchant_category_preview_api(user: dict = Depends(_user()),
                                  body: dict = Body(...),
                                  canon_only: bool = False):
    """`canon_only` (query flag or body field) narrows the preview to the
    payee's single canonical — the scope of the ledger row's "apply to
    whole merchant" write (set_category scope="all"), which the row menu
    previews. The default token-family scope is the history page's
    (BillsHistory previews the bulk set, set_merchant_category). The
    preview must count the same rows as the write behind it, and the two
    writes have different scopes."""
    demoguard.deny(user)
    _may_edit(user)
    payee = _body_text(body, "payee")
    category = _body_text(body, "category")
    if not payee or not category:
        raise HTTPException(400, "payee and category required")
    canon_only = canon_only or bool(body.get("canon_only"))
    conn = _conn(user)
    try:
        return data.merchant_category_preview(conn, payee, category,
                                              family=not canon_only)
    finally:
        conn.close()


@router.post("/bills/merchant-category")
def merchant_category_apply_api(user: dict = Depends(_user()),
                                body: dict = Body(...)):
    demoguard.deny(user)
    _may_edit(user)
    payee = _body_text(body, "payee")
    category = _body_text(body, "category")
    if not payee or not category:
        raise HTTPException(400, "payee and category required")
    conn = _conn(user)
    try:
        out = data.set_merchant_category(conn, payee, category)
        _activity.record(conn, user, "rule", "set",
                         target=out.get("merchant") or payee,
                         label=out.get("merchant") or payee,
                         detail={"after": category, "count": out.get("count")})
        return out
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/bills/merchant-category/undo")
def merchant_category_undo_api(user: dict = Depends(_user()),
                               body: dict = Body(...)):
    demoguard.deny(user)
    _may_edit(user)
    undo = body.get("undo")
    if not isinstance(undo, dict):
        raise HTTPException(400, "undo snapshot required")
    conn = _conn(user)
    try:
        # the snapshot is client-held state posted back verbatim; its shape
        # and every value it would write are checked there, not trusted
        n = data.undo_merchant_category(conn, undo)
        _activity.record(conn, user, "rule", "undone",
                         target=str(undo.get("merchant") or ""),
                         label=str(undo.get("merchant") or "a merchant"),
                         detail={"count": n})
        return {"ok": True, "restored": n}
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


# the learned-rules surface — see every categorization rule the
# system applies + its provenance, and correct / disable / delete any of
# them (never hand-author). Viewing is read for anyone; edits are owner-only.

@router.get("/rules")
def rules_list_api(user: dict = Depends(_user()), source: str = "",
                   q: str = "", page: int = 1):
    """Provenance-grouped, lazily-loaded rules — filter by source
    (user/llm/seed/model), search merchant+category, ~50/page. `counts` carries
    per-source totals for the collapsed group headers."""
    if source and source not in data.RULE_SOURCES:
        raise HTTPException(400, "source must be user, llm, seed, or model")
    # bound like _ck_ym bounds y/m: an unbounded page multiplies into an
    # OFFSET past bigint inside the driver (NumericValueOutOfRange → 500)
    page = min(max(1, page), 10_000_000)
    conn = _conn(user)
    try:
        return data.list_rules(conn, source=source or None, q=q, page=page)
    finally:
        conn.close()


@router.post("/rules/set")
def rules_set_api(user: dict = Depends(_user()), body: dict = Body(...)):
    # the rules editor's set IS set_merchant_category — the same
    # merchant-wide rewrite the /bills/merchant-category doors deny on the
    # demo instance, so it is denied here too (an open spelling of a
    # closed door is no guard at all)
    demoguard.deny(user)
    _may_edit(user)
    merchant = _body_text(body, "merchant")
    category = _body_text(body, "category")
    if not merchant or not category:
        raise HTTPException(400, "merchant and category required")
    conn = _conn(user)
    try:
        out = data.set_merchant_category(conn, merchant, category)
        _activity.record(conn, user, "rule", "set",
                         target=out.get("merchant") or merchant,
                         label=out.get("merchant") or merchant,
                         detail={"after": category, "count": out.get("count")})
        return out
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/categories/rename")
def categories_rename_api(user: dict = Depends(_user()),
                          body: dict = Body(...)):
    """Rename a free-form / custom category everywhere it appears.

    Owner-only. Rewrites transaction overrides + primaries, merchant rules,
    manual_categories, and budget custom_buckets that use the old string.
    Standard Plaid primaries cannot be the *old* name (custom labels only).
    """
    demoguard.deny(user)
    _may_edit(user)
    old = _body_text(body, "old", "from")
    new = _body_text(body, "new", "to")
    if not old or not new:
        raise HTTPException(400, "old and new names are required")
    conn = _conn(user)
    try:
        out = data.rename_category(conn, old, new)
        _activity.record(conn, user, "category", "renamed", target=new,
                         label=new, detail={"before": old, "after": new})
        return out
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/rules/disable")
def rules_disable_api(user: dict = Depends(_user()), body: dict = Body(...)):
    _may_edit(user)
    # same class of tenant-wide rule rewrite as /rules/set — the shared
    # demo login must not corrupt every visitor's rules until the reset
    demoguard.deny(user)
    merchant = _body_text(body, "merchant")
    if not merchant:
        raise HTTPException(400, "merchant required")
    conn = _conn(user)
    try:
        data.set_rule_disabled(conn, merchant, bool(body.get("disabled")))
        _activity.record(conn, user, "rule",
                         "disabled" if body.get("disabled") else "enabled",
                         target=merchant, label=merchant)
        return {"ok": True}
    finally:
        conn.close()


@router.post("/rules/delete")
def rules_delete_api(user: dict = Depends(_user()), body: dict = Body(...)):
    _may_edit(user)
    demoguard.deny(user)  # see /rules/disable
    merchant = _body_text(body, "merchant")
    if not merchant:
        raise HTTPException(400, "merchant required")
    conn = _conn(user)
    try:
        n = data.delete_rule(conn, merchant)
        if n:
            _activity.record(conn, user, "rule", "deleted",
                             target=merchant, label=merchant)
        return {"ok": True, "deleted": n}
    finally:
        conn.close()


# local conversational assistant over your own ledger. Read-only,
# tool-routed, runs on the bundled LLM (data never leaves the box). Hidden
# when no LLM is configured.

@router.get("/assistant")
def assistant_status_api(user: dict = Depends(_user())):
    from ..engine import assistant
    conn = _conn(user)
    try:
        return assistant.status(conn)
    finally:
        conn.close()


@router.post("/assistant",
             dependencies=[Depends(limit("assistant", 30, 3600))])
def assistant_ask_api(user: dict = Depends(_user()), body: dict = Body(...)):
    from ..engine import assistant
    question = _body_text(body, "question")
    if not question:
        raise HTTPException(400, "question required")
    conn = _conn(user)
    try:
        return assistant.ask(conn, question)
    except assistant.AssistantUnavailable:
        raise HTTPException(503, "the assistant needs a local LLM configured")
    except assistant.AssistantBusy:
        raise HTTPException(503, "the assistant is busy — try again in a "
                                 "moment")
    except Exception as e:                # LLM/network failure → friendly 502
        raise HTTPException(502, f"assistant error: {type(e).__name__}")
    finally:
        conn.close()


# ---- accounts management (SPA Accounts page) --------------------------------

@router.get("/connections")
def connections_api(user: dict = Depends(_user())):
    """Aggregator connections with health (status, last successful pull) +
    the header's relative "synced Xm ago" chip — the Accounts page's
    status dots and last_sync, as JSON for the SPA."""
    from .pages import _connections
    conn = _conn(user)
    try:
        rows = [_ser(r) for r in _connections(conn)]
        last = conn.execute(
            "SELECT MAX(ran_at) AS t FROM sync_log WHERE error IS NULL"
        ).fetchone()
        # is a sync running RIGHT NOW, whoever started it. Folded
        # into this endpoint rather than given its own, because the header
        # already polls this one every 60s — a separate /syncing route would
        # have doubled the app's idle request rate to answer a question this
        # response was already 90% of the way to answering.
        #
        # Same staleness window as the claim in /jobs/sync/start — see
        # SYNC_STALE_AFTER. A deploy that restarts the container mid-sync
        # leaves state='running' with no owner, and these two must agree
        # about when that row stops meaning anything: two different windows
        # leave this endpoint reporting no sync running while the other
        # refuses to start one, and a click does nothing with nothing to
        # explain it.
        job = conn.execute(
            "SELECT state, progress, updated_at FROM job_progress "
            "WHERE id='sync'").fetchone()
    finally:
        conn.close()
    syncing, sync_progress = False, None
    if job and job["state"] == "running":
        age = dt.datetime.now(job["updated_at"].tzinfo) - job["updated_at"]
        if age.total_seconds() < 3 * 60:      # SYNC_STALE_AFTER
            from ..engine.compat import as_dict
            prog = as_dict(job["progress"]) or {}
            syncing = True
            total, done = prog.get("total"), prog.get("done")
            if isinstance(total, int) and isinstance(done, int) and total > 0:
                # `phase` names what the tail is doing once every
                # bank has answered, so a full counter that is still moving
                # reads as a named stage rather than a stuck one.
                sync_progress = {"done": min(done, total), "total": total,
                                 "phase": prog.get("phase") or None}
    last_sync = None
    if last and last["t"]:
        then = last["t"]
        now = dt.datetime.now(then.tzinfo) if then.tzinfo else dt.datetime.now()
        mins = max(0, int((now - then).total_seconds() // 60))
        if mins < 60:
            last_sync = f"synced {mins}m ago"
        elif mins < 48 * 60:
            last_sync = f"synced {mins // 60}h ago"
        else:
            last_sync = f"synced {mins // (24 * 60)}d ago"
    return {"connections": rows, "last_sync": last_sync,
            "syncing": syncing, "sync_progress": sync_progress}


@router.get("/holdings")
def holdings_api(user: dict = Depends(_user())):
    """Top investment holdings across accounts — the Accounts page's
    'Top holdings' table (value-ranked, top 60)."""
    conn = _conn(user)
    try:
        rows = conn.execute(
            """SELECT COALESCE(a.display_name, a.name) AS acct,
                      h.symbol, h.name AS fund, h.quantity AS qty,
                      h.price, h.value
               FROM holdings h JOIN accounts a ON a.id = h.account_id
               WHERE h.value IS NOT NULL
               ORDER BY h.value DESC LIMIT 60""").fetchall()
        return {"holdings": [_ser(r) for r in rows]}
    finally:
        conn.close()


@router.post("/accounts/rename")
def accounts_rename_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """User rename — display_name override that sync never touches. An
    empty name reverts to the aggregator's name."""
    account_id = _body_text(body, "account_id")
    if not account_id:
        raise HTTPException(400, "account_id required")
    conn = _conn(user)
    try:
        prev = conn.execute(
            "SELECT COALESCE(NULLIF(display_name, ''), name) AS name "
            "FROM accounts WHERE id=%s", (account_id,)).fetchone()
        if not data.set_account_display_name(conn, account_id,
                                             _body_text(body, "name")):
            raise HTTPException(404, "account not found")
        now = conn.execute(
            "SELECT COALESCE(NULLIF(display_name, ''), name) AS name "
            "FROM accounts WHERE id=%s", (account_id,)).fetchone()
        if prev and now and prev["name"] != now["name"]:
            _activity.record(conn, user, "account", "renamed",
                             target=account_id, label=prev["name"] or account_id,
                             detail={"before": prev["name"],
                                     "after": now["name"]})
        return {"ok": True}
    finally:
        conn.close()


@router.post("/accounts/classify")
def accounts_classify_api(user: dict = Depends(_user()),
                          body: dict = Body(...)):
    from ..engine import budget
    from ..sync import base as sync_base
    kind = _body_text(body, "kind")
    account_id = _body_text(body, "account_id")
    typ, _, sub = kind.partition("/")
    if not typ or not account_id:
        raise HTTPException(400, "account_id and kind required")
    # the type drives the spend universe (depository/credit) and the
    # net-worth grouping — an arbitrary string here would silently drop
    # the account out of every aggregate
    if typ not in ("depository", "credit", "investment", "loan", "other"):
        raise HTTPException(400, "unknown account type")
    conn = _conn(user)
    try:
        pin = bool(body.get("primary_checking")) if "primary_checking" in body \
            else None                       # absent = leave the anchor alone
        # ONE transaction: the refusal below must not leave the type
        # change committed, and the account row is locked so a concurrent
        # entity assignment cannot slip in between the check and the pin
        with budget.config_txn(conn) as cfg:
            row = conn.execute(
                "SELECT entity_id FROM accounts WHERE id=%s FOR UPDATE",
                (account_id,)).fetchone()
            # a write that matched no row is a 404, not an ok: an id can
            # go away under a reconnect adoption's rename, and reporting
            # success for a change nobody will see is how a lost edit
            # stays invisible
            if row is None:
                raise HTTPException(404, "account not found")
            # the runway ignores a business account unless the household
            # combines entities (forecast); a pin the forecast will not
            # honour must be refused, not saved
            if pin and row["entity_id"] and not cfg.get("combine_entities"):
                raise HTTPException(
                    400, "a business account cannot be the household's "
                         "primary checking — move it back to personal "
                         "first, or turn on the combined view")
            sync_base.mark_type_user_set(conn, account_id, typ, sub or None)
            if pin:
                cfg["checking_account_id"] = account_id
            elif pin is False and cfg.get("checking_account_id") == account_id:
                cfg.pop("checking_account_id")
        return {"ok": True}
    finally:
        conn.close()


@router.post("/accounts/exclude")
def accounts_exclude_api(user: dict = Depends(_user()),
                         body: dict = Body(...)):
    """Toggle an account's budget exclusion — config 'excluded_accounts'
    (account-id list); the engine's _spend_rows skips those accounts, so
    their transactions never count as spend (the Accounts page's 'excl'
    toggle)."""
    from ..engine import budget
    aid = str(body.get("account_id") or "")
    if not aid:
        raise HTTPException(400, "account_id required")
    conn = _conn(user)
    try:
        if not conn.execute("SELECT 1 FROM accounts WHERE id=%s",
                            (aid,)).fetchone():
            raise HTTPException(404, "no such account")
        with budget.config_txn(conn) as cfg:
            cur = list(cfg.get("excluded_accounts") or [])
            want = (bool(body["excluded"]) if "excluded" in body
                    else aid not in cur)
            if want and aid not in cur:
                cur.append(aid)
            elif not want:
                cur = [a for a in cur if a != aid]
            if cur:
                cfg["excluded_accounts"] = cur
            else:
                cfg.pop("excluded_accounts", None)
        nm = conn.execute(
            "SELECT COALESCE(NULLIF(display_name, ''), name) AS name "
            "FROM accounts WHERE id=%s", (aid,)).fetchone()
        _activity.record(conn, user, "account",
                         "excluded" if want else "included", target=aid,
                         label=(nm or {}).get("name") or aid)
        return {"ok": True, "account_id": aid, "excluded": want}
    finally:
        conn.close()


@router.post("/accounts/add")
def accounts_add_api(user: dict = Depends(_user()), body: dict = Body(...)):
    import re
    name = _body_text(body, "name")
    if not name:
        raise HTTPException(400, "name required")
    typ, _, sub = _body_text(body, "kind",
                             default="depository/checking").partition("/")
    bal = _money_float(body.get("balance") or "0", "balance")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "account"
    conn = _conn(user)
    try:
        conn.execute(
            """INSERT INTO items (id, aggregator, institution_name, access_token)
               VALUES ('manual','manual','Manual',NULL)
               ON CONFLICT (tenant_id, id) DO NOTHING""")
        aid = f"manual:{slug}"
        n = 2
        while conn.execute("SELECT 1 FROM accounts WHERE id=%s",
                           (aid,)).fetchone():
            aid = f"manual:{slug}-{n}"      # "Checking!" vs "Checking?" must
            n += 1                          # not silently collapse into one
        # balance defaults to 0, never NULL: a NULL balance files the
        # brand-new account under archived/closed and the cash runway
        # screams 'out of money' on day one
        conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype,
                                     balance_current, updated_at)
               VALUES (%s,'manual',%s,%s,%s,%s,now())""",
            (aid, name, typ, sub or None, bal))
        return {"ok": True, "account_id": aid}
    finally:
        conn.close()


@router.post("/accounts/balance")
def accounts_balance_api(user: dict = Depends(_user()),
                         body: dict = Body(...)):
    aid = str(body.get("account_id") or "")
    conn = _conn(user)
    try:
        row = conn.execute(
            "SELECT i.aggregator FROM accounts a JOIN items i ON i.id=a.item_id "
            "WHERE a.id=%s", (aid,)).fetchone()
        if not row:
            raise HTTPException(404, "no such account")
        if row["aggregator"] != "manual":
            raise HTTPException(400, "balance is synced for connected accounts")
        val = _money_float(body.get("balance", ""), "balance")
        conn.execute("UPDATE accounts SET balance_current=%s, "
                     "balance_available=%s WHERE id=%s", (val, val, aid))
        return {"ok": True}
    finally:
        conn.close()


@router.post("/accounts/simplefin")
def accounts_simplefin_api(user: dict = Depends(_user()),
                           body: dict = Body(...)):
    demoguard.deny(user)
    _no_byo_on_hosted()
    import datetime as _dt

    from ..sync import simplefin as _sf
    token = _body_text(body, "token")
    if not token:
        raise HTTPException(400, "token required")
    from ..sync.base import tenant_sync_lock
    conn = _conn(user)
    try:
        with tenant_sync_lock(conn, user["tenant_id"]) as held:
          if not held:
            raise HTTPException(409, "a sync is already running — "
                                     "give it a moment")
          try:
            access = _sf.claim_setup_token(token)
            r = _sf.sync(conn, "sfin-main", access,
                         since=_dt.date.today() - _dt.timedelta(days=90))
          except Exception:  # noqa: BLE001 — surface, don't 500
            import logging
            logging.getLogger("oikonome.simplefin").exception(
                "simplefin connect failed")
            raise HTTPException(400, "connection failed — check the token "
                                "(one-time use, unexpired) and try again")
        return {"ok": True, "accounts": r.get("accounts", 0),
                "transactions": r.get("transactions", 0)}
    finally:
        conn.close()


# ---- settings (SPA Settings page) -------------------------------------------

_SETTINGS_FIELDS = ("food_monthly", "other_monthly", "custom_buckets",
                    "budgeted_income_monthly", "dynamic_variable_budget",
                    "email_recipients", "email_muted",
                    "email_send_hour_utc",
                    "checking_account_id",
                    # retirement / income-scenario keys
                    "birthdate", "ss_estimates", "retirement_saving",
                    "income_biweekly_saving", "income_biweekly_not_saving",
                    # read-only in the SPA: the Accounts 'excl' toggle + the
                    # recurring tweaks — each is WRITTEN by
                    # its own feature, only READ here. manual_assets is
                    # writable (Net Worth page editor).
                    "excluded_accounts", "occurrence_caps", "disabled_bills",
                    "dismissed_hints", "manual_assets",
                    # smart-categorization LLM backend (the same keys as the setup
                    # wizard). llm_api_key and llm_extra_body are
                    # deliberately NOT here: both are credentials, so they are
                    # write-only — GET reports only llm_api_key_set /
                    # llm_extra_body_set.
                    "llm_url", "llm_model",
                    "llm_vision_model", "taxdocs_allow_remote_llm",
                    # provider key presence for the Connections
                    # card (secrets stay write-only)
                    "plaid_client_id", "plaid_env",
                    "mx_client_id", "mx_env",
                    # pinned UI theme (absent = follow the OS)
                    "theme",
                    # default landing page per client (absent = Today):
                    # a web route / a mobile tab name
                    "home_web", "home_mobile",
                    # the household's IANA zone — every scheduled hour is
                    # kept in it (absent = the instance's OIKONOME_TZ)
                    "timezone",
                    # Today hero face (absent = simple): "advanced" shows
                    # the full tile/goal anatomy inline. The toggle lives
                    # on the page and governs the page alone — the daily
                    # email's face is the separate email_schedule.daily
                    # .summary setting (decoupled)
                    "today_view",
                    # SMTP delivery via the web UI (password is
                    # write-only — GET reports only smtp_password_set)
                    "smtp_host", "smtp_port", "smtp_user", "smtp_from",
                    "smtp_starttls",
                    # feedback pipeline on/off (absent = on)
                    "feedback_enabled",
                    # merchant logos on the ledger / merchants / spending /
                    # email (absent = on; False = the minimal look)
                    "merchant_logos",
                    # Plaid's recurring streams as a bill-detection
                    # cross-check (absent = on; False = off)
                    "plaid_recurring",
                    # rows/month ceiling on Plaid Enrich (absent = the
                    # connector's default). Enrich is billed per row, so the
                    # ceiling is the whole cost control.
                    "plaid_enrich_cap",
                    # explicit city names stripped from the TAIL
                    # of merchant labels (banks truncate the store's city
                    # into the descriptor at different lengths). Empty = off;
                    # never inferred — see merchant_dedup.cities_for.
                    "merchant_strip_cities",
                    # read-only mirror of what the aggregator itself
                    # reported, so the Settings card can show what is
                    # already handled without the user retyping it
                    "merchant_cities_seen",
                    # {daily,weekly,monthly} local-time schedule
                    "email_schedule")


# resolved-address cache for the locality verdict below: a settings GET
# classifies up to half a dozen endpoint URLs (bundled + backends + one per
# role), usually sharing one or two hosts, and getaddrinfo on a dead name
# can block for seconds. Short TTL — a name that starts resolving publicly
# must flip the verdict promptly.
_LLM_DNS_TTL = 120.0
_LLM_DNS_CACHE: dict[str, tuple[float, list[str]]] = {}


def _llm_is_local(url: str) -> bool:
    """Whether an LLM endpoint stays on this machine/network: loopback,
    RFC-1918, or a compose-network service. Anything else is a cloud
    endpoint the data leaves the instance to reach.

    Judged by ADDRESS, not by what the name looks like — the verdict tells
    the user their data stays home, and clients surface it beside consent
    decisions, so "ai.internal" or a dotless vanity host that resolves to
    the public internet must not read as local. An IP literal is classified
    directly and a hostname is resolved; every resolved address must be
    non-globally-routable, per netguard's shared address policy (which also
    folds IPv4-mapped IPv6 spellings to their embedded v4 form). The name
    heuristic (localhost / .local / dotless service name) applies
    only to names that do not resolve at all — an mDNS or compose name is
    often invisible from this process, and an unresolvable endpoint cannot
    send data anywhere; the moment it resolves publicly the verdict flips.
    """
    from urllib.parse import urlsplit
    import ipaddress
    import time
    from . import netguard
    host = (urlsplit(url).hostname or "").strip("[]").lower()
    if not host:
        return False
    try:
        ips = [str(ipaddress.ip_address(host))]
    except ValueError:
        now = time.monotonic()
        hit = _LLM_DNS_CACHE.get(host)
        if hit is not None and now - hit[0] < _LLM_DNS_TTL:
            ips = hit[1]
        else:
            ips = netguard._resolved_ips(host)
            if len(_LLM_DNS_CACHE) >= 256:
                _LLM_DNS_CACHE.clear()
            _LLM_DNS_CACHE[host] = (now, ips)
        if not ips:
            return (host == "localhost" or host.endswith(".local")
                    or "." not in host)
    for ip in ips:
        try:
            # netguard's hosted policy raises exactly when an address is
            # NOT globally routable (after IPv4-mapped folding) — reused
            # here as the classifier so the two never drift.
            netguard._check_ip(ip, what="llm endpoint", hosted=True)
        except netguard.BlockedURL:
            continue                  # non-global: stays on this network
        return False                  # any public address means off-box
    return True


def _settings_view(cfg: dict) -> dict:
    out = {k: cfg.get(k) for k in _SETTINGS_FIELDS}
    # business-wizard step marks — always present so the SPA renders one
    # shape whether or not a business has ever been set up
    out["biz_wizard_steps"] = cfg.get("biz_wizard_steps") or {}
    out["ret_wizard_steps"] = cfg.get("ret_wizard_steps") or {}
    # scenarios in migrated list form (the legacy pair maps to two
    # entries) + savings goals — always present so the SPA renders one shape
    from ..engine import budget as _budget
    scen, active = _budget.income_scenarios(cfg)
    out["income_scenarios"] = scen
    out["active_scenario"] = active
    out["savings_goals"] = cfg.get("savings_goals") or []
    # never echo the LLM API key (stored encrypted) — presence only
    out["llm_api_key_set"] = bool(cfg.get("llm_api_key"))
    # the legacy single endpoint's extra_body is the same credential as the
    # per-backend one below (it rides into every request body, so an
    # auth-in-body vendor's org token lives there) and gets the same
    # treatment: presence flag only, with "" keeping the field's declared
    # shape for clients. The save path reads "" as keep-stored, so this
    # masked echo can never wipe the value on a round trip.
    out["llm_extra_body"] = ""
    out["llm_extra_body_set"] = bool(cfg.get("llm_extra_body"))
    out["smtp_password_set"] = bool(cfg.get("smtp_password"))
    out["plaid_secret_set"] = bool(cfg.get("plaid_secret"))
    # Hosted: the host owns mail and the aggregator, so these are not the
    # tenant's to see. Withheld rather than blanked — a blank field reads
    # as "unset, go ahead and fill me in", which is exactly the invitation
    # the 403 then refuses. The SPA renders no card when they are absent.
    # Assigned BEFORE the withhold loop: assigned after, the loop's pop
    # would be a dead no-op and this field would escape the hosted
    # withholding invariant, rendering an MX credential card the
    # platform's API refuses.
    out["mx_api_key_set"] = bool(cfg.get("mx_api_key"))
    import os as _os
    # The AI panel's whole truth, per task: which backend is actually
    # answering each role. Without this, empty fields read as "no AI"
    # while the env-provided bundled Ollama does all the work. Hosted
    # never reveals the env fallback — that is the operator's
    # infrastructure. Stored api keys never leave as anything but a flag.
    _hosted = env_flag("OIKONOME_HOSTED")
    _env_url = _os.environ.get("OIKONOME_LLM_URL", "").strip()
    _env_model = _os.environ.get("OIKONOME_LLM_MODEL", "").strip()
    out["llm_bundled"] = ({"url": _env_url, "model": _env_model,
                           "local": _llm_is_local(_env_url)}
                          if _env_url and not _hosted else None)
    _bks = [b for b in (cfg.get("llm_backends") or []) if isinstance(b, dict)]
    out["llm_backends"] = [
        {"id": str(b.get("id")), "name": b.get("name") or "",
         "url": b.get("url") or "", "model": b.get("model") or "",
         "vision_model": b.get("vision_model") or "",
         # extra_body rides into every request body, so it can carry
         # credentials — masked like api_key beside it: presence flag
         # only, the empty string keeps the field's declared shape for
         # clients. The save path treats "" as keep-stored, so this
         # masked echo can never wipe the value on round trip.
         "extra_body": "",
         "extra_body_set": bool(b.get("extra_body")),
         "api_key_set": bool(b.get("api_key")),
         "local": _llm_is_local(b.get("url") or "")}
        for b in _bks]
    from ..engine.llm_categorize import ROLES as _LLM_ROLES
    from ..engine.llm_categorize import resolve_role as _resolve_role
    out["llm_roles"] = {k: v for k, v in (cfg.get("llm_roles") or {}).items()
                        if k in _LLM_ROLES}
    _own_url = str(cfg.get("llm_url") or "").strip()
    _cfg_vision = str(cfg.get("llm_vision_model") or "").strip()

    def _role_view(role: str):
        # the layer comes from the SAME resolver the execution path uses
        # (llm_categorize._backend) — reimplementing the precedence chain
        # here would agree with it only by hand-verification.
        # Here it is dressed for display: names and locality added, keys
        # and the operator's env fallback (on hosted) withheld.
        layer, b = _resolve_role(cfg, role)
        if layer == "backend":
            model = (b.get("vision_model") or b.get("model")
                     if role == "vision" else b.get("model")) or ""
            return {"source": "backend", "name": b.get("name") or "",
                    "url": b["url"], "model": model,
                    "local": _llm_is_local(b["url"])}
        if layer == "legacy":                  # legacy single endpoint
            model = ((_cfg_vision or cfg.get("llm_model"))
                     if role == "vision" else cfg.get("llm_model")) or ""
            return {"source": "tenant", "url": _own_url, "model": str(model),
                    "local": _llm_is_local(_own_url)}
        if _env_url and not _hosted:           # bundled, chosen or fallback
            model = ((_cfg_vision or _env_model)
                     if role == "vision" else _env_model)
            return {"source": "bundled", "url": _env_url, "model": model,
                    "local": _llm_is_local(_env_url)}
        return None

    out["llm_effective"] = {r: _role_view(r) for r in _LLM_ROLES}
    # the local trained classifier categorizes FIRST (seed rules → model →
    # LLM); when it's active the card must say so, or the Categorization
    # row reads as if the LLM owns work it only sees the abstentions of
    from ..engine import model_categorize as _mc
    out["categorizer_active"] = _mc.available()
    if env_flag("OIKONOME_HOSTED"):
        for _k in ("smtp_host", "smtp_port", "smtp_user", "smtp_from",
                   "smtp_starttls", "smtp_password_set",
                   "plaid_client_id", "plaid_env", "plaid_secret_set",
                   "mx_client_id", "mx_env", "mx_api_key_set"):
            out.pop(_k, None)
    return out


def _recipient_status(tenant_id, configured: list[str],
                      muted: list[str] | None = None) -> list[dict]:
    """Who receives the scheduled mail, and whether it reaches them.

    A plain list of addresses can only tell you what you typed. It cannot say
    that an address has never been confirmed, or that the provider has been
    refusing it for three days — and that is precisely the information the
    person adding a recipient needs, because the failure mode is silence.

    The OWNER leads the list and is marked non-removable: they receive the
    mail by construction (see `worker._recipients`), and a control that
    pretends otherwise would silently replace the owner instead of adding to
    them.

    Per address:
    owner — always mailed, cannot be removed here
    member — holds an account on this tenant. Hosted REQUIRES this; on
    self-host a free-form address is legitimate and simply
    has no account to be verified against.
    verified — the account confirmed its address. null when there is no
    account, because "unverified" would be a claim we have no
    standing to make about someone else's mailbox.
    delivery — the provider says mail to it is failing; the cadence is
    HELD until it is fixed.
    invite — `invited` (asked, no answer yet), `accepted`,
    `declined`, `expired`, or null for an address that has
    never been asked. This is the one that decides whether
    mail flows: only `accepted` is mailed. The owner needs it
    because "I added them and nothing happened" and "I added
    them and they haven't answered" look identical otherwise.
"""
    from ..db import tenancy as _ten
    from ..notify import delivery as _delivery
    from ..notify import recipient_invites as _invites
    rows, owner_email = [], None
    try:
        # users is a control-plane table the app role reads on every
        # request (session lookup) — the pooled connection serves this
        # read too; a fresh admin connect per Settings load would buy nothing
        cc = _ten.control_connect()
        try:
            members = {r["email"].lower(): r for r in cc.execute(
                "SELECT email, verified_at, role FROM users WHERE "
                "tenant_id=%s", (tenant_id,)).fetchall()}
        finally:
            cc.close()
    except Exception:                              # noqa: BLE001
        import logging
        logging.getLogger("oikonome.api").exception("recipient status failed")
        return []
    for m in members.values():
        if m["role"] == "owner":
            owner_email = m["email"]
            break
    # WITH NO LIST CONFIGURED the worker mails every verified user on the
    # tenant (`_recipients_raw`'s fallback) — not just the owner. Listing
    # the owner alone would show a three-person household "you" while all
    # three receive the balances every morning. The card's own promise is
    # "who receives the scheduled mail"; it has to mean it.
    implied = ([m["email"] for m in members.values() if m["verified_at"]]
               if not configured else [])
    ordered, seen = [], set()
    for e in ([owner_email] if owner_email else []) + list(configured) \
            + implied:
        if not e or e.lower() in seen:
            continue
        seen.add(e.lower())
        ordered.append(e)
    # ONE query for every address's delivery state, not one per failing
    # address on top of the set lookup.
    failing = _delivery.states(ordered)
    invites = _invites.states(tenant_id, ordered)
    # a restored config can carry email_muted in any JSON shape — a
    # non-list here 500's every Settings load for the tenant, and
    # Settings is the page you'd fix it from
    muted_set = ({str(m).lower() for m in muted}
                 if isinstance(muted, list) else set())
    for e in ordered:
        m = members.get(e.lower())
        state = failing.get(e.lower())
        inv = invites.get(e.lower())
        is_owner = bool(owner_email and e.lower() == owner_email.lower())
        rows.append({
            "email": e,
            "owner": is_owner,
            # asked not to receive the scheduled mail (per-person opt-out;
            # the owner can mute themselves too)
            "muted": e.lower() in muted_set,
            "member": bool(m),
            "verified": (bool(m["verified_at"]) if m else None),
            "delivery": (state["state"] if state else None),
            "delivery_reason": (state["reason"] if state else None),
            # the owner is mailed by construction and is never invited to
            # their own household's email — reporting a null invite state
            # for them would put a "not invited" badge on the one row that
            # can never need one
            # A member receiving by the no-list fallback was never invited
            # and never needs to be — saying "not invited" about them would
            # be the same wrong badge the owner row must never get.
            "invite": (None if (is_owner or (not configured and m))
                       else (inv["state"] if inv else None)),
            # why this address is on the list at all
            "via": ("owner" if is_owner
                    else "member" if (not configured and m) else "invited"),
            "invited_at": (None if is_owner or not inv else
                           (inv["invited_at"].isoformat()
                            if inv["invited_at"] else None)),
        })
    return rows


@router.get("/settings")
def settings_get(user: dict = Depends(_user())):
    from ..engine import budget
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
    finally:
        conn.close()
    view = _settings_view(cfg)
    # publish the schedule the worker actually runs, so no client has to
    # invent a default for a household that never saved one
    from ..notify.schedule import effective_email_schedule
    view["email_schedule"] = effective_email_schedule(cfg)
    view["email_recipient_status"] = _recipient_status(
        user["tenant_id"], cfg.get("email_recipients") or [],
        cfg.get("email_muted") or [])
    if demoguard.is_demo(user["tenant_id"]):
        # Feedback is OFF on a demo and cannot be switched on: the tab
        # would invite bug reports about synthetic
        # data from anonymous visitors, and its Doctor bundle is instance
        # detail. Reported false regardless of stored config, so the SPA
        # hides the tab; the submit endpoint denies demos outright, and
        # settings writes are refused, so nothing can flip it back.
        view["feedback_enabled"] = False
        view["demo_read_only"] = True
    return view


# Demo lockdown. A demo instance is read-only wholesale (see settings_set)
# — the shared, publicly printed credentials mean any write is one visitor
# reconfiguring the instance for every later visitor. This set is kept
# because it names the keys that must NEVER be writable on a demo even if
# the blanket rule is ever relaxed.
_DEMO_DOOR_KEYS = frozenset({
    "smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from",
    "smtp_starttls",
    "llm_url", "llm_model", "llm_api_key", "llm_vision_model",
    "llm_extra_body", "llm_backends", "llm_roles",
    "email_recipients", "taxdocs_allow_remote_llm",
})


@router.post("/settings")
def settings_set(user: dict = Depends(_user()), body: dict = Body(...)):
    # The demo instance is READ-ONLY: its credentials
    # are shared and printed publicly, so ANY settings write is one visitor
    # reconfiguring the instance for everyone who comes after. Refusing only
    # the "door" keys (_DEMO_DOOR_KEYS — mail/LLM endpoints) is not enough:
    # the whole endpoint is closed. The SPA greys the page out to match, and
    # this is the backstop that makes that true whatever is POSTed.
    demoguard.deny(user)
    # A member edits the household's money, not its plumbing: the mail
    # relay, the daily-email recipient list and the AI endpoint are owner
    # fields. They are DROPPED from the body rather than refused, because
    # the SPA posts the whole settings document on every save — refusing
    # would mean a member could never change a budget either.
    body = permissions.settings_for(user["role"], body)
    from ..engine import budget
    conn = _conn(user)
    try:
        # The whole 500-line body is one read-modify-write
        # of the settings document, so a second save that read before
        # this one committed would overwrite every field this
        # request touched. The lock is held for the WHOLE body — the
        # validation raises inside it abort the transaction, which is
        # intended: a 400 on field nine
        # must not persist fields one through eight.
        #
        # EXCEPT the network validation. netguard's
        # getaddrinfo has no timeout — a blackholed domain stalls glibc
        # for ~10-40s — and running it inside the row lock while holding a
        # pooled tenant connection would let ten concurrent saves serialize into
        # minutes of a starved pool. Caller-supplied values are resolved
        # HERE, before any lock; the in-lock sites re-check only values
        # that came from the stored config (legacy rows).
        # (hosted skips the smtp half: the 403 owns that path and
        # must answer first — no DNS is ever done for a refused write)
        _sup_smtp = ("" if env_flag("OIKONOME_HOSTED")
                     else str(body.get("smtp_host") or "").strip())
        _sup_llm = str(body.get("llm_url") or "").strip()
        # multi-backend AI list: every entry's URL is body-supplied, so all
        # of them are resolved and guarded here, above the lock, like
        # llm_url — the in-lock code never re-checks body values
        _sup_bk = [str(e.get("url") or "").strip()
                   for e in (body.get("llm_backends") or [])
                   if isinstance(e, dict)
                   ] if isinstance(body.get("llm_backends"), list) else []
        if _sup_smtp or _sup_llm or any(_sup_bk):
            from .netguard import BlockedURL as _BURL
            from .netguard import check_host as _chost
            from .netguard import check_url as _curl
            try:
                if _sup_smtp:
                    _chost(_sup_smtp, what="the SMTP host")
                if _sup_llm:
                    _curl(_sup_llm, what="llm_url")
                for _u in _sup_bk:
                    if _u:
                        _curl(_u, what="llm backend url")
            except _BURL as e:
                raise HTTPException(400, str(e))
        with budget.config_txn(conn) as cfg:
            # who was already on the list, so the save can tell an ADD
            # from a re-save of the same people. Captured before anything mutates
            # cfg — a diff taken after the write would invite everyone, every
            # time any unrelated setting was saved.
            _prev_recips = {str(r).strip().lower()
                            for r in (cfg.get("email_recipients") or [])}
            # the whole document before the save, so the activity log can
            # name the KEYS that changed (never a value)
            import copy as _copy
            _cfg_was = _copy.deepcopy(cfg)
            def _num(v, field):
                if v is None or v == "":
                    return 0.0
                import math
                try:
                    n = float(str(v).replace(",", "").replace("$", "").strip())
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{field} must be a number")
                # a literal NaN/Infinity would ride into the jsonb config and make
                # the whole save 500 (Postgres rejects non-finite in jsonb) — 400.
                if not math.isfinite(n):
                    raise HTTPException(400, f"{field} must be a finite number")
                return n

            def _stated(v):
                """Did the caller actually supply a number? `0` is an answer;
                a missing key or a blank field is not."""
                return v is not None and str(v).strip() != ""

            for k in ("food_monthly", "other_monthly"):
                if k in body:
                    cfg[k] = max(0.0, _num(body[k], k))
            if "budgeted_income_monthly" in body:
                v = _num(body["budgeted_income_monthly"], "income")
                if v > 0:
                    cfg["budgeted_income_monthly"] = v
                else:
                    cfg.pop("budgeted_income_monthly", None)
            if "dynamic_variable_budget" in body:
                cfg["dynamic_variable_budget"] = bool(body["dynamic_variable_budget"])
            if "plaid_enrich_cap" in body:
                # rows/month a person is willing to have Plaid Enrich bill
                # for. Clamped rather than free: 0 turns it off, and the
                # upper bound stops a typo'd extra zero from authorising a
                # spend nobody meant to approve.
                try:
                    cap = int(body["plaid_enrich_cap"])
                except (TypeError, ValueError, OverflowError):
                    raise HTTPException(400,
                                        "plaid_enrich_cap must be an integer")
                cfg["plaid_enrich_cap"] = max(0, min(cap,
                                                     _enrich_cap_ceiling()))
            # A stored cap above the ceiling — an operator may lower the ceiling at
            # any time — would keep authorising that spend forever. Any save past this
            # point brings it back under the ceiling, whether or not this
            # request was the one that touched the key.
            _cap_now = cfg.get("plaid_enrich_cap")
            if _cap_now is not None:
                try:
                    _ceiling = _enrich_cap_ceiling()
                    if int(_cap_now) > _ceiling:
                        cfg["plaid_enrich_cap"] = _ceiling
                except (TypeError, ValueError, OverflowError):
                    cfg.pop("plaid_enrich_cap", None)   # corrupt → default
            if "wizard_done" in body:
                cfg["wizard_done"] = bool(body["wizard_done"])
            if "wizard_steps" in body:
                # explicit per-step marks for the strict wizard —
                # merge semantics so each step posts only its own state
                raw = body["wizard_steps"] or {}
                if not isinstance(raw, dict):
                    raise HTTPException(400, "wizard_steps must be an object")
                steps = dict(cfg.get("wizard_steps") or {})
                for k, v in raw.items():
                    if k not in ("connect", "sync", "import",
                                 "bills", "budgets", "email", "finish"):
                        raise HTTPException(400, f"unknown wizard step: {k}")
                    if v in (None, ""):
                        steps.pop(k, None)
                    elif v in ("done", "skipped"):
                        steps[k] = v
                    else:
                        raise HTTPException(
                            400, "step state must be done|skipped|null")
                cfg["wizard_steps"] = steps
            if "biz_wizard_steps" in body:
                # The BUSINESS setup wizard's per-step marks. Separate key from
                # wizard_steps on purpose: they are different flows with
                # different steps, and one tenant can have finished onboarding
                # while never having set up a business (or the reverse).
                #
                # Why persisted at all: completion held in React state is
                # forgotten on refresh — the wizard cannot resume, and it
                # prompts "continue setup" forever because nothing records
                # that it is done. Same merge semantics as above: each
                # step posts only its own mark, null forgets it.
                raw = body["biz_wizard_steps"] or {}
                if not isinstance(raw, dict):
                    raise HTTPException(400, "biz_wizard_steps must be an object")
                steps = dict(cfg.get("biz_wizard_steps") or {})
                for k, v in raw.items():
                    if k not in ("what", "when", "accounts", "startup", "finish"):
                        raise HTTPException(400, f"unknown business step: {k}")
                    if v in (None, ""):
                        steps.pop(k, None)
                    elif v in ("done", "skipped"):
                        steps[k] = v
                    else:
                        raise HTTPException(
                            400, "step state must be done|skipped|null")
                cfg["biz_wizard_steps"] = steps
            if "ret_wizard_steps" in body:
                # Retirement setup's per-step marks. Third flow, third key: a
                # tenant can finish onboarding, never run a business, and be
                # halfway through retirement planning — one shared key could not
                # express that. Same merge semantics as the two above.
                raw = body["ret_wizard_steps"] or {}
                if not isinstance(raw, dict):
                    raise HTTPException(400, "ret_wizard_steps must be an object")
                steps = dict(cfg.get("ret_wizard_steps") or {})
                for k, v in raw.items():
                    if k not in ("age", "spend", "saving", "assumptions", "done"):
                        raise HTTPException(400, f"unknown retirement step: {k}")
                    if v in (None, ""):
                        steps.pop(k, None)
                    elif v in ("done", "skipped"):
                        steps[k] = v
                    else:
                        raise HTTPException(
                            400, "step state must be done|skipped|null")
                cfg["ret_wizard_steps"] = steps
            if "email_schedule" in body:
                # {daily:{on,hour}, weekly:{on,hour,weekday}, monthly:
                # {on,hour}} — hours LOCAL (OIKONOME_TZ), weekday 0=Monday
                raw = body["email_schedule"]
                if raw in (None, {}):
                    cfg.pop("email_schedule", None)   # legacy daily behavior
                elif not isinstance(raw, dict):
                    raise HTTPException(400, "email_schedule must be an object")
                else:
                    clean = {}
                    # each cadence (yearly included) carries
                    # per-channel flags — `on` stays email (back-compat),
                    # sms/push are additive
                    for cad in ("daily", "weekly", "monthly", "yearly"):
                        c = raw.get(cad) or {}
                        if not isinstance(c, dict):
                            raise HTTPException(400, f"{cad} must be an object")
                        try:
                            hour = int(c.get("hour", 7))
                            weekday = int(c.get("weekday", 0))
                        except (TypeError, ValueError):
                            raise HTTPException(400, "hour/weekday must be numbers")
                        if not 0 <= hour <= 23:
                            raise HTTPException(400, "hour must be 0-23")
                        if not 0 <= weekday <= 6:
                            raise HTTPException(400, "weekday must be 0-6")
                        entry = {"on": bool(c.get("on")), "hour": hour}
                        if cad == "weekly":
                            entry["weekday"] = weekday
                        if c.get("sms"):
                            entry["sms"] = True
                        if c.get("push"):
                            entry["push"] = True
                        # daily only: the summary email — just the verdict
                        # pane, not the full report
                        if cad == "daily" and c.get("summary"):
                            entry["summary"] = True
                        clean[cad] = entry
                    cfg["email_schedule"] = clean
            if "merchant_strip_cities" in body:
                # explicit extras on top of the auto-harvested list: city names
                # stripped from the END of a merchant label. Accepts a list or a
                # comma/newline separated string; empty clears it.
                v = body["merchant_strip_cities"]
                if isinstance(v, str):
                    v = re.split(r"[,;\n]", v)
                if not isinstance(v, list):
                    raise HTTPException(400, "merchant_strip_cities must be a "
                                             "list or comma-separated string")
                clean = []
                for x in v[:100]:                     # bounded
                    x = str(x).strip()[:60]
                    if x and x.lower() not in {c.lower() for c in clean}:
                        clean.append(x)
                if clean:
                    cfg["merchant_strip_cities"] = clean
                else:
                    cfg.pop("merchant_strip_cities", None)
            if "merchant_logos" in body:
                if body["merchant_logos"] in (True, None, ""):
                    cfg.pop("merchant_logos", None)      # absent = on
                else:
                    cfg["merchant_logos"] = False
            if "plaid_recurring" in body:
                if body["plaid_recurring"] in (True, None, ""):
                    cfg.pop("plaid_recurring", None)     # absent = on
                else:
                    cfg["plaid_recurring"] = False
            if "feedback_enabled" in body:
                if body["feedback_enabled"] in (True, None, ""):
                    cfg.pop("feedback_enabled", None)   # absent = on
                else:
                    cfg["feedback_enabled"] = False
            if "timezone" in body:
                # An IANA name, or empty to follow the instance again. A
                # zone this host cannot load is refused rather than stored:
                # the sweep would silently fall back to the instance clock
                # and the page would keep promising an hour nobody keeps.
                from .. import localtime
                z = str(body["timezone"] or "").strip()
                if not z:
                    cfg.pop("timezone", None)
                elif not localtime.valid_zone(z):
                    raise HTTPException(400, f"unknown time zone: {z[:40]}")
                else:
                    cfg["timezone"] = z
            # default home page per client — a closed vocabulary, because
            # the clients redirect to it blind at launch and an arbitrary
            # route would strand the app on a 404 every open
            from .. import homepages
            for key, allowed in homepages.VOCAB.items():
                if key in body:
                    v = _body_text(body, key)
                    if not v or v in ("/", "index"):
                        cfg.pop(key, None)          # absent = Today
                    elif v not in allowed:
                        raise HTTPException(400, f"{key}: unknown page")
                    else:
                        cfg[key] = v
            if "theme" in body:
                t = str(body["theme"] or "auto")
                if t not in ("auto", "light", "dark"):
                    raise HTTPException(400, "theme must be auto, light or dark")
                if t == "auto":
                    cfg.pop("theme", None)     # absent = follow the OS
                else:
                    cfg["theme"] = t
            if "today_view" in body:
                tv = str(body["today_view"] or "summary")
                # advanced/simple are accepted as aliases
                tv = {"advanced": "detail", "simple": "summary"}.get(tv, tv)
                if tv not in ("summary", "detail"):
                    raise HTTPException(400,
                                        "today_view must be summary or detail")
                if tv == "summary":
                    cfg.pop("today_view", None)   # absent = the default face
                else:
                    cfg["today_view"] = tv
            if any(k in body for k in ("smtp_host", "smtp_port", "smtp_user",
                                       "smtp_password", "smtp_from",
                                       "smtp_starttls")):
                _no_smtp_byo_on_hosted()
                # web-configured email delivery. host anchors the
                # group — empty host clears everything (env fallback returns);
                # password is write-only and encrypted at rest, empty = keep.
                from ..db import crypto
                host = str(body.get("smtp_host", cfg.get("smtp_host") or "")
                           or "").strip()
                if host:
                    # a tenant-supplied SMTP host is an SSRF primitive
                    # on hosted (probe internal nets on port 25/587) — same
                    # netguard policy the LLM URL gets. Self-host keeps LAN
                    # relays; only the metadata endpoint is blocked there.
                    # A body-supplied host was already resolved ABOVE
                    # the lock — only a stored legacy value re-checks here.
                    if not _sup_smtp:
                        from .netguard import BlockedURL, check_host
                        try:
                            check_host(host, what="the SMTP host")
                        except BlockedURL as e:
                            raise HTTPException(400, str(e))
                    cfg["smtp_host"] = host
                    try:
                        port = int(body.get("smtp_port",
                                            cfg.get("smtp_port") or 587) or 587)
                    except (TypeError, ValueError):
                        raise HTTPException(400, "smtp_port must be a number")
                    if not 1 <= port <= 65535:
                        raise HTTPException(400, "smtp_port out of range")
                    cfg["smtp_port"] = port
                    if "smtp_user" in body:
                        cfg["smtp_user"] = str(body["smtp_user"] or "").strip()
                    if "smtp_from" in body:
                        cfg["smtp_from"] = str(body["smtp_from"] or "").strip()
                    if "smtp_starttls" in body:
                        # `resolve_smtp` reads this key, so it needs a writer:
                        # without one, configuring SMTP in the UI silently forces
                        # STARTTLS and the documented OIKONOME_SMTP_STARTTLS
                        # escape hatch is unreachable — a LAN relay on plain 25
                        # reachable from docker/.env but not from the app that
                        # tells you to use the app.
                        cfg["smtp_starttls"] = bool(body["smtp_starttls"])
                    if str(body.get("smtp_password") or "").strip():
                        cfg["smtp_password"] = crypto.encrypt(
                            conn, str(body["smtp_password"]).strip())
                else:
                    for k in ("smtp_host", "smtp_port", "smtp_user",
                              "smtp_password", "smtp_from", "smtp_starttls"):
                        cfg.pop(k, None)
            if "custom_buckets" in body:
                # full-list replace — the SPA edits the whole set and
                # saves it back, so add/edit/delete are all this one write
                lst = body["custom_buckets"]
                if not lst:
                    cfg.pop("custom_buckets", None)
                elif not isinstance(lst, list):
                    raise HTTPException(400, "custom_buckets must be a list")
                else:
                    from ..engine.budget import RESERVED_BUCKET_NAMES
                    # The ONE settings list with no length cap.
                    # ~20k minimal buckets fit the 1 MB body limit, and every
                    # verdict computation then runs _claim_custom per spend
                    # row over 20k un-memoised matchers — quadratic, tenant-
                    # wide, from one save. 40 is far above any real plan
                    # (mailguard's neighbours cap at 20-30); a hard 400, not
                    # a silent slice, so the SPA can say why.
                    if len(lst) > 40:
                        raise HTTPException(
                            400, "too many budget categories (max 40)")
                    cleaned, seen = [], set()
                    for e in lst:
                        if not isinstance(e, dict):
                            raise HTTPException(400, "each bucket must be an object")
                        name = str(e.get("name") or "").strip()
                        if not name or len(name) > 40:
                            raise HTTPException(
                                400, "each bucket needs a name (1-40 chars)")
                        if name.lower() in RESERVED_BUCKET_NAMES:
                            raise HTTPException(400, f"'{name}' is a reserved name")
                        if name.lower() in seen:
                            raise HTTPException(400, f"duplicate bucket '{name}'")
                        parent = e.get("parent")
                        if parent not in ("food", "other"):
                            raise HTTPException(
                                400, f"bucket '{name}': parent must be food or other")
                        cats = e.get("categories") or []
                        merch = e.get("merchants") or []
                        if not isinstance(cats, list) or not isinstance(merch, list):
                            raise HTTPException(
                                400, f"bucket '{name}': categories and merchants "
                                     "must be lists")
                        cats = [str(c).strip() for c in cats if str(c).strip()]
                        merch = [str(m).strip() for m in merch if str(m).strip()]
                        # Same cap discipline: bounded element counts and
                        # per-string lengths (matchers re-tokenise every one
                        # against every spend row)
                        if len(cats) > 40 or len(merch) > 60:
                            raise HTTPException(
                                400, f"bucket '{name}': too many matchers "
                                     "(max 40 categories / 60 merchants)")
                        if any(len(s) > 120 for s in cats + merch):
                            raise HTTPException(
                                400, f"bucket '{name}': matcher too long "
                                     "(max 120 chars)")
                        # a matcher-less bucket is a valid plan
                        # placeholder (the wizard's add-your-own row) — it
                        # claims nothing until categories/merchants are added
                        # in Settings, and its bar just shows $0 actual
                        seen.add(name.lower())
                        cleaned.append({
                            "name": name, "parent": parent,
                            "monthly": max(0.0, _num(e.get("monthly"),
                                                     f"bucket '{name}' monthly")),
                            "categories": cats, "merchants": merch})
                    cfg["custom_buckets"] = cleaned
            if "email_recipients" in body:
                # Every sibling list key isinstance-checks and 400s; without
                # one a dict/int/bool feeds straight into parse_recipients'
                # `.split` — an AttributeError the global handler turns into
                # a bare 500
                if not isinstance(body["email_recipients"], (list, str)) \
                        and body["email_recipients"] is not None:
                    raise HTTPException(
                        400, "email_recipients must be a list of addresses")
                recips = mailguard.parse_recipients(body["email_recipients"])
                # parse_recipients CAPS the list — a defensive limit that
                # belongs there, since it also guards the invite fan-out.
                # Silently truncating would report success while the address
                # the person just typed was gone and still on their screen.
                # Say no instead of dropping it quietly.
                submitted = mailguard.parse_recipients(
                    body["email_recipients"], cap=None)
                if len(submitted) > mailguard.MAX_RECIPIENTS:
                    raise HTTPException(
                        400, f"that is {len(submitted)} recipients — the "
                             f"limit is {mailguard.MAX_RECIPIENTS}. Remove "
                             f"some and save again.")
                # hosted member-only recipients — shared with the
                # Jinja /settings/email door via mailguard so the
                # two paths can't drift.
                mailguard.check_recipients(conn, recips)
                if recips:
                    cfg["email_recipients"] = recips
                else:
                    cfg.pop("email_recipients", None)
            if "email_muted" in body:
                # per-person opt-out from the scheduled mail, the owner
                # included. Stored lowercased — the worker filters
                # case-insensitively. No membership check on purpose: a
                # mute for an address that later leaves the list is inert,
                # never harmful.
                if not isinstance(body["email_muted"], (list, str)) \
                        and body["email_muted"] is not None:
                    raise HTTPException(
                        400, "email_muted must be a list of addresses")
                # same cap-and-refuse as email_recipients above: without
                # a cap, one request could park
                # a ~1MB list in tenant config that every later
                # load_config — and every settings write's row-locked
                # before/after snapshot — then pays for. There is nothing
                # to mute beyond the recipient list, so the same limit is
                # the honest one.
                muted_in = mailguard.parse_recipients(
                    body["email_muted"], cap=None)
                if len(muted_in) > mailguard.MAX_RECIPIENTS:
                    raise HTTPException(
                        400, f"that is {len(muted_in)} muted addresses — "
                             f"the limit is {mailguard.MAX_RECIPIENTS}.")
                muted_in = sorted({m.lower() for m in muted_in})
                if muted_in:
                    cfg["email_muted"] = muted_in
                else:
                    cfg.pop("email_muted", None)
            if "email_send_hour_utc" in body:
                try:
                    cfg["email_send_hour_utc"] = max(0, min(23,
                                                     int(body["email_send_hour_utc"])))
                except (TypeError, ValueError):
                    raise HTTPException(400, "hour must be 0-23")
            if "birthdate" in body:
                v = str(body["birthdate"] or "").strip()
                if not v:
                    cfg.pop("birthdate", None)      # cleared → age falls back
                else:
                    try:
                        bd = dt.date.fromisoformat(v)
                    except ValueError:
                        raise HTTPException(
                            400, "birthdate must be an ISO date (YYYY-MM-DD)")
                    if bd >= _today(conn):
                        raise HTTPException(400, "birthdate must be in the past")
                    cfg["birthdate"] = bd.isoformat()
            if "retirement_saving" in body:
                cfg["retirement_saving"] = bool(body["retirement_saving"])
            for k in ("income_biweekly_saving", "income_biweekly_not_saving"):
                if k in body:
                    v = _num(body[k], k)
                    if v > 0:
                        cfg[k] = round(v, 2)
                    else:
                        # empty/zero clears the scenario key; with either cleared,
                        # active_monthly_income falls back to the static
                        # budgeted_income_monthly knob (else that knob is dead)
                        cfg.pop(k, None)
            # named income scenarios (one active). Saving the list form
            # supersedes the legacy pair — drop it so there's one source.
            if "income_scenarios" in body:
                scen_in = body["income_scenarios"] or []
                if not isinstance(scen_in, list):
                    raise HTTPException(400, "income_scenarios must be a list")
                scen, names = [], set()
                for s in scen_in[:20]:
                    if not isinstance(s, dict):
                        raise HTTPException(400, "each scenario must be an object")
                    name = str(s.get("name") or "").strip()[:60]
                    if not name:
                        raise HTTPException(400, "every scenario needs a name")
                    if name.lower() in names:
                        raise HTTPException(400, f"duplicate scenario '{name}'")
                    names.add(name.lower())
                    take = _num(s.get("take_home"), "take_home")
                    if take <= 0:
                        raise HTTPException(
                            400, f"scenario '{name}' needs a take-home > 0")
                    cadence = str(s.get("cadence") or "biweekly").strip().lower()
                    from ..engine.budget import CADENCE_MONTHLY
                    if cadence not in CADENCE_MONTHLY:
                        raise HTTPException(
                            400, "cadence must be one of "
                                 + ", ".join(sorted(CADENCE_MONTHLY)))
                    scen.append({"name": name, "take_home": round(take, 2),
                                 "cadence": cadence})
                if scen:
                    active = str(body.get("active_scenario")
                                 or cfg.get("active_scenario") or "").strip()
                    if active not in {s["name"] for s in scen}:
                        active = scen[0]["name"]
                    cfg["income_scenarios"], cfg["active_scenario"] = scen, active
                else:
                    for k in ("income_scenarios", "active_scenario"):
                        cfg.pop(k, None)
                for k in ("income_biweekly_saving", "income_biweekly_not_saving",
                          "retirement_saving"):
                    cfg.pop(k, None)
            elif "active_scenario" in body:
                from ..engine.budget import income_scenarios as _scen_fn
                scen, _ = _scen_fn(cfg)
                active = str(body["active_scenario"] or "").strip()
                if active and active not in {s["name"] for s in scen}:
                    raise HTTPException(400, f"no scenario named '{active}'")
                if scen:
                    # switching persists the migrated list form as a side effect
                    cfg["income_scenarios"] = scen
                    cfg["active_scenario"] = active or scen[0]["name"]
                    for k in ("income_biweekly_saving",
                              "income_biweekly_not_saving", "retirement_saving"):
                        cfg.pop(k, None)
            # savings goals with targets (ledger-driven progress)
            if "savings_goals" in body:
                goals_in = body["savings_goals"] or []
                if not isinstance(goals_in, list):
                    raise HTTPException(400, "savings_goals must be a list")
                out_goals, names = [], set()
                for g in goals_in[:30]:
                    if not isinstance(g, dict):
                        raise HTTPException(400, "each goal must be an object")
                    name = str(g.get("name") or "").strip()[:60]
                    if not name:
                        raise HTTPException(400, "every goal needs a name")
                    if name.lower() in names:
                        raise HTTPException(400, f"duplicate goal '{name}'")
                    names.add(name.lower())
                    target = _num(g.get("target") or 0, "target")
                    plan = round(max(0.0, _num(g.get("monthly_plan") or 0,
                                               "monthly_plan")), 2)
                    # open-ended saving (a monthly plan with no
                    # target yet) is valid — the wizard's default Savings
                    # bucket starts this way.
                    # so is an explicit $0. The planner writes its
                    # goal row on every save, zero included, because the row's
                    # presence is what remembers "I'm not planning savings" —
                    # drop it and the next visit re-prefills the leftover. A
                    # BLANK field is still an unanswered one, so the form that
                    # sends "" keeps its error.
                    if target <= 0 and plan <= 0 and not _stated(
                            g.get("monthly_plan")):
                        raise HTTPException(
                            400, f"goal '{name}' needs a target or a "
                                 "monthly plan")
                    # contribution mode: 'monthly' (fixed commitment, the
                    # default) or 'sweep' (month-end, surplus-capped).
                    # 'monthly'/absent normalizes to no key so existing
                    # configs and old clients round-trip byte-identical.
                    mode = str(g.get("mode") or "").strip().lower()
                    if mode not in ("", "monthly", "sweep"):
                        raise HTTPException(
                            400, "mode must be 'monthly' or 'sweep'")
                    goal = {"name": name, "target": round(max(0.0, target), 2),
                            "monthly_plan": plan,
                            "account_id": str(g.get("account_id") or "") or None,
                            "tokens": [str(t).strip()[:60]
                                       for t in (g.get("tokens") or [])[:8]
                                       if str(t).strip()],
                            "start_balance": round(
                                _num(g.get("start_balance") or 0,
                                     "start_balance"), 2)}
                    if mode == "sweep":
                        goal["mode"] = "sweep"
                    td = str(g.get("target_date") or "").strip()
                    if td:
                        try:
                            goal["target_date"] = dt.date.fromisoformat(
                                td).isoformat()
                        except ValueError:
                            raise HTTPException(
                                400, "target_date must be YYYY-MM-DD")
                    out_goals.append(goal)
                if out_goals:
                    cfg["savings_goals"] = out_goals
                else:
                    cfg.pop("savings_goals", None)
                    cfg.pop("savings_goal_state", None)
            if "manual_assets" in body:
                # property/vehicle/mortgage entries edited on the Net
                # Worth page. Auto-valuation keys (auto/rate/monthly_delta)
                # ride through untouched; a value edit arrives with
                # as_of cleared and gets stamped today, so "manual override
                # wins until the next auto update" is visible in the source tag.
                assets_in = body["manual_assets"] or []
                if not isinstance(assets_in, list):
                    raise HTTPException(400, "manual_assets must be a list")
                out_assets = []
                for a in assets_in[:30]:
                    if not isinstance(a, dict):
                        raise HTTPException(400, "each asset must be an object")
                    name = str(a.get("name") or "").strip()[:60]
                    if not name:
                        raise HTTPException(400, "every asset needs a name")
                    # as_of is read back as a date by the net-worth report and
                    # the auto-valuation step, so a string that is not one
                    # must stop here as a 400 rather than surface there later
                    as_of = _today(conn)
                    if a.get("as_of"):
                        from ..engine.compat import as_date
                        try:
                            as_of = as_date(a["as_of"])
                        except (TypeError, ValueError):
                            raise HTTPException(
                                400, "asset as_of must be YYYY-MM-DD")
                    entry = {"name": name,
                             "kind": str(a.get("kind") or "other").strip()[:30],
                             "value": round(_num(a.get("value") or 0, "value"), 2),
                             "as_of": as_of.isoformat()}
                    for k in ("auto", "rate", "monthly_delta"):
                        if a.get(k) is not None:
                            entry[k] = a[k]
                    out_assets.append(entry)
                if out_assets:
                    cfg["manual_assets"] = out_assets
                else:
                    cfg.pop("manual_assets", None)
            if "ss_estimates" in body:
                est = body["ss_estimates"]
                if not est:
                    cfg.pop("ss_estimates", None)
                elif not isinstance(est, dict):
                    raise HTTPException(
                        400, "ss_estimates must be an object {age: monthly $}")
                else:
                    sched = {}
                    for age_k, amt in est.items():
                        try:
                            a = int(age_k)
                        except (TypeError, ValueError):
                            raise HTTPException(400,
                                                "ss_estimates ages must be integers")
                        if not 62 <= a <= 70:
                            raise HTTPException(400,
                                                "ss_estimates ages must be 62-70")
                        sched[str(a)] = max(0.0, _num(amt, f"ss_estimates[{a}]"))
                    cfg["ss_estimates"] = sched     # JSON keys are strings
            if "llm_url" in body or "llm_model" in body or "llm_api_key" in body:
                # smart-categorization LLM backend — same rules as the setup
                # wizard: URL+model together, key optional (encrypted at rest,
                # never echoed or logged); empty llm_url clears all three.

                from ..db import crypto
                url = str(body.get("llm_url", cfg.get("llm_url") or "") or "").strip()
                model = str(body.get("llm_model",
                                     cfg.get("llm_model") or "") or "").strip()
                if url:
                    # Body-supplied URLs resolved above the lock;
                    # only a stored legacy value re-checks here
                    if not _sup_llm:
                        from . import netguard
                        try:
                            netguard.check_url(url, what="llm_url")
                        except netguard.BlockedURL as e:
                            raise HTTPException(400, str(e))
                    if not model:
                        raise HTTPException(400, "llm_model is required with llm_url")
                    cfg["llm_url"], cfg["llm_model"] = url, model
                    # optional dedicated vision model for receipts —
                    # the chat model is often text-only
                    if "llm_vision_model" in body:
                        vm = str(body["llm_vision_model"] or "").strip()
                        if vm:
                            cfg["llm_vision_model"] = vm
                        else:
                            cfg.pop("llm_vision_model", None)
                    # extra_body is arbitrary JSON merged into every request
                    # to this endpoint, so it can carry credentials — GET
                    # reports presence only, like the api_key below. Masking
                    # forces the per-backend twin's contract: "" (or an
                    # omitted key) KEEPS the stored value and an explicit
                    # JSON null clears it, because a masked "" is all a
                    # client has to send back — were that a clear, every save
                    # from a client that didn't retype the JSON would
                    # silently wipe it.
                    if "llm_extra_body" in body and body["llm_extra_body"] is None:
                        cfg.pop("llm_extra_body", None)   # explicit null
                    elif str(body.get("llm_extra_body") or "").strip():
                        eb = str(body["llm_extra_body"]).strip()
                        import json as _json
                        # must parse to an OBJECT: the value is merged
                        # into each request with dict.update, so "42"
                        # or "[1,2]" would pass a loads-only check and then
                        # crash every call using the backend
                        try:
                            _eb = _json.loads(eb)
                        except ValueError:
                            _eb = None
                        if not isinstance(_eb, dict):
                            raise HTTPException(
                                400,
                                "llm_extra_body must be a JSON object")
                        # encrypted at rest like the api_key beside it —
                        # same credential, same envelope (untagged rows
                        # pass through decrypt unchanged)
                        cfg["llm_extra_body"] = crypto.encrypt(conn, eb)
                else:
                    # no endpoint (given or stored): reject stray fields, else
                    # treat as an explicit clear of the whole group
                    if (str(body.get("llm_model") or "").strip()
                            or str(body.get("llm_api_key") or "").strip()):
                        raise HTTPException(400, "llm_url is required with "
                                            "llm_model / llm_api_key")
                    for k in ("llm_url", "llm_model", "llm_api_key",
                              "llm_extra_body", "llm_vision_model"):
                        cfg.pop(k, None)
                if url and "llm_api_key" in body:
                    # same keep-the-stored-value contract as the
                    # per-backend twin and llm_extra_body above: the key is
                    # masked on GET, so "" is all a client that didn't
                    # retype it can send back — were "" a clear, every
                    # round-trip save of the AI card would silently wipe
                    # the credential. Explicit JSON null clears.
                    if body["llm_api_key"] is None:
                        cfg.pop("llm_api_key", None)
                    else:
                        key = str(body["llm_api_key"]).strip()
                        if key:
                            cfg["llm_api_key"] = crypto.encrypt(conn, key)
            # ---- multi-backend AI (per-task routing) -------------------
            # llm_backends is a full-list REPLACE. An entry that omits
            # api_key keeps its stored (encrypted) key; api_key:"" clears
            # it. The legacy single-endpoint keys fold in as entry id
            # "legacy" the first time the list carries one, inheriting the
            # stored key — nothing is lost crossing over. URLs were all
            # guarded above the lock (_sup_bk).
            if "llm_backends" in body:
                from ..db import crypto as _crypto
                raw_bk = body["llm_backends"] or []
                if not isinstance(raw_bk, list):
                    raise HTTPException(400, "llm_backends must be a list")
                if len(raw_bk) > 12:
                    raise HTTPException(400, "llm_backends: 12 backends max")
                prior_bk = {str(b.get("id")): b
                            for b in (cfg.get("llm_backends") or [])
                            if isinstance(b, dict)}
                _fold_legacy = bool(cfg.get("llm_url")) and "legacy" not in prior_bk
                import uuid as _uuid
                # one tenant-key unwrap for the whole list — per-entry
                # crypto.encrypt would round-trip the DB for every backend
                # while the settings row lock is held
                _enc_key = (_crypto.encryptor(conn)
                            if (_fold_legacy
                                or any(isinstance(e, dict)
                                       and (e.get("api_key")
                                            or e.get("extra_body"))
                                       for e in raw_bk)) else None)
                if _fold_legacy:
                    # the fold-in inherits the stored extra_body alongside
                    # the key — since GET masks both, the client cannot
                    # resend them; omission means "keep". An untagged (plaintext)
                    # value is encrypted as it crosses over, because
                    # from here on nothing ever rewrites it (decrypt passes
                    # untagged values through, so it would keep working —
                    # and keep sitting in the clear in every DB dump).
                    prior_bk["legacy"] = {
                        k: (v if v.startswith(_crypto.PREFIX) else _enc_key(v))
                        for k, v in (("api_key",
                                      str(cfg.get("llm_api_key") or "")),
                                     ("extra_body",
                                      str(cfg.get("llm_extra_body") or "")))
                        if v}
                entries, seen_bk = [], set()
                for e in raw_bk:
                    if not isinstance(e, dict):
                        raise HTTPException(
                            400, "each llm backend must be an object")
                    bid = (str(e.get("id") or "").strip()
                           or _uuid.uuid4().hex[:8])
                    if bid == "bundled" or bid in seen_bk:
                        raise HTTPException(
                            400, "llm_backends: reserved or duplicate id")
                    seen_bk.add(bid)
                    b_url = str(e.get("url") or "").strip()
                    b_model = str(e.get("model") or "").strip()
                    if not b_url or not b_model:
                        raise HTTPException(
                            400, "every llm backend needs a url and a model")
                    ent = {"id": bid,
                           "name": (str(e.get("name") or "").strip()[:40]
                                    or "Backend"),
                           "url": b_url, "model": b_model}
                    vm = str(e.get("vision_model") or "").strip()
                    if vm:
                        ent["vision_model"] = vm
                    # extra_body is arbitrary JSON merged into every request
                    # to the backend — it can carry credentials (an
                    # auth-in-body vendor, an org token), so GET masks it
                    # like api_key and this write path mirrors api_key's
                    # keep-the-stored-value contract. One asymmetry, forced
                    # by the masking: clients round-trip the masked ""
                    # from GET, so "" (or an omitted key) KEEPS the stored
                    # value and an explicit JSON null clears it — were ""
                    # a clear, every save from a client that didn't retype
                    # the JSON would silently wipe it.
                    if "extra_body" in e and e.get("extra_body") is None:
                        pass                     # explicit null → cleared
                    elif str(e.get("extra_body") or "").strip():
                        eb = str(e["extra_body"]).strip()
                        import json as _json
                        # an OBJECT, not just JSON — same reason as the
                        # legacy twin above: it is dict.update'd into each
                        # request body at call time
                        try:
                            _eb = _json.loads(eb)
                        except ValueError:
                            _eb = None
                        if not isinstance(_eb, dict):
                            raise HTTPException(
                                400,
                                "llm backend extra_body must be a JSON object")
                        # encrypted at rest like api_key — it can carry a
                        # credential, and the export scrub drops it for the
                        # same reason (untagged legacy rows pass through
                        # decrypt unchanged)
                        ent["extra_body"] = _enc_key(eb)
                    elif prior_bk.get(bid, {}).get("extra_body"):
                        ent["extra_body"] = prior_bk[bid]["extra_body"]
                    if "api_key" in e:
                        k = str(e.get("api_key") or "").strip()
                        if k:
                            ent["api_key"] = _enc_key(k)
                    elif prior_bk.get(bid, {}).get("api_key"):
                        ent["api_key"] = prior_bk[bid]["api_key"]
                    entries.append(ent)
                if entries:
                    cfg["llm_backends"] = entries
                else:
                    cfg.pop("llm_backends", None)
                # the fold-in retires the legacy keys so one endpoint never
                # lives in two places (the resolver would prefer the routed
                # entry anyway; the leftover would only mislead)
                if any(e["id"] == "legacy" for e in entries):
                    for k in ("llm_url", "llm_model", "llm_api_key",
                              "llm_extra_body", "llm_vision_model"):
                        cfg.pop(k, None)
                # roles pointing at removed entries unroute themselves
                kept = {k: v for k, v in (cfg.get("llm_roles") or {}).items()
                        if v == "bundled" or v in seen_bk}
                if kept:
                    cfg["llm_roles"] = kept
                else:
                    cfg.pop("llm_roles", None)
            if "llm_roles" in body:
                from ..engine.llm_categorize import ROLES as _LLM_ROLES
                raw_r = body["llm_roles"] or {}
                if not isinstance(raw_r, dict):
                    raise HTTPException(400, "llm_roles must be an object")
                ids_now = {str(b.get("id"))
                           for b in (cfg.get("llm_backends") or [])
                           if isinstance(b, dict)}
                bundled_ok = (bool(os.environ.get("OIKONOME_LLM_URL",
                                                  "").strip())
                              and not env_flag("OIKONOME_HOSTED"))
                roles_out = {}
                for k, v in raw_r.items():
                    if k not in _LLM_ROLES:
                        raise HTTPException(400, f"unknown llm role {k!r}")
                    v = str(v or "").strip()
                    if not v:
                        continue                 # explicit unroute
                    if v == "bundled":
                        if not bundled_ok:
                            raise HTTPException(
                                400, "no bundled backend on this instance")
                    elif v not in ids_now:
                        raise HTTPException(
                            400, f"llm_roles[{k}] names an unknown backend")
                    roles_out[k] = v
                if roles_out:
                    cfg["llm_roles"] = roles_out
                else:
                    cfg.pop("llm_roles", None)
            # standalone vision-model save. The AI settings card pairs a
            # tenant-picked vision model with the BUNDLED endpoint and
            # sends {llm_vision_model} alone — were the write path only
            # inside the single-endpoint llm_url block above, a
            # bundled-Ollama install could not set a vision model at all.
            # When any of those keys travels with it, the block above
            # already decided its fate (set with the group, or cleared
            # with it). After the llm_backends block on purpose: an
            # explicit value in this save outlives the legacy fold-in's
            # retirement of the key. Empty string clears.
            if ("llm_vision_model" in body
                    and not any(k in body for k in
                                ("llm_url", "llm_model", "llm_api_key"))):
                vm = str(body["llm_vision_model"] or "").strip()
                if vm:
                    cfg["llm_vision_model"] = vm
                else:
                    cfg.pop("llm_vision_model", None)
            # "yes, this endpoint may receive a document with my SSN on it".
            # The tax-document parser refuses a remote model without this and
            # names the setting in the error it raises, so it has to be
            # settable — an instruction pointing at a knob with no write path
            # is a dead end, not a fix. Both clients send it with the AI
            # card's payload (backends + roles) and never with the
            # single-endpoint llm_url group. Unconditional, unlike the vision
            # model above: consent is about WHERE the document goes, so
            # nothing in that group's set-or-clear decides its fate.
            if "taxdocs_allow_remote_llm" in body:
                if str(body["taxdocs_allow_remote_llm"]).strip().lower() in (
                        "1", "true", "yes", "on"):
                    cfg["taxdocs_allow_remote_llm"] = "1"
                else:
                    cfg.pop("taxdocs_allow_remote_llm", None)
        # OUTSIDE the lock and after it commits: sending mail while
        # holding a row lock would hold it for the length of an SMTP
        # conversation, and an invite must only go out once the save it
        # describes is durable.
        # an address added to the list is INVITED, not enrolled.
        # After the save, never before — mailing someone about a change that
        # then 400s on the next field would be a promise the app didn't keep.
        # The invite is also what starts the mail: until it's accepted,
        # `worker._recipients_raw` skips the address entirely.
        _changed_keys = sorted(k for k in set(_cfg_was) | set(cfg)
                               if _cfg_was.get(k) != cfg.get(k))
        if _changed_keys:
            _activity.record(conn, user, "settings", "changed",
                             detail={"keys": _changed_keys})
        added = mailguard.added_recipients(_prev_recips,
                                           cfg.get("email_recipients"))
        view = _settings_view(cfg)
        if added:
            mailguard.invite_added(user, added)
        view["email_recipient_status"] = _recipient_status(
            user["tenant_id"], cfg.get("email_recipients") or [],
            cfg.get("email_muted") or [])
        return view
    finally:
        conn.close()


@router.post("/settings/recipients/resend",
             dependencies=[Depends(limit("recipient_invite_resend", 10, 3600))])
def settings_recipient_resend(user: dict = Depends(_user()),
                              body: dict = Body(...)):
    """Send the invite again — the answer to "they never got it".

    Rotates the token (migration 074 keeps one row per address, so the old
    link stops working by construction) and re-sends. Refuses an address
    that isn't on the saved list, so this can't be used as a way to mail
    arbitrary people from someone else's instance."""
    demoguard.deny(user)
    from ..engine import budget
    email = str(body.get("email") or "").strip()
    if not email:
        raise HTTPException(400, "email is required")
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
        if email.lower() not in {str(r).strip().lower()
                                 for r in (cfg.get("email_recipients") or [])}:
            raise HTTPException(400, "that address is not a saved recipient")
        # On hosted, being ON the saved list is not enough — a stored config
        # (a restore, or one saved before the instance was hosted) can hold a
        # free-form external address, and this door would then make the platform mail
        # invite
        # links (naming the inviter's account) to an arbitrary stranger the
        # worker's own membership filter would never actually enrol. Same
        # member-only check the save and restore doors already enforce.
        mailguard.check_recipients(conn, [email])
    finally:
        conn.close()
    mailguard.invite_added(user, [email])
    return {"ok": True,
            "email_recipient_status": _recipient_status(
                user["tenant_id"], cfg.get("email_recipients") or [],
                cfg.get("email_muted") or [])}


@router.post("/settings/smtp-test",
             dependencies=[Depends(limit("smtp-test", 5, 3600))])
def settings_smtp_test(user: dict = Depends(_user())):
    """Prove delivery works — a test email to EVERY recipient on the
    list, through the same resolution and fan-out the real senders use.

    Mailing only the signed-in user makes the button actively misleading
    for the thing people use it for: "did adding this person work?" A test
    that never touches the address you just added cannot answer that, and it
    would report success while a second recipient silently received nothing.

    Per-recipient results are returned so a partial failure is visible
    instead of averaging into one green tick.
"""
    demoguard.deny(user)
    from ..jobs.worker import _recipients_and_raw
    from ..engine import budget
    from . import report
    conn = _conn(user)
    try:
        smtp = report.resolve_smtp(conn)
        recipients, raw = _recipients_and_raw(conn, user["tenant_id"])
        configured = list(budget.load_config(conn).get("email_recipients")
                          or [])
    finally:
        conn.close()
    # Who is on the list but will NOT be mailed, and why. `_recipients`
    # filters to accepted invites (and, hosted, to members) — correctly —
    # but the button exists to answer "did adding this person work?", so a
    # green tick that says nothing about them is not an answer.
    mailed = {e.lower() for e in recipients}
    delivered_raw = {e.lower() for e in raw}
    skipped = [
        {"email": e,
         "reason": ("delivery is failing to this address"
                    if e.lower() in delivered_raw
                    else "they have not accepted their invitation yet")}
        for e in configured if e.lower() not in mailed]
    if not smtp["configured"]:
        raise HTTPException(400, "no SMTP configured — fill the form above "
                                 "(or OIKONOME_SMTP_* in docker/.env)")
    if not recipients:
        raise HTTPException(400, "no recipients configured")
    results = report.send_each(
        "Oikonome test email",
        "It works — this instance can send email.",
        "<p>It works — this instance can send email.</p>",
        recipients, smtp=smtp)
    ok = [r["email"] for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    if failed and not ok:
        _via = ("the host relay" if env_flag("OIKONOME_HOSTED")
                else smtp["host"])
        raise HTTPException(
            400, f"send failed via {_via}: {failed[0]['error']}")
    return {"ok": not failed, "sent_to": ok, "skipped": skipped,
            "failed": [{"email": r["email"], "error": r["error"]}
                       for r in failed],
            # Hosted withholds the operator relay's hostname —
            # the same rule _settings_view already applies
            "via": ("host-managed" if env_flag("OIKONOME_HOSTED")
                    else smtp["host"])}


@router.get("/budgets/suggest")
def budgets_suggest(user: dict = Depends(_user())):
    """'Suggest from history': trailing 6-full-month median per
    bucket (current month excluded, months >2× the median excluded), for
    food/other AND every custom bucket. Read-only — the user approves each
    number in the Settings UI before anything is saved."""
    from ..engine import budget
    conn = _conn(user)
    try:
        return budget.suggest_budgets(conn, _today(conn))
    finally:
        conn.close()


# ---- retirement (SPA Retirement page) ---------------------------------------

def _blank_is(default):
    """Query coercion for a numeric what-if field the reader CLEARED.

    The adjust panels render these as text boxes where empty means "use
    the default" — the cash-yield box says so in its own placeholder. But
    an emptied box still rides the query string as `x=`, and an empty
    string is not a number, so without this one cleared field would 422
    the whole projection instead of reverting one assumption. The clients
    drop their own blanks before they build the query, but a bookmarked
    link or an older client can still carry one, so the server reads blank
    the way the UI labels it.
    Anything else still validates exactly as before: "abc" is a broken
    client, not an answer, and stays a 422."""
    def coerce(v):
        return default if isinstance(v, str) and not v.strip() else v
    return BeforeValidator(coerce)


def _rate(v: float | None, default: float | None) -> float | None:
    """A what-if percentage, bounded to the range the page's controls offer.

    NaN reaches here — "nan" parses as a float — and then survives a
    min/max clamp, because `max(lo, min(nan, hi))` is `lo`: a NaN yield
    would quietly become the WORST assumption in the range and come back
    in `inputs` as though the reader had chosen it. No what-if can mean
    NaN, so it reads as the default. ±inf is ordinary out-of-range input
    and clamps to an end of the range like any other huge number."""
    import math
    if v is None or math.isnan(v):
        return default
    return max(-20.0, min(v, 30.0))


# A cache MISS here is ~10k simulations (one age→end_age walk per retire-age
# row, plus the bisection and the 1928-2025 historical replay), the handler
# is sync so it occupies a shared threadpool worker for all of it, and the
# projection cache is process-wide. So the request budget is small on
# purpose: the page fetches once per load and once per Recalculate, which
# 30/min is far past, while a script walking a parameter to miss the cache
# every time is stopped before it can hold the pool.
@router.get("/retirement",
            dependencies=[Depends(limit("retirement", 30, 60))])
def retirement_api(user: dict = Depends(_user()),
                   age: Annotated[int, _blank_is(0)] = 0,
                   spend: Annotated[int, _blank_is(0)] = 0,
                   ret: Annotated[float, _blank_is(7.0)] = 7.0,
                   infl: Annotated[float, _blank_is(3.0)] = 3.0,
                   end: Annotated[int, _blank_is(95)] = 95,
                   ssage: Annotated[int, _blank_is(67)] = 67,
                   employer_mo: Annotated[int, _blank_is(0)] = 0,
                   taxable_mo: Annotated[int, _blank_is(0)] = 0,
                   resume: Annotated[int, _blank_is(0)] = 0,
                   stockpct: Annotated[int, _blank_is(90)] = 90,
                   cash: Annotated[float | None, _blank_is(None)] = None):
    """Thin wrapper over engine/retirement.project, as JSON. All query
    params are what-if overrides; age defaults
    from config `birthdate` and spend from the household's own last-12-months
    ledger (both real, self-updating). `cash` is the cash bucket's nominal
    yield in %; unset, cash keeps pace with inflation."""
    from ..engine import budget, retirement as retire
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
        if not spend:
            spend = retire.default_spend(conn)
        bd = cfg.get("birthdate")
        birth_year = None
        try:
            birth_year = dt.date.fromisoformat(bd).year if bd else None
        except (TypeError, ValueError):
            pass                                # corrupt config must not 500
        if not age:
            try:
                age = retire.age_from_birthdate(bd) if bd else 45
            except (TypeError, ValueError):
                age = 45
        # clamp adversarial/typo'd query params — bad input must not 500
        age = max(18, min(age, 90))
        end = max(age + 1, min(end, 110))
        ssage = max(62, min(ssage, 70))
        infl = _rate(infl, 3.0)
        ret = _rate(ret, 7.0)
        cash = _rate(cash, None)
        stockpct = max(0, min(stockpct, 100))
        # `resume` is an age like the others and is bounded the same way.
        # It is part of the projection's memo key, so an unclamped value is
        # also an unbounded set of cache entries an anonymous query string
        # picks — the same "size decided by the client" shape the clamps
        # above close.
        resume = max(0, min(resume, 110))
        # These three are floored AND capped: the block
        # above exists precisely because bad input must not 500. A monthly
        # contribution of 1e308 overflows to inf inside the compounding loop
        # and the projection comes back as a wall of NaN that json.dumps
        # refuses to serialize. $10M/month and $100M/year of spend are past
        # any real plan and far short of the float ceiling.
        spend = max(0, min(spend, 100_000_000))
        employer_mo = max(0, min(employer_mo, 10_000_000))
        taxable_mo = max(0, min(taxable_mo, 10_000_000))
        # Snap the continuous inputs to the grid the page's own controls
        # step on ($1k spend, $50/mo contributions, 0.1% rates). The
        # projection is memoized on its exact parameter tuple, so an
        # unquantized dollar would be a distinct cache key: spend=1,2,3,… would
        # miss the cache on every request and each miss would run the full
        # simulation in a shared threadpool worker. A projection does not change
        # meaningfully between $38,400 and $38,000 of spend — it does
        # change between a cached answer and 10k simulations — and
        # `default_spend` already rounds the page's starting figure to the
        # same $1k. `inputs` echoes what was actually used, so the client
        # always shows the numbers the answer was computed from.
        spend = int(round(spend, -3)) or (1000 if spend else 0)
        employer_mo = int(round(employer_mo / 50.0) * 50)
        taxable_mo = int(round(taxable_mo / 50.0) * 50)
        ret = round(ret, 1)
        infl = round(infl, 1)
        if cash is not None:
            cash = round(cash, 1)
        # A restored/hand-edited config can carry a non-integer ss_estimates
        # key (the Settings-save path validates it as an int in [62,70], but
        # the restore config-merge does not) — a bad key must be dropped, not
        # 500 the whole Retirement page, same as the birthdate guard above.
        sched = {}
        for k, v in (cfg.get("ss_estimates") or {}).items():
            try:
                sched[int(k)] = v
            except (ValueError, TypeError):
                continue
        your_ss = sched.get(ssage, 0)           # from the SSA statement
        resume_age = resume or age              # default: resume saving now
        r = retire.project(conn, age=age, end_age=end, spend=spend,
                           nominal=ret / 100, inflation=infl / 100,
                           ss_monthly=your_ss, ss_age=ssage,
                           rental_monthly=0, td_annual=employer_mo * 12,
                           taxable_annual=taxable_mo * 12,
                           resume_age=resume_age, stock_frac=stockpct / 100,
                           cash_nominal=(cash / 100 if cash is not None
                                         else None),
                           birth_year=birth_year)
    finally:
        conn.close()
    r["inputs"] = dict(age=age, spend=spend, ret=ret, infl=infl, end=end,
                       ssage=ssage, employer_mo=employer_mo,
                       taxable_mo=taxable_mo, resume=resume_age,
                       stockpct=stockpct, cash=cash,
                       saving_yr=(employer_mo + taxable_mo) * 12,
                       your_ss=your_ss, has_sched=bool(sched))
    return r


# ---- debt payoff planner (SPA Debt page) -----------------------------------
# The walk is a few hundred months over a handful of debts, three times per
# request (chosen method, the other method, minimums-only), so the budget is
# generous next to the retirement projection's; the limit is there so a
# script cannot make the pool do it in a loop.

@router.get("/debt-plan", dependencies=[Depends(limit("debt", 120, 60))])
def debt_plan_api(user: dict = Depends(_user()),
                  method: Annotated[str | None, _blank_is(None)] = None,
                  extra: Annotated[float | None, _blank_is(None)] = None):
    """The payoff plan for every card and loan carrying a balance.
    `method` and `extra` are what-if overrides of the saved plan and are
    not written; the reply says which plan is saved."""
    from ..engine import debt as _debt
    conn = _conn(user)
    try:
        return _debt.build(conn, _today(conn), method=method, extra=extra)
    finally:
        conn.close()


@router.post("/debt-plan")
def debt_plan_save_api(user: dict = Depends(_user()),
                       body: dict = Body(...)):
    """Save the plan — the method, the extra monthly payment and any
    per-debt rate / minimum overrides — and return the plan it produces.
    Bookkeeping like a budget, so a member may, and the demo may."""
    _may_edit(user)
    from ..engine import budget, debt as _debt
    try:
        plan = _debt.normalize_plan(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    conn = _conn(user)
    try:
        with budget.config_txn(conn) as cfg:
            changed = cfg.get(_debt.PLAN_KEY) != plan
            cfg[_debt.PLAN_KEY] = plan
        if changed:
            _activity.record(conn, user, "settings", "debt_plan",
                             detail={"keys": [_debt.PLAN_KEY]})
        return _debt.build(conn, _today(conn))
    finally:
        conn.close()


# ---- the continuity packet: the household's money, on paper, for whoever
# is left to run it. The PDF itself leaves through pages.py's ticket door;
# these are its two fields and the emailed copy.

_CONTINUITY_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/continuity")
def continuity_get(user: dict = Depends(_user())):
    """The owner's two fields (who it is for, where the rest is) and what
    the email door will accept, so the clients can offer the right thing:
    on hosted, only a household member's address; on self-host, any."""
    from ..engine import budget, continuity
    from .report import resolve_smtp
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
        out = continuity.settings_of(cfg)
        out["mail_configured"] = bool(resolve_smtp(conn)["configured"])
        out["members_only"] = mailguard.members_only()
        out["members"] = [r["email"] for r in conn.execute(
            "SELECT email FROM users WHERE tenant_id = "
            "current_setting('app.tenant_id')::uuid ORDER BY created_at"
        ).fetchall()]
        out["note_max"] = continuity.NOTE_MAX
        return out
    finally:
        conn.close()


@router.put("/continuity")
def continuity_put(user: dict = Depends(_user()), body: dict = Body(...)):
    """Save the note and the name. Owner only: the note says where the
    will and the passwords are, which is the owner's to write."""
    _owner_only(user)
    demoguard.deny(user)
    from ..engine import budget, continuity
    try:
        note = (continuity.clean_note(body.get("note"))
                if "note" in body else None)
        who = (continuity.clean_for(body.get("prepared_for"))
               if "prepared_for" in body else None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    conn = _conn(user)
    try:
        changed = []
        with budget.config_txn(conn) as cfg:
            for key, val in ((continuity.KEY_NOTE, note),
                             (continuity.KEY_FOR, who)):
                if val is None:
                    continue
                if (cfg.get(key) or "") != val:
                    changed.append(key)
                if val:
                    cfg[key] = val
                else:
                    cfg.pop(key, None)
            out = continuity.settings_of(cfg)
        if changed:
            _activity.record(conn, user, "settings", "changed",
                             detail={"keys": changed})
        return {"ok": True, **out}
    finally:
        conn.close()


@router.post("/continuity/email",
             dependencies=[Depends(limit("continuity_email", 6, 3600))])
def continuity_email(user: dict = Depends(_user()), body: dict = Body(...)):
    """Mail the packet to one address. The same fresh elevation the
    download demands — this is the whole shape of the household's money
    leaving the instance, and a stolen cookie must not be able to send it
    anywhere. The recipient is held to the household mail rule: hosted
    mails balances only to an address with an account here, self-host to
    any address the operator's relay will carry."""
    _owner_only(user)
    demoguard.deny(user)
    from .app import _require_elevation
    from .pages import _continuity_pdf
    from .report import resolve_smtp
    from .report import send as _send
    _require_elevation(user, fresh_seconds=60,
                       password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    to = mailguard.parse_recipients([str(body.get("to") or "")], cap=1)
    if not to or not _CONTINUITY_ADDRESS.match(to[0]) or len(to[0]) > 254:
        raise HTTPException(400, "a valid email address is required")
    conn = _conn(user)
    try:
        mailguard.check_recipients(conn, to)
        smtp = resolve_smtp(conn)
        if not smtp["configured"]:
            raise HTTPException(400, "email is not configured on this "
                                     "instance — download the packet instead")
        pdf, name = _continuity_pdf(user)
        who = to[0]
        plain = ("Attached is the continuity packet for the household in "
                 "Oikonome, printed today: every account and where it is "
                 "held, the balances that day, the recurring bills and what "
                 "pays them, the income, and the businesses. It holds no "
                 "passwords — its first section says where those are "
                 f"kept.\n\nSent by {user['email']} from Oikonome.")
        html = ("<p>Attached is the continuity packet for the household in "
                "Oikonome, printed today: every account and where it is "
                "held, the balances that day, the recurring bills and what "
                "pays them, the income, and the businesses. It holds no "
                "passwords — its first section says where those are "
                f"kept.</p><p>Sent by {user['email']} from Oikonome.</p>")
        try:
            _send("Continuity packet from Oikonome", plain, html, [who],
                  attachments=[(name, pdf, "application/pdf")],
                  smtp=smtp, bcc=False)
        except Exception as e:                             # noqa: BLE001
            raise HTTPException(502, "the mail relay refused it: "
                                     f"{type(e).__name__}")
        _activity.record(conn, user, "settings", "continuity_emailed",
                         label=who)
        return {"ok": True, "to": who}
    finally:
        conn.close()


@router.get("/activity", dependencies=[Depends(limit("activity", 120, 60))])
def activity_api(user: dict = Depends(_user()),
                 limit_: int = Query(50, alias="limit", ge=1, le=200),
                 before: str = "", kind: str = "", who: str = "",
                 target: str = ""):
    """The household's activity log, newest first: who changed which
    category, bill, note, rule… and when. Everyone in the household reads
    it (everyone sees everything); `before` pages by the last row's
    timestamp, `kind` / `who` / `target` filter."""
    if kind and kind not in _activity.KINDS:
        raise HTTPException(400, "unknown kind")
    conn = _conn(user)
    try:
        return _activity.list_activity(
            conn, limit=limit_, before=before or None, kind=kind or None,
            actor=who or None, target=target or None)
    finally:
        conn.close()


@router.post("/alerts/dismiss")
def alerts_dismiss_api(user: dict = Depends(_user()), body: dict = Body(...)):
    from ..engine import alerts as _alerts
    conn = _conn(user)
    try:
        n = _alerts.dismiss(conn, body.get("kind") or "",
                            body.get("message") or "")
        return {"ok": True, "dismissed": n}
    finally:
        conn.close()


@router.post("/alerts/restore")
def alerts_restore_api(user: dict = Depends(_user()), body: dict = Body(...)):
    from ..engine import alerts as _alerts
    conn = _conn(user)
    try:
        n = _alerts.restore(conn, body.get("kind") or "",
                            body.get("message") or "")
        return {"ok": True, "restored": n}
    finally:
        conn.close()


@router.get("/alerts/history")
def alerts_history_api(user: dict = Depends(_user())):
    """The persistent alert log (active first, then newest) — the SPA
    Alerts page. Rows: kind, severity, message, first_seen, last_seen,
    active, dismissed."""
    from ..engine import alerts as _alerts
    conn = _conn(user)
    try:
        return {"alerts": [_ser(r) for r in _alerts.history(conn)]}
    finally:
        conn.close()


@router.get("/calendar")
def calendar_api(user: dict = Depends(_user()), days: int = 35):
    """Dated money events for the next N days — bills out, paychecks in,
    card payments (full-balance scenario only; the statement scenario would
    double-count the same debt) — grouped per day for a calendar strip.
    Same engine pass the cash forecast uses; nothing recomputed."""
    from ..engine import forecast
    days = max(7, min(90, days))
    conn = _conn(user)
    try:
        # the household's day, not the instance clock: on the hosted (UTC)
        # box an evening request would otherwise label a bill due today
        # against tomorrow's date on this one screen
        today = _today(conn)
        fc = forecast.build(conn, today=today, days=days)
    finally:
        conn.close()
    by_day: dict[str, list[dict]] = {}
    for kind, evs in (("bill", fc["events"]), ("card", fc["card_events"])):
        for d, amount, label in evs:
            by_day.setdefault(d, []).append(
                {"label": label, "amount": amount,
                 "kind": "income" if amount > 0 else kind})
    return {"days": days,
            "start": today.isoformat(),
            "calendar": [{"date": d,
                          "total": round(sum(e["amount"] for e in evs), 2),
                          "events": sorted(evs, key=lambda e: e["amount"])}
                         for d, evs in sorted(by_day.items())]}


# ---- receipts ------------------------------------------------------

def _parse_receipt_async(tenant_id: str, receipt_id: str) -> None:
    """The vision parse takes 30s+ on CPU — never inside the
    upload/parse request. The receipt is stamped 'parsing' before the
    thread starts; the UI polls status for its progress bar."""
    import logging
    import threading

    def _run() -> None:
        conn = None
        try:
            from ..engine import receipts
            conn = tenancy.tenant_connect(tenant_id)
            # claimed=True: the route claim()ed before spawning us
            receipts.parse_one(conn, receipt_id, claimed=True)
        except Exception as e:                    # noqa: BLE001
            # class only at WARNING; the text can carry receipt contents
            # or a backend URL, so it stays out of the feedback log ring
            rlog = logging.getLogger("oikonome.receipts")
            rlog.warning("background receipt parse failed tenant=%s: %s",
                         tenant_id, type(e).__name__)
            rlog.debug("background receipt parse failure detail: %s", e,
                       extra={"no_capture": True})
        finally:
            if conn is not None:
                conn.close()

    threading.Thread(target=_run, daemon=True).start()


@router.post("/transactions/{txn_id}/receipt",
             dependencies=[Depends(limit("receipt", 60, 3600))])
async def receipt_upload(txn_id: str, user: dict = Depends(_user()),
                         file: UploadFile = File(...),
                         kind: str = Form("receipt")):
    """Attach a receipt (kind='receipt') or a check image (kind='check');
    kicks the vision parse in the background when an LLM is configured
    (failure leaves 'failed' with a reason — the nightly sweep and the
    retry button exist). A parsed check also feeds its payee to the
    categorizer."""
    demoguard.deny(user)
    from ..engine import llm_categorize, receipts
    from .pages import read_capped
    # Stream with a hard ceiling — an unbounded read() would let an
    # authenticated user OOM the (shared, on hosted) host with a multi-GB
    # upload. read_capped stops buffering the moment it blows the cap.
    # The cap is the RECEIPT limit — buffering 50 MB only for
    # receipts.add to refuse at 5 MB would be pointless exposure.
    data = await read_capped(file, cap=receipts.MAX_IMAGE)
    if data is None:
        raise HTTPException(413, "receipt too large (5 MB max)")
    mime = file.content_type or "application/octet-stream"
    conn = _conn(user)
    try:
        try:
            # add runs the image-optimize (Pillow) pass; keep
            # that CPU off the async event loop so a large upload can't stall
            # the shared worker (pixel size is separately capped in receipts).
            rid = await run_in_threadpool(receipts.add, conn, txn_id, data,
                                          mime, kind)
        except ValueError as e:
            raise HTTPException(400, str(e))
        _activity.record(conn, user, "receipt", "attached", target=txn_id,
                         label=_activity.txn_label(conn, txn_id),
                         detail={"receipt_kind": kind, "receipt_id": rid})
        if llm_categorize.configured(conn, role="vision") and receipts.claim(conn, rid):
            # claim before responding so the first poll already shows
            # 'parsing' — and so a racing retry can't double-parse
            _parse_receipt_async(str(user["tenant_id"]), rid)
        return {"id": rid,
                "receipts": receipts.for_txn(conn, txn_id)}
    finally:
        conn.close()


@router.get("/transactions/{txn_id}/receipts")
def receipt_list(txn_id: str, user: dict = Depends(_user())):
    from ..engine import receipts
    conn = _conn(user)
    try:
        return {"receipts": [_ser(r) for r in receipts.for_txn(conn, txn_id)]}
    finally:
        conn.close()


@router.get("/receipts/{rid}/image")
def receipt_image(rid: str, request: Request, user: dict = Depends(_user()),
                  variant: str = "original"):
    _same_site_download(request)
    from fastapi.responses import Response as _Resp

    from ..engine import receipts
    conn = _conn(user)
    try:
        r = receipts.image(conn, rid, variant=variant)
    finally:
        conn.close()
    if r is None:
        raise HTTPException(404, "no such receipt")
    # The stored mime is data, not policy: restore paths and old uploads
    # could carry anything. Real image types render inline (the receipt
    # panel opens them in a tab); everything else — PDFs included, whose
    # viewers run script — is a named download. Sniffing off either way,
    # so an attacker-shaped "image" can never render as origin-hosted HTML.
    mime = str(r["mime"] or "")
    if mime in receipts.IMAGE_MIMES:
        disp = "inline"
    else:
        ext = "pdf" if mime == "application/pdf" else "bin"
        mime = mime if mime == "application/pdf" else "application/octet-stream"
        disp = f'attachment; filename="receipt-{str(rid)[:8]}.{ext}"'
    return _Resp(bytes(r["image"]), media_type=mime,
                 headers={"Content-Disposition": disp,
                          "X-Content-Type-Options": "nosniff"})


@router.delete("/receipts/{rid}")
def receipt_delete(rid: str, user: dict = Depends(_user())):
    from ..engine import receipts
    conn = _conn(user)
    try:
        # the receipt's row before it goes: the log names the charge
        was = None
        try:
            was = conn.execute("SELECT txn_id, kind FROM receipts "
                               "WHERE id = %s::uuid", (rid,)).fetchone()
        except Exception:                                  # noqa: BLE001
            was = None  # a malformed id: delete below answers 404
        if not receipts.delete(conn, rid):
            raise HTTPException(404, "no such receipt")
        if was:
            _activity.record(conn, user, "receipt", "removed",
                             target=was["txn_id"],
                             label=_activity.txn_label(conn, was["txn_id"]),
                             detail={"receipt_kind": was["kind"],
                                     "receipt_id": rid})
        return {"ok": True}
    finally:
        conn.close()


@router.post("/receipts/{rid}/parse",
             dependencies=[Depends(limit("receipt_parse", 60, 3600))])
def receipt_parse(rid: str, user: dict = Depends(_user())):
    """Kick a (re)parse in the background; the UI polls receipt status.
    Rate-limited like upload: each call spawns a decode+vision thread."""
    demoguard.deny(user)
    from ..engine import llm_categorize, receipts
    conn = _conn(user)
    try:
        if receipts.image(conn, rid) is None:
            raise HTTPException(404, "no such receipt")
        if not llm_categorize.configured(conn, role="vision"):
            raise HTTPException(
                400, "no LLM configured — Settings → AI")
        if not receipts.claim(conn, rid):
            # A parse is already in flight — don't start a second
            # thread to fight it over the line items; the poll loop the
            # caller is about to enter will see the live one's result
            return {"status": "parsing"}
    finally:
        conn.close()
    _parse_receipt_async(str(user["tenant_id"]), rid)
    return {"status": "parsing"}


@router.get("/receipts/items")
def receipt_items_api(user: dict = Depends(_user()), q: str = "",
                      group: str = "item"):
    """Search + group line items across every parsed receipt."""
    from ..engine import receipts
    conn = _conn(user)
    try:
        out = receipts.search_items(conn, q=q, group=group)
    finally:
        conn.close()
    return {"rows": [_ser(r) for r in out["rows"]],
            "groups": [_ser(g) for g in out["groups"]],
            "truncated": out["truncated"]}


@router.post("/receipts/{rid}/items/{line}/tag")
def receipt_tag(rid: str, line: int, user: dict = Depends(_user()),
                body: dict = Body(...)):
    from ..engine import receipts
    conn = _conn(user)
    try:
        if not receipts.set_tag(conn, rid, line,
                                str(body.get("tag") or "")):
            raise HTTPException(404, "no such line item")
        return {"ok": True}
    finally:
        conn.close()


@router.get("/receipts/report")
def receipt_report(request: Request, user: dict = Depends(_user()),
                   tag: str = "business",
                   y: int = 0, m: int = 0, format: str = "json"):
    _same_site_download(request)
    import csv as _csv
    import io as _io

    from ..engine import receipts
    conn = _conn(user)
    try:
        today = _today(conn)
        y, m = y or today.year, m or today.month
        _ck_ym(y, m)
        rep = receipts.report(conn, tag, y, m)
    finally:
        conn.close()
    if format == "csv":
        from fastapi.responses import Response as _Resp
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["date", "payee", "description", "qty", "amount", "tag",
                    "txn_id", "receipt_id"])
        for r in rep["rows"]:
            w.writerow([r["date"], _csv_safe(r["payee"]),
                        _csv_safe(r["description"]), r["qty"], r["amount"],
                        _csv_safe(r["tag"]), r["txn_id"], r["receipt_id"]])
        w.writerow([])
        w.writerow(["total", "", "", "", rep["total"]])
        return _Resp(buf.getvalue(), media_type="text/csv", headers={
            "Content-Disposition":
            f'attachment; filename="expenses-{_filename_safe(tag, "all")}-{y}-{m:02d}.csv"'})
    rep["rows"] = [_ser(r) for r in rep["rows"]]
    return rep


# ---- script tokens -------------------------------------------------
# Bearer credentials for host-side collector scripts: minted once, shown
# once, revocable; the auth layer restricts them to the import doors.

def _owner_only(user: dict) -> None:
    if user.get("role") != "owner" or user.get("script_token"):
        raise HTTPException(403, "owner only")


def _may_edit(user: dict) -> None:
    """A human in the household who may change its data — owner or member.

    A collector token pushes rows through the import endpoints and has no
    business renaming merchants or rewriting a year of categories, so it is
    refused; members pass, so a household can have more than one person
    doing the books.
    """
    if user.get("script_token"):
        raise HTTPException(
            403, "script tokens are limited to the import push endpoints")
    if user.get("role") not in ("owner", "member"):
        raise HTTPException(
            403, permissions.denial(str(user.get("role") or ""), ""))


@router.get("/tokens")
def tokens_list_api(user: dict = Depends(_user())):
    from ..auth import api_tokens
    from .app import _control_conn
    _owner_only(user)
    with _control_conn() as conn:
        rows = api_tokens.list_for_tenant(conn, user["tenant_id"])
    return {"tokens": [_ser(r) for r in rows]}


@router.post("/tokens", dependencies=[Depends(limit("tokens", 10, 3600))])
def tokens_mint_api(user: dict = Depends(_user()), body: dict = Body(...)):
    demoguard.deny(user)
    from ..auth import api_tokens
    from .app import _control_conn, _require_elevation
    _owner_only(user)
    # A script token is a long-lived
    # push credential that a password change/reset does NOT revoke — a
    # stolen session could mint one for persistence. Password step-up so a
    # hijacked cookie alone can't; rotation revokes existing tokens (see
    # reset.consume + password_change). recovery_code is forwarded, or a
    # passkey-only account could not mint at all.
    # TOTP accounts also owe a live code, exactly like the support-access
    # grant: a durable credential minted on password alone would let a
    # session + password thief sidestep the second factor entirely — the
    # token outlives the session and no password rotation touches it.
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    # push (the default, and what every older client sends) = the import
    # doors; read = the integrations doors. Never both.
    scope = str(body.get("scope") or "push")
    if scope not in api_tokens.SCOPES:
        raise HTTPException(400, "scope must be push or read")
    with _control_conn() as conn:
        token, row = api_tokens.mint(conn, user["tenant_id"],
                                     user.get("user_id"),
                                     str(body.get("name") or ""), scope)
    return {"token": token, **_ser(row)}


@router.post("/tokens/revoke",
             dependencies=[Depends(limit("tokens_revoke", 10, 3600))])
def tokens_revoke_api(user: dict = Depends(_user()), body: dict = Body(...)):
    demoguard.deny(user)
    from ..auth import api_tokens
    from .app import _control_conn, _require_elevation
    _owner_only(user)
    # Mint steps up; revoke gets the same — a stolen cookie shouldn't be
    # able to silently cut every collector off, and the symmetric door means
    # one rule to reason about, not two. The recovery code is forwarded for
    # passkey-only accounts, same as mint.
    # and the same live TOTP code the mint door demands — symmetric doors,
    # one rule; a hijacked cookie + password must not silence every
    # collector without the second factor.
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    try:
        token_id = str(uuid.UUID(str(body.get("id") or "")))
    except ValueError:
        raise HTTPException(404, "no such token")
    with _control_conn() as conn:
        ok = api_tokens.revoke(conn, user["tenant_id"], token_id)
    if not ok:
        raise HTTPException(404, "no such token")
    return {"ok": True}


# ---- import hub (SPA Import page) -------------------------------------------

def _categorize_after_import(tenant_id: str) -> None:
    """File imports land rows with no category, and the sync-time
    categorize pass only runs after aggregator syncs — so a
    files-only first run would reach the wizard's budgets step with everything
    uncategorized (no food/other split to suggest from). Run the same
    best-effort pass here, in a thread so the import response doesn't wait
    on the LLM; a categorize failure must never fail the import."""
    import logging
    import threading

    def _run() -> None:
        conn = None
        try:
            from ..engine import llm_categorize
            conn = tenancy.tenant_connect(tenant_id)
            llm_categorize.categorize_new(conn)
        except Exception as e:                              # noqa: BLE001
            logging.getLogger("oikonome.import").warning(
                "post-import categorize failed tenant=%s: %s", tenant_id, e)
        finally:
            if conn is not None:
                conn.close()

    threading.Thread(target=_run, daemon=True).start()


# Rate limits on the upload doors, bounding the PARSE and the work after it.
# They do not bound the ingest, and must not be read as if they did: FastAPI
# solves a route's dependencies only after it has called `request.form()`, so
# by the time this limiter answers, Starlette has already received, spooled and
# parsed the whole multipart. The bytes are bounded a layer out, before any of
# that runs — security.BodySizeLimitMiddleware caps the body, the per-IP ingest
# window and the in-flight cap beside it.
#
# This one is the most expensive of the set (up to 200 files and the whole
# tenant staging budget in a single request), so it gets the tightest budget —
# still far above any real folder import, where a person analyzes a batch,
# edits the plan and runs it.
@router.post("/import/bulk/analyze",
             dependencies=[Depends(limit("import_analyze", 20, 3600))])
async def import_bulk_analyze(user: dict = Depends(_user()),
                              files: list[UploadFile] = File(...)):
    """Stage a whole folder of files and return the per-file
    plan (type, destination recommendation, sign guess) for review.
    Nothing imports here."""
    demoguard.deny(user)
    from .pages import MAX_UPLOAD, read_capped

    from . import bulk_import
    if len(files) > bulk_import.MAX_FILES:
        raise HTTPException(400, f"too many files (max "
                                 f"{bulk_import.MAX_FILES})")
    from ..db.staging import TENANT_BUDGET
    staged: list[tuple[str, bytes]] = []
    total = 0
    for f in files:
        # read_capped streams and bails past MAX_UPLOAD instead of buffering
        # a multi-GB file into RAM first
        data = await read_capped(f)
        total += len(data or b"")
        # The per-FILE cap alone would let 200 × 50 MB sit in this worker's
        # RAM — refuse the batch outright once the AGGREGATE blows the same
        # budget the staging table enforces at rest
        if total > TENANT_BUDGET:
            raise HTTPException(
                413, f"these files total more than "
                     f"{TENANT_BUDGET // (1024*1024)} MB — import in "
                     f"smaller batches")
        staged.append((f.filename or "?", data if data is not None else b""))

    # the parse (sniff + decode + stage) is CPU- and DB-bound: it must not
    # run on the event loop, where a big batch would stall every other
    # request in the process
    def _analyze():
        conn = _conn(user)
        try:
            try:
                plan = bulk_import.analyze(conn, staged)
            except ValueError as e:
                raise HTTPException(400, str(e))
            # oversized files were blanked above — mark them skipped AND
            # flag them so the UI can keep the import checkbox disabled;
            # size 0 would mislead, so report the cap instead
            cap_mb = MAX_UPLOAD // (1024 * 1024)
            for entry, (name, data) in zip(plan["files"], staged):
                if not data and entry.get("kind") != "unsupported":
                    entry.update(action="skip", oversized=True,
                                 note=f"file too large (over {cap_mb} MB)")
            return plan
        finally:
            conn.close()

    return await run_in_threadpool(_analyze)


@router.post("/import/bulk/run")
def import_bulk_run(user: dict = Depends(_user()), body: dict = Body(...)):
    demoguard.deny(user)
    from . import bulk_import
    from ..sync import heartbeat
    # interactive imports must not auto-watch collector heartbeats
    _watch = heartbeat.AUTO_WATCH.set(bool(user.get("script_token")))
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    conn = _conn(user)
    try:
        try:
            results = bulk_import.run(conn, str(body.get("token") or ""),
                                      list(body.get("files") or []),
                                      role=str(user.get("role") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        _categorize_after_import(str(user["tenant_id"]))
        return {"results": results}
    finally:
        heartbeat.AUTO_WATCH.reset(_watch)
        heartbeat.PUSH_VIA.reset(_via)
        conn.close()


@router.get("/restore/progress")
def restore_progress(user: dict = Depends(_user())):
    """Where the background restore has got to. Polled by the Import page
    from the moment the upload returns until the job settles."""
    from ..sync import restore_job
    conn = _conn(user)
    try:
        return restore_job.status(conn)
    finally:
        conn.close()


# The hub takes one 50 MB file per call. Collector tokens push through it
# hourly and already answer to the central script-token budget, so this
# matches that number rather than undercutting it.
@router.post("/import",
             dependencies=[Depends(limit("import_upload", 60, 3600))])
async def import_api(user: dict = Depends(_user()),
                     account_id: str = Form(""),
                     amount_sign: str = Form("bank"),
                     new_account_name: str = Form(""),
                     file: UploadFile = File(...)):
    demoguard.deny(user)
    from ..sync import heartbeat
    from .pages import OVERSIZE_MSG, dispatch_import, read_capped
    filename = file.filename or ""
    # the same fork pages.import_submit makes: a whole-ledger archive is
    # streamed to disk under the restore cap, everything else is one
    # statement file read under the ordinary one
    if filename.lower().endswith(".zip") and not user.get("script_token"):
        # the role check BEFORE the read: a member may import but not
        # restore, and must not get to spool 500 MB onto a door that then
        # refuses them
        if not permissions.may_restore(user["role"]):
            raise HTTPException(403, permissions.RESTORE_DENIED)
        from .pages import RESTORE_OVERSIZE_MSG, read_restore_capped
        data = await read_restore_capped(file)
        if data is None:
            return {"result": {"error": RESTORE_OVERSIZE_MSG}, "mapping_needed": None}
    else:
        data = await read_capped(file)
        if data is None:
            return {"result": {"error": OVERSIZE_MSG}, "mapping_needed": None}

    # A ZIP here is a whole-ledger restore, and that is the one import that
    # routinely outlives the edge timeout in front of a hosted instance —
    # so it starts a background job and the client polls /restore/progress
    # instead of holding the POST open (see sync/restore_job). Script-token
    # pushes are NOT sent down this path: dispatch_import owes them a 403,
    # and starting a job for a principal that may not restore would answer
    # the wrong question first.
    if filename.lower().endswith(".zip") and not user.get("script_token"):
        # authorization is unchanged by going async: viewers are already
        # refused every write by the gate in app.py, and this endpoint
        # never asked for more than that. A MEMBER, though, passes that
        # gate — importing is theirs — so the restore half is refused
        # here, where the filename is what separates the two.
        if not permissions.may_restore(user["role"]):
            raise HTTPException(403, permissions.RESTORE_DENIED)
        from ..sync import restore_job
        return {"result": restore_job.start(str(user["tenant_id"]), data),
                "mapping_needed": None}

    # parse + upsert are CPU- and DB-bound (a 50 MB OFX/ZIP takes seconds
    # to minutes): off the event loop, or every other request in the
    # process waits behind this one. The heartbeat contextvars are set
    # inside the worker so they scope to exactly this import.
    def _run():
        _watch = heartbeat.AUTO_WATCH.set(bool(user.get("script_token")))
        _via = heartbeat.PUSH_VIA.set(
            "token" if user.get("script_token") else "app")
        conn = _conn(user)
        try:
            dest = account_id
            # the single-file page can create the destination account
            # inline, like bulk already could — same slug-collision-safe
            # helper, so two different "Chase" accounts never merge
            if not dest and new_account_name.strip():
                from .bulk_import import _ensure_account
                # the sign convention also types the account (card /
                # investment), exactly as the bulk path does
                dest = _ensure_account(conn, new_account_name.strip(),
                                       amount_sign)
            result, mapping_needed = dispatch_import(
                conn, dest, amount_sign, filename, data,
                role=str(user.get("role") or ""))
            if result and not mapping_needed and not (
                    isinstance(result, dict) and result.get("error")):
                _categorize_after_import(str(user["tenant_id"]))
            return {"result": result or None,
                    "mapping_needed": mapping_needed}
        finally:
            heartbeat.AUTO_WATCH.reset(_watch)
            heartbeat.PUSH_VIA.reset(_via)
            conn.close()

    return await run_in_threadpool(_run)


@router.post("/import/mapped")
def import_mapped_api(user: dict = Depends(_user()), body: dict = Body(...)):
    demoguard.deny(user)
    from ..sync import heartbeat
    from .pages import run_mapped_import
    _watch = heartbeat.AUTO_WATCH.set(bool(user.get("script_token")))
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    conn = _conn(user)
    try:
        result = run_mapped_import(conn, body.get("token") or "", body)
        if not (isinstance(result, dict) and result.get("error")):
            _categorize_after_import(str(user["tenant_id"]))
        return {"result": result}
    finally:
        heartbeat.AUTO_WATCH.reset(_watch)
        heartbeat.PUSH_VIA.reset(_via)
        conn.close()


# A tax document goes through OCR and, for a 1040, a model — the most
# CPU-expensive parse in the product for a single upload.
@router.post("/import/taxdoc/analyze",
             dependencies=[Depends(limit("taxdoc_analyze", 20, 3600))])
async def taxdoc_analyze_api(user: dict = Depends(_user()),
                             file: UploadFile = File(...)):
    """Detect + parse a tax/income document, return the per-year
    preview rows behind a commit token."""
    demoguard.deny(user)
    from ..engine import taxdocs
    from .pages import read_capped
    data = await read_capped(file)
    if data is None:
        raise HTTPException(413, "file too large (max 50 MB)")
    filename, ctype = file.filename or "", file.content_type or ""

    # PDF/OCR/LLM parse is slow and blocking — keep it off the event loop
    def _analyze():
        conn = _conn(user)
        try:
            try:
                return taxdocs.analyze(conn, filename, data, ctype)
            except ValueError as e:
                raise HTTPException(400, str(e))
        finally:
            conn.close()

    return await run_in_threadpool(_analyze)


@router.post("/import/taxdoc/commit")
def taxdoc_commit_api(user: dict = Depends(_user()), body: dict = Body(...)):
    demoguard.deny(user)
    from ..engine import taxdocs
    conn = _conn(user)
    try:
        try:
            return taxdocs.commit(conn, str(body.get("token") or ""),
                                  list(body.get("rows") or []))
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/import/batches")
def import_batches_api(user: dict = Depends(_user())):
    from ..sync import batches
    conn = _conn(user)
    try:
        return {"batches": [_ser(b) for b in batches.recent(conn)]}
    finally:
        conn.close()


@router.post("/import/rollback")
def import_rollback_api(user: dict = Depends(_user()),
                        body: dict = Body(...)):
    demoguard.deny(user)
    from ..sync import batches
    conn = _conn(user)
    try:
        n = batches.rollback(conn, str(body.get("batch_id") or ""))
    finally:
        conn.close()
    return {"ok": True, "deleted": int(n),
            "warning": getattr(n, "warning", None)}


@router.get("/onboarding")
def onboarding_api(user: dict = Depends(_user())):
    """Fresh-instance progress for the SPA's guided checklist."""
    from ..engine import budget
    conn = _conn(user)
    try:
        counts = conn.execute(
            """SELECT (SELECT COUNT(*) FROM accounts) AS accounts,
                      (SELECT COUNT(*) FROM transactions WHERE removed=0)
                          AS transactions,
                      (SELECT COUNT(*) FROM bills WHERE active=1)
                          AS bills,
                      (SELECT COUNT(*) FROM items
                       WHERE aggregator IN ('plaid','simplefin','mx')
                         AND COALESCE(status,'') NOT IN ('archived','restored'))
                          AS connections,
                      (SELECT COUNT(*) FROM bill_proposals
                       WHERE status='pending') AS pending_proposals""").fetchone()
        cfg = budget.load_config(conn)
    finally:
        conn.close()
    return {"accounts": counts["accounts"],
            "transactions": counts["transactions"],
            "bills": counts["bills"],
            # top-level connections (the simplefin bridge counts as
            # one; its per-bank children don't inflate this) + the pending
            # recurring-proposal queue for the wizard's Recurring step
            "connections": counts["connections"],
            "pending_proposals": counts["pending_proposals"],
            "budgets_set": bool(cfg.get("food_monthly")
                                or cfg.get("other_monthly")),
            "primary_checking_set": bool(cfg.get("checking_account_id")),
            # the guided wizard is offered until every step is
            # done or the user dismisses it for good
            "wizard_done": bool(cfg.get("wizard_done")),
            # explicit per-step done/skipped marks (strict flow)
            "wizard_steps": cfg.get("wizard_steps") or {}}


# The historical window Plaid backfills after a link, in months — the
# denominator of the wizard's import bar.
BACKFILL_WINDOW_MONTHS = 24


def _backfill_status(conn) -> tuple[dict, list]:
    """Where each Plaid connection stands in its one-time historical
    backfill — the read half shared by the status GET and the nudge POST.
    Returns (payload, waiting_rows): the client-facing shape plus the raw
    rows of items still backfilling (unfinished, unbroken).

    An item is ready when /transactions/sync has reported
    HISTORICAL_UPDATE_COMPLETE; items that predate the tx_update_status
    column count as ready once they are a day old (their backfill finished
    long ago)."""
    rows = conn.execute(
        """SELECT i.id, i.institution_name, i.status, i.tx_update_status,
                  (i.tx_update_status IS NULL
                   AND i.linked_at < now() - interval '24 hours')
                      AS legacy_done,
                  (SELECT COUNT(*) FROM transactions t
                   JOIN accounts a ON a.id = t.account_id
                   WHERE a.item_id = i.id AND COALESCE(t.removed,0)=0)
                      AS transactions,
                  (SELECT MIN(t.date) FROM transactions t
                   JOIN accounts a ON a.id = t.account_id
                   WHERE a.item_id = i.id AND COALESCE(t.removed,0)=0)
                      AS earliest,
                  (SELECT MAX(ran_at) FROM sync_log s
                   WHERE s.item_id = i.id
                     AND s.ran_at > now() - interval '45 seconds')
                      AS recent_attempt
           FROM items i
           WHERE i.aggregator = 'plaid'
             AND COALESCE(i.status,'') != 'archived'
           ORDER BY i.linked_at""").fetchall()
    running = conn.execute(
        """SELECT 1 FROM job_progress WHERE id='sync'
           AND state='running'
           AND updated_at > now() - %s::interval""",
        (SYNC_STALE_AFTER,)).fetchone() is not None
    items = []
    today = _today(conn)
    for r in rows:
        stalled = (r["status"] or "").startswith("error")
        ready = (r["tx_update_status"] == "HISTORICAL_UPDATE_COMPLETE"
                 or bool(r["legacy_done"]))
        # A bar the client can draw. Plaid never says how many rows are
        # coming, only that the two-year window is complete, so the fill
        # is how much of that window the oldest landed row already
        # covers — capped short of full until the bank says complete,
        # because a bar parked at 100% while still importing reads as
        # hung. A window that reaches further back than the two years
        # (a bank that keeps more) is simply full.
        if ready:
            progress = 1.0
        elif r["earliest"]:
            days = (today - r["earliest"]).days
            progress = max(0.02, min(0.95, days / (BACKFILL_WINDOW_MONTHS
                                                   * 30.4)))
        else:
            progress = 0.0
        items.append({
            "id": r["id"],
            "institution": r["institution_name"],
            "transactions": int(r["transactions"] or 0),
            # how far back the history reaches so far — the one
            # progress signal a backfill can honestly show (Plaid
            # never says how many rows are coming, only that the
            # window is complete)
            "earliest": r["earliest"].isoformat() if r["earliest"] else None,
            "progress": round(progress, 3),
            # a pull touched this connection within the last 45 seconds:
            # the client shows "pulling from your bank" against the
            # quieter "waiting for the bank to send more" — the
            # difference between working and stuck, from the outside
            "active": r["recent_attempt"] is not None and not ready,
            "ready": ready,
            # a broken connection will not finish its backfill —
            # surfaced so the UI can say "needs attention" instead of
            # waiting forever, and excluded from the all_ready gate
            "stalled": stalled,
        })
    waiting = [r for r, it in zip(rows, items)
               if not it["ready"] and not it["stalled"]]
    all_ready = all(it["ready"] or it["stalled"] for it in items)
    return ({"items": items, "all_ready": all_ready, "syncing": running},
            waiting)


@router.get("/onboarding/backfill")
def onboarding_backfill(user: dict = Depends(_user())):
    """Where each Plaid connection stands in its one-time historical
    backfill. READ ONLY — two clients poll this every few seconds and a
    family viewer (or a cross-site top-level GET, which still carries a
    SameSite=Lax cookie) may reach it, so it must never start a sync. The
    pull lives behind POST /onboarding/backfill/nudge.

    Plaid delivers the two-year window ASYNCHRONOUSLY: the first sync
    lands ~30 days and the rest follows over minutes. The wizard's
    bills-&-income step waits on this — detection over a sliver of
    history proposes almost nothing."""
    conn = _conn(user)
    try:
        payload, _waiting = _backfill_status(conn)
    finally:
        conn.close()
    return payload


@router.post("/onboarding/backfill/nudge",
             dependencies=[Depends(limit("backfill_nudge", 30, 600))])
def onboarding_backfill_nudge(user: dict = Depends(_user())):
    """The self-driving half of the backfill wait: when an unfinished item
    has not been pulled recently and no sync is running, kick the
    background tenant sync. Hosted installs hear HISTORICAL_UPDATE by
    webhook, but a self-host without a public webhook URL would otherwise
    sit until the hourly cron. Owner-only and demo-denied like every other
    door that starts a sync (it pulls new transactions, moves cursors and
    spends aggregator quota). Returns the same status shape as the GET."""
    import threading

    demoguard.deny(user)
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    tid = str(user["tenant_id"])
    conn = _conn(user)
    try:
        payload, waiting = _backfill_status(conn)
        # nudge: an unfinished, unbroken item with no pull in the last 45 s
        # and no sync running → start one. 45, not 90: the wizard re-nudges
        # every 30 s while it waits, and a 90 s window would decline the
        # first two nudges after a link and leave the history over two
        # minutes away; the bank's window is usually ready
        # within a minute of the link (same claim as /jobs/sync/start,
        # so cron and button clicks can never double-run)
        if waiting and not payload["syncing"] \
                and all(r["recent_attempt"] is None for r in waiting):
            claimed = conn.execute(
                """INSERT INTO job_progress (id, state, progress)
                   VALUES ('sync', 'running', '{}'::jsonb)
                   ON CONFLICT (tenant_id, id) DO UPDATE SET
                       state='running', progress='{}'::jsonb,
                       started_at=now(), updated_at=now()
                   WHERE job_progress.state != 'running'
                      OR job_progress.updated_at < now() - %s::interval
                   RETURNING id""", (SYNC_STALE_AFTER,)).fetchone()
            if claimed is not None:
                payload["syncing"] = True

                def _pull():
                    from ..engine.compat import jsonb as _jsonb
                    from ..jobs import worker
                    wconn = tenancy.tenant_connect(tid)
                    try:
                        results = worker.sync_tenant(tid)
                        # Another door's sync beat this nudge to the
                        # per-tenant advisory lock: nothing was pulled, so
                        # 'done' would be a lie. The lock holder owns the
                        # row and will settle its state; just note it.
                        if results.get("_status") == "already-running":
                            wconn.execute(
                                "UPDATE job_progress "
                                "SET progress=COALESCE(progress,"
                                "'{}'::jsonb) || %s::jsonb, "
                                "updated_at=now() WHERE id='sync'",
                                (_jsonb({"already_running": True}),))
                            return
                        wconn.execute(
                            "UPDATE job_progress SET state='done', "
                            "progress=COALESCE(progress,'{}'::jsonb) || "
                            "%s::jsonb, updated_at=now() WHERE id='sync'",
                            (_jsonb({"results": results}),))
                    except Exception as e:          # noqa: BLE001
                        try:
                            wconn.execute(
                                "UPDATE job_progress SET state='error', "
                                "progress=COALESCE(progress,'{}'::jsonb) "
                                "|| %s::jsonb, updated_at=now() "
                                "WHERE id='sync'",
                                (_jsonb({"error":
                                         f"{type(e).__name__}: {e}"}),))
                        except Exception:           # noqa: BLE001
                            pass
                    finally:
                        wconn.close()

                threading.Thread(target=_pull, daemon=True,
                                 name=f"backfill-sync-{tid[:8]}").start()
    finally:
        conn.close()
    return payload


@router.get("/history/coverage")
def history_coverage(user: dict = Depends(_user())):
    """Per-account transaction-history bounds — the wizard's gap-aware
    Import step ("Chase checking ← Apr 12, 2026 — 90 days"). Shadow
    accounts (linked non-primaries) are excluded like every other
    aggregate."""
    from ..engine import links
    conn = _conn(user)
    try:
        shadows = set(links.shadow_ids(conn))
        rows = conn.execute(
            """SELECT a.id, COALESCE(a.display_name, a.name) AS name,
                      a.type, MIN(t.date) AS earliest, MAX(t.date) AS latest,
                      COUNT(t.id) AS transactions
               FROM accounts a
               LEFT JOIN transactions t
                 ON t.account_id = a.id AND t.removed = 0
               GROUP BY a.id, COALESCE(a.display_name, a.name), a.type
               ORDER BY 2""").fetchall()
    finally:
        conn.close()
    return {"accounts": [
        {"id": r["id"], "name": r["name"], "type": r["type"],
         "earliest": r["earliest"].isoformat() if r["earliest"] else None,
         "latest": r["latest"].isoformat() if r["latest"] else None,
         "transactions": r["transactions"]}
        for r in rows if r["id"] not in shadows]}


# ---- business expenses (Schedule C tagging) ----------------------------------

def _require_business(conn):
    """Business / expense tracking is not gated; this no-op is the one
    place a feature gate would go, so adding one is a change to a single
    function rather than to every route below."""
    return None


def _require_entity_id(entity_id: str) -> str:
    """404 a path segment that is not a uuid.

    business_entity.id is a Postgres uuid column, so a malformed id fails in
    the cast — before any of our own checks — and surfaces as a bare 500 from
    the global handler. One definition rather than the inline copy each of
    these ~15 routes would otherwise carry: an inline copy is missed every
    time a route is added, which is the whole failure mode."""
    from ..engine import entities as _ent
    if not _ent._valid_uuid(entity_id):
        raise HTTPException(404, "no such entity")
    return entity_id


def _require_record_id(record_id: str, what: str) -> str:
    """The same rule for a nested record id (equity movement, mileage trip,
    compliance obligation). Those columns are uuid too, and the DELETE goes
    straight to Postgres with whatever the path carried."""
    from ..engine import entities as _ent
    if not _ent._valid_uuid(record_id):
        raise HTTPException(404, f"no such {what}")
    return record_id


@router.post("/business/{txn_id}/flag")
def business_flag(txn_id: str, user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        _require_business(conn)
        data.flag_business(conn, txn_id)
        _activity.record(conn, user, "business", "flagged", target=txn_id,
                         label=_activity.txn_label(conn, txn_id))
        return {"ok": True}
    finally:
        conn.close()


@router.post("/business/{txn_id}/unflag")
def business_unflag(txn_id: str, user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        _require_business(conn)
        data.unflag_business(conn, txn_id)
        _activity.record(conn, user, "business", "unflagged", target=txn_id,
                         label=_activity.txn_label(conn, txn_id))
        return {"ok": True}
    finally:
        conn.close()


@router.get("/business")
def business_list(user: dict = Depends(_user()), year: int | None = None,
                  limit: int = 500, offset: int = 0):
    """Flagged-transaction worksheet, paged. The default cap is generous
    (one fetch covers almost every ledger) but bounded — an all-years
    request on a decade-old ledger must not ship the entire history to
    paint one screen. `count` covers the whole scope, so a client can
    page with limit/offset when rows hits the cap — and must SAY when it
    is showing fewer rows than `count`, or the drop is invisible. The CSV
    export stays unpaged: a worksheet download is the full worksheet."""
    _ck_year(year, "year")
    limit = max(1, min(int(limit), 1000))
    # `offset` is capped as well as `limit`: an offset past bigint would
    # reach OFFSET and die inside the driver as
    # NumericValueOutOfRange -> 500. Same ceiling the paged ledger and the
    # rules list clamp their page number to: far past any real worksheet,
    # and small enough that offset * anything still binds.
    offset = min(max(0, int(offset)), 10_000_000)
    conn = _conn(user)
    try:
        _require_business(conn)
        r = data.business_expenses(conn, year, limit=limit, offset=offset)
    finally:
        conn.close()
    return {"rows": [_ser(x) for x in r["rows"]], "total": r["total"],
            "count": r["count"], "years": r["years"]}


@router.get("/business/export.csv")
def business_export(request: Request, user: dict = Depends(_user()),
                    year: int | None = None):
    _same_site_download(request)
    """The Schedule C worksheet: flagged transactions as CSV."""
    import csv as _c
    import io as _io

    from fastapi.responses import Response
    _ck_year(year, "year")
    conn = _conn(user)
    try:
        _require_business(conn)
        r = data.business_expenses(conn, year)
    finally:
        conn.close()
    buf = _io.StringIO()
    w = _c.writer(buf)
    w.writerow(["date", "payee", "category", "account", "amount"])
    for row in r["rows"]:
        w.writerow([row["date"].isoformat(), _csv_safe(row["payee"]),
                    _csv_safe(row["category"]),
                    _csv_safe(row["account"] or ""), f'{row["amount"]:.2f}'])
    w.writerow([])
    w.writerow(["total", "", "", "", f'{r["total"]:.2f}'])
    name = f"oikonome-business-{year or 'all'}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{name}"'})


# ---- business entities ----------------------------------------------

@router.get("/business/entities")
def entities_list(user: dict = Depends(_user())):
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        return {"entities": entities.list_entities(conn)}
    finally:
        conn.close()


@router.post("/business/entities")
def entities_create(user: dict = Depends(_user()), body: dict = Body(...)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            ent = entities.create_entity(
                conn, name=_body_text(body, "name"),
                structure=_body_text(body, "structure"),
                state=_body_text(body, "state") or None,
                formation_date=_body_text(body, "formation_date") or None,
                business_start_date=_body_text(
                    body, "business_start_date") or None,
                ein=_body_text(body, "ein") or None,
                registered_agent=_body_text(body, "registered_agent") or None,
                fiscal_year_end=_body_text(body, "fiscal_year_end") or None)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return ent
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}")
def entities_get(entity_id: str, user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.get_entity(conn, entity_id)
        if not ent:
            raise HTTPException(404, "no such entity")
        from ..engine import equity
        ent["members"] = entities.list_members(conn, entity_id)
        ent["capital"] = equity.capital_summary(conn, entity_id)
        return ent
    finally:
        conn.close()


@router.patch("/business/entities/{entity_id}")
def entities_update(entity_id: str, user: dict = Depends(_user()),
                    body: dict = Body(...)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        # whitelist body keys — a stray "entity_id"/"conn" would collide with
        # the positional args and 500 the handler
        patch = {k: v for k, v in (body or {}).items()
                 if k in entities._EDITABLE or k == "ein"}
        try:
            ent = entities.update_entity(conn, entity_id, **patch)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not ent:
            raise HTTPException(404, "no such entity")
        return ent
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/status")
def entities_status(entity_id: str, user: dict = Depends(_user()),
                    body: dict = Body(...)):
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            ent = entities.set_status(conn, entity_id, body.get("status"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not ent:
            raise HTTPException(404, "no such entity")
        return ent
    finally:
        conn.close()


def _entity_lifecycle(entity_id: str, user: dict, status: str) -> dict:
    """Shared archive/restore body — editors only, demo-denied, 404 on a
    missing or malformed id."""
    from ..engine import entities
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.set_status(conn, entity_id, status)
        if not ent:
            raise HTTPException(404, "no such entity")
        return ent
    finally:
        conn.close()


@router.delete("/business/entities/{entity_id}")
def entities_delete(entity_id: str, user: dict = Depends(_user())):
    """'Delete' ARCHIVES — the safe default. The entity's equity ledger,
    mileage log, 1099 vendors, and compliance calendar are tax records with
    retention obligations, so a plain REST delete (including any stale
    client that still issues one) must never destroy them. Actual
    destruction is the separate delete-forever call, which demands the
    entity's typed name."""
    return _entity_lifecycle(entity_id, user, "archived")


@router.post("/business/entities/{entity_id}/archive")
def entities_archive(entity_id: str, user: dict = Depends(_user())):
    """Hide the entity from active use; every record stays readable."""
    return _entity_lifecycle(entity_id, user, "archived")


@router.post("/business/entities/{entity_id}/restore")
def entities_restore(entity_id: str, user: dict = Depends(_user())):
    """Reopen an archived entity — the round trip out of archive."""
    return _entity_lifecycle(entity_id, user, "active")


@router.get("/business/entities/{entity_id}/delete-impact")
def entities_delete_impact(entity_id: str, user: dict = Depends(_user())):
    """What delete-forever would destroy (cascaded rows, counted per table)
    and detach (accounts/transactions returned to personal) — so the
    confirmation dialog states exactly what is at stake."""
    from ..engine import entities
    _require_entity_id(entity_id)
    conn = _conn(user)
    try:
        _require_business(conn)
        impact = entities.delete_impact(conn, entity_id)
        if not impact:
            raise HTTPException(404, "no such entity")
        return impact
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/delete-forever")
def entities_delete_forever(entity_id: str, user: dict = Depends(_user()),
                            body: dict = Body(...)):
    """Permanent destruction — the made-this-by-mistake case. The body must
    carry the entity's EXACT name; a mismatch is a 400 and nothing is
    touched. Enforced server-side so the typed-name confirmation cannot be
    bypassed by calling the API directly.

    Owner-only. Archiving (the safe default a member may do) keeps every
    record; this call irrecoverably DELETEs the entity and cascades to its
    equity ledger, compliance calendar, mileage log and 1099 vendors —
    retention-obligated tax records. Destroying them is exactly the
    irreversible, household-control-losing act the role design reserves for
    the owner, and the typed-name string is a mistake-guard, not an
    authentication control (every member can read the name)."""
    from ..engine import entities
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _owner_only(user)
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            impact = entities.delete_forever(
                conn, entity_id, str((body or {}).get("name") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not impact:
            raise HTTPException(404, "no such entity")
        return {"ok": True, **impact}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/members")
def entities_add_member(entity_id: str, user: dict = Depends(_user()),
                        body: dict = Body(...)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        try:
            return entities.add_member(
                conn, entity_id, member_name=_body_text(body, "member_name"),
                ownership_pct=body.get("ownership_pct"),
                is_manager=bool(body.get("is_manager")))
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/equity")
def equity_list(entity_id: str, user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    from ..engine import equity
    conn = _conn(user)
    try:
        _require_business(conn)
        return {"movements": equity.list_movements(conn, entity_id),
                "capital": equity.capital_summary(conn, entity_id)}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/equity")
def equity_record(entity_id: str, user: dict = Depends(_user()),
                  body: dict = Body(...)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities, equity
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        try:
            return equity.record_movement(
                conn, entity_id, kind=body.get("kind"),
                amount=body.get("amount"), date=body.get("date"),
                member_id=_body_text(body, "member_id") or None,
                form=_body_text(body, "form") or None,
                note=_body_text(body, "note") or None)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/business/entities/{entity_id}/equity/{movement_id}")
def equity_delete(entity_id: str, movement_id: str,
                  user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import equity
    conn = _conn(user)
    try:
        _require_business(conn)
        if not equity.delete_movement(
                conn, _require_record_id(movement_id, "movement"), entity_id):
            raise HTTPException(404, "no such movement")
        return {"ok": True}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/reimburse")
def equity_reimburse(entity_id: str, user: dict = Depends(_user()),
                     body: dict = Body(...)):
    """A business cost paid on a personal card → capitalize it (contribution)
    or mark it reimbursable, assigning the transaction to the entity either way."""
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities, equity
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        txn_id = _body_text(body, "txn_id")
        mode = body.get("mode")
        if not txn_id:
            raise HTTPException(400, "txn_id required")
        try:
            fn = (equity.contribute_expense if mode == "contribute"
                  else equity.reimburse_expense if mode == "reimburse"
                  else None)
            if fn is None:
                raise HTTPException(400, "mode must be contribute or reimburse")
            return fn(conn, entity_id, txn_id,
                      member_id=body.get("member_id") or None)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/suggestions")
def business_suggestions(entity_id: str, user: dict = Depends(_user())):
    """Proposed Schedule C lines for unclassified business transactions.

    SUGGEST-ONLY by design. The personal side
    auto-applies a learned merchant category because a wrong one is
    cosmetic; a wrong Schedule C line is a wrong number on a filed return,
    so nothing is written until a human confirms. `source` tells the caller
    how much to trust each row: "merchant" is this entity's own prior
    decision, "category" is a weak guess from the ledger category.
    """
    _require_entity_id(entity_id)
    from ..engine import books
    conn = _conn(user)
    try:
        _require_business(conn)
        return {"lines": books.SCHED_C_LINES,
                "suggestions": books.suggest_lines(conn, entity_id),
                # the true total — `suggestions` is capped, so counting it
                # under-reports a large backlog
                "unclassified": books.unclassified_count(conn, entity_id)}
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/transactions")
def business_txns(entity_id: str, user: dict = Depends(_user()),
                  year: int | None = None):
    _require_entity_id(entity_id)
    _ck_year(year, "year")
    from ..engine import books
    conn = _conn(user)
    try:
        _require_business(conn)
        return {"transactions": books.entity_transactions(conn, entity_id, year)}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/transactions/{txn_id}/class")
def business_classify(entity_id: str, txn_id: str,
                      user: dict = Depends(_user()), body: dict = Body(...)):
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import books, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)   # archived = read-only
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        # the class row is keyed by txn alone, so the URL's entity must
        # actually own the transaction — without this, entity A's endpoint
        # could classify (and rewrite) entity B's rows, archived ones
        # included, sidestepping the read-only guard above
        if not books.txn_belongs_to_entity(conn, entity_id, txn_id):
            raise HTTPException(404, "no such transaction for this entity")
        try:
            books.classify(conn, txn_id, body.get("bucket"),
                           sched_c_line=body.get("sched_c_line") or None,
                           note=body.get("note") or None)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/pnl")
def business_pnl(entity_id: str, user: dict = Depends(_user()),
                 year: int | None = None):
    _ck_year(year, "year")
    from ..engine import books, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        if not entities.get_entity(conn, entity_id):
            raise HTTPException(404, "no such entity")
        return books.pnl(conn, entity_id, year)
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/package.zip")
def business_year_package(entity_id: str, request: Request,
                          user: dict = Depends(_user()),
                          year: int | None = None):
    _same_site_download(request)
    """Everything a CPA asks for, in one file.

    One button producing P&L, balance sheet, 1099s, mileage log and the
    categorized ledger. Every part exists as its own endpoint — the work is
    the BUNDLING, because the failure mode is not a missing report, it is a
    person exporting four things separately in March and missing the
    fifth.

    Deliberately a plain zip of CSVs, not a rendered document: a CPA wants
    numbers they can open in a spreadsheet and re-foot, not a PDF they have
    to retype. A README states what the figures are and, importantly, what
    they are not.
    """
    import csv as _c
    import io as _io
    import zipfile as _z

    from fastapi.responses import Response
    from ..engine import books, entities, selfemploy
    _ck_year(year, "year")
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.get_entity(conn, entity_id)
        if not ent:
            raise HTTPException(404, "no such entity")
        txns = books.entity_transactions(conn, entity_id, year)
        pnl = books.pnl(conn, entity_id, year, txns=txns)
        sheet = selfemploy.balance_sheet(conn, entity_id)
        vendors = selfemploy.vendor_totals(conn, entity_id, year)
        trips = selfemploy.list_trips(conn, entity_id, year)
        miles = selfemploy.mileage_deduction(conn, entity_id, year)
    finally:
        conn.close()

    def _csv(rows) -> str:
        buf = _io.StringIO()
        w = _c.writer(buf)
        for r in rows:
            w.writerow(r)
        return buf.getvalue()

    period = str(year) if year else "all-years"
    name = _csv_safe(ent["name"])
    buf = _io.BytesIO()
    with _z.ZipFile(buf, "w", _z.ZIP_DEFLATED) as z:
        z.writestr("README.txt",
                   f"Oikonome year-end package\n"
                   f"Business: {name} ({ent.get('structure', '')})\n"
                   f"Period:   {period}\n\n"
                   "Contents\n"
                   "  profit-and-loss.csv  revenue, operating expenses, net,\n"
                   "                       and the startup/organizational split\n"
                   "  ledger.csv           every business transaction with its\n"
                   "                       bucket and Schedule C line\n"
                   "  balance-sheet.csv    assets, liabilities, capital account\n"
                   "  vendors-1099.csv     paid vendors with totals, for 1099\n"
                   "                       reporting decisions\n"
                   "  mileage.csv          trip log and the standard-rate\n"
                   "                       deduction it implies\n\n"
                   "These figures are bookkeeping output, NOT tax advice or a\n"
                   "filed return. Deductibility, capitalization, meal limits,\n"
                   "vehicle and home-office method are the preparer's call.\n"
                   "Transactions with no Schedule C line are listed as\n"
                   "Unclassified — they need a decision before filing.\n")
        z.writestr("profit-and-loss.csv", _csv([
            ["Business", name], ["Period", period], [],
            ["Line", "Amount"],
            ["Revenue", f'{pnl["revenue"]:.2f}'],
            ["Operating expenses", f'{pnl["operating_expenses"]:.2f}'],
            ["Net operating profit", f'{pnl["net_operating"]:.2f}'],
            ["Organizational costs (248)", f'{pnl["organizational"]:.2f}'],
            ["Start-up costs (195)", f'{pnl["startup_195"]:.2f}'],
            [],
            ["Schedule C line", "Amount"],
            # The line KEYS are user text too — an
            # unclassified expense's line is its category_override verbatim
            # (free text by design), and this file is built to be opened by
            # an accountant's Excel. Same _csv_safe the ledger.csv three
            # lines down already applies to the same strings.
        ] + [[_csv_safe(str(k)), f"{v:.2f}"] for k, v in
             sorted((pnl.get("operating_by_line") or {}).items())]))
        z.writestr("ledger.csv", _csv([
            ["Date", "Payee", "Amount", "Bucket", "Schedule C line",
             "Category"]] + [
            # every free-text cell goes through _csv_safe — `category` carries
            # the user's own category_override verbatim, and this file is built
            # to be handed to an accountant, so the person who opens it in Excel
            # is not the person who typed it
            [str(x.get("date") or ""), _csv_safe(x.get("payee") or ""),
             f'{float(x.get("amount") or 0):.2f}',
             _csv_safe(x.get("bucket") or ""),
             _csv_safe(x.get("sched_c_line") or "Unclassified"),
             _csv_safe(x.get("category") or "")] for x in txns]))
        z.writestr("balance-sheet.csv", _csv(
            [["Assets", "Amount"]]
            + [[_csv_safe(a["name"]), f'{a["amount"]:.2f}']
               for a in sheet.get("assets", [])]
            + [["Total assets", f'{sheet.get("total_assets", 0):.2f}'], []]
            + [["Liabilities", "Amount"]]
            + [[_csv_safe(l["name"]), f'{l["amount"]:.2f}']
               for l in sheet.get("liabilities", [])]
            + [["Total liabilities", f'{sheet.get("total_liabilities", 0):.2f}'],
               [], ["Net", f'{sheet.get("net", 0):.2f}'],
               ["Capital account", f'{sheet.get("capital_account", 0):.2f}']]))
        z.writestr("vendors-1099.csv", _csv(
            [["Vendor", "Total paid", "Marked reportable"]]
            + [[_csv_safe(v.get("merchant") or ""),
                f'{float(v.get("paid") or 0):.2f}',
                "yes" if v.get("reportable") else ""] for v in vendors]))
        z.writestr("mileage.csv", _csv(
            [["Date", "Miles", "Purpose", "Note"]]
            + [[str(x.get("date") or ""), x.get("miles"),
                _csv_safe(x.get("purpose") or ""),
                _csv_safe(x.get("note") or "")] for x in trips]
            + [[], ["Total miles", miles.get("miles")]]
            # a year can carry two IRS rates; list each with its miles
            + [["Rate", r.get("rate"), r.get("miles")]
               for r in (miles.get("rates") or [{"rate": miles.get("rate")}])]
            + [["Deduction", f'{float(miles.get("deduction") or 0):.2f}']]))
    fn = f"oikonome-{period}-{entity_id[:8]}.zip"
    return Response(
        content=buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@router.get("/business/entities/{entity_id}/pnl.csv")
def business_pnl_csv(entity_id: str, request: Request,
                     user: dict = Depends(_user()),
                     year: int | None = None):
    _same_site_download(request)
    """A CPA-consumable export: entity, P&L summary, and every classified line."""
    import csv as _c
    import io as _io

    from fastapi.responses import Response
    from ..engine import books, entities
    _ck_year(year, "year")
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.get_entity(conn, entity_id)
        if not ent:
            raise HTTPException(404, "no such entity")
        txns = books.entity_transactions(conn, entity_id, year)
        p = books.pnl(conn, entity_id, year, txns=txns)   # reuse, don't re-query
    finally:
        conn.close()
    buf = _io.StringIO()
    w = _c.writer(buf)
    w.writerow(["Oikonome business books export", _csv_safe(ent["name"]),
                _csv_safe(ent.get("structure", "")),
                f"EIN •••••{ent.get('ein_last4') or ''}"])
    w.writerow(["Period", year or "all years"])
    w.writerow([])
    w.writerow(["Summary", "Amount"])
    w.writerow(["Revenue", f'{p["revenue"]:.2f}'])
    w.writerow(["Operating expenses", f'{p["operating_expenses"]:.2f}'])
    w.writerow(["Net operating profit", f'{p["net_operating"]:.2f}'])
    w.writerow(["Organizational costs (§248)", f'{p["organizational"]:.2f}'])
    w.writerow(["Start-up costs (§195)", f'{p["startup_195"]:.2f}'])
    w.writerow(["  §195 first-year deduction (est.)",
                f'{p["startup_deduction"]["immediate"]:.2f}'])
    w.writerow(["  §195 amortizable over 180 mo (est.)",
                f'{p["startup_deduction"]["amortizable"]:.2f}'])
    w.writerow(["Capital account balance", f'{p["capital"]["capital_balance"]:.2f}'])
    w.writerow([])
    w.writerow(["Date", "Payee", "Amount", "Bucket", "Schedule C line",
                "Category"])
    for t in txns:
        # bucket goes through _csv_safe like every other free-text cell (and
        # like the same field in the year-end package): the column is plain
        # TEXT with the three-value taxonomy enforced in application code
        # only, and the full-ledger restore writes it straight from an
        # imported CSV — so what arrives here is not guaranteed to be one of
        # the three, and this file is meant to be opened in a spreadsheet.
        w.writerow([t["date"], _csv_safe(t["payee"]), f'{t["amount"]:.2f}',
                    _csv_safe(t["bucket"] or ""),
                    _csv_safe(t["sched_c_line"] or ""),
                    _csv_safe(t["category"] or "")])
    name = f"oikonome-books-{_filename_safe(ent['name'], 'entity')}-{year or 'all'}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{name}"'})


@router.get("/business/entities/{entity_id}/compliance")
def compliance_list(entity_id: str, user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    from ..engine import compliance, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.get_entity(conn, entity_id)
        if not ent:
            raise HTTPException(404, "no such entity")
        return {"obligations": compliance.obligations(conn, ent)}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/compliance")
def compliance_add(entity_id: str, user: dict = Depends(_user()),
                   body: dict = Body(...)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import compliance, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        try:
            return compliance.add_obligation(
                conn, entity_id, title=body.get("title"),
                due_date=body.get("due_date"),
                recurrence=body.get("recurrence", "yearly"),
                fee=body.get("fee"), url=body.get("url"),
                note=body.get("note"))
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/business/entities/{entity_id}/compliance/{obligation_id}")
def compliance_delete(entity_id: str, obligation_id: str,
                      user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import compliance
    conn = _conn(user)
    try:
        _require_business(conn)
        if not compliance.delete_obligation(
                conn, _require_record_id(obligation_id, "obligation"),
                entity_id):
            raise HTTPException(404, "no such obligation")
        return {"ok": True}
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/compliance.ics")
def compliance_ics(entity_id: str, request: Request,
                   user: dict = Depends(_user())):
    _same_site_download(request)
    _require_entity_id(entity_id)
    from fastapi.responses import Response
    from ..engine import compliance, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        ent = entities.get_entity(conn, entity_id)
        if not ent:
            raise HTTPException(404, "no such entity")
        body = compliance.ics(conn, ent)
    finally:
        conn.close()
    return Response(body, media_type="text/calendar",
                    headers={"Content-Disposition":
                             'attachment; filename="oikonome-compliance.ics"'})


# ---- self-employed tax pack -----------------------------------------

@router.get("/business/entities/{entity_id}/estimated-tax")
def se_estimated_tax(entity_id: str, user: dict = Depends(_user()),
                     year: int | None = None):
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        if not entities.get_entity(conn, entity_id):
            raise HTTPException(404, "no such entity")
        _ck_year(year, "year")
        return selfemploy.estimated_tax(conn, entity_id, year)
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/balance-sheet")
def se_balance_sheet(entity_id: str, user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        if not entities.get_entity(conn, entity_id):
            raise HTTPException(404, "no such entity")
        return selfemploy.balance_sheet(conn, entity_id)
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/mileage")
def se_mileage_list(entity_id: str, user: dict = Depends(_user()),
                    year: int | None = None):
    _ck_year(year, "year")
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        if not entities.get_entity(conn, entity_id):
            raise HTTPException(404, "no such entity")
        return {"trips": selfemploy.list_trips(conn, entity_id, year),
                "summary": selfemploy.mileage_deduction(conn, entity_id, year)}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/mileage")
def se_mileage_add(entity_id: str, user: dict = Depends(_user()),
                   body: dict = Body(...)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        try:
            return selfemploy.add_trip(conn, entity_id, date=body.get("date"),
                                       miles=body.get("miles"),
                                       purpose=body.get("purpose"),
                                       note=body.get("note"))
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/business/entities/{entity_id}/mileage/{trip_id}")
def se_mileage_delete(entity_id: str, trip_id: str,
                      user: dict = Depends(_user())):
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        if not selfemploy.delete_trip(
                conn, _require_record_id(trip_id, "trip"), entity_id):
            raise HTTPException(404, "no such trip")
        return {"ok": True}
    finally:
        conn.close()


@router.get("/business/entities/{entity_id}/vendors")
def se_vendors(entity_id: str, user: dict = Depends(_user()),
               year: int | None = None):
    _ck_year(year, "year")
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        if not entities.get_entity(conn, entity_id):
            raise HTTPException(404, "no such entity")
        return {"vendors": selfemploy.vendor_totals(conn, entity_id, year)}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/vendors")
def se_vendor_mark(entity_id: str, user: dict = Depends(_user()),
                   body: dict = Body(...)):
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        if not body.get("merchant"):
            raise HTTPException(400, "merchant required")
        selfemploy.mark_vendor(conn, entity_id, body["merchant"],
                               reportable=bool(body.get("reportable", True)),
                               tin_last4=body.get("tin_last4"))
        return {"ok": True}
    finally:
        conn.close()


@router.get("/business/summary")
def business_summary(user: dict = Depends(_user())):
    """SPA header state: entity count, unassigned legacy-flag count, combined
    toggle — so the Business section can render its empty/reconcile/combined UI."""
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        ents = entities.list_entities(conn)
        return {"entities": ents,
                "flagged_unassigned": entities.count_flagged(conn),
                "combined": entities.is_combined(conn)}
    finally:
        conn.close()


@router.post("/business/combine")
def business_combine(user: dict = Depends(_user()), body: dict = Body(...)):
    """Toggle the combined view (personal aggregates include business)."""
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        return {"combined": entities.set_combined(conn, bool(body.get("on")))}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/import-flags")
def entities_import_flags(entity_id: str, user: dict = Depends(_user())):
    """Reconcile the legacy business_flags tags into this entity."""
    _require_entity_id(entity_id)
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        moved = entities.import_flagged_transactions(conn, entity_id)
        return {"ok": True, "moved": moved}
    finally:
        conn.close()


@router.post("/accounts/{account_id}/entity")
def account_assign_entity(account_id: str, user: dict = Depends(_user()),
                          body: dict = Body(...)):
    """Assign a whole account to a business entity (or null → personal)."""
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        eid = body.get("entity_id") or None
        if eid:                          # assigning INTO an entity is a write —
            try:                         # an archived entity is read-only
                entities.require_active(conn, eid)
            except ValueError as _e:
                raise HTTPException(409 if "archived" in str(_e) else 404,
                                    str(_e))
        if not entities.assign_account(conn, account_id, eid):
            raise HTTPException(404, "no such account")
        return {"ok": True, "account_id": account_id, "entity_id": eid}
    finally:
        conn.close()


@router.post("/transactions/{txn_id}/entity")
def txn_assign_entity(txn_id: str, user: dict = Depends(_user()),
                      body: dict = Body(...)):
    """Per-transaction business override (or null → clear)."""
    demoguard.deny(user)
    _may_edit(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        eid = body.get("entity_id") or None
        if eid:                          # assigning INTO an entity is a write —
            try:                         # an archived entity is read-only
                entities.require_active(conn, eid)
            except ValueError as _e:
                raise HTTPException(409 if "archived" in str(_e) else 404,
                                    str(_e))
        if not entities.assign_transaction(conn, txn_id, eid):
            raise HTTPException(404, "no such transaction")
        return {"ok": True, "txn_id": txn_id, "entity_id": eid}
    finally:
        conn.close()


@router.post("/accounts/coinbase/link")
def coinbase_link_api(user: dict = Depends(_user()), body: dict = Body(...)):
    demoguard.deny(user)
    from ..sync import coinbase
    for k in ("label", "key_name", "private_key"):
        if not body.get(k):
            raise HTTPException(400, f"{k} required")
    conn = _conn(user)
    try:
        item_id = coinbase.link(conn, body["label"], body["key_name"],
                                body["private_key"])
        return {"ok": True, "item_id": item_id}
    finally:
        conn.close()


def _pin_kind(body: dict):
    """Collectors declare how their collection half works
    (api | scrape) — pin it for the heartbeat stamps this request makes."""
    from ..sync import heartbeat
    kind = body.get("kind")
    return heartbeat.PUSH_KIND.set(
        kind if kind in ("api", "scrape") else None)


@router.get("/import/enrich")
def enrich_status_api(user: dict = Depends(_user())):
    """How much imported history Plaid Enrich could still describe, and
    this month's cap/usage — the Settings card's numbers."""
    # the run is the owner's (it is billed); so are its numbers — both
    # clients already show the card to the owner only
    _owner_only(user)
    from ..sync import plaid as _plaid
    conn = _conn(user)
    try:
        try:
            _plaid.credentials(conn)
            configured = True
        except _plaid.PlaidError:
            configured = False
        # the connector owns the definition of a candidate — a count computed
        # separately here would drift from what a click would actually send
        return {"candidates": _plaid.enrich_candidate_count(conn),
                "plaid_configured": configured,
                **_plaid.enrich_usage(conn)}
    finally:
        conn.close()


@router.post("/import/enrich",
             dependencies=[Depends(limit("import-enrich", 20, 3600.0))])
def enrich_run_api(user: dict = Depends(_user()), body: dict = Body(default={})):
    """Send a batch of imported rows to Plaid Enrich (owner, on request —
    billed per row, so never automatic; capped per month)."""
    demoguard.deny(user)
    _owner_only(user)
    from ..sync import plaid as _plaid
    try:
        limit_n = int(body.get("limit") or _plaid.ENRICH_BATCH)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, "limit must be an integer")
    limit_n = max(1, min(limit_n, 500))
    conn = _conn(user)
    try:
        # Enrich is billed per row on the operator's Plaid account, so the
        # plan is consulted BEFORE the instance configuration — a tenant
        # whose plan does not carry it should be told that, not invited to
        # set Plaid up first. Self-host is ungated (effective_tier None):
        # the operator brings their own Plaid keys and pays their own bill.
        if not ext.gate.tier_allows(conn, "enrich"):
            raise HTTPException(
                402, "Enriching imported history isn't enabled for this account.")
        try:
            _plaid.credentials(conn)
        except _plaid.PlaidError:
            raise HTTPException(400, "Plaid is not configured on this instance")
        return _plaid.enrich_rows(conn, limit=limit_n)
    finally:
        conn.close()


@router.post("/import/coinbase")
def coinbase_import_api(user: dict = Depends(_user()),
                        body: dict = Body(...)):
    """The Coinbase collector's push door (script-token
    friendly) — normalized items/accounts/transactions payload; rows the
    source stopped reporting are retired inside the pushed prefix."""
    demoguard.deny(user)
    from ..sync import coinbase_push, heartbeat
    _watch = heartbeat.AUTO_WATCH.set(bool(user.get("script_token")))
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    _kind = _pin_kind(body)
    conn = _conn(user)
    try:
        try:
            return coinbase_push.import_payload(conn, body)
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, f"bad payload: {e}")
    finally:
        heartbeat.AUTO_WATCH.reset(_watch)
        heartbeat.PUSH_VIA.reset(_via)
        heartbeat.PUSH_KIND.reset(_kind)
        conn.close()


@router.post("/import/amazon")
def amazon_import_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """The Amazon-orders push door (script-token friendly) —
    orders upsert + drift refresh + the app's own order↔txn matcher."""
    demoguard.deny(user)
    from ..sync import amazon_orders, heartbeat
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    _kind = _pin_kind(body)
    conn = _conn(user)
    try:
        try:
            return amazon_orders.import_orders(conn,
                                               body.get("orders") or [])
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, f"bad payload: {e}")
    finally:
        heartbeat.PUSH_VIA.reset(_via)
        heartbeat.PUSH_KIND.reset(_kind)
        conn.close()


@router.post("/import/costco")
def costco_import_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """The Costco-receipts push door (script-token friendly) —
    receipts upsert/refresh + the app's own receipt↔txn matcher."""
    demoguard.deny(user)
    from ..sync import costco_receipts, heartbeat
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    _kind = _pin_kind(body)
    conn = _conn(user)
    try:
        try:
            return costco_receipts.import_receipts(
                conn, body.get("receipts") or [])
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, f"bad payload: {e}")
    finally:
        heartbeat.PUSH_VIA.reset(_via)
        heartbeat.PUSH_KIND.reset(_kind)
        conn.close()


@router.post("/import/plan-activity")
def plan_activity_import_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """A workplace plan's activity CSV, pushed by a host-side collector:
    `{plan, csv, balance?, mask?, provider?, institution?}`. `provider` is
    the collector's slug for the plan administrator (its item and id
    namespace), `institution` the name shown for it."""
    demoguard.deny(user)
    from ..sync import heartbeat, plan_csv
    plan = _body_text(body, "plan").upper()
    if plan not in plan_csv.PLAN_CODES:
        raise HTTPException(400, f"plan must be one of {sorted(plan_csv.PLAN_CODES)}")
    if not body.get("csv"):
        raise HTTPException(400, "csv required")
    # The collector posts whatever it scraped. A non-string csv would reach
    # the parser's string methods, and a non-numeric balance would reach a
    # float column — both would surface as an opaque 500, which tells a scripted
    # caller nothing about which field it got wrong.
    if not isinstance(body["csv"], str):
        raise HTTPException(400, "csv must be a string")
    try:
        provider = plan_csv.provider_slug(body.get("provider"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    institution = _body_text(body, "institution")[:80]
    balance = body.get("balance")
    if balance not in (None, ""):
        try:
            balance = float(balance)
        except (TypeError, ValueError):
            raise HTTPException(400, "balance must be a number")
        import math as _math
        if not _math.isfinite(balance):
            raise HTTPException(400, "balance must be a finite number")
    else:
        balance = None
    # the plan number's trailing digits, as an aggregator would report
    # them (Plaid's `mask`) — optional, and refused rather than stored
    # when it is not a short digit string, since it lands on the account
    # row and drives the account-link suggestions
    mask = body.get("mask")
    if mask not in (None, ""):
        mask = str(mask).strip()
        if not (2 <= len(mask) <= 8 and mask.isdigit()):
            raise HTTPException(400, "mask must be 2-8 digits")
    else:
        mask = None
    _watch = heartbeat.AUTO_WATCH.set(bool(user.get("script_token")))
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    _kind = _pin_kind(body)
    conn = _conn(user)
    try:
        try:
            r = plan_csv.import_csv(conn, plan, body["csv"], balance=balance,
                                    mask=mask, provider=provider,
                                    institution=institution)
        except (KeyError, TypeError, ValueError) as e:
            # a CSV nothing parses out of, or one past the row ceiling, is a
            # statement about the POSTed document — same 400 the coinbase
            # door gives for the same class of thing
            raise HTTPException(400, str(e))
        return {"ok": True, **r}
    finally:
        heartbeat.AUTO_WATCH.reset(_watch)
        heartbeat.PUSH_VIA.reset(_via)
        heartbeat.PUSH_KIND.reset(_kind)
        conn.close()


# ---- maintenance jobs (SPA Settings "Maintenance" card) ---------------------
# On-demand job triggers: the worker's per-tenant task bodies, run
# synchronously for the caller's own tenant. Lightly rate-limited — these
# hit aggregators / send real mail.


@router.post("/jobs/sync", dependencies=[Depends(limit("jobs-sync", 6, 600))])
def jobs_sync_api(user: dict = Depends(_user())):
    """Pull every connection for the caller's tenant NOW (the hourly sweep's
    task body, run inline). Returns per-item results."""
    demoguard.deny(user)
    from ..jobs import worker
    results = worker.sync_tenant(user["tenant_id"])
    # sync_tenant returns this sentinel instead of per-item results when the
    # per-tenant single-flight lock is already held. It carries no "error"
    # prefix, so it is checked for explicitly: otherwise a caller pressing
    # Sync while one was running would be told it had synced nothing,
    # successfully.
    if results.get("_status") == "already-running":
        return {"ok": True, "started": False, "reason": "already running",
                "results": {}}
    return {"ok": all(not v.startswith("error") for v in results.values()),
            "started": True, "results": results}


@router.post("/connections/{item_id}/disconnect")
def connection_disconnect(item_id: str, user: dict = Depends(_user())):
    """Archive a connection so it stops syncing; transaction history is
    kept. For Plaid this ALSO releases the Item on Plaid's side
    (/item/remove) — that's what frees a slot on limited plans; a local
    archive alone never would."""
    demoguard.deny(user)
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    from ..sync import base as sync_base
    conn = _conn(user)
    try:
        try:
            r = sync_base.disconnect_item(conn, item_id)
        except KeyError:
            raise HTTPException(404, "no such connection")
        return {"ok": True, "plaid_released": r["plaid_released"],
                "note": r["note"]}
    finally:
        conn.close()


# Items whose aggregator writes balances back on every sync or push refill a
# nulled balance by themselves; these two never do — 'manual' is the hand-kept
# account, 'csv' the file-import shell — and neither does an account with no
# item at all.
_FEEDLESS_AGGREGATORS = ("manual", "csv")


def _account_balance_refills(conn, account_id: str) -> bool:
    """True when something upstream will write this account's balance again.

    The aggregator name alone is not enough: a Plaid account on an
    ARCHIVED connection (disconnected, token released) will never sync
    again either, so nulling its balance is the same permanent loss as
    on a manual account."""
    row = conn.execute(
        "SELECT i.aggregator, i.status FROM accounts a "
        "LEFT JOIN items i ON i.id = a.item_id WHERE a.id=%s",
        (account_id,)).fetchone()
    agg = (row["aggregator"] or "") if row else ""
    from ..sync.base import item_is_live
    return (bool(agg) and agg not in _FEEDLESS_AGGREGATORS
            and item_is_live(row["status"]))


def _hide_account(conn, account_id: str) -> None:
    """Soft-remove one account, nulling its balances only when a feed will
    write them again.

    Hiding is reversible and excludes the account everywhere through
    `user_removed_at` alone, so nulling the balance buys nothing extra — it
    only keeps a live account from showing a frozen number until the next
    sync. A manual or imported account has no next sync: the balance a
    person typed (or a statement import wrote) is the only copy, and unhide
    has nowhere to fetch it back from, so it stays.
    """
    if _account_balance_refills(conn, account_id):
        conn.execute(
            "UPDATE accounts SET user_removed_at=now(), "
            "balance_current=NULL, balance_available=NULL WHERE id=%s",
            (account_id,))
    else:
        conn.execute(
            "UPDATE accounts SET user_removed_at=now() WHERE id=%s",
            (account_id,))
    # hiding changes who serves a linked group — the ambient shadow set
    # this connection already computed must follow, or an aggregate later
    # in the same request reads the old election
    from ..engine import links as _links
    _links.set_shadow_scope(conn)


@router.post("/accounts/{account_id}/hidden")
def account_set_hidden(account_id: str, user: dict = Depends(_user()),
                       body: dict = Body(...)):
    """Hide or unhide one account, keeping its connection and history.

    The case this exists for: a Plaid login covers several accounts and one
    of them is simply not wanted — a personal share-savings sitting under a
    business connection, say. Disconnecting is too blunt (it takes the whole
    institution with it) and purging destroys history.

    Hidden means hidden EVERYWHERE, not just off the Accounts list: the
    balance leaves net worth, the transactions leave spend, budgets, bills,
    savings, retirement and the ledger (`links.shadow_ids`), and sync stops
    storing new rows for it (`sync.base.upsert_transactions`). The row and
    its past transactions stay, so unhiding restores the account — though
    anything the aggregator reported while it was hidden was never written
    and does not come back without a re-sync of that window.
    """
    demoguard.deny(user)
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    hidden = bool(body.get("hidden"))
    conn = _conn(user)
    try:
        row = conn.execute("SELECT id, name FROM accounts WHERE id=%s",
                           (account_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such account")
        refills = _account_balance_refills(conn, account_id)
        if hidden:
            _hide_account(conn, account_id)
        else:
            # a fed account's balances stay NULL until the next sync
            # refreshes them — the aggregator is the only source of truth
            # for a live balance. A manual/imported one was never nulled,
            # so it comes back with the number it had.
            conn.execute(
                "UPDATE accounts SET user_removed_at=NULL WHERE id=%s",
                (account_id,))
            # unhiding changes the group's election exactly as hiding
            # does — the ambient shadow set follows on this path too
            from ..engine import links as _links
            _links.set_shadow_scope(conn)
        if hidden:
            note = (f"{row['name']} is hidden — it won't appear in or "
                    f"count toward anything, and stops pulling new data.")
        elif refills:
            note = (f"{row['name']} is back. Its balance fills in on the "
                    f"next sync.")
        else:
            note = f"{row['name']} is back, with the balance it had."
        return {"ok": True, "account_id": account_id, "hidden": hidden,
                "note": note}
    finally:
        conn.close()


@router.post("/accounts/{account_id}/remove")
def accounts_remove_api(account_id: str, user: dict = Depends(_user()),
                        body: dict = Body(...)):
    """Remove one bank/manual account. Modes:

    * ``hide`` — soft-remove this account only (keeps the institution
      connection and history; a fed account's balance is nulled until its
      next sync, a manual/imported one keeps the balance it had, since
      nothing would ever write it back). Use when a multi-account Plaid
      Item has one account you don't want in the app.
    * ``disconnect`` — stop syncing the whole institution connection,
      keep history. For Plaid this calls ``/item/remove`` so billing
      stops.
    * ``disconnect_purge`` — disconnect the institution AND delete all
      local data for every account under that connection.
    * ``purge`` — hard-delete this account and its transactions. Only
      when the connection is already archived/manual (a live Plaid Item
      would re-create the account on the next sync).

    Plaid cannot release a single account under an Item — disconnect
    always operates at the institution (Item) level.
    """
    demoguard.deny(user)
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    mode = _body_text(body, "mode").lower()
    if mode not in ("hide", "disconnect", "disconnect_purge", "purge"):
        raise HTTPException(
            400, "mode must be hide, disconnect, disconnect_purge, or purge")
    from ..sync import base as sync_base
    conn = _conn(user)
    try:
        row = conn.execute(
            """SELECT a.id, a.item_id, a.name, a.user_removed_at,
                      i.aggregator, i.status AS item_status,
                      i.institution_name
               FROM accounts a
               LEFT JOIN items i ON i.id = a.item_id
               WHERE a.id=%s""",
            (account_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such account")
        item_id = row["item_id"]
        agg = row["aggregator"] or ""
        conn_live = bool(item_id) and sync_base.item_is_live(row["item_status"])
        # only pull aggregators re-create accounts on sync — refuse purge
        # for those while live; manual/csv/scripts can hard-delete anytime
        pull_live = conn_live and agg in (
            "plaid", "mx", "simplefin", "simplefin-org")
        siblings = []
        if item_id:
            siblings = [r["id"] for r in conn.execute(
                "SELECT id FROM accounts WHERE item_id=%s AND id<>%s "
                "AND user_removed_at IS NULL",
                (item_id, account_id)).fetchall()]
        result: dict = {"ok": True, "mode": mode, "account_id": account_id,
                        "item_id": item_id, "plaid_released": False,
                        "note": "", "purged": None,
                        "affected_accounts": [account_id]}

        if mode == "hide":
            _hide_account(conn, account_id)
            if pull_live and not siblings:
                result["note"] = (
                    "Account hidden. This was the only active account on "
                    "the connection — disconnect the institution if you "
                    "want to free the aggregator slot.")
            else:
                result["note"] = "Account hidden; history kept."
            return result

        if mode == "disconnect":
            if not item_id:
                raise HTTPException(400, "account has no connection to disconnect")
            if not conn_live:
                raise HTTPException(400, "connection is already disconnected")
            try:
                r = sync_base.disconnect_item(conn, item_id)
            except KeyError:
                raise HTTPException(404, "no such connection")
            result["plaid_released"] = r["plaid_released"]
            result["note"] = r["note"] or "Disconnected; history kept."
            if siblings:
                result["affected_accounts"] = [account_id] + siblings
                result["note"] = (
                    (result["note"] + " " if result["note"] else "")
                    + f"Also stopped syncing {len(siblings)} other account(s) "
                      f"on this institution.")
            return result

        if mode == "disconnect_purge":
            if not item_id:
                raise HTTPException(
                    400, "account has no connection — use purge instead")
            if conn_live:
                try:
                    r = sync_base.disconnect_item(conn, item_id)
                except KeyError:
                    raise HTTPException(404, "no such connection")
                result["plaid_released"] = r["plaid_released"]
                result["note"] = r["note"]
            # all accounts under this item (incl. simplefin children)
            all_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM accounts WHERE item_id=%s OR item_id LIKE %s",
                (item_id, item_id + ":%")).fetchall()]
            result["affected_accounts"] = all_ids
            result["purged"] = sync_base.purge_item_accounts(conn, item_id)
            if not result["note"]:
                result["note"] = "Disconnected and deleted local data."
            else:
                result["note"] = (result["note"] + " Local data deleted.")
            return result

        # mode == "purge" — single-account hard delete
        if pull_live:
            raise HTTPException(
                400,
                "can't purge a live connected account (sync would recreate it) "
                "— use disconnect_purge for the institution, or hide to keep "
                "the connection")
        result["purged"] = sync_base.purge_account_data(conn, account_id)
        # drop orphan empty item shells (manual / archived with no accounts)
        if item_id:
            left = conn.execute(
                "SELECT COUNT(*) AS n FROM accounts WHERE item_id=%s",
                (item_id,)).fetchone()["n"]
            if left == 0 and agg == "manual":
                # shared 'manual' item may still have siblings — only delete
                # empty non-shared shells; leave archived pull items for audit
                conn.execute(
                    "DELETE FROM items WHERE id=%s AND NOT EXISTS ("
                    "SELECT 1 FROM accounts a WHERE a.item_id=items.id)",
                    (item_id,))
            elif left == 0 and agg not in (
                    "plaid", "mx", "simplefin", "simplefin-org"):
                conn.execute("DELETE FROM items WHERE id=%s", (item_id,))
        result["note"] = "Account and its transactions deleted."
        return result
    finally:
        conn.close()


# the wizard's Sync step — start the sweep in the background and
# poll live progress (per-connection rows + the categorize phase). State
# lives in job_progress (RLS), written by the worker thread via the
# progress callback, so any web process can serve the poll.


# How long a job_progress row may go unheard from before its owner is
# treated as dead. ONE constant, because the claim below and the liveness
# read in /api/connections must agree: two different windows leave
# /api/connections reporting no sync running while this endpoint refuses to
# start one, because the same orphaned row still looks alive to it. A click
# does nothing, with nothing to say why.
#
# A short claim window is safe because the claim is not the real guard.
# sync_tenant holds a Postgres advisory lock for the whole pull, and a
# second run that gets past this check returns immediately on that lock.
# This row is bookkeeping; the lock is the mutex.
SYNC_STALE_AFTER = "3 minutes"


@router.post("/jobs/sync/start",
             dependencies=[Depends(limit("jobs-sync", 6, 600))])
def jobs_sync_start(user: dict = Depends(_user())):
    """Kick off the full-tenant sync in the background; returns at once.
    One running job per tenant — a stale 'running' row (no update for 15
    minutes: a died worker) is reclaimed."""
    demoguard.deny(user)
    import threading

    from ..engine.compat import jsonb
    tid = str(user["tenant_id"])
    conn = _conn(user)
    try:
        claimed = conn.execute(
            """INSERT INTO job_progress (id, state, progress)
               VALUES ('sync', 'running', '{}'::jsonb)
               ON CONFLICT (tenant_id, id) DO UPDATE SET
                   state='running', progress='{}'::jsonb,
                   started_at=now(), updated_at=now()
               WHERE job_progress.state != 'running'
                  OR job_progress.updated_at < now() - %s::interval
               RETURNING id""", (SYNC_STALE_AFTER,)).fetchone()
    finally:
        conn.close()
    if claimed is None:
        return {"ok": True, "started": False, "reason": "already running"}

    def _run():
        from ..jobs import worker
        snap: dict = {"items": [], "categorize": None}
        order: dict[str, int] = {}
        wconn = tenancy.tenant_connect(tid)

        def _write():
            # MERGE, don't assign. sync_tenant writes {done,total} into this
            # same row so that any sync — cron included — can be reported as
            # live; a plain assignment here fires on every item callback and
            # wipes those keys within a second of them being written, so the
            # header chip could only ever say "syncing…" and never
            # "syncing 3 of 7".
            wconn.execute(
                "UPDATE job_progress "
                "SET progress=COALESCE(progress,'{}'::jsonb) || %s::jsonb, "
                "    updated_at=now() WHERE id='sync'", (jsonb(snap),))

        def _cb(stage, payload):
            if stage == "item":
                iid = payload["id"]
                if iid not in order:
                    order[iid] = len(snap["items"])
                    snap["items"].append(payload)
                else:
                    snap["items"][order[iid]] = payload
            elif stage == "categorize":
                snap["categorize"] = payload
            _write()

        try:
            results = worker.sync_tenant(tid, progress=_cb)
            # The advisory lock is the real mutex; the job_progress claim
            # above is only bookkeeping. When another door's sync got the
            # lock first, sync_tenant returns this sentinel having pulled
            # nothing — writing state='done' here would report a sync that
            # never happened as a success. Leave the row to the run that
            # actually holds the lock (every door marks progress into this
            # same row and settles its state when it finishes); merge a
            # note so /jobs/sync/status can say what happened.
            if results.get("_status") == "already-running":
                wconn.execute(
                    "UPDATE job_progress "
                    "SET progress=COALESCE(progress,'{}'::jsonb) || "
                    "%s::jsonb, updated_at=now() WHERE id='sync'",
                    (jsonb({"already_running": True}),))
                return
            ok = all(not v.startswith("error") for v in results.values())
            wconn.execute(
                "UPDATE job_progress SET state='done', progress=%s, "
                "updated_at=now() WHERE id='sync'",
                (jsonb({**snap, "ok": ok, "results": results}),))
        except Exception as e:                   # noqa: BLE001
            try:
                wconn.execute(
                    "UPDATE job_progress SET state='error', progress=%s, "
                    "updated_at=now() WHERE id='sync'",
                    (jsonb({**snap,
                            "error": f"{type(e).__name__}: {e}"}),))
            except Exception:                    # noqa: BLE001
                pass
        finally:
            wconn.close()

    threading.Thread(target=_run, daemon=True,
                     name=f"wizard-sync-{tid[:8]}").start()
    return {"ok": True, "started": True}


@router.get("/jobs/sync/status")
def jobs_sync_status(user: dict = Depends(_user())):
    """Live progress of the background sync started by /jobs/sync/start.

    When a settled run still shows transactions=0 (older workers reported
    only the per-run *added* delta), refresh counts from the ledger so the
    wizard does not claim empty banks after a successful first pull.
    """
    from ..engine.compat import as_dict
    from ..jobs import worker as _worker
    conn = _conn(user)
    try:
        row = conn.execute(
            "SELECT state, progress, started_at, updated_at "
            "FROM job_progress WHERE id='sync'").fetchone()
        if row is None:
            return {"state": "none", "progress": {}}
        progress = as_dict(row["progress"]) or {}
        items = progress.get("items") or []
        if items and row["state"] in ("done", "error"):
            for it in items:
                iid = it.get("id")
                if not iid or iid == "mx":
                    continue
                n = _worker._item_txn_count(conn, iid)
                # Prefer the ledger when it knows MORE than the snapshot —
                # older workers stored only this-run `added`, which under-
                # reports after a re-sync. Never clobber a higher snapshot
                # with a zero (test fakes / MX multi-member).
                if n > (it.get("transactions") or 0):
                    it["transactions"] = n
        return {"state": row["state"],
                "progress": progress,
                "started_at": row["started_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat()}
    finally:
        conn.close()


@router.post("/jobs/email", dependencies=[Depends(limit("jobs-email", 3, 600))])
def jobs_email_api(user: dict = Depends(_user())):
    """Build and send today's verdict email NOW (the daily email's task
    body, run inline — same recipients rules). Returns the subject line."""
    demoguard.deny(user)
    from ..jobs import worker
    try:
        # force_email: this is the explicit "email me now" button — it must
        # send the EMAIL regardless of the daily channel toggle, and must
        # NOT fire the SMS/push side effects or stamp the cadence heartbeat
        # (without this, a user whose daily email
        # is off but SMS is on would click "send test email" and silently
        # get an SMS and no email).
        subject = worker.email_tenant(user["tenant_id"], send_it=True,
                                      force_email=True)
    except worker.NoRecipients as e:
        # nothing was wrong with the mail — there was nobody to send it to,
        # and the button must say so instead of "Sent"
        raise HTTPException(409, str(e))
    except Exception as e:               # noqa: BLE001 — surface SMTP/config
        raise HTTPException(400, f"email failed — {type(e).__name__}: {e}")
    return {"ok": True, "subject": subject}


# ---- notification channels (SMS / web push) ------------------------
# Settings surface for the per-cadence delivery matrix's non-email channels:
# a tenant-level VERIFIED phone for SMS, per-browser push subscriptions, and
# channel availability (so the UI can say WHY a toggle is disabled).


@router.get("/notify")
def notify_status_api(user: dict = Depends(_user())):
    from .. import notify  # noqa: F401
    from ..notify import push as _push, push_native as _push_native, \
        sms as _sms
    from .app import _control_conn
    conn = _conn(user)
    try:
        from ..engine import budget
        cfg = budget.load_config(conn)
        sms_entitled = ext.gate.tier_allows(conn, "sms")
    finally:
        conn.close()
    # a restore can plant any shape under these; a non-dict reads as
    # "no number" rather than crashing the status page
    phone = cfg.get("notify_phone")
    if not isinstance(phone, dict):
        phone = {}
    pending = cfg.get("notify_phone_pending")
    if not isinstance(pending, dict):
        pending = {}
    with _control_conn() as cc:
        subs = cc.execute(
            "SELECT count(*) AS n FROM push_subscriptions "
            "WHERE user_id=%s AND dead_at IS NULL",
            (user["user_id"],)).fetchone()["n"]
        # phones with a live native push registration — tenant-wide, like
        # the delivery fan-out itself (notify/push_native.py)
        native = cc.execute(
            """SELECT count(*) AS n FROM device_tokens
               WHERE tenant_id=%s AND revoked_at IS NULL
                     AND expires_at > now() AND push_token IS NOT NULL
                     AND push_dead_at IS NULL""",
            (user["tenant_id"],)).fetchone()["n"]
        vapid_pub = None
        if _push.available():
            try:
                _priv, vapid_pub = _push.vapid_keys(cc)
            except Exception:                    # noqa: BLE001
                vapid_pub = None
    return {"sms_available": _sms.configured() and sms_entitled,
            "sms_entitled": sms_entitled,
            "phone": _sms.mask(phone.get("number")),
            "phone_verified": bool(phone.get("verified")),
            "phone_pending": _sms.mask(pending.get("number")),
            "push_available": _push.available() and vapid_pub is not None,
            "vapid_public_key": vapid_pub,
            "push_subscribed": subs > 0,
            # web push and phone push are separate channels: an instance with
            # no VAPID key can still reach the mobile apps through the relay,
            # so a phone-only household must not be told push is impossible
            "native_push_available": _push_native.available(),
            "native_push_devices": native}


@router.post("/notify/phone",
             dependencies=[Depends(limit("notify-phone", 5, 3600))])
def notify_phone_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """Set (or clear) the tenant's SMS number. Setting sends a 6-digit
    verification code to the number — summaries only ever go to a number
    that typed its code back (a typo must not leak a household's budget
    to a stranger)."""
    demoguard.deny(user)
    _owner_only(user)
    from ..engine import budget
    from ..notify import sms as _sms
    number = str(body.get("number") or "").strip().replace(" ", "")
    conn = _conn(user)
    try:
        if not number:                        # clear
            # Same lock as the send/verify paths below: dropping a phone
            # number is exactly when a
            # concurrent settings save must not put it back.
            with budget.config_txn(conn) as cfg:
                cfg.pop("notify_phone", None)
                cfg.pop("notify_phone_pending", None)
            return {"ok": True, "cleared": True}
        if not _sms.configured():
            raise HTTPException(400, "SMS isn't configured on this instance")
        # SMS is one of the features an installed gate can withhold. Block
        # verification up front so a tenant never spends a verification SMS
        # on a channel they cannot use. With no gate installed nothing is
        # withheld: the operator brings their own SMS provider.
        if not ext.gate.tier_allows(conn, "sms"):
            raise HTTPException(
                402, "Text alerts aren't enabled for this account — your daily "
                     "verdict and alerts arrive by email and push.")
        if not _sms.valid_e164(number):
            raise HTTPException(400, "phone must be E.164, e.g. +12175551234")
        # Consent is parsed BEFORE the rate limiters: a request
        # carrying no valid consent can never send, so letting it burn one of
        # the user's 3 verification SMS per day would let a malformed client
        # lock them out of their own opt-in without a single message going
        # out.
        programs = body.get("consent")
        if programs is True:
            programs = list(_sms.PROGRAMS)
        # isinstance, not truthiness: `consent: 1` — any truthy
        # non-list scalar — would reach `for p in 1` and raise TypeError, so a
        # scripted call would get a 500 instead of the 400 this branch exists to
        # give. `x or []` only rescues FALSY junk. Anything that is not a
        # list of program names is simply not consent.
        if not isinstance(programs, list):
            programs = []
        programs = [p for p in programs if p in _sms.PROGRAMS]
        if not programs:
            raise HTTPException(
                400, "SMS consent is required — tick at least one box saying "
                     "which texts you agree to receive")

        # SMS-pump / toll-fraud guard: the
        # per-IP limit is defeated by IP rotation across free accounts, so
        # ALSO cap by account (source-IP-independent) and cool down the
        # destination number, since every send is billed to the instance's
        # Twilio account.
        from .security import _account_limit
        if not _account_limit(("sms-acct", str(user["user_id"])), 3, 86400):
            raise HTTPException(429, "verification SMS limit reached for "
                                     "today — try again tomorrow")
        import hashlib
        if not _account_limit(
                ("sms-dest", hashlib.sha256(number.encode()).hexdigest()),
                3, 86400):
            raise HTTPException(429, "too many codes sent to that number "
                                     "recently")
        import secrets as _secrets
        code = f"{_secrets.randbelow(1_000_000):06d}"
        # TCPA / carrier evidence: the SPA
        # gates the send-code button on an explicit SMS-only consent checkbox,
        # and the timestamp of that affirmative action is recorded HERE, with
        # the number it applied to. A UI-only gate leaves nothing to show a
        # carrier — or a regulator — six months later. Stored, not asserted:
        # if consent is absent the request is refused rather than defaulted.
        # SPLIT per program: one box covering the verdict
        # AND the alerts is a single opt-in spanning two message programs,
        # which carriers reject. `consent` is the list of programs ticked; a
        # bare True is an older client and means both.
        # The whole blob
        # is written back, so a settings save that read BEFORE this one
        # silently drops the pending record — the user then gets a real,
        # unexpired code and is told it is "wrong or expired". config_txn
        # takes the row lock for the read-modify-write.
        with budget.config_txn(conn) as locked:
            locked["notify_phone_pending"] = {
                "number": number,
                "code_hash": hashlib.sha256(code.encode()).hexdigest(),
                "consent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "consent_programs": programs,
                "expires": (dt.datetime.now(dt.timezone.utc)
                            + dt.timedelta(minutes=10)).isoformat()}
    finally:
        conn.close()
    if not _sms.send(number, f"Oikonome verification code: {code}"):
        raise HTTPException(502, "couldn't send the verification SMS")
    return {"ok": True, "sent_to": _sms.mask(number)}


@router.post("/notify/phone/verify",
             dependencies=[Depends(limit("notify-verify", 10, 3600))])
def notify_phone_verify_api(user: dict = Depends(_user()),
                            body: dict = Body(...)):
    demoguard.deny(user)
    _owner_only(user)
    import hashlib
    from ..engine import budget
    code = str(body.get("code") or "").strip()
    conn = _conn(user)
    try:
        # Verify reads the pending record, promotes it and drops it
        # — a read-modify-write of the whole blob, so it takes the same lock
        # as the send path above. Without it a concurrent settings save can
        # resurrect the consumed pending record or lose the verified number.
        with budget.config_txn(conn) as cfg:
            pending = cfg.get("notify_phone_pending") or {}
            exp = pending.get("expires")
            expired = (not exp or dt.datetime.fromisoformat(exp)
                       < dt.datetime.now(dt.timezone.utc))
            if (not pending.get("number") or expired
                    or hashlib.sha256(code.encode()).hexdigest()
                    != pending.get("code_hash")):
                raise HTTPException(400, "wrong or expired code")
            # consent_at travels with the verified record: the pending row is
            # dropped below, and losing the consent timestamp there would
            # leave a verified number with no evidence of how it opted in.
            cfg["notify_phone"] = {"number": pending["number"],
                                   "verified": True,
                                   "consent_at": pending.get("consent_at"),
                                   "consent_programs":
                                       pending.get("consent_programs"),
                                   "verified_at": dt.datetime.now(
                                       dt.timezone.utc).isoformat()}
            cfg.pop("notify_phone_pending", None)
    finally:
        conn.close()
    return {"ok": True}


# Push endpoints are server-POSTed by the worker fan-out, so an
# attacker-chosen endpoint is an SSRF primitive.
# The guard: the host MUST belong to one of the real push
# services. An internal address (10.0.0.5, 169.254.169.254) or an
# arbitrary attacker host can't match these suffixes, so it can never be
# stored — and the delivery POST that pywebpush makes verifies TLS to the
# real service name, so a DNS rebind of a genuine push host doesn't help
# either. (A resolve-time IP check would flap whenever a known-good
# host's DNS is briefly unavailable; the allowlist is the
# robust gate.)
_PUSH_HOST_SUFFIXES = (
    ".googleapis.com",          # FCM (fcm.googleapis.com)
    ".push.apple.com",          # Safari / iOS
    ".notify.windows.com",      # WNS (Edge)
    ".push.services.mozilla.com",  # Firefox autopush
)


def _valid_push_endpoint(endpoint: str) -> bool:
    from urllib.parse import urlsplit
    p = urlsplit(endpoint)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not host:
        return False
    return any(host.endswith(s) for s in _PUSH_HOST_SUFFIXES)


@router.post("/notify/push/subscribe")
def push_subscribe_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """Store this browser's PushSubscription (upsert on endpoint — a
    re-subscribe refreshes keys instead of duplicating)."""
    demoguard.deny(user)
    from .app import _control_conn
    endpoint = str(body.get("endpoint") or "")
    keys = body.get("keys") or {}
    if not _valid_push_endpoint(endpoint) or not keys.get("p256dh") \
            or not keys.get("auth"):
        raise HTTPException(400, "bad subscription")
    # Reassigning user_id/tenant_id on ANY endpoint conflict would let a
    # posted endpoint string that belongs to someone else silently take the
    # row over. push_subscriptions has no RLS (control plane), so nothing
    # else stops it. The victim's alerts then die quietly: the row now carries the attacker's p256dh/auth, which
    # cannot decrypt on the victim's browser, so every send fails and the
    # subscription is marked dead. Silent denial of service on somebody
    # else's notifications, from knowing one URL.
    #
    # Takeover requires proof of possession — the same browser
    # re-subscribing presents the SAME keys, which is exactly the legitimate
    # case (a second person logging in on a shared browser profile). Knowing
    # only the endpoint proves nothing and is refused.
    with _control_conn() as conn:
        row = conn.execute(
            """INSERT INTO push_subscriptions
                 (user_id, tenant_id, endpoint, p256dh, auth, user_agent)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (endpoint) DO UPDATE SET
                 user_id=EXCLUDED.user_id, tenant_id=EXCLUDED.tenant_id,
                 p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth,
                 user_agent=EXCLUDED.user_agent,
                 dead_at=NULL
               WHERE push_subscriptions.user_id = EXCLUDED.user_id
                  OR (push_subscriptions.p256dh = EXCLUDED.p256dh
                      AND push_subscriptions.auth = EXCLUDED.auth)
               RETURNING id""",
            (user["user_id"], user["tenant_id"], endpoint,
             str(keys["p256dh"]), str(keys["auth"]),
             str(body.get("user_agent") or "")[:200])).fetchone()
    if row is None:
        raise HTTPException(
            409, "that push endpoint is registered to another account")
    return {"ok": True}


@router.post("/notify/push/unsubscribe")
def push_unsubscribe_api(user: dict = Depends(_user()),
                         body: dict = Body(...)):
    from .app import _control_conn
    with _control_conn() as conn:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE user_id=%s AND endpoint=%s",
            (user["user_id"], str(body.get("endpoint") or "")))
    return {"ok": True}


@router.post("/notify/test",
             dependencies=[Depends(limit("notify-test", 5, 3600))])
def notify_test_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """Send today's short verdict through one channel, right now."""
    demoguard.deny(user)
    _owner_only(user)
    from ..engine import budget
    from ..notify import push as _push, render as _short, sms as _sms
    from .app import _control_conn
    channel = str(body.get("channel") or "")
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
        # the test-send honors the same SMS tier gate, or it becomes a way
        # around it
        sms_ok = ext.gate.tier_allows(conn, "sms")
        from . import report
        d = report.gather(conn, _today(conn))
        short = _short.short_daily(d)
    finally:
        conn.close()
    if channel == "sms":
        if not sms_ok:
            raise HTTPException(
                402, "Text alerts aren't enabled for this account — alerts "
                     "arrive by email and push")
        phone = cfg.get("notify_phone") or {}
        if not (phone.get("verified") and phone.get("number")):
            raise HTTPException(400, "verify a phone number first")
        # the test send IS the summary text, so it rides the summary opt-in —
        # a number that only ticked the alerts box never receives one
        if not _sms.consented(phone, "summary"):
            raise HTTPException(
                400, "that number didn't opt in to budget-summary texts — "
                     "re-add it and tick that box to receive them")
        if not _sms.send(phone["number"], short):
            raise HTTPException(502, "SMS send failed — check the app log")
        return {"ok": True}
    if channel == "push":
        from ..notify import push_native as _native
        native_detail: dict = {}
        with _control_conn() as cc:
            n = _push.send_tenant(cc, user["tenant_id"],
                                  "Oikonome test", short)
            n += _native.send_tenant(cc, user["tenant_id"], "test",
                                     detail=native_detail)
        if not n:
            # a registered phone whose platform leg isn't live is a
            # CONFIG story, not an account one — say the true reason
            # instead of blaming the sign-in
            if (native_detail.get("targets")
                    and native_detail.get("unconfigured")):
                raise HTTPException(
                    400, "your device is registered, but this server has "
                         "no push relay configured "
                         "(OIKONOME_PUSH_RELAY_URL) — native pushes "
                         "can't be delivered")
            if (native_detail.get("targets")
                    and native_detail.get("unavailable")):
                legs = ", ".join(native_detail["unavailable"])
                raise HTTPException(
                    400, f"your device is registered, but this server's "
                         f"push relay can't deliver to it yet "
                         f"({legs} isn't live)")
            raise HTTPException(400, "no live push subscription — enable "
                                     "push in this browser or sign in from "
                                     "the mobile app first")
        return {"ok": True, "delivered": n}
    raise HTTPException(400, "channel must be sms or push")


# ---- consented support access ---------------------------------------
# A tenant grants a time-boxed, revocable window during which an operator's
# data-touching console actions (export today; any future data view) are
# permitted-and-audited. Default is NO access — the console shows no tenant
# data without this. Owner-only; the grant rides the tenant's own session.

@router.get("/support-access")
def support_access_status_api(user: dict = Depends(_user())):
    from .. import tenant_export as _exp
    from .app import _control_conn
    with _control_conn() as cc:
        st = _exp.consent_status(cc, user["tenant_id"])
    return {"granted": st is not None,
            "expires_at": st["expires_at"].isoformat() if st else None,
            "reason": (st or {}).get("reason")}


@router.post("/support-access",
             dependencies=[Depends(limit("support-access", 10, 3600))])
def support_access_grant_api(user: dict = Depends(_user()),
                             body: dict = Body(...)):
    """Grant support access for a bounded window (hours, 1–168, default
    24). Revokes any prior live grant first so there is at most one."""
    demoguard.deny(user)
    _owner_only(user)
    # Session alone must not open a support window (stolen cookie
    # → operator data access). Same posture as invites/tokens/passkeys.
    # And a durable grant (up to 168h operator data access)
    # also owes a LIVE TOTP when enrolled — password+session without the
    # authenticator must not be enough to open the support door.
    from .app import _control_conn, _require_elevation
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    try:
        hours = int(body.get("hours", 24))
    except (TypeError, ValueError):
        raise HTTPException(400, "hours must be a number")
    if not 1 <= hours <= 168:
        raise HTTPException(400, "hours must be 1–168")
    reason = str(body.get("reason") or "")[:200]
    with _control_conn() as conn:
        conn.execute(
            "UPDATE support_consents SET revoked_at=now() "
            "WHERE tenant_id=%s AND revoked_at IS NULL", (user["tenant_id"],))
        conn.execute(
            "INSERT INTO support_consents (tenant_id, granted_by, reason, "
            "expires_at) VALUES (%s, %s, %s, now() + make_interval(hours => %s))",
            (user["tenant_id"], user["user_id"], reason, hours))
    return {"ok": True, "hours": hours}


@router.post("/support-access/revoke")
def support_access_revoke_api(user: dict = Depends(_user())):
    demoguard.deny(user)
    _owner_only(user)
    from .app import _control_conn
    with _control_conn() as conn:
        conn.execute(
            "UPDATE support_consents SET revoked_at=now() "
            "WHERE tenant_id=%s AND revoked_at IS NULL", (user["tenant_id"],))
    return {"ok": True}
