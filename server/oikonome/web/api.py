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
import contextlib
import datetime as dt
import decimal
import functools
import json
import os
from ..envnum import env_flag
from .. import ext
import re
import threading as _threading
import uuid
from typing import Annotated

import psycopg

from fastapi import (APIRouter, Body, Depends, File, Form, HTTPException,
                     Query, Request, UploadFile)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BeforeValidator
from starlette.concurrency import run_in_threadpool

from ..db import tenancy
from ..engine import activity as _activity
from ..engine import assistant
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


# A reader can cut one cell into several: Excel in a locale whose list
# separator is ';' (most of continental Europe, much of Latin America)
# splits a .csv on ';', a tab splits it in a tab-separated read, and a
# line break ends the record in a reader that does not honour the quote
# around it (in a ';' read the quote sits mid-field, so it opens nothing).
# A double quote protects only a field it OPENS, so quoting the cell does
# not stop the piece after any of these from being read as a cell of its
# own.
_CSV_PIECE_BREAK = frozenset(";\t\r\n")
_CSV_INVISIBLE = frozenset(_CSV_INVISIBLE_LEAD)
_CSV_FORMULA = frozenset(_CSV_FORMULA_LEAD)


def _csv_piece_safe(v: str) -> str:
    """Put a quote after every piece break whose piece would open with a
    formula character once its invisible lead is trimmed.

    One pass from right to left, carrying "the next visible character is
    a formula character". A tab, CR or line feed is both a break and
    invisible, so a run of them does not clear that flag; the quote put in
    after the break nearest the formula character does, because it is
    then the first visible character of every piece that reaches it. A
    lookahead regex does the same job in time quadratic in the length of a
    tab run, and the regex engine holds the interpreter lock the whole
    while — one restored note of tabs would stall every household."""
    if not ("=" in v or "+" in v or "-" in v or "@" in v):
        return v
    if not (";" in v or "\t" in v or "\n" in v or "\r" in v):
        return v
    out = []
    lead = False
    changed = False
    for ch in reversed(v):
        if lead and ch in _CSV_PIECE_BREAK:
            out.append("'")
            lead = False
            changed = True
        out.append(ch)
        if ch in _CSV_FORMULA:
            lead = True
        elif ch not in _CSV_INVISIBLE and ch != '"':
            # a double quote keeps the flag: csv doubles it, and a reader
            # that splits on the break opens a quoted field there, closes it
            # at the doubled quote and reads what follows as the cell
            lead = False
    if not changed:
        return v
    out.reverse()
    return "".join(out)


class _SafeCsvWriter:
    """csv.writer whose text cells cannot open a formula after a ';', a
    tab or a line break: the piece is prefixed with a quote, the same mark
    _csv_safe puts on a cell's own head. Only text that both holds a break
    and has a formula character after it changes; a plain note, a signed
    amount ("-12.50") and a date are written as they were."""

    def __init__(self, w):
        self._w = w

    @staticmethod
    def _cell(v):
        if isinstance(v, str):
            return _csv_piece_safe(v)
        return v

    def writerow(self, row):
        return self._w.writerow([self._cell(v) for v in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def _csv_writer(buf):
    """The one way this module writes a CSV: every text cell is also safe
    in a spreadsheet that splits on ';' (see _SafeCsvWriter). Callers still
    run free text through _csv_safe for the cell's own head."""
    import csv as _csvmod
    return _SafeCsvWriter(_csvmod.writer(buf))


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


# The largest money figure a person may type. Far past anything a
# household holds and far inside what a double carries to the cent — and,
# unlike the float ceiling, small enough that any realistic number of such
# values still SUM to a finite total. Two balances near 1e308 are each
# finite, but net worth, the runway and every other total over them
# overflow to infinity (a 500 under allow_nan=False, or Postgres's float8
# "value out of range") until someone finds and edits the balance.
_MONEY_MAX = 1e12


def _money_float(raw, field: str) -> float:
    """Parse a user money string; reject NaN/Infinity — they parse fine
    but poison DOUBLE PRECISION columns and then permanently 500 every
    JSON response containing the row (allow_nan=False) — and anything
    past _MONEY_MAX, which is finite alone but overflows the totals."""
    import math
    try:
        v = float(str(raw).replace(",", "").replace("$", "").strip() or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be a number")
    if not math.isfinite(v):
        raise HTTPException(400, f"{field} must be a real number")
    if abs(v) > _MONEY_MAX:
        raise HTTPException(400, f"{field} is too large")
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
    detail = {"proposal": kind, "income": kind == "income", "pid": pid}
    if r.get("note"):
        # the apply changed something the person did not ask for (a pin
        # cleared); the log keeps the explanation next to the decision
        detail["note"] = r["note"]
    _activity.record(conn, user, "bill", act, target=payee, label=payee,
                     detail=detail)


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


def _budgets_set(conn) -> bool:
    """Has the household a budget of its own? (budget.budgets_set — the
    stored row, never the provisional seed.)"""
    from ..engine import budget
    return budget.budgets_set(conn)


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
        from .security import claim_netloc
        # A Referer that does not parse (None) cannot name this host, so it
        # is refused like any other foreign one rather than raising a 500.
        netloc, h = claim_netloc(ref), request.headers.get("host", "")
        if netloc is None or (netloc and h and netloc.lower() != h.lower()):
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
    from ..engine import budget
    from . import report, todayview
    if date:
        day = _iso_date(date, "date must be an ISO date (YYYY-MM-DD)")
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
            # `paced_budget`: the share of the budget the month may spend
            # (the whole of it unless the ledger began inside the month) —
            # what a money-map row measures its "$X left" against, the
            # tiles' own money. month_budget stays for the plan line.
            out["paced_budget"] = round(
                todayview.paced_budget(st, v["month_budget"]), 2)
            out["children"] = [{"name": c["name"],
                                "actual": round(c["actual"], 2),
                                "expected": round(c["expected"], 2),
                                "month_budget": round(c["month_budget"], 2),
                                "paced_budget": round(todayview.paced_budget(
                                    st, c["month_budget"]), 2)}
                               for c in v["children"]]
            out["display_actual"] = round(v["display_actual"], 2)
            out["display_expected"] = round(v["display_expected"], 2)
            out["remaining_budget"] = round(v["remaining_budget"], 2)
            out["paced_remaining_budget"] = round(
                todayview.paced_budget(st, v["remaining_budget"]), 2)
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
        # the hero's "$X of $Y left": the month's spendable share (the
        # whole month unless the ledger began inside it) and what is left
        # of it — composed once in month_status, read by the web, the app
        # and both email renders, equal to the tiles times the days left
        "month_share": round(st["month_share"], 6),
        "variable_budget_paced": round(st["variable_budget_paced"], 2),
        "month_remaining_paced": round(st["month_remaining_paced"], 2),
        # "This month's plan" card (today_body.html) — all straight
        # from budget.month_status
        "income": (round(st["income"], 2)
                   if st.get("income") is not None else None),
        "plan_surplus": (round(st["plan_surplus"], 2)
                         if st.get("plan_surplus") is not None else None),
        "avg_bills": round(st.get("avg_bills") or 0, 2),
        # this month's own bill schedule (what is due this month, not the
        # evened average) — the daily email's "bills due" figure
        "scheduled_month": round(st.get("scheduled_month") or 0, 2),
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
        # "Scheduled but not yet posted" line: [payee, amount, due,
        # withdrawn_on] rows — withdrawn_on is the date the bank feed took
        # the charge back (null when it never posted)
        "overdue_unpaid": [[p, round(a, 2), due, gone]
                           for p, a, due, gone in budget.overdue_entries(st)],
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
                        bool(st["runway"].get("balance_unreliable")),
                    # no checking account to start cash from: headroom and
                    # forecast are null, and the client asks for the account
                    "no_checking": bool(st["runway"].get("no_checking"))}
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
# — the same functions the monthly/yearly emails render, so API and email
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


def _iso_date(v: str, msg: str) -> dt.date:
    """A caller's ISO date, parsed strictly AND inside the years the app
    can read (1900-2100, as `_ck_ym`). `date.fromisoformat` alone takes
    0001-01-01 and 9999-12-31: stored, they poison every later reader
    (goal pacing, the receipt matcher's day window, `as_date`), and a
    day stepped past either end of the calendar raises OverflowError —
    a 500, or a write that commits half way. Refused here as a 400."""
    try:
        d = dt.date.fromisoformat(v)
    except (TypeError, ValueError):
        raise HTTPException(400, msg)
    if not 1900 <= d.year <= 2100:
        raise HTTPException(400, msg)
    return d


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


def _relay_default_from(relay_host: str) -> str | None:
    """The From for a relay saved with neither a username nor a From:
    no-reply at the host name in OIKONOME_BASE_URL, else at the relay's
    own host name. None when neither is a name (an IP address is not a
    mail domain)."""
    import ipaddress
    from urllib.parse import urlsplit
    for host in ((urlsplit(os.environ.get("OIKONOME_BASE_URL", "")).hostname
                  or ""), relay_host or ""):
        host = host.strip().strip("[]").lower()
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
            continue
        except ValueError:
            pass
        addr = f"no-reply@{host}"
        if mailguard.valid_address(addr):
            return addr
    return None


def _category_long_ok(user: dict, name: str) -> bool:
    """May a name over the length cap be written anyway? Only when the
    household already uses it. Older versions, and imports, stored longer
    names, and the pickers keep offering them; refusing one would freeze
    the household's own category in place, with renaming it shorter the
    only way out. The cap is for names that do not exist yet."""
    conn = _conn(user)
    try:
        return bool(data.category_in_use(conn, name))
    finally:
        conn.close()


def _category_text(body: dict, *keys: str, user: dict) -> str:
    """A category NAME a request is about to write: _body_text plus the
    one length every category door keeps for a NEW name. A category is
    stored on every row, rule and split it touches and rendered in every
    picker and email, so a name with no bound is a cheap way to bloat the
    ledger by the kilobyte per row."""
    v = _body_text(body, *keys)
    if len(v) > data.CATEGORY_MAX_LEN and not _category_long_ok(user, v):
        raise HTTPException(
            400, f"a category name is at most {data.CATEGORY_MAX_LEN} "
                 f"characters")
    return v


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
    from . import report, todayview
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        today = _today(conn)
        budgets_set = _budgets_set(conn)
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
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        # the 304 restates the cache policy: a bare one would pick up the
        # API-wide no-store and tell the client to drop the copy it holds
        return Response(status_code=304, headers=headers)
    return Response(json.dumps(body), media_type="application/json",
                    headers=headers)


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
        # a saved budget, never the loader's unsaved first-month guess —
        # the backfill refuses to freeze that guess, so it is not offered
        has_budget = budget.budgets_set(conn)
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
            "budget_set": bool(has_budget
                               and ((cfg.get("food_monthly") or 0) > 0
                                    or (cfg.get("other_monthly") or 0) > 0))}


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
    _recurring_stamper(conn)(rows)


def _recurring_stamper(conn):
    """_stamp_recurring with the active bills read once: the returned
    function stamps any number of row lists against the same bills, so a
    download stamping its rows a slice at a time reads the bills table
    once, not once per slice."""
    from ..engine import budget
    from ..engine.compat import as_dict
    bills = []
    active = conn.execute("SELECT payee, merchant, amount, raw "
                          "FROM bills WHERE active=1").fetchall()
    # each bill's identity, so the pill follows a renamed payee
    ids = budget.bill_merchant_ids(conn, active) if active else {}
    for b in active:
        raw = as_dict(b["raw"])
        bills.append((b["payee"],
                      budget.merchant_matcher(b["merchant"],
                                              budget._key_token(b["payee"]),
                                              ids.get(b["payee"] or "")),
                      raw.get("bill_type", "occurrence"),
                      (b["amount"] or 0) > 0,          # income series
                      # the current amount and the ones before it, so a
                      # price change keeps the earlier payments marked
                      budget.bill_amount_bands(b["amount"], raw)))

    def stamp(rows: list[dict]) -> None:
        for r in rows:
            r["recurring_bill"] = None
            if not bills:
                continue
            text = (r.get("payee") or "").lower()
            amt = r.get("amount") or 0
            # the row's tokens and merchant id once, not once per bill:
            # every matcher would otherwise re-split the same text and
            # re-format the same id for each bill it is tested against
            toks = budget._tokens(text)
            mid = budget.row_merchant_id(r)
            for payee, match, btype, income, bands in bills:
                if not match(text, toks, mid):
                    continue
                if btype == "envelope":
                    if amt > 0:
                        r["recurring_bill"] = payee
                        break
                elif ((amt < 0 if income else amt > 0)
                      and budget.amount_fits(bands, amt, r.get("date"))):
                    r["recurring_bill"] = payee
                    break
    return stamp


def _decorated(conn, rows, chunk: int):
    """The ledger rows as the page renders them: serialised, with the
    category's 'why' and the bill pill. With `chunk` the rows arrive as a
    generator of slices (search_transactions' chunked form) and come back
    the same way, each slice decorated only as it is read."""
    if not chunk:
        out = [_ser(r) for r in rows]
        for o in out:
            o["category_why"] = _category_why(o)
        _stamp_recurring(conn, out)
        return out
    stamp = _recurring_stamper(conn)

    def _gen():
        for part in rows:
            out = [_ser(r) for r in part]
            for o in out:
                o["category_why"] = _category_why(o)
            stamp(out)
            yield out
    return _gen()


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


def _ledger_first(conn, *, budget_view: bool = True):
    """The ledger's first day. `budget_view` leaves out the accounts the
    budget excludes, the way the month's pace reads it; a raw search can
    list those accounts' rows too, so it counts from the first row of any
    account."""
    from ..engine import budget as _budget
    excluded = (_budget.excluded_account_ids(_budget.load_config(conn))
                if budget_view else None)
    return _budget.ledger_start(conn, excluded)


def _per_seen_day(total, windows):
    """Average daily spend over the days a ledger has seen of one or more
    months, each read through its pace window (budget.pace_window,
    specs/short-ledger-pacing.md): from the later of the 1st and the
    ledger's first day, through the day read on (today included) or month
    end once it is closed — the days the Why's "running $X/day" divides
    by. Days before the ledger are no data, not days of no spending. A
    month the ledger had not reached adds nothing. (None, None) when
    nothing was spent or no day was seen."""
    if not total:
        return None, None
    days = sum(w["seen_days"] for w in windows if w["paced_days"] > 0)
    if days <= 0:
        return None, None
    return round(total / days, 2), days


def _month_windows(first, year: int, months, today: dt.date) -> list[dict]:
    """The pace windows of the named months of `year` up to `today`: the
    month of `today` live, earlier ones closed, later ones left out."""
    from ..engine import budget as _budget
    out = []
    for m in months:
        if (year, m) > (today.year, today.month):
            continue
        closed = (year, m) < (today.year, today.month)
        out.append(_budget.pace_window_for(
            first, dt.date(year, m, 1) if closed else today, closed=closed))
    return out


def _json_default(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (uuid.UUID, set, frozenset)):
        return str(v) if isinstance(v, uuid.UUID) else sorted(v)
    raise TypeError(f"not serialisable: {type(v).__name__}")


def _flat_json(fn):
    """Serialise a handler's dict straight to JSON. FastAPI's encoder
    walks every value of a payload to make it JSON-safe; the rows the
    ledger doors hand back are already flat (`_ser` writes ISO dates),
    and that walk over a month of them cost more than the query."""
    @functools.wraps(fn)
    def wrapped(*a, **kw):
        out = fn(*a, **kw)
        if not isinstance(out, dict):
            return out
        return Response(json.dumps(out, default=_json_default, allow_nan=False),
                        media_type="application/json")
    return wrapped


@router.get("/transactions")
@_flat_json
def transactions(user: dict = Depends(_user()), y: int = 0, m: int = 0,
                 q: str = "", acct: str = "", cat: str = "",
                 date_from: str = "", date_to: str = "", page: int = 1,
                 reimb: int = 0, owner: str = "", scope: str = "all",
                 channel: str = "", checks: int = 0, bucket: str = "",
                 as_of: str = "", counted: int = 0):
    conn = _conn(user)
    try:
        return _transactions_payload(conn, y=y, m=m, q=q, acct=acct, cat=cat, date_from=date_from, date_to=date_to, page=page, reimb=reimb, owner=owner, scope=scope, channel=channel, checks=checks, bucket=bucket, as_of=as_of, counted=counted)
    finally:
        conn.close()


# The download door answers with the page's whole row set at once; a
# household's entire history is a few tens of thousands of rows, and a
# wider answer than this is a sign of a filter that means "everything",
# which the full export (Settings) is for.
TRANSACTIONS_EXPORT_CAP = 50_000

# the columns a person reads, in the order the page shows them; the
# ledger's own keys stay out (ids are last, for anyone matching rows back)
_EXPORT_COLUMNS = (
    ("date", "date"), ("payee", "payee"), ("amount", "amount"),
    ("category", "category"), ("account", "account"), ("mask", "account_mask"),
    ("pending", "pending"), ("note", "note"), ("owner", "owner"),
    ("entity", "business"), ("reimb", "reimbursable"),
    ("payment_channel", "channel"), ("check_number", "check_number"),
    ("location_city", "city"), ("location_region", "region"),
    ("item_summary", "items"), ("recurring_bill", "bill"),
    ("bank_text", "bank_text"), ("id", "id"),
)


# How many rows a download reads, decorates and writes out at a time: big
# enough that the per-slice query is not the cost, small enough that a
# slice of the wide ledger row is a few megabytes rather than the whole
# history.
_EXPORT_CHUNK = 1_000

# Downloads being built. One per household — a second click (or CSV then
# XLSX) while the first is still being written would only build the same
# file twice — and a few for the whole process, so households exporting
# at the same moment queue at the door instead of stacking up their
# working sets in one process. A slot covers BUILDING the file only, which
# the server does at its own pace; the transfer that follows holds no slot
# and no connection, so a client that reads slowly can neither starve the
# shared connection pool nor keep other households at the door. A slot
# that outlives _EXPORT_SLOT_TTL is treated as abandoned, so a leak can
# never lock a household out.
# The gate lives in this process because the server is one process:
# `oikonome serve` starts a single uvicorn worker and the image runs
# nothing else. Running several workers would need a database-backed
# per-household lock here instead.
_EXPORT_MAX = 2
_EXPORT_SLOT_TTL = 15 * 60
_EXPORT_SLOTS: dict[object, tuple[str, float]] = {}
_EXPORT_SLOTS_LOCK = _threading.Lock()
# A built file stays in memory up to this size and spills to a temporary
# file past it, so a whole history is never held in memory at once; it is
# sent in pieces of _EXPORT_SEND_PIECE.
_EXPORT_SPOOL_MEMORY = 4 * 1024 * 1024
_EXPORT_SEND_PIECE = 64 * 1024
# Files built and not yet fully sent. The slot is given back once a file
# is built, but the file itself lives until the client has read it or
# left, and how fast that happens is up to the client — so without a
# bound of their own, readers that stall after the headers would pile up
# spooled files (4 MiB of memory each, then disk) in the one process
# every household shares. A household may hold _EXPORT_HELD_PER_HOUSEHOLD
# of them and the process _EXPORT_HELD_MAX; past that the next download
# is refused up front, before anything is built. The hold is given back
# when the send ends, however it ends, and no send may run longer than
# _EXPORT_SEND_MAX_S: a reader that takes the headers and then stalls is
# cut off and its place given back, so two stalled households cannot hold
# the door shut for everyone else. _EXPORT_HELD_TTL is only a safety net
# behind that deadline, and the process cap is several households' worth.
_EXPORT_HELD_MAX = 16
_EXPORT_HELD_PER_HOUSEHOLD = 2
_EXPORT_SEND_MAX_S = 20 * 60
_EXPORT_HELD_TTL = 2 * _EXPORT_SEND_MAX_S
_EXPORT_HELD: dict[object, tuple[str, float]] = {}


def _export_slot(tenant_id: str):
    """Take a download slot for this household, or answer 429. Returns
    the release function (safe to call more than once)."""
    import time as _time
    now = _time.monotonic()
    token = object()
    tenant_id = str(tenant_id)
    with _EXPORT_SLOTS_LOCK:
        for k, (_t, began) in list(_EXPORT_SLOTS.items()):
            if now - began > _EXPORT_SLOT_TTL:
                del _EXPORT_SLOTS[k]
        if any(t == tenant_id for t, _b in _EXPORT_SLOTS.values()):
            raise HTTPException(429, "an export is already running — wait "
                                     "for it to finish, then try again")
        if len(_EXPORT_SLOTS) >= _EXPORT_MAX:
            raise HTTPException(429, "other downloads are being prepared "
                                     "right now — try again in a minute")
        _EXPORT_SLOTS[token] = (tenant_id, now)

    def release():
        with _EXPORT_SLOTS_LOCK:
            _EXPORT_SLOTS.pop(token, None)
    return release


def _export_hold(tenant_id: str):
    """Take a place for one built-but-unsent download file, or refuse:
    429 when this household already holds its share, 503 when the
    process is full. Returns the release function (safe to call more than
    once)."""
    import time as _time
    now = _time.monotonic()
    token = object()
    tenant_id = str(tenant_id)
    with _EXPORT_SLOTS_LOCK:
        for k, (_t, began) in list(_EXPORT_HELD.items()):
            if now - began > _EXPORT_HELD_TTL:
                del _EXPORT_HELD[k]
        mine = sum(1 for t, _b in _EXPORT_HELD.values() if t == tenant_id)
        if mine >= _EXPORT_HELD_PER_HOUSEHOLD:
            raise HTTPException(
                429, "earlier downloads are still being sent — let them "
                     "finish, then try again", headers={"Retry-After": "30"})
        if len(_EXPORT_HELD) >= _EXPORT_HELD_MAX:
            raise HTTPException(
                503, "other downloads are being sent right now — try again "
                     "in a minute", headers={"Retry-After": "30"})
        _EXPORT_HELD[token] = (tenant_id, now)

    def release():
        with _EXPORT_SLOTS_LOCK:
            _EXPORT_HELD.pop(token, None)
    return release


class _ClosingStream(StreamingResponse):
    """A streamed download that closes its body generator the moment the
    response ends, however it ends.

    Starlette only stops iterating when the client leaves: the body's
    generator is left suspended at its last `yield`, and its `finally` —
    which gives back what the body holds (the built file) — would run only
    when the garbage collector reached it. So the close is done here, in
    the thread pool, shielded from the cancellation that a disconnect
    delivers."""

    def __init__(self, content, close, **kw):
        super().__init__(content, **kw)
        self._close_body = close

    async def __call__(self, scope, receive, send):
        import anyio
        try:
            # a send has a deadline: a client that stalls after the headers
            # is cut off here, and the finally below gives the file back
            with anyio.fail_after(_EXPORT_SEND_MAX_S):
                await super().__call__(scope, receive, send)
        except TimeoutError:
            import logging
            logging.getLogger("oikonome.api").info("export send cut off after %ss: the client stalled",
                                                    _EXPORT_SEND_MAX_S)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._close_body)


@router.get("/transactions/export")
def transactions_export(request: Request, user: dict = Depends(_user()),
                        format: str = "csv",
                        y: int = 0, m: int = 0, q: str = "", acct: str = "",
                        cat: str = "", date_from: str = "", date_to: str = "",
                        reimb: int = 0, owner: str = "", scope: str = "all",
                        channel: str = "", checks: int = 0, bucket: str = "",
                        as_of: str = "", counted: int = 0):
    """The ledger page as a file: the same filters GET /api/transactions
    takes, the same rows it would list (every page of them), as CSV or a
    one-sheet .xlsx. Amounts are signed the way the page reads them —
    money in positive, money out negative.

    The filters are matched once up front (so a bad filter is still a
    plain 400), then the rows are read, decorated and written a slice at a
    time on one connection into a spooled file (memory, then disk), so a
    whole history is never held in memory at once. The connection and the
    household's download slot are given back once the file is built,
    BEFORE the first byte is sent: the connection is pooled and shared by
    every household, and how fast a download is read is up to the client.
    The built file is counted until the send ends (_export_hold), so
    readers that stall cannot pile files up in the process."""
    _same_site_download(request)
    if format not in ("csv", "xlsx"):
        raise HTTPException(400, "format must be csv or xlsx")
    release = _export_slot(user["tenant_id"])
    try:
        unhold = _export_hold(user["tenant_id"])
    except BaseException:
        release()
        raise
    conn = None
    try:
        conn = _conn(user)
        payload = _transactions_payload(
            conn, y=y, m=m, q=q, acct=acct, cat=cat, date_from=date_from,
            date_to=date_to, page=1, reimb=reimb, owner=owner, scope=scope,
            channel=channel, checks=checks, bucket=bucket, as_of=as_of,
            counted=counted, per_page=TRANSACTIONS_EXPORT_CAP,
            chunk=_EXPORT_CHUNK)
    except BaseException:
        if conn is not None:
            conn.close()
        release()
        unhold()
        raise
    rows = payload.get("rows") or []
    # the month browse answers with one list; a search with slices
    batches = iter([rows]) if isinstance(rows, list) else rows
    total = payload.get("total")

    def _value(r, key):
        v = r.get(key)
        if key == "amount":
            return -v if isinstance(v, (int, float)) else v
        if key in ("pending", "reimb"):
            return "yes" if v else ""
        if isinstance(v, (int, float)) or v is None:
            return v
        return _csv_safe(str(v))

    header = [label for _k, label in _EXPORT_COLUMNS]

    def _tables():
        written = 0
        for part in batches:
            written += len(part)
            yield [[_value(r, k) for k, _l in _EXPORT_COLUMNS]
                   for r in part]
        if total is not None and total > written:
            yield [[f"{total - written} more rows were not included "
                    f"(a download holds up to {TRANSACTIONS_EXPORT_CAP:,}); "
                    "narrow the filters or use the full export"]]

    try:
        # name the file after what was asked for, the way a person would
        if payload.get("mode") == "month":
            stem = f"transactions-{payload['year']}-{payload['month']:02d}"
        else:
            bits = [b for b in (q.strip(), cat, bucket, acct and "account",
                                date_from, date_to) if b]
            stem = "transactions-" + _filename_safe("-".join(bits) or "search",
                                                    "search")
        import tempfile as _tempfile
        out = _tempfile.SpooledTemporaryFile(max_size=_EXPORT_SPOOL_MEMORY)
        try:
            if format == "csv":
                import io as _io
                buf = _io.StringIO()
                w = _csv_writer(buf)
                w.writerow(header)
                out.write(buf.getvalue().encode())
                for table in _tables():
                    buf.seek(0)
                    buf.truncate()
                    w.writerows(table)
                    out.write(buf.getvalue().encode())
                media, ext_ = "text/csv", "csv"
            else:
                from . import xlsx as _xlsx
                for part in _xlsx.stream_workbook("Transactions", header,
                                                  _tables()):
                    out.write(part)
                media, ext_ = _xlsx.MEDIA_TYPE, "xlsx"
            out.seek(0)
        except BaseException:
            out.close()
            raise
    except BaseException:
        unhold()
        raise
    finally:
        # the file is built (or the build failed): nothing the client does
        # from here holds the shared connection or the household's slot
        try:
            try:
                if hasattr(batches, "close"):
                    batches.close()
            finally:
                conn.close()
        finally:
            release()

    def _send():
        try:
            while True:
                piece = out.read(_EXPORT_SEND_PIECE)
                if not piece:
                    return
                yield piece
        finally:
            # closing a spooled file that spilled to disk removes it
            try:
                out.close()
            finally:
                unhold()
    import itertools as _it
    body = _send()
    # Start the generator here, so its cleanup is armed: a generator that
    # was never started runs no `finally` when it is discarded, and a
    # client that leaves before the first byte would otherwise leave the
    # built file open until the collector reached it.
    try:
        first = next(body)
    except BaseException:
        body.close()
        out.close()
        unhold()
        raise
    return _ClosingStream(_it.chain([first], body), body.close,
                          media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{stem}.{ext_}"',
        "Cache-Control": "no-store"})


def _transactions_payload(conn, *, y: int = 0, m: int = 0, q: str = "",
                          acct: str = "", cat: str = "", date_from: str = "",
                          date_to: str = "", page: int = 1, reimb: int = 0,
                          owner: str = "", scope: str = "all",
                          channel: str = "", checks: int = 0,
                          bucket: str = "", as_of: str = "", counted: int = 0,
                          per_page: int = 100, chunk: int = 0) -> dict:
    """The ledger page's answer for one set of filters: month browse, a
    search, a bucket's or a counted category's row set. The page reads it
    through GET /api/transactions a page at a time; the download door
    reads the same answer with `per_page` wide enough for the whole set,
    so what is exported is exactly what the page would list. `chunk`
    turns a search's "rows" into a generator of row slices read on
    `conn` (see search_transactions); the month browse, one month of
    rows, stays a list either way."""
    # bound like _ck_ym bounds y/m: an unbounded page multiplies into an
    # OFFSET past bigint inside the driver (NumericValueOutOfRange → 500)
    page = min(max(1, page), 10_000_000)
    # which rows the ledger lists: household + business (default, business
    # rows labelled by entity), household only, or business only
    if scope not in ("all", "personal", "business"):
        raise HTTPException(400, "scope must be all, personal or business")
    if channel and channel not in ("in store", "online", "other"):
        raise HTTPException(400, "channel must be in store, online or other")
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
            mst = _lenses.month_state(conn, yy, mm, read_on)
            info = _budget.bucket_ledger(mst, bucket)
        if info is None:
            raise HTTPException(400, "unknown bucket")
        rows, total, amount_sum, amazon_items, spend, account_hits = \
            data.search_transactions(conn, "", ids=info["txn_ids"],
                                     page=max(1, page), per_page=per_page, scope="all",
                                     chunk=chunk)
        out = _decorated(conn, rows, chunk)
        avg = (round(spend["sum"] / spend["count"], 2)
               if spend["count"] else None)
        # A bucket is one month's (or one year's) number, and its daily
        # pace is THAT number — the figure the label wore, reimbursements
        # netted and envelope overflow counted only past the envelope —
        # over the days it was counted across, up to the day it was read
        # on. The listed rows' raw sum is neither: an overflowing
        # purchase is listed whole. A year's figure sums only its
        # budgeted months, so its days are those months' days, not every
        # day since January 1. Either way the days are the ones the
        # ledger has seen: a ledger that began on the 10th divides from
        # the 10th, the way the Why's "running $X/day" does.
        if year_scope:
            per_day, per_day_days = _per_seen_day(
                info["amount"], _month_windows(
                    _ledger_first(conn), yy, info.get("months") or [],
                    read_on))
        else:
            per_day, per_day_days = _per_seen_day(info["amount"],
                                                  [mst["pace"]])
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
            mst = _lenses.month_state(conn, yy, mm, today)
            info = _lenses.month_category_ledger(mst, cat)
        rows, total, _gross, amazon_items, spend, account_hits = \
            data.search_transactions(conn, "", ids=info["txn_ids"],
                                     page=max(1, page), per_page=per_page, scope="all",
                                     chunk=chunk)
        out = _decorated(conn, rows, chunk)
        avg = (round(spend["sum"] / spend["count"], 2)
               if spend["count"] else None)
        # the label's own number per day over the days of the month or
        # year it counts that the ledger has seen
        if date_from:
            windows = _month_windows(_ledger_first(conn), int(yr),
                                     range(1, 13), today)
        else:
            windows = [mst["pace"]]
        per_day, per_day_days = _per_seen_day(info["amount"], windows)
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
                reimb_only=bool(reimb), page=max(1, page), per_page=per_page, scope=scope,
                channel=channel or None, checks_only=bool(checks),
                chunk=chunk)
        out = _decorated(conn, rows, chunk)
        # A total answers "how much have I given them"; the average
        # answers "what does a visit cost", which is the question a
        # merchant search is usually really asking. Sent only when
        # there is spending to average — a search matching nothing but
        # income or transfers has no average, and 0 would read as one.
        avg = round(spend["sum"] / spend["count"], 2) if spend["count"] else None
        # the average per DAY answers "what is this costing me" over the
        # window asked for — a month's category, a date range. A search
        # with no start date has no window to divide by. The window starts
        # no earlier than the ledger: days before its first row are no
        # data, not days of no spending (the pace window's boundary rule).
        start = date_from or None
        if start and spend["sum"]:
            from ..engine.compat import as_date as _as_date
            first = _ledger_first(conn, budget_view=False)
            if first and first > _as_date(start[:10]):
                start = first
        per_day, per_day_days = _per_day(spend["sum"], start,
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
    # In and net are null for a month before the bank feed starts:
    # its income is unknown (the Month lens says the same), and $0 in
    # would read the month's whole spend as a loss.
    from ..engine import entities as _ent, reporting as _reporting
    combined = _ent.is_combined(conn)
    flows = _reporting.month_flows(conn, y or today.year,
                                   m or today.month)
    # per day over the days of the month the ledger has seen, the way the
    # month's pace counts them (from the ledger's first day when it began
    # inside the month)
    per_day, per_day_days = _per_seen_day(
        flows["out"], _month_windows(_ledger_first(conn), yy, [mm], today))
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


# Every door naming a transaction takes its id as a `path` segment: a
# file-import or bridge id carries the source's own reference, which can
# hold a '/', and the server decodes the clients' %2F before it matches a
# route — a plain segment would stop at the slash and 404 the request.
@router.post("/transactions/{txn_id:path}/category")
def set_category(txn_id: str, user: dict = Depends(_user()),
                 body: dict = Body(...)):
    category = _category_text(body, "category", user=user)
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
        # the row is checked and written under the shared pairing lock, so
        # a settlement cannot retire it in between (see _live_txn_for_edit)
        with conn.transaction():
            txn_id = _live_txn_for_edit(conn, txn_id)
            exists = txn_id and conn.execute(
                "SELECT COALESCE(category_override, category_primary) AS cat "
                "FROM transactions WHERE id=%s", (txn_id,)).fetchone()
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
@router.put("/transactions/{txn_id:path}/split")
def set_split_api(txn_id: str, user: dict = Depends(_user()),
                  body: dict = Body(...)):
    _may_edit(user)
    from ..engine import splits as _splits
    parts = body.get("parts")
    conn = _conn(user)
    try:
        try:
            # under the pairing lock, onto the live row (live_edit_ids)
            with conn.transaction():
                txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id,
                                                                 txn_id)
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


@router.delete("/transactions/{txn_id:path}/split")
def clear_split_api(txn_id: str, user: dict = Depends(_user())):
    _may_edit(user)
    from ..engine import splits as _splits
    conn = _conn(user)
    try:
        # under the pairing lock, onto the live row (live_edit_ids)
        with conn.transaction():
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id)
            if not txn_id:
                raise HTTPException(404, "transaction not found")
            had = _splits.clear_split(conn, txn_id)
            if had and had != "user":
                # the parts were a store receipt's reading, and the store
                # matcher writes them again on its next run unless the row
                # reads as answered: pin the row's category as the person's
                # (the whole charge under it), exactly what removing a
                # split means. Clearing that pin later brings the parts back.
                cat = conn.execute(
                    "SELECT COALESCE(category_override, category_primary) AS c "
                    "FROM transactions WHERE id = %s", (txn_id,)).fetchone()
                if cat and cat["c"]:
                    data.set_category(conn, txn_id, cat["c"])
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
    # "Select all shown" on an unpaged view (a merchant's whole history, a
    # month shown in full) routinely selects well over a thousand rows, so
    # the cap sits far above what a screen shows rather than at it.
    if not isinstance(ids, list) or len(ids) > BULK_MAX_IDS:
        raise HTTPException(400, f"ids must be a list of at most "
                                 f"{BULK_MAX_IDS}")
    action = str(body.get("action") or "")
    category = _category_text(body, "category", user=user) \
        if action == "category" else ""
    ids = [str(i) for i in dict.fromkeys(str(i) for i in ids)]
    conn = _conn(user)
    try:
        # A bulk set offers only names that already exist: the standard
        # set and the household's own categories (a new custom name is made
        # on one row, through the row's own picker). Ten thousand rows of a
        # name nobody picked from a list is a typo or a flood, either way
        # not something to stamp across a selection.
        if category:
            from ..engine import categories as _cat
            if category not in _cat.PLAID_PRIMARIES \
                    and category not in data.all_categories(conn):
                raise HTTPException(
                    400, "not a known category — pick one of the standard "
                         "categories or one of your own")
        out = {"applied": 0, "missing": 0, "flow": 0}
        try:
            # Worked through in slices so no single statement carries an
            # unbounded id array, but inside ONE transaction: the person
            # chose one act over one selection, and a failure part-way
            # must leave none of it applied rather than an arbitrary prefix.
            with conn.transaction():
                # each id resolved under the pairing lock to the row it now
                # stands as: a pending hold a settlement retired is edited
                # through its posted row, and none lands on a hidden hold
                if action in ("reimb_flag", "reimb_unflag"):
                    # flagging takes the pairing lock exclusively per row;
                    # take it exclusive up front, because upgrading the
                    # shared hold below deadlocks two concurrent bulk flags
                    # (each holds it shared and waits for the other)
                    conn.execute(data._REIMB_LOCK)
                live = data.live_edit_ids(conn, ids)
                ids = list(dict.fromkeys(live.get(i, i) for i in ids))
                for at in range(0, len(ids), _BULK_SLICE):
                    part = data.bulk_apply(conn, ids[at:at + _BULK_SLICE],
                                           action, category)
                    for k in out:
                        out[k] += int(part.get(k) or 0)
                if not ids:
                    # still validates the action and category
                    data.bulk_apply(conn, [], action, category)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if out.get("applied"):
            _log_bulk(conn, user, action, category, ids, out)
        return {"ok": True, **out}
    finally:
        conn.close()


BULK_MAX_IDS = 10_000
_BULK_SLICE = 1_000


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
                      limit: int = 200, order: str = "count",
                      sort: str | None = None, filter: str = "all",
                      offset: int = 0):
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
        try:
            # filter and sort apply before the cap, so a filtered view
            # reaches the long tail the cap would otherwise cut
            rows = merchant_dedup.listing(conn, q, min(500, max(1, limit)),
                                          order=order, sort=sort,
                                          filter=filter, offset=offset)
        except ValueError as e:
            raise HTTPException(400, str(e))
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
    # The source is read the way the detail read reads it — uncapped: an
    # imported descriptor over the cap is a real name the catalog lists and
    # opens, and the long ones are exactly those that most need renaming.
    # An unknown source is a 404 below. The TARGET is written into
    # merchant_canonical for every variant, journalled once per variant and
    # rendered on every surface, so a new name keeps the cap; a target the
    # ledger already displays writes nothing new (a merge into it), so its
    # length is not held against it.
    _merchant_name_or_400("display", display, max_len=None)
    _merchant_name_or_400("to", to, max_len=None)
    conn = _conn(user)
    try:
        if len(to) > 200 and not merchant_dedup.raw_strings_for(conn, to):
            _merchant_name_or_400("to", to)
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


# The pairing lock in SHARED mode: person-written edits on one row run side
# by side, and wait only for a writer holding it exclusively — a feed
# settling a pending hold into its posted row, which carries the hold's
# note and category across and retires it in one commit.
_PAIRING_LOCK_SHARED = data._REIMB_LOCK.replace(
    "pg_advisory_xact_lock(", "pg_advisory_xact_lock_shared(", 1)


def _live_txn_for_edit(conn, txn_id: str) -> str | None:
    """Inside the caller's transaction: take the shared pairing lock, then
    name the live row an edit of `txn_id` belongs on — the row itself, or,
    when a settlement has just retired it, the posted row that names it as
    its pending authorization. None when there is neither.

    Checked and written under the lock: read before it, a settlement could
    commit between the check and the write, and the note or category would
    land on the retired hold after its carry had already run — hidden, with
    the posted row showing nothing the person wrote."""
    conn.execute(_PAIRING_LOCK_SHARED)
    if conn.execute("SELECT 1 FROM transactions WHERE id=%s AND removed=0",
                    (txn_id,)).fetchone():
        return txn_id
    twin = conn.execute(
        """SELECT t.id FROM transactions t
             JOIN transactions p ON p.id = %s AND p.removed = 1
            WHERE t.removed = 0 AND t.account_id = p.account_id
              AND t.raw->>'pending_transaction_id' = p.id
            ORDER BY t.id LIMIT 1""", (txn_id,)).fetchone()
    return twin["id"] if twin else None


@router.post("/transactions/{txn_id:path}/note")
def set_txn_note(txn_id: str, user: dict = Depends(_user()),
                 body: dict = Body(...)):
    """Set (or clear, when empty) a free-text note on a transaction."""
    demoguard.deny(user)
    _may_edit(user)
    conn = _conn(user)
    try:
        with conn.transaction():
            txn_id = _live_txn_for_edit(conn, txn_id)
            if not txn_id:
                raise HTTPException(404, "transaction not found")
            prev = conn.execute(
                "SELECT note FROM transaction_notes WHERE txn_id=%s",
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


@router.post("/transactions/{txn_id:path}/owner")
def set_txn_owner_api(txn_id: str, user: dict = Depends(_user()),
                      body: dict = Body(...)):
    _may_edit(user)
    owner = _body_text(body, "owner")
    conn = _conn(user)
    try:
        # under the pairing lock, onto the live row: a settlement retiring
        # the hold between the check and the write would strand the owner
        # on the hidden hold (see _live_txn_for_edit)
        with conn.transaction():
            txn_id = _live_txn_for_edit(conn, txn_id)
            n = txn_id and data.set_txn_owner(conn, txn_id, owner or None)
            if not n:
                raise HTTPException(404, "transaction not found")
        return {"ok": True, "owner": owner or None, "id": txn_id}
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
    can't finish a setup flow with broken keys). api_key encrypted.

    The aggregator client decides where the household's bank data is
    delivered, so repointing it is a step-up door like the relay, the AI
    endpoint and the webhooks: a session cookie alone is not proof of the
    owner, and the write runs under the factor lock so a request that
    authenticated just before a reset cannot land after it."""
    demoguard.deny(user)
    demoguard.deny_shared_grant(user)
    from .app import _require_elevation
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    # the mode answer comes after the step-up: an unelevated caller
    # learns nothing about the instance from this door
    _no_byo_on_hosted()
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
    from ..sync.base import tenant_sync_lock
    conn = _conn(user)
    try:
        # MX members only appear through a sync, which holds this lock:
        # with it held, no member can land under the old client between the
        # live-connection check and the key write
        with tenant_sync_lock(conn, user["tenant_id"]) as held:
            if not held:
                raise HTTPException(409, "a sync is running — save the "
                                         "keys again in a moment")
            from .app import _still_signed_in_under_factor_lock
            with _still_signed_in_under_factor_lock(user):
                _mx_keys_save(conn, user, client_id, api_key, env)
    finally:
        conn.close()
    return {"ok": True, "env": env}


def _mx_keys_save(conn, user: dict, client_id: str, api_key: str,
                  env: str) -> None:
    from ..db import crypto
    from ..engine import budget
    from ..sync import mx as mx_mod
    cur = budget.load_config(conn)
    # An MX user belongs to the client (and its environment), not to one
    # API key: rotating the key keeps the user, and with it every bank
    # linked under it. Dropping the guid here would create a fresh, empty
    # user on the next sync and leave every linked bank behind at MX.
    moving = bool(cur.get("mx_user_guid")) and (
        cur.get("mx_client_id") != client_id
        or (cur.get("mx_env") or "production") != env)
    if moving:
        # a different client cannot see the old user at all, so its
        # banks would stop syncing with nothing left to release them by
        _refuse_live_connections(conn, "mx", "MX")
        # nothing live is left under the old user: release it (and the
        # financial data MX holds for it) while its keys still work
        mx_mod.release_tenant_user(user["tenant_id"])
    with budget.config_txn(conn) as cfg:
        cfg["mx_client_id"] = client_id
        cfg["mx_api_key"] = crypto.encrypt(conn, api_key)
        cfg["mx_env"] = env
        # when these keys were saved, for the bank-link hold's banner;
        # written by the server only, never through the Settings save
        cfg["mx_keys_saved_at"] = _keys_saved_now()
        if moving:
            cfg.pop("mx_user_guid", None)  # the new client's user next use


def _keys_saved_now() -> str:
    """The `<provider>_keys_saved_at` stamp: when the household's own
    aggregator keys were saved, so the bank-link hold's banner can tell the
    owner whether the held keys predate the record."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _refuse_live_connections(conn, aggregator: str, label: str) -> None:
    """400 naming the live connections of one aggregator. Their handle at
    the provider (a Plaid access token, an MX user) only works with the keys
    that made it, so swapping or clearing those keys now would leave the
    connections syncing nothing — and still billing — with no way to
    release them. Disconnecting is the door that releases them."""
    tok = "AND access_token IS NOT NULL " if aggregator == "plaid" else ""
    live = conn.execute(
        "SELECT id, institution_name FROM items WHERE aggregator=%s "
        + tok + "AND COALESCE(status,'ok') <> 'archived' ORDER BY id",
        (aggregator,)).fetchall()
    if live:
        names = ", ".join(r["institution_name"] or r["id"] for r in live)
        raise HTTPException(
            400,
            f"Disconnect these {label} connections first: {names}. They "
            f"only work with the keys saved now — replacing or clearing "
            f"those keys would leave the connections billing at {label} "
            f"with no way to release them.")


@router.post("/accounts/mx/connect")
def mx_connect(user: dict = Depends(_user())):
    """A fresh MX Connect-widget URL to link (or repair) banks. A bank
    disconnected earlier and relinked here comes back live on the next
    sync (mx.connect_widget_url notes when the widget was opened)."""
    demoguard.deny(user)
    from ..sync import mx as mx_mod
    from ..sync.base import tenant_sync_lock
    conn = _conn(user)
    try:
        # Opening the widget may create the household's MX user, under the
        # keys read at the start; saving new keys holds this same lock. Held
        # here too, the two cannot interleave: otherwise a user created
        # under the old client could be stored beside the new client's
        # keys, every later sync would fail on it, and the banks linked
        # into it would stay at the old client with nothing to release
        # them by.
        with tenant_sync_lock(conn, user["tenant_id"]) as held:
            if not held:
                raise HTTPException(409, "a sync is running — try again "
                                         "in a moment")
            try:
                return {"url": mx_mod.connect_widget_url(conn)}
            except ValueError as e:
                raise HTTPException(400, str(e))
            except Exception as e:                # noqa: BLE001
                raise HTTPException(400, f"MX error: {type(e).__name__}")
    finally:
        conn.close()


@router.post("/accounts/mx/sync",
             dependencies=[Depends(limit("account-sync", 20, 600))])
def mx_sync_now(user: dict = Depends(_user())):
    """Pull every MX member NOW. A live aggregator pull per press, so it
    shares the per-item sync doors' budget: 20 per 10 minutes per
    address, 30 per household (pages.SYNC_PER_IP / SYNC_PER_TENANT)."""
    demoguard.deny(user)
    from .pages import _SYNC_LIMITED_MSG, _tenant_sync_allowed
    if not _tenant_sync_allowed(user):
        raise HTTPException(429, _SYNC_LIMITED_MSG)
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
    the guided flow's final step. Secret encrypted at rest. A step-up
    door written under the factor lock, for the reason the MX one is."""
    demoguard.deny(user)
    demoguard.deny_shared_grant(user)
    from .app import _require_elevation
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    # the mode answer comes after the step-up: an unelevated caller
    # learns nothing about the instance from this door
    _no_byo_on_hosted()
    from .app import _still_signed_in_under_factor_lock
    from ..db import crypto
    from ..engine import budget
    from ..sync import plaid as plaid_mod
    r = plaid_validate(user=user, body=body)     # raises 400 on bad keys
    new_id = str(body["client_id"]).strip()
    conn = _conn(user)
    try:
        if _plaid_client_moves(conn, new_id, r["env"]):
            # another Plaid account (or environment) cannot use the access
            # tokens this one minted: the same stranding clearing the keys
            # refuses. Archived Items still holding a token get their last
            # release here, under the keys that can still make it. Which
            # Items is decided under the lock; the releases run after it,
            # because each is a Plaid round-trip that can take minutes and a
            # link finishing meanwhile waits on that lock holding a pooled
            # connection.
            _plaid_release_stragglers(conn, _plaid_straggler_ids(conn))
        # under the lock a new Item's row is written under, so a link in
        # flight either lands before the live-connection check or sees the
        # keys it exchanged under replaced and hands its Item back. The
        # check is asked again here because the release above ran unlocked.
        with _still_signed_in_under_factor_lock(user), \
                conn.transaction():
            plaid_mod.link_lock(conn)
            if _plaid_client_moves(conn, new_id, r["env"]):
                _refuse_live_connections(conn, "plaid", "Plaid")
            with budget.config_txn(conn) as cfg:
                cfg["plaid_client_id"] = new_id
                cfg["plaid_secret"] = crypto.encrypt(
                    conn, str(body["secret"]).strip())
                cfg["plaid_env"] = r["env"]
                # the bank-link hold's banner says when (server-written)
                cfg["plaid_keys_saved_at"] = _keys_saved_now()
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
    from ..notify import outbound_pause
    from ..sync import plaid as plaid_mod
    conn = _conn(user)
    try:
        # Refused before Plaid is called at all, and the Items to release
        # are listed under the link lock; the releases themselves run with
        # no transaction open and no lock held: each is a Plaid round-trip
        # that can take minutes, and a bank link finishing meanwhile would
        # wait on the link lock holding a pooled connection the whole time.
        # Only the listed Items are released, so a bank linked while Plaid
        # was answering is never removed by this door — the check below
        # refuses it as live instead.
        released, _ = _plaid_release_stragglers(conn,
                                                _plaid_straggler_ids(conn))
        # the link lock, as the swap takes it: a link in flight must not
        # land a token minted under keys about to be forgotten. The live
        # check is asked again under it — a bank linked while Plaid was
        # answering above still stops the keys from going — and whatever
        # still holds a token now is what the clear strands.
        with conn.transaction():
            plaid_mod.link_lock(conn)
            _refuse_live_connections(conn, "plaid", "Plaid")
            stranded = conn.execute(
                "SELECT count(*) AS n FROM items WHERE aggregator='plaid' "
                "AND access_token IS NOT NULL").fetchone()["n"]
            with budget.config_txn(conn) as cfg:
                for k in ("plaid_client_id", "plaid_secret", "plaid_env",
                          "plaid_keys_saved_at"):
                    cfg.pop(k, None)
                # the bank-link hold stops naming the client just forgotten
                # (and goes if that was all it held), so a later rotation
                # writes its own record and saving the same client again is
                # not held by this one
                outbound_pause.drop_stale_hold(cfg)
    finally:
        conn.close()
    return {"ok": True, "released": released, "stranded": stranded}


def _plaid_client_moves(conn, new_id: str, new_env: str) -> bool:
    """True when saving (new_id, new_env) changes the Plaid client or
    environment the household's tokens are bound to. No stored client id
    means the household's Items were linked under the instance's own keys
    (credentials() falls back to them), and those tokens are just as bound
    to that client."""
    from ..engine import budget
    cur = budget.load_config(conn)
    had_id = (cur.get("plaid_client_id")
              or os.environ.get("OIKONOME_PLAID_CLIENT_ID"))
    had_env = ((cur.get("plaid_env") if cur.get("plaid_client_id") else None)
               or os.environ.get("OIKONOME_PLAID_ENV", "production"))
    return had_id != new_id or had_env != new_env


def _plaid_straggler_ids(conn) -> list[str]:
    """Under the link lock: refuse while any Plaid connection is live, then
    list the Items that still hold a token — every one of them archived,
    since the refusal passed. A link can only add an Item under this same
    lock, so the list is exactly what was there when the keys were asked to
    go. Raises the refusal's 400."""
    from ..sync import plaid as plaid_mod
    with conn.transaction():
        plaid_mod.link_lock(conn)
        _refuse_live_connections(conn, "plaid", "Plaid")
        # archived named again, not left to the refusal above: a live
        # Item must never be released by this door, whatever the order
        return [r["id"] for r in conn.execute(
            "SELECT id FROM items WHERE aggregator='plaid' "
            "AND access_token IS NOT NULL "
            "AND COALESCE(status,'ok') = 'archived' ORDER BY id").fetchall()]


def _plaid_release_stragglers(conn, ids: list[str]) -> tuple[int, int]:
    """Best-effort /item/remove for the listed archived Plaid Items (see
    `_plaid_straggler_ids`), clearing the token on the ones that release.
    Returns (released, stranded). Called with no transaction open and no
    lock held — the calls are network round-trips, and each cleared token
    stands on its own. Never raises: the keys are going either way, and a
    Plaid outage must not wedge the door shut — the same trade erasure
    makes.
    """
    import logging

    from ..sync import base as sync_base
    from ..sync import plaid as plaid_mod
    rows = [{"id": i} for i in ids]
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
    """Forget the tenant's BYO MX credentials.

    Refused while live MX connections exist, for the reason the Plaid door
    refuses: the MX user holding them is only reachable with these keys.
    Once nothing is live, the MX user is deleted at MX before the keys go —
    it holds the household's bank credentials and history, and after this
    nothing here could ever ask MX to remove them (erasure included).
    Best-effort, like the Plaid release: an MX outage must not wedge the
    door shut, and `mx_user` reports what happened."""
    demoguard.deny(user)
    from ..engine import budget
    from ..notify import outbound_pause
    from ..sync import mx as mx_mod
    from ..sync.base import tenant_sync_lock
    conn = _conn(user)
    try:
        # under the sync lock, as saving keys is: a sync adding a member
        # must not land between the check and forgetting the keys
        with tenant_sync_lock(conn, user["tenant_id"]) as held:
            if not held:
                raise HTTPException(409, "a sync is running — try again "
                                         "in a moment")
            _refuse_live_connections(conn, "mx", "MX")
            released = mx_mod.release_tenant_user(user["tenant_id"])
            with budget.config_txn(conn) as cfg:
                for k in ("mx_client_id", "mx_api_key", "mx_env",
                          "mx_user_guid", "mx_keys_saved_at"):
                    cfg.pop(k, None)
                # as the Plaid door does: the hold forgets this client
                outbound_pause.drop_stale_hold(cfg)
    finally:
        conn.close()
    return {"ok": True, "mx_user": released}


@router.post("/connections/export",
             dependencies=[Depends(limit("connections_bundle", 5, 3600))])
def connections_export(user: dict = Depends(_user()), body: dict = Body(...)):
    """Passphrase-sealed full-config bundle — provider keys, LLM,
    SMTP, and live bank links — master-key-independent so it survives a move
    to a fresh environment. Owner only: these are instance-wide credentials,
    not one member's view."""
    demoguard.deny_shared_credentials(user)
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
    # In hosted mode every bank link was made under the operator's provider
    # keys: its access tokens must stay on the server, and the import side
    # drops them anyway, so the bundle has no use there. Refused here, not
    # only hidden in the app, because the file cannot be recalled once made.
    if env_flag("OIKONOME_HOSTED"):
        raise HTTPException(
            403, "in hosted mode bank connections run under the operator's "
                 "provider keys and stay on the server — there is no "
                 "connections bundle to export")
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
    # a bundle carries the AI endpoints, their routing and the mail relay,
    # and lands them wholesale — the same new destinations a Settings save
    # refuses to the shared demonstration login, whose step-up below proves
    # nothing because its password is handed out
    demoguard.deny_shared_grant(user)
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
    import copy
    import json as _json
    from ..sync.base import tenant_sync_lock
    from .app import _still_signed_in_under_factor_lock
    # The slow work happens before any lock: the passphrase KDF takes its
    # cost from the file, and the endpoint hostnames are resolved here so
    # the write below, which checks them again, answers from that lookup.
    try:
        payload = _json.loads(cb._open(data, passphrase))
        if isinstance(payload, dict):
            config, _secrets, _items = cb._checked_shapes(
                copy.deepcopy(payload))
            cb._check_urls(config)
            cb._clean_backends(copy.deepcopy(payload.get("llm_backends")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    conn = _conn(user)
    try:
        # under the sync lock, as the MX key door is: a sync creating the
        # household's MX user, or adding a member to it, must not land
        # between the bundle's live-connection check and its key write.
        # The bundle carries aggregator keys, the mail relay and the AI
        # endpoint, so it writes only while the caller is still signed in,
        # under the factor lock a reset takes: an import authenticated just
        # before a reset lands before it (and is paused by it) or not at all.
        with tenant_sync_lock(conn, user["tenant_id"]) as held:
            if not held:
                raise HTTPException(409, "a sync is running — import the "
                                         "file again in a moment")
            with _still_signed_in_under_factor_lock(user):
                try:
                    summary = cb.apply_payload(conn, payload)
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


_PAIRS_PAGE = 50


@router.get("/reimburse/pairs")
def reimburse_pairs(user: dict = Depends(_user()), page: int = 1,
                    q: str = ""):
    """Linked pairs, newest first — what the undo button acts on.

    `page` (from 1) reaches further back and `q` finds a pair by either
    side's merchant, so an older wrong match can still be undone; `more`
    says whether a further page exists."""
    page = max(1, min(int(page), 10_000))
    conn = _conn(user)
    try:
        rows = data.recent_reimbursement_pairs(
            conn, limit=_PAIRS_PAGE + 1, offset=(page - 1) * _PAIRS_PAGE,
            q=q[:200])
        return {"pairs": [_ser(p) for p in rows[:_PAIRS_PAGE]],
                "more": len(rows) > _PAIRS_PAGE, "page": page}
    finally:
        conn.close()


@router.get("/reimburse/{txn_id:path}/candidates")
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
        reimburse_id = str(body.get("reimburse_id") or "")
        # A settlement moves a pending hold's pairs onto its posted row
        # under the pairing lock, so a list read before it names the hold.
        # Resolved under that same lock (exclusive, as the unlink writes),
        # each side follows its hold to the row the pair now stands on;
        # with the hold's id the unlink would find nothing and the pair
        # would quietly stay.
        with conn.transaction():
            conn.execute(data._REIMB_LOCK)
            live = data.live_edit_ids(conn, [expense_id, reimburse_id])
            expense_id = live.get(expense_id, expense_id)
            reimburse_id = live.get(reimburse_id, reimburse_id)
            removed = data._unlink_locked(conn, expense_id, reimburse_id)
        # logged only when a pair really went: a second tap, the other
        # device or a retry finds nothing to remove and records nothing
        if removed:
            _activity.record(conn, user, "reimbursement", "unlinked",
                             target=expense_id,
                             label=_activity.txn_label(conn, expense_id))
        return {"ok": True, "removed": bool(removed),
                "expense_id": expense_id, "reimburse_id": reimburse_id}
    finally:
        conn.close()


@router.post("/reimburse/{txn_id:path}/flag")
def reimburse_flag(txn_id: str, user: dict = Depends(_user()),
                   body: dict | None = Body(default=None)):
    body = body or {}
    expected = body.get("expected")
    try:
        expected = float(expected) if expected not in (None, "") else None
    except (TypeError, ValueError, OverflowError):
        # OverflowError: a JSON integer too wide for a double
        raise HTTPException(400, "expected must be a number")
    # NaN/Infinity parse fine and slip past the <=0 check (all NaN comparisons
    # are False), then poison the DOUBLE column and 500 every later
    # /reimburse/pending response (allow_nan=False) — reject them.
    import math
    if expected is not None and not math.isfinite(expected):
        raise HTTPException(400, "expected must be a finite number")
    from ..engine.compat import MAX_AMOUNT
    if expected is not None and expected > MAX_AMOUNT:
        raise HTTPException(400, "expected is too large")
    if expected is not None and expected <= 0:
        raise HTTPException(400, "expected must be positive")
    conn = _conn(user)
    try:
        # a hold that settled since the list was read is flagged on the
        # posted row it became, resolved under the pairing lock the
        # settlement holds (exclusive up front: the flag takes it so)
        with conn.transaction():
            conn.execute(data._REIMB_LOCK)
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
            refused = data.flag_reimbursement(
                conn, txn_id, partial=bool(body.get("partial")),
                expected=expected)
        if refused == data.NO_SUCH_FLAG_TXN:
            raise HTTPException(404, refused)
        if refused:
            raise HTTPException(409, refused)
        _activity.record(conn, user, "reimbursement", "flagged", target=txn_id,
                         label=_activity.txn_label(conn, txn_id),
                         detail={"expected": expected,
                                 "partial": bool(body.get("partial"))})
        return {"ok": True, "id": txn_id}
    finally:
        conn.close()


@router.post("/reimburse/{txn_id:path}/unflag")
def reimburse_unflag(txn_id: str, user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        # A settlement moves a hold's flag onto its posted row under the
        # pairing lock; resolved under it, the unflag follows the hold
        # there instead of deleting nothing and leaving the flag counted.
        with conn.transaction():
            conn.execute(data._REIMB_LOCK)
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
            dropped = data.unflag_reimbursement(conn, txn_id)
        # logged only when a flag really went, as unlink is: a second tap
        # or the other device finds nothing to drop and records nothing
        if dropped:
            _activity.record(conn, user, "reimbursement", "unflagged",
                             target=txn_id,
                             label=_activity.txn_label(conn, txn_id))
        return {"ok": True, "removed": bool(dropped), "id": txn_id}
    finally:
        conn.close()


# a bill with no occurrence/envelope activity this month (income bills, or a
# fresh tenant) still carries the stats keys — the SPA renders "·" from zeros
_EMPTY_BILL_STATS = {"occurrences": 0, "paid": 0, "paid_amount": 0.0,
                     "used": None, "overflow": None, "next_unpaid": None,
                     "withdrawn_on": None, "pool": None, "period_months": None,
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
    # the accounts the "excl" toggle drops, read the way month_status reads
    # them — a malformed setting means "nothing excluded", never a 500
    excluded = budget.excluded_account_ids(cfg)
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
        excluded=excluded)
    in_month = [i for i, r in enumerate(match_rows)
                if as_date(r["date"]) >= month_start]
    _pos = {src: dst for dst, src in enumerate(in_month)}
    rows = [match_rows[i] for i in in_month]
    ny, nm = ((today.year, today.month + 1) if today.month < 12
              else (today.year + 1, 1))
    py, pm = ((today.year, today.month - 1) if today.month > 1
              else (today.year - 1, 12))

    def _match(bill_list, candidate_rows):
        # LAST month's occurrences go in front as decoys, as month_status
        # does: a bill due the 30th paid three days late on the 3rd belongs
        # to last month's occurrence, and without the decoy it matches
        # nothing here (or an envelope takes it) while Today counts it as
        # fixed spend. A decoy paid inside this month is this month's cash
        # and stays; an unpaid one, or one settled last month, is last
        # month's business and is dropped.
        occs = budget.month_occurrences(bill_list, py, pm)
        n_prev = len(occs)
        occs += budget.month_occurrences(bill_list, today.year, today.month)
        n_this = len(occs)
        occs += budget.month_occurrences(bill_list, ny, nm)
        matched, occs = budget.match_occurrences(candidate_rows, occs)
        # A next-month occurrence nobody has paid yet was a matching
        # candidate and nothing more — keeping it would put two rents in
        # one month's Paid x/y. One that WAS paid here stays, on the same
        # cash basis the verdict uses: the money left in this month, so
        # this is the month that counts it.
        return matched, ([o for o in occs[:n_prev] if o["txn"] is not None
                          and as_date(o["txn"]["date"]) >= month_start]
                         + occs[n_prev:n_this]
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
    # the same withdrawn-charge reading Today gives its overdue line, so a
    # bill the feed took back says so on both pages
    budget.mark_withdrawn(
        conn, occs, today,
        since=month_start - dt.timedelta(days=budget.PREPAY_MATCH_DAYS),
        cfg=cfg)
    # Rows the BUDGET occurrences claimed (payment plus the fees that rode
    # with it), translated to `rows` indices for the envelope pass. They come
    # from the index set the matcher returns, never from the occurrence's
    # `txn`: a store-split charge pays a bill as the whole charge put back
    # together — a new row that is none of its parts — so reading ownership
    # off `txn` would leave every part free for an envelope to draw, and the
    # same money would count as the bill paid and as envelope spending. A
    # match on a prior-month payment has no in-month row and drops out.
    matched_idx = {_pos[i] for i in matched_all if i in _pos}
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
            excluded=excluded)
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
            conn, envelopes, today, excluded=excluded,
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
            # only a missed occurrence can be explained by a withdrawal;
            # one not yet due is just upcoming
            on = occ.get("withdrawn_on")
            e["withdrawn_on"] = (on.isoformat()
                                 if on and occ["due"] <= today else None)
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
            "next_unpaid": None, "withdrawn_on": None}
    return per


@router.get("/bills")
def bills_list(user: dict = Depends(_user()),
               month: str | None = Query(None)):
    """Proposals + bills — the SPA Bills page's data. Each bill row
    carries health, this month's occurrence/envelope stats (Paid x/y,
    envelope Used/Chgs/Paid), and the ledger-fit hint (💡): the fit summary,
    whether it diverges from the config, and its dismissed flag. `archived`
    lists soft-removed bills (active=0) for the archived section.

    `month=YYYY-MM` reads a PAST month instead: the same stats as that
    month ended (paid x of y, received, envelope used), so the page can
    step back through the year. The schedule itself (the bills, their
    health, the totals) is always the live one — a bill added since shows
    with no occurrence in a month it did not exist for. The answer names
    the month it describes (`month`) and whether it is closed
    (`month_final`)."""
    import calendar as _calendar
    from ..engine import bills, budget
    from ..engine.compat import as_dict
    from .pages import _cadence_label, _proposal_summary
    want_month = None
    if month:
        try:
            yy, mm = (int(x) for x in month.split("-"))
        except (TypeError, ValueError):
            raise HTTPException(400, "month must be YYYY-MM")
        # the shared range check, not a bare dt.date(): year 1 builds a
        # date the month arithmetic below then steps out of (a 500)
        _ck_ym(yy, mm)
        want_month = (yy, mm)
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
        # the month the stats describe: this one as of today, or a past
        # one as of its last day — the same reading the Month lens gives
        as_of, month_key, month_final = today, f"{today:%Y-%m}", False
        if want_month and want_month != (today.year, today.month):
            if want_month > (today.year, today.month):
                raise HTTPException(400, "that month has not started")
            yy, mm = want_month
            as_of = dt.date(yy, mm, _calendar.monthrange(yy, mm)[1])
            month_key, month_final = f"{yy:04d}-{mm:02d}", True
        stats = _month_bill_stats(conn, cfg, as_of)
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
            # Two dates, two meanings, kept apart. `withdrawn_on` (from the
            # month stats) is THIS month's missed occurrence, and only it may
            # turn the row's status amber; `health_withdrawn_on` is the
            # earlier cycle behind a "withdrawn" health verdict, for the
            # health pill alone. Folding one into the other dressed a later
            # cycle that was never charged — or one not yet due — in last
            # cycle's withdrawal, while Today called it overdue.
            rows[-1]["health_withdrawn_on"] = (
                h["withdrawn_on"].isoformat() if h.get("withdrawn_on")
                else None)
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
        # "withdrawn" is left out on purpose: the bill WAS charged, and a
        # charge the feed took back is a warning to check, not a decision
        totals["attention"] = sum(
            1 for r in rows
            if (r.get("health") or "") in ("stale", "mismatch", "misdated",
                                           "drifting")
            and not r.get("health_dismissed") and not r.get("disabled"))
        return {"proposals": props, "bills": rows, "archived": archived,
                "totals": totals, "cadences": CADENCES,
                "month": month_key, "month_final": month_final}
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


def _proposal_superseded(conn, pid: str) -> bool:
    """Was this offer withdrawn because what it offered already exists?"""
    row = conn.execute("SELECT status FROM bill_proposals WHERE id=%s",
                       (pid,)).fetchone()
    return bool(row and row["status"] == "superseded")


def _proposal_adjust(raw) -> dict:
    """The fields a person changed on an offer before approving it, read
    and checked the way the bill save checks them, before anything is
    written. A blank or null field is one they left alone."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(400, "adjust must be an object")
    out: dict = {}
    name = _body_text(raw, "name")
    if name:
        out["name"] = name
    if raw.get("amount") not in (None, ""):
        if isinstance(raw["amount"], bool):
            raise HTTPException(400, "amount must be a number")
        amount = _money_float(raw["amount"], "amount")
        if not amount > 0:
            raise HTTPException(400, "amount must be a positive dollar value")
        out["amount"] = amount
    cadence = _body_text(raw, "cadence")
    if cadence:
        out["cadence"] = _parse_cadence(cadence)
    if raw.get("due_day") not in (None, ""):
        day = raw["due_day"]
        if isinstance(day, bool) or not isinstance(day, int) \
                or not 1 <= day <= 31:
            raise HTTPException(
                400, "due_day must be a day of the month, 1 to 31")
        out["due_day"] = day
    return out


def _next_on_day(today: dt.date, day: int, freq: str, interval: int,
                 due: dt.date | None) -> dt.date | None:
    """The due date of a bill whose due day the person set: the day of the
    month moved inside the cycle the bill is already due in (its due date's
    month and year, this month when it has none, the last day of a shorter
    month), then stepped on by the bill's own interval, in years for a
    yearly bill, while it is still before `today`. Stepping by one month
    instead would pull a quarterly bill into next month and a two-yearly
    one into next year, and the plan would count a charge that is not
    coming. A cadence with no day of the month (weekly, daily, one time)
    keeps its date; an envelope has no due date at all."""
    if freq == "ENVELOPE":
        return None
    if freq not in ("MONTHLY", "YEARLY"):
        return due
    step = max(1, int(interval or 1)) * (12 if freq == "YEARLY" else 1)
    base = due or today
    n = base.year * 12 + base.month - 1

    def on(n: int) -> dt.date:
        y, m = divmod(n, 12)
        return dt.date(y, m + 1, min(day, calendar.monthrange(y, m + 1)[1]))
    d = on(n)
    if d < today:
        # whole cycles to the first one on or after today, not a loop: a
        # long-stale due date must not cost one iteration per cycle
        behind = (today.year * 12 + today.month - 1) - n
        n += max(0, behind // step) * step
        d = on(n)
        if d < today:
            d = on(n + step)
    return d


def _adjust_approved_bill(conn, payee: str, adj: dict) -> str:
    """Apply what the person changed to the bill an approval just created,
    inside the approval's transaction, and return the bill's name.

    The approval creates the bill exactly as an unadjusted one would, so
    the merchants it was found from and the history it was read from are
    attached by the same code. The changes are then an edit of that bill:
    a rename takes everything that names it along, and the schedule save
    carries the identity forward. A changed amount counts as a hand edit,
    so the drift check leaves it alone for its grace window."""
    try:
        return _adjust_bill_now(conn, payee, adj)
    except psycopg.errors.UniqueViolation:
        # the existence checks and the writes are separate statements, so a
        # rename or a save racing onto the same name can still trip the
        # (tenant_id, id) key at the write: the same answer as a name
        # already taken, and the caller's transaction leaves the offer
        # pending, not approved under values the person did not choose
        raise HTTPException(
            409, f"a bill named {adj.get('name') or payee!r} already exists")


def _adjust_bill_now(conn, payee: str, adj: dict) -> str:
    from .. import localtime
    from ..engine import bills
    from ..engine.compat import as_dict
    new = adj.get("name") or payee
    if new != payee:
        # the offer was checked against the bills already tracked under its
        # own name; a new name is checked again, by the token it would
        # match on, so a rename cannot slip a second tracker onto charges
        # another bill already claims
        by = bills.covering_bill(conn, new, besides=payee)
        if by is not None:
            raise HTTPException(409, f"'{by}' already tracks these charges")
        # an archived bill's name, or a spelling of it, is refused as the
        # Bills page's rename refuses it: rename_bill counts every bill
        if (bills.get_bill(conn, new) is not None
                or bills.rename_bill(conn, payee, new) == 0):
            raise HTTPException(409, f"a bill named {new!r} already exists")
    if not any(k in adj for k in ("amount", "cadence", "due_day")):
        return new
    bill = conn.execute("SELECT * FROM bills WHERE payee = %s FOR UPDATE",
                        (new,)).fetchone()
    raw = as_dict(bill["raw"])
    if "cadence" in adj:
        freq, interval = adj["cadence"]
    else:
        f, _, i = _bill_cadence(raw).partition(":")
        freq, interval = f.upper(), int(i or 1)
    rec = raw.get("recurrence") or {}
    bmd = rec.get("byMonthDay") if isinstance(rec, dict) else None
    # the day a monthly bill is anchored to, kept unless the person moved
    # it: re-read from a due date clamped into a short month it would slide
    # a bill due on the 31st to the 30th for good
    anchor = (bmd[0] if isinstance(bmd, list) and bmd
              and isinstance(bmd[0], int) else None)
    next_due = bill["due_on"]
    if "due_day" in adj:
        next_due = _next_on_day(localtime.household_day(conn),
                                adj["due_day"], freq, interval,
                                bill["due_on"])
        anchor = adj["due_day"]
    try:
        bills.save_bill(
            conn, payee=new,
            amount=adj.get("amount") or abs(bill["amount"] or 0),
            bill_type="envelope" if freq == "ENVELOPE" else "occurrence",
            frequency=None if freq in ("ONE_TIME", "ENVELOPE") else freq,
            interval=interval, next_due=next_due,
            category=bill["category"],
            source=raw.get("source", "detected"),
            last_seen=raw.get("lastDueOn"),
            by_month_day=anchor if freq == "MONTHLY" else None,
            income=(bill["amount"] or 0) > 0,
            manual_touch="amount" in adj)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # what the bill matches moved with its amount: stamp its rows now, as
    # the bill save does, not on the next nightly pass
    bills.apply_txn_categories(conn, bill_id=bills._slug(new))
    return new


def _hold_bill_claims(conn) -> None:
    """Inside the caller's transaction: wait until no other approval in
    the household is between its check for a bill already claiming these
    charges and the commit of the bill it creates. Two approvals of
    different offers (one renamed onto the other's charges) would each
    check before the other's bill was visible and both create one, and
    the plan would count the charge twice. Household-wide rather than per
    charge: what a bill claims includes its merchant's names, which are
    not known until the check reads them."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('oikonome:bill-claims:' ||"
        " COALESCE(current_setting('app.tenant_id', true), '')))")


def _decide_proposal(conn, pid: str, action: str, adj: dict) -> dict:
    """Approve or dismiss one offer; an approval applies `adj` too."""
    from ..engine import bills
    if action != "approve":
        return bills.apply_proposal(conn, pid, action)
    # one transaction: an adjustment that cannot be applied (the new name
    # is taken) leaves the offer pending, not approved under the values
    # the person just declined; and the claim lock is held until the bill
    # this approval makes is committed
    with conn.transaction():
        _hold_bill_claims(conn)
        if adj:
            p = conn.execute(
                "SELECT kind, status FROM bill_proposals WHERE id = %s "
                "FOR UPDATE", (pid,)).fetchone()
            if p is not None and p["status"] == "pending" \
                    and p["kind"] != "add":
                raise HTTPException(
                    400, "only an offer to add a bill or income can be "
                         "adjusted")
        r = bills.apply_proposal(conn, pid, action)
        if adj and not r.get("error"):
            r["approved"] = _adjust_approved_bill(conn, r["approved"], adj)
    return r


@router.post("/bills/proposal")
def bills_proposal_api(user: dict = Depends(_user()),
                           body: dict = Body(...)):
    """Approve or dismiss one offer. An approval may carry `adjust`
    ({name, amount, cadence, due_day}, each optional) — what the person
    changed on an offer to add a bill before saying yes; the bill is
    created with those values and keeps everything else the offer found.
    `adjust` is ignored on a dismissal."""
    action = body.get("action") or ""
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    adj = (_proposal_adjust(body.get("adjust")) if action == "approve"
           else {})
    conn = _conn(user)
    try:
        pid = body.get("pid") or ""
        r = _decide_proposal(conn, pid, action, adj)
        if not r.get("error"):
            _log_proposal(conn, user, pid, action, r)
        superseded = bool(r.get("error")) and _proposal_superseded(conn, pid)
    finally:
        conn.close()
    if superseded:
        # the offer is gone (the bill it offered is already tracked), so a
        # client drops its card rather than keep offering a dead button
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": r["error"], "superseded": True},
                            status_code=400)
    if r.get("error"):
        raise HTTPException(400, r["error"])
    out = {"ok": True, "payee": r.get("approved") or r.get("rejected")}
    if r.get("note"):
        # an approve can undo something the person set (a pinned amount);
        # the client shows this so the change is never silent
        out["note"] = r["note"]
    return out


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
    conn = _conn(user)
    try:
        pids = [r["id"] for r in conn.execute(
            "SELECT id FROM bill_proposals WHERE status='pending' "
            "ORDER BY created_at, id").fetchall()]
        approved, failed, notes = [], [], []
        for pid in pids:
            r = _decide_proposal(conn, pid, "approve", {})
            if r.get("error"):
                failed.append({"pid": pid, "error": r["error"],
                               "superseded": _proposal_superseded(conn,
                                                                  pid)})
            else:
                approved.append(r.get("approved"))
                if r.get("note"):
                    notes.append({"payee": r.get("approved"),
                                  "note": r["note"]})
                _log_proposal(conn, user, pid, "approve", r)
    finally:
        conn.close()
    return {"ok": True, "approved": len(approved), "payees": approved,
            "failed": failed, "notes": notes}


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


def _parse_cadence(value: str) -> tuple[str, int]:
    """A CADENCES value ("<FREQ>:<interval>") → (FREQ, interval), or a 400.
    Both doors that write a bill's schedule read it here: the bill save and
    an approved offer the person adjusted first."""
    from ..engine import bills
    freq, _, iv = str(value or "").partition(":")
    freq = freq.strip().upper()
    # Allowlist the frequency BEFORE any mutation: an unknown token (e.g.
    # "HOURLY") is not ONE_TIME/ENVELOPE, so it reaches PER_YEAR[freq] in
    # recurring._monthly and raises KeyError → a 500 that lands AFTER the
    # rename/config migration has already committed (partial mutation).
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY",
                    "ONE_TIME", "ENVELOPE"):
        raise HTTPException(400, "bad cadence frequency")
    try:
        interval = int(iv or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "bad cadence interval")
    # Bound the cycle, not the count: an unbounded value later overflows
    # dt.timedelta in the occurrence expansion (a persistent 500 on every
    # Today render), and a count that is modest for MONTHLY is centuries for
    # YEARLY — the nightly roll would carry the due date past any date the
    # product can read. Twenty years is well past any real bill cadence.
    if interval < 1 or bills.cadence_too_long(freq, interval):
        raise HTTPException(400, "cadence interval out of range")
    return freq, interval


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


class _ArchivedBillExists(Exception):
    """An add named a bill that is archived; the save writes nothing."""

    def __init__(self, payee: str):
        super().__init__(payee)
        self.payee = payee


@router.post("/bills/save")
def bills_save_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """Add or edit a bill.

    Body: payee, amount (positive $), cadence "<FREQ>:<interval>" (see
    CADENCES), next_due (ISO date or ""), income (bool), orig_payee (set on
    rename), cap (int occurrence cap; ""/null clears; only touched when the
    key is present), merchant (match key for NEW bills only), merchants
    (the display names of the merchants the bill pays — its identity),
    show_today / match_category (bools) and category (string) — each only
    touched when its key is present. An add (no orig_payee) over an
    archived bill's name answers 409 {detail, archived: true, payee} unless
    the body carries restore: true, which brings the bill back with the
    values sent."""
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
    freq, interval = _parse_cadence(
        _body_text(body, "cadence", default="MONTHLY:1"))
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
        if (len(cat_s) > data.CATEGORY_MAX_LEN
                and not _category_long_ok(user, cat_s)):
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
            except (TypeError, ValueError, OverflowError):
                raise HTTPException(400, "companion window must be days")
            if not 0 <= win <= 14:
                raise HTTPException(400, "companion window is 0–14 days")
            companions_v.append({"tokens": label, "amount": famt,
                                 "window_days": win})
    if "txn_category" in body:
        txn_cat_v = str(body["txn_category"] or "").strip()
        if (len(txn_cat_v) > data.CATEGORY_MAX_LEN
                and not _category_long_ok(user, txn_cat_v)):
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
            # uncapped: a chip must name a merchant the ledger already has
            # (checked below), so it writes no new name, and an imported
            # descriptor over the rename cap is a real merchant a bill pays
            _merchant_name_or_400("merchant", m, max_len=None)
            if m.strip():
                merchants_v.append(m.strip())
    # Validate next_due up front too: save_bill parses it via as_date only
    # AFTER the rename commits, so a malformed date would otherwise leave a
    # half-applied rename behind (same partial-mutation window as amount).
    next_due_in = _body_text(body, "next_due")
    if next_due_in:
        # the same year range save_bill's as_date enforces, or a year it
        # refuses would raise there after the rename committed
        _iso_date(next_due_in, "next_due must be an ISO date")
    conn = _conn(user)
    try:
        if orig_payee and bills.get_bill(conn, orig_payee) is None:
            # The form carries the bill's name as the list showed it, and
            # names are read trimmed here, so a stored name that carries
            # stray whitespace never matches by string and the edit reads
            # as "deleted" though the row is right there. The slug ignores
            # whitespace: resolve the row by it and edit under the name
            # the row really holds — which makes this save a rename onto
            # the trimmed name, so the stray whitespace leaves with it.
            stored = conn.execute("SELECT payee FROM bills WHERE id=%s",
                                  (bills._slug(orig_payee),)).fetchone()
            if stored is not None and (stored["payee"] or "").strip() == orig_payee:
                orig_payee = stored["payee"]
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
        # The edit form names the bill it was opened on (`orig_payee`), so
        # under that bill's row lock the save can tell that it was archived
        # or deleted since: an archived bill stays out of the plan until it
        # is restored, and a deleted one is not quietly recreated by the
        # insert path. The queue's amount accept sends `orig_payee` too, so
        # it is an edit and never brings an archived bill back. A save
        # without `orig_payee` is an add. An add whose name an ARCHIVED
        # bill already holds would otherwise write into that row and leave
        # it archived while the form reports the bill added, so it is
        # refused with `archived: true` unless the body says
        # `restore: true` — the explicit ask to bring the bill back with
        # the values just entered. The rename, the bill write and these
        # checks commit together, so a refusal leaves no half-applied
        # rename behind either.
        renamed = bool(orig_payee and orig_payee != payee)
        revive = False
        with conn.transaction():
            if not orig_payee:
                held = conn.execute(
                    "SELECT payee, active FROM bills WHERE id=%s FOR UPDATE",
                    (bills._slug(payee),)).fetchone()
                if held is not None and not held["active"]:
                    if body.get("restore") is not True:
                        raise _ArchivedBillExists(held["payee"] or payee)
                    revive = True
            if orig_payee:
                if renamed:
                    # the rename's own lock order — pending proposals first,
                    # then the bill — so taking the bill here cannot invert
                    # the order an approval locks them in
                    conn.execute(
                        "SELECT id FROM bill_proposals WHERE status='pending' "
                        "AND (payee=%s OR (kind IN ('attach','merchant') "
                        "AND evidence->>'bill_payee'=%s)) FOR UPDATE",
                        (orig_payee, orig_payee))
                held = conn.execute(
                    "SELECT active FROM bills WHERE payee=%s FOR UPDATE",
                    (orig_payee,)).fetchone()
                if held is None:
                    raise HTTPException(
                        409, "this bill was deleted after the form was "
                             "opened — add it again to keep tracking it")
                if not held["active"]:
                    raise HTTPException(
                        409, "this bill was archived after the form was "
                             "opened — restore it to edit it")
            # rename: move the row (its payee-keyed config follows below).
            # A rename onto an EXISTING different bill aborts the whole save —
            # falling through would overwrite the unrelated target.
            if renamed:
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
                        and 1 <= _n <= 240
                        # the count cap is not the length cap: a legacy
                        # YEARLY:21+ would make save_bill refuse the Accept
                        # until a new cadence was picked by hand
                        and not bills.cadence_too_long(_f.upper(), _n)):
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
            # Direction: an explicit flag wins. Absent, an existing bill
            # (archived included) keeps its own, as it keeps its cadence and
            # category; a new bill takes the direction its payee's history
            # shows — the same majority vote the history page renders as
            # "received" — so adding an employer from its own history page
            # saves income, not an expense that would match no row and read
            # as an unpaid paycheck-sized charge every cycle.
            if "income" in body:
                income_v = bool(body.get("income"))
            elif old is not None:
                income_v = (old["amount"] or 0) > 0
            else:
                income_v = bool(bills.merchant_history(
                    conn, merchant_val or payee, months=None)["inflow"])
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
                    last_seen=last_seen, income=income_v,
                    merchant=merchant_val,
                    manual_touch=True,  # human edit → drift grace
                    show_today=show_today_v, match_category=match_cat_v,
                    txn_category=txn_cat_v, companions=companions_v,
                    merchants=merchants_v, revive=revive)
            except ValueError as e:     # $0 amount / slug collision / bad date
                raise HTTPException(400, str(e))
        if renamed:
            # payee-keyed settings follow the rename once it has committed
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
                         "restored" if revive
                         else "edited" if _was is not None else "added",
                         target=payee, label=payee,
                         detail={"before": _was, "after": _now})
        return ({"ok": True, "payee": payee, "restored": True} if revive
                else {"ok": True, "payee": payee})
    except _ArchivedBillExists as e:
        return JSONResponse(status_code=409, content={
            "detail": f"an archived bill named {e.payee!r} already exists — "
                      "restore it to track it again",
            "archived": True, "payee": e.payee})
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
    from ..engine import bills, budget, merchant_dedup
    from ..engine.compat import as_dict
    from .pages import _cadence_label, _proposal_summary
    conn = _conn(user)
    try:
        # whether this name is a merchant the ledger files rows under — the
        # clients offer "Merge into…" only then (a bill's own label that no
        # row displays has nothing to merge)
        merchant_in_use = bool(payee.strip()) and merchant_dedup.display_in_use(
            conn, payee)
        hist = bills.merchant_history(
            conn, payee, window=window if window in bills.HISTORY_WINDOWS else "all")
        bill = bills.get_bill(conn, payee)
        # a merchant clicked from Transactions may already be covered by a
        # bill under a different display name — link to it instead of
        # offering a duplicate add. Uses the bill's REAL matcher.
        #
        # Several bills can match one string (the matcher keys on a brand's
        # leading word, so "Zenith Gas" and "Zenith Coffee" both match a
        # coffee-shop descriptor). The answer has to be the same on every
        # load and the bill that names this merchant most closely: the one
        # whose match key shares the most words with the text, then the
        # longer key, then the payee name as a stable tiebreak. Only live
        # bills are candidates — an archived one covers nothing.
        covered_by = None
        if bill is None and payee.strip():
            text = payee.lower()
            words = set(re.findall(r"[a-z0-9]+", text))
            best = None
            for b in conn.execute(
                    "SELECT payee, merchant FROM bills "
                    "WHERE active=1 AND amount<0 ORDER BY payee").fetchall():
                if b["merchant"] and budget.text_matches_merchant(
                        text, b["merchant"]):
                    key = b["merchant"].lower()
                    shared = len(words & set(re.findall(r"[a-z0-9]+", key)))
                    rank = (-shared, -len(key), b["payee"])
                    if best is None or rank < best[0]:
                        best = (rank, b["payee"])
            covered_by = best[1] if best else None
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
        bands = (budget.bill_amount_bands(bill["amount"], bill["raw"])
                 if bill is not None else
                 [(ref, budget.bill_tolerance(ref), None)] if ref else None)
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
            or (bands is not None
                and (row["amount"] < 0 if want_inflow else row["amount"] > 0)
                and budget.amount_fits(bands, row["amount"], row.get("date"))))
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
            # an archived bill under this payee: `bill` is null for it (it
            # is not live), so a client offering "add as recurring" here
            # would edit that bill instead — offer Restore on true
            "bill_archived": bill is not None and not bill["active"],
            "merchant_in_use": merchant_in_use,
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
    category = _category_text(body, "category", user=user)
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
    category = _category_text(body, "category", user=user)
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
    category = _category_text(body, "category", user=user)
    if not merchant or not category:
        raise HTTPException(400, "merchant and category required")
    conn = _conn(user)
    try:
        # this one rule, not the history page's token family: the key is
        # the rule's stored merchant, whose word match can reach other
        # merchants' rows and teach them too
        out = data.set_merchant_category(conn, merchant, category,
                                         family=False)
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
    new = _category_text(body, "new", "to", user=user)
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


# local conversational assistant over your own ledger. Tool-routed, and
# every tool only reads the ledger; the one row an ask writes is its own
# entry in the household's question log (assistant.record_turn, trimmed
# per asker so no one asker can push out another's). Runs on the
# household's configured LLM. Hidden when no LLM is configured.

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
    """Answer a question; `thread_id` in the body makes it a follow-up of
    that conversation (404 when the household has none such, 400 when it
    is not a uuid).

    The reply is the answer (`answer`, `blocks`, `tools_used`) plus what
    became of it in the log:
      `id`        the kept turn's id, or null when nothing was kept;
      `thread_id` the conversation it belongs to — its own id for a new
                  question — or null when no conversation holds it;
      `saved`     true when the turn was kept;
      `removed`   true when this was a follow-up and its conversation was
                  deleted while the answer was being written: the answer
                  still comes back (it was waited for), nothing is kept,
                  and `thread_id` is null, so a client starts the next
                  question as a new conversation rather than following
                  up on one that is gone.
    An answer that could not be kept for any other reason has `saved`
    false, and `thread_id` null on a new question (there is no
    conversation to follow up on) or the conversation's id on a
    follow-up (that conversation is still there)."""
    from ..engine import assistant
    question = _body_text(body, "question")
    if not question:
        raise HTTPException(400, "question required")
    # a follow-up names the conversation it continues; the model then
    # sees that thread's earlier turns — and only that thread's
    thread_id = body.get("thread_id") or None
    if thread_id is not None:
        try:
            thread_id = str(uuid.UUID(str(thread_id)))
        except ValueError:
            raise HTTPException(400, "bad thread id")
    conn = _conn(user)
    try:
        context: list[dict] = []
        if thread_id:
            context = assistant.thread_context(conn, thread_id)
            if not context:
                raise HTTPException(404, "no such conversation")
        out = assistant.ask(conn, question, context=context)
        # an answered question joins the household's log; a reply that
        # could not be kept still reaches the asker, and says so — a
        # silent null thread would turn the next "follow-up" into a fresh
        # question with no context and nothing telling anyone why
        removed = False
        try:
            out["id"] = assistant.record_turn(conn, user, question, out,
                                              thread_id=thread_id)
        except assistant.ThreadRemoved:
            conn.rollback()
            out["id"], removed = None, True
        except Exception as e:                            # noqa: BLE001
            import logging
            conn.rollback()
            logging.getLogger("oikonome.api").warning(
                "assistant: answer not kept: %s", type(e).__name__)
            out["id"] = None
        out["saved"] = out["id"] is not None
        out["removed"] = removed
        out["thread_id"] = None if removed else (thread_id or out["id"])
        return out
    except HTTPException:
        raise
    except assistant.AssistantUnavailable:
        raise HTTPException(503, "the assistant needs a local LLM configured")
    except assistant.AssistantBusy:
        raise HTTPException(503, "the assistant is busy — try again in a "
                                 "moment")
    except Exception as e:                # LLM/network failure → friendly 502
        raise HTTPException(502, f"assistant error: {type(e).__name__}")
    finally:
        conn.close()


# a saved conversation keeps its newest turns: this many, and this many
# blocks in all (each block is at most a 6,000-character paragraph or a
# bounded chart), so one request cannot ask for megabytes of SVG
REPORT_TURNS = 50
REPORT_BLOCKS = 120


@router.post("/assistant/report",
             dependencies=[Depends(limit("assistant_report", 60, 3600))])
def assistant_report_api(user: dict = Depends(_user()),
                         body: dict = Body(...)):
    """One answer — or, with `turns` (a list of {question, blocks}), a
    whole conversation — as a self-contained HTML page (inline SVG +
    CSS, no script) to save or share. The client sends the turns back —
    nothing is stored server-side — and every block is re-validated
    here, so a client can no more smuggle markup into the file than the
    model can. A turn with nothing valid in it is left out; a report
    with none is a 400. A conversation has no turn limit, so the page
    keeps the NEWEST turns — the ones a person saving a long conversation
    came for — up to REPORT_TURNS of them and REPORT_BLOCKS blocks in
    all, and says how many earlier questions it left out; `partial` is
    the client saying the conversation's start was not loaded on its
    side either."""
    from ..engine import assistant, assistant_blocks
    # the cheap refusal first: nothing is parsed or drawn for an instance
    # with no assistant
    conn = _conn(user)
    try:
        if not assistant.available(conn):
            raise HTTPException(503, "the assistant needs a local LLM configured")
        today = _today(conn)
    finally:
        conn.close()
    turns: list[tuple[str, list[dict]]] = []
    omitted = 0
    sent = body.get("turns")
    if isinstance(sent, list):
        valid: list[tuple[str, list[dict]]] = []
        for t in sent:
            if not isinstance(t, dict):
                continue
            blks = assistant_blocks.clean(t.get("blocks"))
            if blks:
                valid.append((_body_text(t, "question"), blks))
        if not valid:
            raise HTTPException(400, "nothing to report")
        kept = valid[-REPORT_TURNS:]
        while len(kept) > 1 and sum(len(b) for _, b in kept) > REPORT_BLOCKS:
            kept = kept[1:]
        omitted = len(valid) - len(kept)
        turns = kept
        question = turns[0][0]
    else:
        question = _body_text(body, "question")
        blocks = assistant_blocks.clean(body.get("blocks"))
        if not blocks:
            raise HTTPException(400, "nothing to report")
        turns.append((question, blocks))
    partial = body.get("partial") is True
    note = None
    if omitted and partial:
        note = (f"{omitted} earlier question{'s' if omitted != 1 else ''} in this "
                "conversation not included, and the ones before them were not loaded")
    elif omitted:
        note = (f"{omitted} earlier question{'s' if omitted != 1 else ''} in this "
                "conversation not included")
    elif partial:
        note = "earlier questions in this conversation not included (its start was not loaded)"
    page = assistant_blocks.report_html(question, turns[0][1], today.isoformat(),
                                        turns=turns, note=note)
    what = "conversation" if len(turns) > 1 else "answer"
    name = f"oikonome-{what}-{today.isoformat()}.html"
    return Response(page, media_type="text/html; charset=utf-8",
                    headers={"content-disposition":
                             f'attachment; filename="{name}"',
                             "cache-control": "no-store"})


@router.get("/assistant/history")
def assistant_history_api(user: dict = Depends(_user()),
                          limit: int = Query(assistant.HISTORY_PAGE),
                          before: str | None = Query(None),
                          before_id: str | None = Query(None)):
    """The household's answered questions, newest first — every member
    reads the same log, so a chart drawn on the phone is on the web too.
    The shared demonstration login is shown its own address only: every
    visitor holds it, so who asked what would hand each of them the
    household's people."""
    conn = _conn(user)
    try:
        out = assistant.history(conn, limit=limit, before=before,
                                before_id=before_id)
    except ValueError:
        raise HTTPException(400, "bad cursor")
    finally:
        conn.close()
    if demoguard.people_hidden(user):
        for t in out["turns"]:
            t["asked_by"] = demoguard.own_address_only(user, t["asked_by"])
    return out


@router.delete("/assistant/history/{turn_id}")
def assistant_forget_api(turn_id: str, user: dict = Depends(_user())):
    """Drop one turn — the question and answer with this id — and nothing
    else of its conversation. The apps already installed send a turn's
    id here to remove that one answer, so this door keeps that meaning;
    a whole conversation goes through /assistant/threads/{thread_id}.
    A turn already gone answers 200 with deleted 0, never 404: the
    installed apps read a 404 here as "could not remove it" and leave the
    row on screen, when the row is in fact gone. 400 for a bad id.

    A conversation's OPENING turn (its id is the thread's) goes only as
    the conversation's last row: while follow-ups remain, the door keeps
    it and answers 200 {ok, deleted: 0, reason}. Refused rather than
    widened to the whole conversation — the installed apps mean one answer
    by this id and expect exactly {ok, deleted}, so taking the follow-ups
    with it would remove rows nobody chose; and deleting the opening alone
    would leave those follow-ups headless."""
    # the shared demonstration login is every visitor at once: deleting
    # from the log as it would remove what other visitors asked
    demoguard.deny_shared_log(user)
    try:
        turn_id = str(uuid.UUID(str(turn_id)))
    except ValueError:
        raise HTTPException(400, "bad id")
    conn = _conn(user)
    try:
        out = assistant.forget_turn(conn, turn_id)
    finally:
        conn.close()
    return {"ok": True, **out}


@router.delete("/assistant/threads/{thread_id}")
def assistant_forget_thread_api(thread_id: str,
                                user: dict = Depends(_user())):
    """Drop one conversation: every turn of the thread named, whoever in
    the household asked it — the log is the household's, read alike by
    every member, and a member may tidy it (a viewer may not: the role
    gate refuses the write). 404 when the household has no such
    conversation, 400 when the id is not a uuid."""
    demoguard.deny_shared_log(user)    # see assistant_forget_api
    try:
        thread_id = str(uuid.UUID(str(thread_id)))
    except ValueError:
        raise HTTPException(400, "bad id")
    conn = _conn(user)
    try:
        gone = assistant.forget_thread(conn, thread_id)
    finally:
        conn.close()
    if not gone:
        raise HTTPException(404, "no such conversation")
    return {"ok": True, "deleted": gone}


@router.delete("/assistant/history")
def assistant_clear_api(user: dict = Depends(_user())):
    demoguard.deny_shared_log(user)    # see assistant_forget_api
    conn = _conn(user)
    try:
        return {"ok": True, "deleted": assistant.forget(conn)}
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
            # …and a disconnected or restored-shell bank's account is a
            # frozen balance the forecast ignores (forecast._checking_start)
            if pin and conn.execute(
                    "SELECT 1 FROM accounts a JOIN items i ON i.id = a.item_id "
                    "WHERE a.id=%s AND i.aggregator IN ('plaid','mx','simplefin','simplefin-org') "
                    "AND COALESCE(i.status,'') IN ('archived','restored')",
                    (account_id,)).fetchone():
                raise HTTPException(
                    400, "that account's bank is disconnected — reconnect "
                         "it, or pick a live checking account")
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
    kind = _body_text(body, "kind", default="depository/checking")
    # A bare "checking"/"savings" (the business wizards on both clients
    # sent that) is a subtype, not a type: stored as type='checking' the
    # account matched no depository filter, so it dropped out of cash,
    # the wizard's own list and every aggregate keyed on the type.
    kind = {"checking": "depository/checking",
            "savings": "depository/savings",
            "credit": "credit/credit card"}.get(kind.strip().lower(), kind)
    typ, _, sub = kind.partition("/")
    # same vocabulary as /accounts/classify — an arbitrary type silently
    # drops the account out of every aggregate
    if typ not in ("depository", "credit", "investment", "loan", "other"):
        raise HTTPException(400, "unknown account type")
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
          from . import setup as _setup
          try:
            r = _setup.connect_simplefin(conn, token)
          except _setup.SimplefinFirstPullFailed:
            import logging
            logging.getLogger("oikonome.simplefin").exception(
                "simplefin first pull failed")
            # the token is spent and the bridge saved: telling the person
            # to "check the token" would send them for a new one they
            # don't need
            raise HTTPException(502, _setup.SIMPLEFIN_PULL_RETRYING)
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
                    # the owner's UI theme: auto/light/dark (absent = the
                    # dark default)
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
                    # {daily,monthly,yearly} local-time schedule
                    "email_schedule",
                    # the Net Worth page's investment fee estimate and
                    # money-in anchors (Settings → Connections → Investment
                    # fees & basis): {institution fragment: yearly fraction},
                    # [account masks that pay no advisory fee], {account
                    # mask: dollars put in}
                    "advisory_fees", "advisory_fee_exempt_masks",
                    "investment_basis_override",
                    # how far back the ledger reaches (1/3/6/12 months;
                    # absent = everything a source has): shortens a new bank
                    # link's backfill and trims file imports
                    "history_months",
                    # how the household set up its bills: "auto" (the
                    # finder proposes) or "manual" (typed in by hand), and
                    # whether the nightly finder proposes new bills and
                    # income at all (amount drift is not affected)
                    "setup_mode", "bill_detection_auto")


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


def _keep_masked_llm_url(sent: str, cfg: dict) -> str:
    """A saved LLM endpoint URL with any masked credential restored from
    the stored config (GET masks them — see _settings_view), or a 400 when
    the mask no longer matches anything stored."""
    from ..engine.llm_categorize import unredact_url
    stored = [str(cfg.get("llm_url") or "")] + [
        str(b.get("url") or "") for b in (cfg.get("llm_backends") or [])
        if isinstance(b, dict)]
    try:
        return unredact_url(sent, stored)
    except ValueError as e:
        raise HTTPException(400, str(e))


# Removed members' addresses, kept only as digests so the
# tenant config never goes on holding a departed person's address in the
# clear. Capped: it only has to outlive the settings pages that were open
# when the removal happened.
_REMOVED_KEY = "email_removed_members"
_REMOVED_CAP = 50


def _address_digest(email: str) -> str:
    import hashlib
    return hashlib.sha256(
        str(email or "").strip().lower().encode()).hexdigest()


def remember_removed_address(cfg: dict, email: str) -> None:
    """Note (by digest) that this address left the household, inside the
    caller's config transaction, so a stale settings page cannot quietly
    put it back on the mail list."""
    if not str(email or "").strip():
        return
    d = _address_digest(email)
    seen = [x for x in (cfg.get(_REMOVED_KEY) or []) if x != d]
    cfg[_REMOVED_KEY] = (seen + [d])[-_REMOVED_CAP:]


def _drop_stale_readds(conn, cfg: dict, prev: set, recips: list,
                       added) -> list:
    """Every client saves the WHOLE recipient list from a copy it loaded
    earlier. A page loaded before a member was removed still holds that
    member's address, and its next unrelated save would write them back
    onto the household's mail. So an address that was not on the stored
    list and belongs to no one in the household survives only if the
    person actually added it in this edit: clients name their adds in
    `email_recipients_added`. A client too old to send that field is held
    to the narrower rule — it cannot restore an address whose owner was
    removed from the household."""
    new = [r for r in recips if r.strip().lower() not in prev]
    if not new:
        return recips
    members = {str(r["email"]).strip().lower() for r in conn.execute(
        "SELECT email FROM users WHERE tenant_id = "
        "current_setting('app.tenant_id')::uuid").fetchall()}
    tomb = set(cfg.get(_REMOVED_KEY) or [])
    if isinstance(added, (list, str)):
        meant = {a.strip().lower()
                 for a in mailguard.parse_recipients(added)}
        drop = {r.strip().lower() for r in new
                if r.strip().lower() not in members
                and r.strip().lower() not in meant}
        # an address someone deliberately adds back is theirs to add
        if tomb and meant:
            keep_tomb = [x for x in cfg.get(_REMOVED_KEY) or []
                         if x not in {_address_digest(m) for m in meant}]
            if keep_tomb:
                cfg[_REMOVED_KEY] = keep_tomb
            else:
                cfg.pop(_REMOVED_KEY, None)
    else:
        drop = {r.strip().lower() for r in new
                if r.strip().lower() not in members
                and _address_digest(r) in tomb}
    return [r for r in recips if r.strip().lower() not in drop]



def _apply_recipient_deltas(conn, cfg: dict, body: dict, user: dict) -> None:
    """An add or a remove names the one address it changes instead of
    posting the whole list. A whole list is a copy loaded earlier: saved
    from a second device, it silently drops whatever the first device
    added since. Applied to the STORED list inside the caller's config
    transaction, so two edits made at once both land.

    Without a whole list in the body, `email_recipients_added` is the add
    itself; alongside one it stays the hint `_drop_stale_readds` reads.
    An added address passes the same rules as one in a whole list: valid,
    under the cap, and on hosted a member of this household."""
    deltas = {}
    for key in ("email_recipients_added", "email_recipients_removed"):
        if key not in body:
            continue
        if not isinstance(body[key], (list, str)):
            raise HTTPException(400, f"{key} must be a list of addresses")
        deltas[key] = mailguard.parse_recipients(body[key], cap=None)
    if not deltas:
        return
    added = deltas.get("email_recipients_added") or []
    removed = {a.strip().lower()
               for a in deltas.get("email_recipients_removed") or []}
    if demoguard.people_hidden(user):
        # the shared demonstration login is shown only its own row, so
        # the only entry it can mean to take off is its own
        mine = str(user.get("email") or "").strip().lower()
        removed &= {mine}
    stored = mailguard.parse_recipients(cfg.get("email_recipients") or [],
                                        cap=None)
    have = {r.strip().lower() for r in stored}
    fresh = [a for a in added
             if a.strip().lower() not in have
             and a.strip().lower() not in removed]
    mailguard.check_recipients(conn, fresh)
    mailguard.refuse_owner_added(user["tenant_id"], fresh)
    out = [r for r in stored if r.strip().lower() not in removed] + fresh
    if len(out) > mailguard.MAX_RECIPIENTS:
        raise HTTPException(
            400, f"that is {len(out)} recipients — the limit is "
                 f"{mailguard.MAX_RECIPIENTS}. Remove some and save again.")
    if added and cfg.get(_REMOVED_KEY):
        # an address someone deliberately adds back is theirs to add
        meant = {_address_digest(a) for a in added}
        keep = [x for x in cfg[_REMOVED_KEY] if x not in meant]
        if keep:
            cfg[_REMOVED_KEY] = keep
        else:
            cfg.pop(_REMOVED_KEY, None)
    if out:
        cfg["email_recipients"] = out
    else:
        cfg.pop("email_recipients", None)

def _apply_email_pref(cfg: dict, change, user: dict) -> None:
    """One address, one kind, on or off — applied to the stored choices
    inside the caller's config transaction, so two devices changing two
    boxes at once both land. The owner decides for anyone on the mail;
    everyone else, and the shared demonstration login (which is shown no
    one else's row), only for their own address."""
    from ..notify import email_prefs
    if not isinstance(change, dict):
        raise HTTPException(
            400, "email_prefs_set must be {address, kind, on}")
    kind, on = change.get("kind"), change.get("on")
    if kind not in email_prefs.KINDS:
        raise HTTPException(
            400, f"kind must be one of {', '.join(email_prefs.KINDS)}")
    if not isinstance(on, bool):
        raise HTTPException(400, "on must be true or false")
    boxes = mailguard.parse_recipients(
        change.get("address") if isinstance(change.get("address"), str)
        else "", cap=None)
    if len(boxes) != 1:
        raise HTTPException(400, "address must be one email address")
    address = boxes[0].strip().lower()
    mine = str(user.get("email") or "").strip().lower()
    if address != mine and (user.get("role") != "owner"
                            or demoguard.people_hidden(user)):
        raise HTTPException(
            403, "you can only change which emails you receive yourself")
    try:
        email_prefs.set_pref(cfg, address, kind, on,
                             cap=mailguard.MAX_RECIPIENTS)
    except OverflowError as e:
        raise HTTPException(400, str(e))


def _settings_view(cfg: dict) -> dict:
    out = {k: cfg.get(k) for k in _SETTINGS_FIELDS}
    # Publish the toggle the budget engine actually obeys: a household
    # whose config never stored the key runs with dynamic budgeting ON.
    # Sent raw it would arrive as null, read as off, and the next bucket
    # save would post that false back and switch the feature off unasked.
    out["dynamic_variable_budget"] = bool(
        cfg.get("dynamic_variable_budget", True))
    # the same for the setup choices: what the worker and the importers
    # obey, never a null a client has to guess the meaning of
    from ..sync.base import history_months_of
    out["history_months"] = history_months_of(cfg)
    out["setup_mode"] = _setup_mode_of(cfg)
    out["bill_detection_auto"] = cfg.get("bill_detection_auto") is not False
    # publish the schedule the worker actually runs, so no client has to
    # invent a default for a household that never saved one. Here, not in
    # one handler: the read and the save reply both build from this view,
    # and clients seed their cache from either.
    from ..notify.schedule import effective_email_schedule
    out["email_schedule"] = effective_email_schedule(cfg)
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
    # what a credential rotation paused, for the banner (None = nothing)
    from ..notify import outbound_pause as _outbound
    out["outbound_paused"] = _outbound.paused(cfg)
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
    # endpoint URLs leave masked: a password or key embedded in one is a
    # credential like api_key beside it, and this view is read by every
    # member, viewers included. The save path restores the stored value
    # when the masked form comes back unchanged.
    from ..engine.llm_categorize import redact_url as _redact_url
    if out.get("llm_url"):
        out["llm_url"] = _redact_url(str(out["llm_url"]))
    out["llm_bundled"] = ({"url": _redact_url(_env_url), "model": _env_model,
                           "local": _llm_is_local(_env_url)}
                          if _env_url and not _hosted else None)
    _bks = [b for b in (cfg.get("llm_backends") or []) if isinstance(b, dict)]
    out["llm_backends"] = [
        {"id": str(b.get("id")), "name": b.get("name") or "",
         "url": _redact_url(b.get("url") or ""),
         "model": b.get("model") or "",
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
                    "url": _redact_url(b["url"]), "model": model,
                    "local": _llm_is_local(b["url"])}
        if layer == "legacy":                  # legacy single endpoint
            model = ((_cfg_vision or cfg.get("llm_model"))
                     if role == "vision" else cfg.get("llm_model")) or ""
            return {"source": "tenant", "url": _redact_url(_own_url),
                    "model": str(model),
                    "local": _llm_is_local(_own_url)}
        if _env_url and not _hosted:           # bundled, chosen or fallback
            model = ((_cfg_vision or _env_model)
                     if role == "vision" else _env_model)
            return {"source": "bundled", "url": _redact_url(_env_url),
                    "model": model,
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
                      muted: list[str] | None = None,
                      cfg: dict | None = None) -> list[dict]:
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
    prefs — which kinds of mail this address receives
    (`email_prefs.KINDS`), as the worker reads them; a mute
    still stops all of it.
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
    from ..notify import email_prefs as _email_prefs
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
    # A delivery row is keyed on the ADDRESS, instance-wide: the failure
    # it records may have come from another household's mail. Its details
    # (bounce vs complaint, the provider's own words about the mailbox)
    # belong to whoever that mailbox answers to. This household sees them
    # for an address it has a standing with — one of its own accounts, or
    # a recipient who accepted its invitation — and for any other address
    # only that mail to it is held. A single-household box has no one else
    # whose record it could be, so the tenant count is asked only when an
    # address actually carries a row — and then once for the whole grid,
    # since each ask is an admin connection.
    # The rule itself lives in one place so Resend and this grid cannot
    # drift apart (recipient_invites.delivery_detail_visible).
    from ..notify import recipient_invites as _invites
    multi_tenant = None
    for e in ordered:
        m = members.get(e.lower())
        state = failing.get(e.lower())
        inv = invites.get(e.lower())
        if state and multi_tenant is None:
            multi_tenant = _invites.multi_household()
        if state and not _invites.delivery_detail_visible(
                tenant_id, e, member=bool(m),
                invite_state=inv and inv.get("state"),
                multi_tenant=multi_tenant):
            state = {"state": "bouncing", "reason": None}
        is_owner = bool(owner_email and e.lower() == owner_email.lower())
        rows.append({
            "email": e,
            "owner": is_owner,
            # asked not to receive the scheduled mail (per-person opt-out;
            # the owner can mute themselves too)
            "muted": e.lower() in muted_set,
            "prefs": _email_prefs.row(cfg or {}, e),
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


def _hide_people(view: dict, user: dict) -> dict:
    """Cut the settings view's recipient lists to the caller's own row
    when people are hidden from it. Every door that returns the view calls
    this one helper, so the read and the write answer alike."""
    if demoguard.people_hidden(user):
        for k in ("email_recipients", "email_muted"):
            if k in view:
                view[k] = demoguard.own_addresses_only(user, view.get(k))
        if "email_recipient_status" in view:
            mine = set(demoguard.own_addresses_only(
                user, [r.get("email") for r in
                       view["email_recipient_status"]]))
            view["email_recipient_status"] = [
                r for r in view["email_recipient_status"]
                if r.get("email") in mine]
        # the rotation's record names the paused recipients too; a record
        # left naming nothing the caller may see is no record for it
        paused = view.get("outbound_paused")
        if isinstance(paused, dict):
            paused = {**paused, "recipients": demoguard.own_addresses_only(
                user, paused.get("recipients"))}
            # and the members a recovery removed, by address
            if "removed_members" in paused:
                gone = [m for m in (paused.get("removed_members") or [])
                        if isinstance(m, dict)]
                mine = set(demoguard.own_addresses_only(
                    user, [m.get("email") for m in gone]))
                paused["removed_members"] = [
                    m for m in gone if m.get("email") in mine]
            view["outbound_paused"] = (
                paused if (paused["smtp"] or paused["llm"]
                           or paused["recipients"]
                           or paused.get("aggregator")
                           or paused.get("removed_members")) else None)
    paused = view.get("outbound_paused")
    if (isinstance(paused, dict) and paused.get("removed_members")
            and user.get("role") != "owner"):
        # who an owner's recovery removed is the owner's to review; anyone
        # else reading the household's settings learns how many, not who
        paused = dict(paused)
        paused["removed_members_count"] = len(paused.pop("removed_members"))
        view["outbound_paused"] = paused
    paused = view.get("outbound_paused")
    if (isinstance(paused, dict) and "aggregator_keys" in paused
            and (user.get("role") != "owner"
                 or demoguard.people_hidden(user))):
        # which Plaid or MX client the bank-link hold is about is the
        # owner's to judge (they confirm or remove it); anyone else learns
        # that new links are held, not which client
        paused = dict(paused)
        paused.pop("aggregator_keys")
        view["outbound_paused"] = paused
    return view


def _merge_unseen_people(body: dict, cfg: dict, user: dict) -> None:
    """The shared demonstration login is shown only its own row of the
    recipient and mute lists, and both clients save those lists whole — so
    its save carries only what it saw. Put back every stored entry it was
    not shown before the save reads the body, or one toggle of its own
    stops everyone else's mail and clears their mutes. Its own row is
    whatever it posted."""
    if not demoguard.people_hidden(user):
        return
    mine = str(user.get("email") or "").strip().lower()
    for key, stored in (
            ("email_recipients",
             mailguard.parse_recipients(cfg.get("email_recipients") or [])),
            ("email_muted", list(cfg.get("email_muted") or []))):
        if key not in body:
            continue
        posted = body[key]
        if isinstance(posted, str):
            posted = mailguard.parse_recipients(posted)
        if not isinstance(posted, list):
            continue                    # the save's own type check answers
        unseen = [a for a in stored if str(a).strip().lower() != mine]
        kept = {str(u).strip().lower() for u in unseen}
        if key == "email_muted":
            # a mute is the one entry here that needs no step-up: an
            # address it was never shown, posted on this list, would
            # silence that member's mail. Only its own mute is its to set.
            posted = demoguard.own_addresses_only(user, posted)
        body[key] = unseen + [p for p in posted
                              if str(p).strip().lower() not in kept]


@router.get("/settings")
def settings_get(user: dict = Depends(_user())):
    from ..engine import budget
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
    finally:
        conn.close()
    view = _settings_view(cfg)
    view["email_recipient_status"] = _recipient_status(
        user["tenant_id"], cfg.get("email_recipients") or [],
        cfg.get("email_muted") or [], cfg)
    _hide_people(view, user)
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
    "email_recipients", "email_recipients_added", "email_recipients_removed",
    "taxdocs_allow_remote_llm",
})


def _secret_values(conn, cfg: dict) -> set[str]:
    """Every stored LLM credential in plaintext: the single-endpoint key
    and extra body plus each backend's. Decrypted because a save can
    re-encrypt an unchanged value (the legacy fold-in wraps a plaintext
    key on its way into the backend list), and a fresh ciphertext of the
    same key is not a new credential."""
    from ..db import crypto
    stored = [cfg.get("llm_api_key"), cfg.get("llm_extra_body")]
    for b in cfg.get("llm_backends") or []:
        if isinstance(b, dict):
            stored += [b.get("api_key"), b.get("extra_body")]
    return {crypto.decrypt(conn, v) or "" for v in stored if v} - {""}


def _llm_urls(cfg: dict) -> set[str]:
    urls = {str(cfg.get("llm_url") or "").strip()}
    for b in cfg.get("llm_backends") or []:
        if isinstance(b, dict):
            urls.add(str(b.get("url") or "").strip())
    return urls - {""}


def _outbound_repointed(conn, before: dict, after: dict) -> list[str]:
    """What this save points somewhere NEW that the household's data or
    its sign-in mail travels to. Judged on the stored documents before and
    after the save, not on the body, so the SPA re-posting the whole
    document unchanged repoints nothing; and a removal or a clear is never
    a repoint, because it only takes a destination away.

    Each group is a destination a stolen session could otherwise swap for
    its own: the mail relay carries the owner's password-reset link, the
    LLM endpoint and its credential (which, on a proxy, picks whose account
    logs the prompts) receive every transaction the categorize pass reads,
    the tax-document consent widens what that endpoint is sent, and a new
    daily-mail recipient is a copy of the household's money every morning.
    """
    changed = []

    def _relay(c: dict) -> tuple:
        # unset fields read as the values the sender uses for them, so a
        # client filling in the defaults on a re-save is not a repoint
        # a stored port that does not parse still has to compare — this
        # runs on every settings save, including the one that fixes it
        try:
            port = int(c.get("smtp_port") or 587)
        except (TypeError, ValueError, OverflowError):
            port = str(c.get("smtp_port"))
        # an empty From sends as the relay account when that is an
        # address (report.resolve_smtp), so filling it in is no repoint
        user_s = str(c.get("smtp_user") or "")
        from_s = str(c.get("smtp_from") or "") or (
            user_s if mailguard.valid_address(user_s) else "")
        return (str(c.get("smtp_host") or ""), port, user_s, from_s,
                c.get("smtp_starttls", True) is not False,
                str(c.get("smtp_password") or ""))
    if after.get("smtp_host") and _relay(before) != _relay(after):
        changed.append("smtp")
    if _llm_urls(after) - _llm_urls(before):
        changed.append("llm_url")
    llm_secret_keys = ("llm_api_key", "llm_extra_body", "llm_backends")
    if any(before.get(k) != after.get(k) for k in llm_secret_keys) and (
            _secret_values(conn, after) - _secret_values(conn, before)):
        changed.append("llm_api_key")
    if after.get("taxdocs_allow_remote_llm") and not before.get(
            "taxdocs_allow_remote_llm"):
        changed.append("taxdocs_allow_remote_llm")
    if (mailguard.stored_mailboxes(after.get("email_recipients"))[0]
            - mailguard.stored_mailboxes(before.get("email_recipients"))[0]):
        changed.append("email_recipients")
    return changed


# Investment fee / basis settings: a household names a handful of
# institutions and accounts, so the cap only stops a runaway client.
_INVEST_MAX_ROWS = 50


def _invest_mask(v, field: str) -> str:
    """One account mask as stored: the tail of the account number, which
    is all an aggregator reports and what the net-worth engine compares
    against. Plaid's masks are 2-4 characters and some banks put letters
    in them, so letters are accepted and stored upper-case — the case the
    feeds report them in, so the engine's exact comparison still matches.
    Blank comes back blank for the caller to skip."""
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise HTTPException(
            400, f"{field}: an account mask must be letters or digits")
    m = str(v).strip().upper()
    if m and not (m.isascii() and m.isalnum() and 2 <= len(m) <= 8):
        raise HTTPException(
            400, f"{field}: an account mask is the account's last 2 to 8 "
                 "letters or digits")
    return m


@router.post("/settings")
def settings_set(user: dict = Depends(_user()), body: dict = Body(...)):
    # The demo instance is READ-ONLY: its credentials
    # are shared and printed publicly, so ANY settings write is one visitor
    # reconfiguring the instance for everyone who comes after. Refusing only
    # the "door" keys (_DEMO_DOOR_KEYS — mail/LLM endpoints) is not enough:
    # the whole endpoint is closed. The SPA greys the page out to match, and
    # this is the backstop that makes that true whatever is POSTed.
    demoguard.deny(user)
    # an older store build re-proves in the body rather than through the
    # elevation window; read before the role filter, which knows nothing
    # of these keys
    _proof = ({k: str(body.get(k) or "") for k in
               ("password", "totp_code", "recovery_code")}
              if isinstance(body, dict) else {})
    # A member edits the household's money, not its plumbing: the mail
    # relay, the daily-email recipient list and the AI endpoint are owner
    # fields. They are DROPPED from the body rather than refused, because
    # the SPA posts the whole settings document on every save — refusing
    # would mean a member could never change a budget either.
    body = permissions.settings_for(user["role"], body)

    def _refuse_member_change(key, requested, stored):
        # the owner-only keys a member is refused for changing rather than
        # having them dropped (permissions.OWNER_REFUSED_SETTINGS)
        why = permissions.refused_setting_change(user["role"], key,
                                                 requested, stored)
        if why:
            raise HTTPException(403, why)
    from ..engine import budget
    conn = _conn(user)
    # set inside the lock when the wizard's connect step is left; the pin
    # itself takes the same lock, so it runs after this one is let go
    pin_primary_after = False
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
            # Compared as MAILBOXES: the client re-posts the stored list and
            # the save splits `Name <a>` / `a;b` entries, so raw strings here
            # would make every such entry look new and drop it.
            _prev_entries = cfg.get("email_recipients")
            if isinstance(_prev_entries, list):
                _prev_entries = list(_prev_entries)
            _prev_recips, _prev_asked = mailguard.stored_mailboxes(
                _prev_entries)
            _merge_unseen_people(body, cfg, user)
            # the whole document before the save, so the activity log can
            # name the KEYS that changed (never a value)
            import copy as _copy
            _cfg_was = _copy.deepcopy(cfg)
            _smtp_from_missing = _smtp_user_given = False
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
                # finite alone is not enough: two asset values near the
                # float ceiling overflow the net-worth sum (see _MONEY_MAX)
                if abs(n) > _MONEY_MAX:
                    raise HTTPException(400, f"{field} is too large")
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
                    if k not in ("appearance", "connect", "sync", "import",
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
                # leaving the connect step with one checking account in:
                # the forecast's anchor is obvious, pin it now so the
                # Today checklist's step closes with the wizard's
                if raw.get("connect") in ("done", "skipped"):
                    pin_primary_after = True
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
                # {daily:{on,hour}, monthly:{on,hour}, yearly:{on,hour}} —
                # hours LOCAL (OIKONOME_TZ). (The weekly report is
                # retired; a stored `weekly` entry is dropped
                # on the next save and ignored by the worker.)
                raw = body["email_schedule"]
                if raw in (None, {}):
                    cfg.pop("email_schedule", None)   # legacy daily behavior
                elif not isinstance(raw, dict):
                    raise HTTPException(400, "email_schedule must be an object")
                else:
                    clean = {}
                    # A cadence the body leaves out keeps what the worker
                    # runs for it now (the effective schedule, so a
                    # household that never saved one keeps its default
                    # daily email): a client saving only the monthly card
                    # must not switch the daily email off by omission.
                    # Turning one off is an explicit {"on": false}.
                    from ..notify.schedule import effective_email_schedule
                    _was_sched = effective_email_schedule(cfg)
                    # each cadence (yearly included) carries
                    # per-channel flags — `on` stays email (back-compat),
                    # sms/push are additive
                    for cad in ("daily", "monthly", "yearly"):
                        if cad not in raw:
                            if isinstance(_was_sched.get(cad), dict):
                                raw = {**raw, cad: _was_sched[cad]}
                        c = raw.get(cad) or {}
                        if not isinstance(c, dict):
                            raise HTTPException(400, f"{cad} must be an object")
                        try:
                            hour = int(c.get("hour", 7))
                        except (TypeError, ValueError, OverflowError):
                            raise HTTPException(400, "hour must be a number")
                        if not 0 <= hour <= 23:
                            raise HTTPException(400, "hour must be 0-23")
                        entry = {"on": bool(c.get("on")), "hour": hour}
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
                # A client adopting the device's zone on its own says so.
                # That adoption is only right while the household still
                # follows the instance default: two phones in two places,
                # or a phone restoring an old snapshot, must not overwrite
                # a zone someone chose by hand in the meantime.
                if body.get("timezone_adopted") and localtime.valid_zone(
                        cfg.get("timezone")):
                    raise HTTPException(
                        409, "the household already keeps its own time zone")
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
                # "auto" (follow the OS) is a choice of its own and is
                # stored as one: both clients read an absent theme as the
                # dark default, so storing auto as absence would turn it
                # back into dark on the next reconcile. Only an empty
                # value clears the key, back to that default.
                t = str(body["theme"] or "")
                if t and t not in ("auto", "light", "dark"):
                    raise HTTPException(400, "theme must be auto, light or dark")
                if not t:
                    cfg.pop("theme", None)
                else:
                    cfg["theme"] = t
            if "history_months" in body:
                # a whole number of months from the offered choices, or
                # null for everything. Strict: "6" or 6.5 is a client bug,
                # and guessing would quietly narrow what the next import
                # or bank link keeps.
                from ..sync.base import HISTORY_MONTHS, history_months_of
                hm = body["history_months"]
                if hm == "":
                    hm = None
                if hm is not None and (isinstance(hm, bool)
                                       or not isinstance(hm, int)
                                       or hm not in HISTORY_MONTHS):
                    raise HTTPException(
                        400, "history_months must be 1, 3, 6, 12 or null")
                # owner-only and refused rather than dropped (see
                # permissions.OWNER_REFUSED_SETTINGS); a stored value outside
                # the choices reads as null, as the settings GET publishes it
                _refuse_member_change("history_months", hm,
                                      history_months_of(cfg))
                if hm is None:
                    cfg.pop("history_months", None)
                else:
                    cfg["history_months"] = hm
            if "bill_detection_auto" in body:
                v = body["bill_detection_auto"]
                if not isinstance(v, bool):
                    raise HTTPException(
                        400, "bill_detection_auto must be true or false")
                _refuse_member_change(
                    "bill_detection_auto", v,
                    cfg.get("bill_detection_auto") is not False)
                cfg["bill_detection_auto"] = v
            if "setup_mode" in body:
                sm = body["setup_mode"]
                if sm is not None and sm != "" \
                        and sm not in ("auto", "manual"):
                    raise HTTPException(
                        400, "setup_mode must be auto or manual")
                was = _setup_mode_of(cfg)
                _refuse_member_change("setup_mode", sm or "auto", was)
                if not sm:
                    cfg.pop("setup_mode", None)       # absent = auto
                else:
                    cfg["setup_mode"] = sm
                    # someone who enters their bills by hand has said they
                    # do not want the finder's offers every morning; the
                    # body may still say otherwise in the same save. Only
                    # on the move to manual: a save echoing the stored
                    # choice must not undo detection the owner turned back
                    # on since
                    if sm == "manual" and was != "manual" \
                            and "bill_detection_auto" not in body:
                        cfg["bill_detection_auto"] = False
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
                    except (TypeError, ValueError, OverflowError):
                        raise HTTPException(400, "smtp_port must be a number")
                    if not 1 <= port <= 65535:
                        raise HTTPException(400, "smtp_port out of range")
                    cfg["smtp_port"] = port
                    if "smtp_user" in body:
                        cfg["smtp_user"] = str(body["smtp_user"] or "").strip()
                    if "smtp_from" in body:
                        cfg["smtp_from"] = str(body["smtp_from"] or "").strip()
                    if not cfg.get("smtp_from"):
                        # An empty From would send as a made-up local
                        # address that an authenticated relay refuses (the
                        # sender is not one the account owns) or the
                        # recipient's DMARC check drops, and the daily
                        # email would stop with nothing saying why. The
                        # relay account is almost always the mailbox, so it
                        # stands in, as the setup wizard already does.
                        # Without one the save is refused, after the
                        # step-up check below so an unproven session learns
                        # nothing from the order of the answers.
                        relay_user = str(cfg.get("smtp_user") or "")
                        if mailguard.valid_address(relay_user):
                            cfg["smtp_from"] = relay_user
                        elif not relay_user and (
                                default_from := _relay_default_from(host)):
                            # an unauthenticated LAN relay, which is all
                            # the setup wizard asks for: it accepts any
                            # sender, so a no-reply address on the
                            # instance's own name (or the relay's) is one
                            # it and the recipient can both read
                            cfg["smtp_from"] = default_from
                        else:
                            _smtp_from_missing = True
                            _smtp_user_given = bool(relay_user)
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
            # A phone sends the one-address delta AND the whole list from
            # its copy, because a server older than the delta keys knows
            # only the list; here the delta wins, so the stale copy never
            # drops an address another device added. The delta wins only
            # when it actually names an address: older phones and web tabs
            # always send an empty `email_recipients_added` beside the whole
            # list, and for them the shortened list IS the edit — taking an
            # empty delta over it would silently keep the removed address
            _delta_keys = ("email_recipients_added", "email_recipients_removed")
            if "email_recipients" in body and any(body.get(k) for k in _delta_keys):
                body = {k: v for k, v in body.items() if k != "email_recipients"}
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
                recips = _drop_stale_readds(
                    conn, cfg, _prev_recips, recips,
                    body.get("email_recipients_added"))
                mailguard.check_recipients(conn, recips)
                if recips:
                    cfg["email_recipients"] = recips
                else:
                    cfg.pop("email_recipients", None)
            else:
                _apply_recipient_deltas(conn, cfg, body, user)
            if "email_muted" in body and (body.get("email_muted_add")
                                           or body.get("email_muted_remove")):
                # same rule as the recipient list: a delta that names an
                # address wins over the fallback copy an older server would
                # have used; an empty one leaves the whole list in charge
                body = {k: v for k, v in body.items() if k != "email_muted"}
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
            # A toggle names the one address it changes instead of posting
            # the whole list: two members muting themselves from two phones,
            # or one phone working from a cached copy, must not un-mute
            # each other. Applied to the stored list inside this write.
            for key, add in (("email_muted_add", True),
                             ("email_muted_remove", False)):
                if key not in body:
                    continue
                if not isinstance(body[key], (list, str)):
                    raise HTTPException(400, f"{key} must be a list of addresses")
                delta = {m.lower() for m in
                         mailguard.parse_recipients(body[key], cap=None)}
                # the shared demonstration login is shown only its own
                # address, so its own is the only mute it can mean to
                # change: anyone else's would silence (or un-silence) a
                # member it was never shown
                delta = set(demoguard.own_addresses_only(user, delta))
                have = {str(m).lower() for m in (cfg.get("email_muted") or [])}
                have = have | delta if add else have - delta
                if len(have) > mailguard.MAX_RECIPIENTS:
                    raise HTTPException(
                        400, f"that is {len(have)} muted addresses — "
                             f"the limit is {mailguard.MAX_RECIPIENTS}.")
                if have:
                    cfg["email_muted"] = sorted(have)
                else:
                    cfg.pop("email_muted", None)
            if "email_prefs_set" in body:
                _apply_email_pref(cfg, body["email_prefs_set"], user)
            if "email_send_hour_utc" in body:
                try:
                    cfg["email_send_hour_utc"] = max(0, min(23,
                                                     int(body["email_send_hour_utc"])))
                except (TypeError, ValueError, OverflowError):
                    raise HTTPException(400, "hour must be 0-23")
            if "birthdate" in body:
                v = str(body["birthdate"] or "").strip()
                if not v:
                    cfg.pop("birthdate", None)      # cleared → age falls back
                else:
                    bd = _iso_date(
                        v, "birthdate must be an ISO date (YYYY-MM-DD)")
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
                # the stored goals by name, for the match-words rule below:
                # both editors and the balance planner post the WHOLE list,
                # so an untouched legacy row rides along on every save
                stored_goals = {
                    str(sg.get("name") or "").strip().lower(): sg
                    for sg in (cfg.get("savings_goals") or [])
                    if isinstance(sg, dict)}
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
                    # the match words are a list: an object or a number
                    # cannot be sliced (a 500), and a bare string would be
                    # sliced into single letters that match everything
                    tokens_in = g.get("tokens") or []
                    if not isinstance(tokens_in, list):
                        raise HTTPException(
                            400, f"goal '{name}': tokens must be a list")
                    goal = {"name": name, "target": round(max(0.0, target), 2),
                            "monthly_plan": plan,
                            "account_id": str(g.get("account_id") or "") or None,
                            "tokens": [str(t).strip()[:60]
                                       for t in tokens_in[:8]
                                       if str(t).strip()],
                            "start_balance": round(
                                _num(g.get("start_balance") or 0,
                                     "start_balance"), 2)}
                    # Match words with no account cannot be measured: they
                    # match both legs of a transfer and nothing says which
                    # side is the fund, so the goal would read as plan-only
                    # forever. Refuse the shape on the way in, except for a
                    # stored goal posted back with the same (absent) account
                    # and the same words: refusing that row would lock every
                    # later save of the list, the planner's included.
                    if goal["tokens"] and goal["account_id"] is None:
                        was = stored_goals.get(name.lower())
                        was_tokens = ([str(t).strip()[:60]
                                       for t in (was.get("tokens") or [])[:8]
                                       if str(t).strip()]
                                      if was and isinstance(
                                          was.get("tokens"), list) else None)
                        if not (was and not was.get("account_id")
                                and was_tokens == goal["tokens"]):
                            raise HTTPException(
                                400, f"goal '{name}': match words need an "
                                     "account — pick the account the money "
                                     "lands in, or clear the match words")
                    if mode == "sweep":
                        goal["mode"] = "sweep"
                    td = str(g.get("target_date") or "").strip()
                    if td:
                        goal["target_date"] = _iso_date(
                            td, "target_date must be YYYY-MM-DD").isoformat()
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
            # The three investment keys are owner settings, like every
            # other knob in the Connections section (the bank-connection
            # ones are owner-only on the server too). Refused rather than
            # dropped: no client shows these to a member, so only a direct
            # call sends them, and a silent drop would answer it "saved".
            _invest_keys = ("advisory_fees", "advisory_fee_exempt_masks",
                            "investment_basis_override")
            if user.get("role") != "owner" and any(
                    k in body or k + "_base" in body for k in _invest_keys):
                raise HTTPException(403, "owner only")

            def _invest_base_check(key: str) -> None:
                """Each section replaces its stored key whole, so a tab
                that loaded the section before another device changed it
                would put back its stale copy of the rows it never
                touched. A save that names the section as it was read
                (`<key>_base`) is refused when the stored section has
                moved since; a save without a base replaces as before."""
                if key + "_base" not in body:
                    return
                base = body[key + "_base"]
                if base is not None and not isinstance(base, (dict, list)):
                    raise HTTPException(
                        400, f"{key}_base must be the section as last read")

                def _norm(v):
                    if not v:
                        return None
                    if isinstance(v, list):
                        return sorted(str(x) for x in v)
                    if isinstance(v, dict):
                        return {str(k): v[k] for k in v}
                    return v
                if _norm(base) != _norm(cfg.get(key)):
                    raise HTTPException(
                        409, "These settings were changed on another device "
                             "since this page loaded. The current ones are "
                             "shown now — make your change again.")

            for _k in _invest_keys:
                if _k in body:
                    _invest_base_check(_k)
            if "advisory_fees" in body:
                # {institution-name fragment: yearly rate as a fraction}.
                # The fee estimate matches the fragment against institution
                # names with LIKE, so it is stored lowercase and trimmed —
                # a blank fragment would match every institution and
                # charge the rate on the whole portfolio. A rate is a
                # fraction (0.0025 = 0.25%); one above 1 is a percentage
                # typed into the wrong box and would estimate a fee larger
                # than the balance, so it is refused rather than guessed at.
                fees_in = body["advisory_fees"]
                if not fees_in:
                    cfg.pop("advisory_fees", None)
                elif not isinstance(fees_in, dict):
                    raise HTTPException(
                        400, "advisory_fees must be an object "
                             "{institution: yearly rate}")
                else:
                    if len(fees_in) > _INVEST_MAX_ROWS:
                        raise HTTPException(400, "too many advisory fees")
                    fees_out = {}
                    for frag, rate in fees_in.items():
                        frag = str(frag or "").strip().lower()
                        if not frag:
                            raise HTTPException(
                                400, "every advisory fee needs an institution")
                        if len(frag) > 60:
                            raise HTTPException(
                                400, "advisory fee institution is too long")
                        if not _stated(rate):
                            raise HTTPException(
                                400, f"advisory fee for {frag} needs a rate")
                        r = _num(rate, "advisory fee rate")
                        if not 0 <= r <= 1:
                            raise HTTPException(
                                400, "advisory fee rate must be a fraction "
                                     "between 0 and 1 (0.0025 = 0.25%)")
                        fees_out[frag] = r
                    if fees_out:
                        cfg["advisory_fees"] = fees_out
                    else:
                        cfg.pop("advisory_fees", None)
            if "advisory_fee_exempt_masks" in body:
                # the last characters of self-directed accounts at a
                # fee-charging institution. Compared exactly against the
                # account's mask, so anything that is not a mask could never
                # match and would leave the account silently charged.
                masks_in = body["advisory_fee_exempt_masks"]
                if isinstance(masks_in, str):
                    # separators at either end split off empty pieces that
                    # are not masks and must not count against the cap
                    masks_in = [p for p in re.split(r"[,;\s]+", masks_in)
                                if p]
                if not masks_in:
                    cfg.pop("advisory_fee_exempt_masks", None)
                elif not isinstance(masks_in, list):
                    raise HTTPException(
                        400, "advisory_fee_exempt_masks must be a list of "
                             "account masks")
                else:
                    # refused before the scan, like the other two maps: a
                    # body near the size limit carries a six-figure list,
                    # and checking it entry by entry would hold the
                    # household's config row locked while it ran
                    if len(masks_in) > _INVEST_MAX_ROWS:
                        raise HTTPException(400, "too many exempt accounts")
                    # dict.fromkeys drops repeats in one pass and keeps the
                    # order they were typed in
                    masks_out = list(dict.fromkeys(
                        m for m in (_invest_mask(x, "advisory_fee_exempt_masks")
                                    for x in masks_in) if m))
                    if masks_out:
                        cfg["advisory_fee_exempt_masks"] = masks_out
                    else:
                        cfg.pop("advisory_fee_exempt_masks", None)
            if "investment_basis_override" in body:
                # {account mask: dollars actually put in} — replaces the
                # feed's cost basis where in-kind transfers arrived with a
                # near-zero one. Money in cannot be negative; zero is a
                # real answer (an account funded only by transfers in).
                bo_in = body["investment_basis_override"]
                if not bo_in:
                    cfg.pop("investment_basis_override", None)
                elif not isinstance(bo_in, dict):
                    raise HTTPException(
                        400, "investment_basis_override must be an object "
                             "{account mask: dollars}")
                else:
                    if len(bo_in) > _INVEST_MAX_ROWS:
                        raise HTTPException(400, "too many basis overrides")
                    bo_out = {}
                    for m, amt in bo_in.items():
                        m = _invest_mask(m, "investment_basis_override")
                        if not m:
                            raise HTTPException(
                                400, "every basis override needs an "
                                     "account mask")
                        if not _stated(amt):
                            raise HTTPException(
                                400, f"basis override for {m} needs an amount")
                        v = _num(amt, "basis override")
                        if v < 0:
                            raise HTTPException(
                                400, "basis override must not be negative")
                        bo_out[m] = round(v, 2)
                    if bo_out:
                        cfg["investment_basis_override"] = bo_out
                    else:
                        cfg.pop("investment_basis_override", None)
            if "llm_url" in body or "llm_model" in body or "llm_api_key" in body:
                # smart-categorization LLM backend — same rules as the setup
                # wizard: URL+model together, key optional (encrypted at rest,
                # never echoed or logged); empty llm_url clears all three.

                from ..db import crypto
                url = str(body.get("llm_url", cfg.get("llm_url") or "") or "").strip()
                url = _keep_masked_llm_url(url, cfg)
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
            _folded = False
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
                    b_url = _keep_masked_llm_url(
                        str(e.get("url") or "").strip(), cfg)
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
                _folded = (_fold_legacy
                           and any(e["id"] == "legacy" for e in entries))
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
            # Nothing is lost crossing over: before the fold, every task
            # without a live route resolved to the legacy endpoint
            # (llm_categorize.resolve_role). The fold retires the keys that
            # resolution read, so each such task is routed to the folded
            # entry explicitly — otherwise the clients' first AI-card save,
            # which sends the roles it read (none), silently moves every
            # task to the operator's backend or to nothing. Only on the save
            # that folds: afterwards an unrouted task means what it says.
            if _folded:
                from ..engine.llm_categorize import ROLES as _LLM_ROLES
                _live = {str(b.get("id")) for b in (cfg.get("llm_backends")
                                                    or [])
                         if isinstance(b, dict)}
                _routes = dict(cfg.get("llm_roles") or {})
                for _r in _LLM_ROLES:
                    if _routes.get(_r) != "bundled" and _routes.get(_r) not in _live:
                        _routes[_r] = "legacy"
                cfg["llm_roles"] = _routes
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
            # Pointing mail, the AI endpoint or the daily report somewhere
            # new is a step-up act, like adding a webhook or changing the
            # account email: with a cookie alone, a thief could route the
            # owner's reset link through their own relay and take the
            # account. Checked last, inside the lock, against what the
            # save actually stored, so the refusal rolls every field of
            # this save back and a re-save of unchanged values never asks.
            if _outbound_repointed(conn, _cfg_was, cfg):
                # the shared demonstration login's step-up proves nothing
                # (its password is handed out, its factor waived), so it
                # cannot point the household's mail or data anywhere new
                demoguard.deny_shared_grant(user)
                from .app import _require_elevation
                _require_elevation(
                    user, password=_proof.get("password", ""),
                    totp_code=_proof.get("totp_code", ""),
                    recovery_code=_proof.get("recovery_code", ""))
            if _smtp_from_missing:
                raise HTTPException(
                    400, "smtp_from: enter the From address mail is sent "
                         "as — the relay username is not an email address, "
                         "so it cannot stand in" if _smtp_user_given else
                         "smtp_from: enter the From address mail is sent as")
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
        # a display-form entry's consent moves to the bare mailbox the
        # save stored it as, BEFORE the invite diff, so nobody who already
        # said yes is asked again; a joined entry's mailboxes were never
        # asked one by one, so the diff invites them
        if _prev_entries and ("email_recipients" in body
                              or "email_recipients_added" in body
                              or "email_recipients_removed" in body):
            from ..notify import recipient_invites as _ri
            _ri.carry_acceptance(user["tenant_id"], _prev_entries)
        added = mailguard.added_recipients(_prev_asked,
                                           cfg.get("email_recipients"))
        if pin_primary_after:
            from ..engine import forecast
            if forecast.auto_pin_primary_checking(conn):
                cfg = budget.load_config(conn)
        view = _settings_view(cfg)
        if added:
            mailguard.invite_added(user, added)
        view["email_recipient_status"] = _recipient_status(
            user["tenant_id"], cfg.get("email_recipients") or [],
            cfg.get("email_muted") or [], cfg)
        return _hide_people(view, user)
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
    if demoguard.own_address_only(user, email) != email:
        # the shared demonstration login may re-invite only itself: an
        # answer for any other address would say whether it is on the list
        raise HTTPException(400, "that address is not a saved recipient")
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
        # a mailbox inside a stored `Name <a>` / `a;b` entry is on the list
        # (the sender mails it), so it can be re-invited like any other
        if email.lower() not in mailguard.stored_mailboxes(
                cfg.get("email_recipients"))[0]:
            raise HTTPException(400, "that address is not a saved recipient")
        # On hosted, being ON the saved list is not enough — a stored config
        # (a restore, or one saved before the instance was hosted) can hold a
        # free-form external address, and this door would then make the
        # platform mail invite links (naming the inviter's account) to an
        # arbitrary stranger the
        # worker's own membership filter would never actually enrol. Same
        # member-only check the save and restore doors already enforce.
        mailguard.check_recipients(conn, [email])
    finally:
        conn.close()
    # an owner may sit on a list stored before additions were checked, and
    # is still mailed by construction: there is no invitation to resend
    if email.lower() in mailguard.owner_addresses(user["tenant_id"]):
        raise HTTPException(400, "That's you — as the owner you already "
                                 "receive this mail.")
    # The answer says whether mail actually went out. The cooldown and the
    # hourly cap stop a send without any error, and a bare `ok` let both
    # clients tell the owner an invitation was on its way when nothing
    # was. `ok` stays true (the request was valid and nothing failed);
    # `sent` is the truth, and `reason` names why when it is false.
    from ..notify import recipient_invites as _ri
    try:
        res = _ri.resend(user["tenant_id"], email,
                         invited_by=user.get("user_id"),
                         inviter=user.get("email"))
    except Exception:                                    # noqa: BLE001
        import logging
        logging.getLogger("oikonome.api").exception(
            "recipient invite resend failed for %s", email)
        res = None
    if not isinstance(res, dict):
        res = {"sent": False, "reason": "error"}
    out = {"ok": True, "sent": bool(res.get("sent"))}
    if not out["sent"]:
        reason = str(res.get("reason") or "").strip()
        out["reason"] = reason if reason in _RESEND_REASONS else "held"
    out["email_recipient_status"] = _recipient_status(
        user["tenant_id"], cfg.get("email_recipients") or [],
        cfg.get("email_muted") or [], cfg)
    return _hide_people(out, user)


# The words a not-sent resend may carry. `cooldown` and `cap` are the two
# throttles; the rest say the address's own state stopped it (answered,
# refused, or undeliverable) or the instance cannot send. Anything else
# collapses to `held`, so a client keyed on these words never meets an
# unknown one.
_RESEND_REASONS = frozenset({
    "cooldown", "cap", "held", "accepted", "declined", "complained",
    "blocked", "undelivered", "no_mail", "invalid", "error"})


@router.post("/settings/outbound/resume",
             dependencies=[Depends(limit("outbound-resume", 10, 3600))])
def settings_outbound_resume(user: dict = Depends(_user()),
                             body: dict | None = Body(default=None)):
    """The owner confirmed the relay, the extra recipients and the AI
    endpoint a credential rotation paused are theirs: the senders use them
    again. Owner only — the pause exists to protect the owner's account.
    Nothing to confirm is not an error.

    Resuming re-opens the same destinations that typing them in fresh
    opens (`_outbound_repointed`), so it is guarded the same way and the
    same way a paused webhook is switched back on: the shared
    demonstration login may not do it, the caller steps up, and the write
    runs under the factor lock after a re-check that the caller is still
    signed in. Without that last part a request that authenticated a
    moment before a reset would wait on the settings row and lift the
    pause the reset had just written, from a session the reset evicted."""
    demoguard.deny(user)
    if user.get("script_token") or user["role"] != "owner":
        raise HTTPException(403, "owner only")
    demoguard.deny_shared_grant(user)
    body = body if isinstance(body, dict) else {}
    from .app import _require_elevation, _still_signed_in_under_factor_lock
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    from ..notify import outbound_pause as _outbound
    conn = _conn(user)
    try:
        # the banner sends back the fingerprint of the record it showed;
        # one that has changed since is refused, and the client reloads
        version = body.get("version")
        with _still_signed_in_under_factor_lock(user):
            try:
                lifted = _outbound.resume(
                    conn, None if version is None else str(version))
            except _outbound.StaleConfirmation:
                raise HTTPException(
                    409, "what is paused changed since this page loaded — "
                         "reload Settings and check it again")
    finally:
        conn.close()
    return {"ok": True, "resumed": lifted}


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
        _cfg = budget.load_config(conn)
        configured = list(_cfg.get("email_recipients") or [])
    finally:
        conn.close()
    from ..notify import outbound_pause as _outbound
    held = _outbound.paused(_cfg) or {}
    # While a credential rotation holds the Settings relay, resolve_smtp
    # answers with the instance's own relay or with nothing, so a test
    # would either refuse as though the form were empty or prove a relay
    # the form does not show. Say what is actually going on instead.
    if held.get("smtp") and _cfg.get("smtp_host"):
        raise HTTPException(
            400, "your mail relay is paused since your password or second "
                 "factor changed — check it below, then press “These are "
                 "mine — resume” at the top of Settings and test again")
    held_recipients = set(held.get("recipients") or [])
    # the shared demonstration login tests delivery to itself only: the
    # answer names every recipient, and the mail would reach members it is
    # never shown
    recipients = demoguard.own_addresses_only(user, recipients)
    configured = demoguard.own_addresses_only(user, configured)
    # Who is on the list but will NOT be mailed, and why. `_recipients`
    # filters to accepted invites (and, hosted, to members) — correctly —
    # but the button exists to answer "did adding this person work?", so a
    # green tick that says nothing about them is not an answer.
    mailed = {e.lower() for e in recipients}
    delivered_raw = {e.lower() for e in raw}
    # a paused recipient is left out of `raw` too, so it is named before
    # the invitation reason could claim it
    skipped = [
        {"email": e,
         "reason": ("paused since your password, login email or second factor changed "
                    "— confirm on the banner at the top of Settings"
                    if e.strip().lower() in held_recipients
                    else "delivery is failing to this address"
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
        # The amounts get the same guard: text, null, a list, a negative or
        # a non-finite figure would reach the simulation as its monthly SS
        # and fail it on every load, so such an entry is dropped too.
        import math
        sched = {}
        raw_sched = cfg.get("ss_estimates")
        for k, v in (raw_sched.items() if isinstance(raw_sched, dict)
                     else ()):
            if isinstance(v, bool):
                continue
            try:
                amt = float(v)
                age_k = int(k)
            except (ValueError, TypeError, OverflowError):
                continue
            if math.isfinite(amt) and amt >= 0:
                sched[age_k] = amt
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
        # every member's address, which the shared demonstration login is
        # shown only its own of; and the owner's note (where the will and
        # the passwords are) and who it is for are withheld from it too
        out["members"] = demoguard.own_addresses_only(user, [
            r["email"] for r in conn.execute(
                "SELECT email FROM users WHERE tenant_id = "
                "current_setting('app.tenant_id')::uuid ORDER BY created_at"
            ).fetchall()])
        if demoguard.people_hidden(user):
            out["note"], out["prepared_for"] = "", ""
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
    if demoguard.people_hidden(user):
        # it is shown an empty note, so a save from it would erase the
        # owner's
        raise HTTPException(403, demoguard.SHARED_PEOPLE_DENIED)
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


# An address anywhere inside free text. Bounded classes and no nested
# quantifiers, so a long run of dots cannot make it backtrack.
_ADDRESS_IN_TEXT = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


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
    # length before shape: the shape regex backtracks quadratically on a
    # long run of dots after the first @, and parse_recipients keeps an
    # unparseable entry verbatim at whatever length it arrived
    if not to or len(to[0]) > 254 or not _CONTINUITY_ADDRESS.match(to[0]):
        raise HTTPException(400, "a valid email address is required")
    conn = _conn(user)
    try:
        mailguard.check_recipients(conn, to)
        smtp = resolve_smtp(conn)
        if not smtp["configured"]:
            raise HTTPException(400, "email is not configured on this "
                                     "instance — download the packet instead")
        pdf, name = _continuity_pdf(user)
        # the sender's address is account-supplied text; in the HTML part
        # it is escaped like any other, so it reads as text and never as
        # markup (a link dressed as a sign-in prompt, say)
        import html as _html
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
                f"kept.</p><p>Sent by {_html.escape(user['email'])} from "
                "Oikonome.</p>")
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
    hidden = demoguard.people_hidden(user)
    if hidden and who and not demoguard.own_addresses_only(user, [who]):
        # filtering by another member's address would confirm who acts here,
        # and the anonymised name matches no stored row
        return {"rows": [], "more": False, "next": None,
                "actors": [], "kinds": list(_activity.KINDS)}
    conn = _conn(user)
    try:
        out = _activity.list_activity(
            conn, limit=limit_, before=before or None, kind=kind or None,
            actor=who or None, target=target or None)
    finally:
        conn.close()
    if hidden:
        # the actor column is the acting member's address; the label (and
        # the sentence built from it) can be one too — the continuity
        # packet's row names who it was mailed to
        def _mask(value):
            if isinstance(value, str):
                return _ADDRESS_IN_TEXT.sub(
                    lambda m: demoguard.own_address_only(user, m.group(0)),
                    value)
            if isinstance(value, dict):
                return {k: _mask(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_mask(v) for v in value]
            return value
        for r in out["rows"]:
            r["actor"] = demoguard.own_address_only(user, r.get("actor"))
            label = r.get("label")
            masked = _mask(label)
            if masked != label:
                r["label"] = masked
                r["summary"] = _activity.sentence(
                    r.get("kind") or "", r.get("action") or "", masked,
                    r.get("detail"))
            # a note's text or a merchant name can quote an address as well,
            # and the detail (before/after values) is returned beside it
            r["summary"] = _mask(r.get("summary"))
            r["detail"] = _mask(r.get("detail"))
        # the filter offers only the caller: a chip for "a household
        # member" could only ever return nothing
        out["actors"] = list(dict.fromkeys(
            demoguard.own_addresses_only(user, out["actors"])))
    return out


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

def _receipt_id_or_404(rid: str) -> str:
    """A receipt id that is not even a uuid names no receipt: 404, before
    any lock or query. Checked up front so the doors below need no blanket
    except — a lock wait that timed out or a database fault then surfaces as
    the server error it is, instead of telling the person the receipt they
    are looking at does not exist."""
    try:
        return str(uuid.UUID(str(rid)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(404, "no such receipt")


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


@router.post("/transactions/{txn_id:path}/receipt",
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
        # add attaches to the row the charge stands as NOW: a pending hold
        # that settled since the client read it hands its receipt to the
        # posted row. The log, the answer and the id the clients re-key on
        # all name that row, or the upload reads back empty from the
        # retired hold and invites a second, duplicate upload.
        landed = conn.execute("SELECT txn_id FROM receipts WHERE id = %s",
                              (rid,)).fetchone()
        if landed and landed["txn_id"]:
            txn_id = landed["txn_id"]
        _activity.record(conn, user, "receipt", "attached", target=txn_id,
                         label=_activity.txn_label(conn, txn_id),
                         detail={"receipt_kind": kind, "receipt_id": rid})
        if llm_categorize.configured(conn, role="vision") and receipts.claim(conn, rid):
            # claim before responding so the first poll already shows
            # 'parsing' — and so a racing retry can't double-parse
            _parse_receipt_async(str(user["tenant_id"]), rid)
        return {"id": rid, "txn_id": txn_id,
                "receipts": receipts.for_txn(conn, txn_id)}
    finally:
        conn.close()


@router.post("/receipts",
             dependencies=[Depends(limit("receipt", 60, 3600))])
async def receipt_snap(user: dict = Depends(_user()),
                       file: UploadFile = File(...)):
    """Snap a receipt before its transaction reaches the ledger. Stored as
    WAITING (no transaction), parsed in the background like any receipt,
    and paired with its charge by engine/receipt_match — when the parse
    finishes if the charge is already here, else when a sync brings it.
    Same limits, same rate bucket and the same demo refusal as attaching a
    receipt to a transaction."""
    demoguard.deny(user)
    from ..engine import llm_categorize, receipt_match, receipts
    from .pages import read_capped
    data = await read_capped(file, cap=receipts.MAX_IMAGE)
    if data is None:
        raise HTTPException(413, "receipt too large (5 MB max)")
    mime = file.content_type or "application/octet-stream"
    conn = _conn(user)
    try:
        try:
            rid = await run_in_threadpool(receipts.add, conn, None, data,
                                          mime, "receipt")
        except ValueError as e:
            raise HTTPException(400, str(e))
        _activity.record(conn, user, "receipt", "snapped",
                         label="a waiting receipt",
                         detail={"receipt_kind": "receipt",
                                 "receipt_id": rid})
        if llm_categorize.configured(conn, role="vision") and receipts.claim(conn, rid):
            _parse_receipt_async(str(user["tenant_id"]), rid)
        return {"id": rid,
                **_ser(receipt_match.waiting(conn, today=_today(conn)))}
    finally:
        conn.close()


@router.get("/receipts/waiting")
def receipt_waiting(user: dict = Depends(_user()),
                    limit: int | None = Query(None, ge=1),
                    offset: int = Query(0, ge=0, le=1_000_000)):
    """Receipts waiting for their transaction, each with what was read off
    it and the charges it could be; plus what the matcher paired recently.
    One page at a time: `limit` (capped at the matcher's page size, which
    is also the default) and `offset` reach the rest, and `count` is every
    receipt waiting, not just this page."""
    from ..engine import receipt_match
    conn = _conn(user)
    try:
        return _ser(receipt_match.waiting(
            conn, today=_today(conn),
            limit=min(limit or receipt_match.PAGE_LIMIT,
                      receipt_match.PAGE_LIMIT),
            offset=offset))
    finally:
        conn.close()


@router.post("/receipts/{rid}/match")
def receipt_match_by_hand(rid: str, user: dict = Depends(_user()),
                          body: dict = Body(...)):
    """Pair a waiting receipt with a transaction a person chose."""
    from ..engine import receipt_match
    rid = _receipt_id_or_404(rid)
    txn_id = str((body or {}).get("txn_id") or "").strip()
    if not txn_id:
        raise HTTPException(400, "txn_id required")
    conn = _conn(user)
    try:
        ok = receipt_match.match_by_hand(conn, rid, txn_id)
        if not ok:
            raise HTTPException(
                404, "no such waiting receipt, or that transaction is gone")
        _activity.record(conn, user, "receipt", "matched", target=txn_id,
                         label=_activity.txn_label(conn, txn_id),
                         detail={"receipt_kind": "receipt",
                                 "receipt_id": rid})
        return _ser(receipt_match.waiting(conn, today=_today(conn)))
    finally:
        conn.close()


@router.post("/receipts/{rid}/unmatch")
def receipt_unmatch(rid: str, user: dict = Depends(_user())):
    """Take a receipt off its transaction and put it back to waiting. The
    matcher never pairs it with that transaction again."""
    from ..engine import receipt_match
    rid = _receipt_id_or_404(rid)
    conn = _conn(user)
    try:
        was = receipt_match.unmatch(conn, rid)
        if was is None:
            raise HTTPException(404, "no such matched receipt")
        _activity.record(conn, user, "receipt", "unmatched", target=was,
                         label=_activity.txn_label(conn, was),
                         detail={"receipt_kind": "receipt",
                                 "receipt_id": rid})
        return {"txn_id": was,
                **_ser(receipt_match.waiting(conn, today=_today(conn)))}
    finally:
        conn.close()


@router.post("/receipts/{rid}/details")
def receipt_details(rid: str, user: dict = Depends(_user()),
                    body: dict = Body(...)):
    """Type in a receipt's total, date and (optionally) store — for an
    instance with no AI to read it, a parse that failed, or a total read
    wrong. Stored as the receipt's parsed fields, marked as entered by hand
    so no later parse writes over them; a waiting receipt then looks for
    its charge at once. Saving the same details again changes nothing."""
    demoguard.deny(user)
    from ..engine import receipt_match, receipts
    body = body or {}
    if body.get("total") in (None, ""):
        raise HTTPException(400, "total required")
    total = round(_money_float(body.get("total"), "total"), 2)
    if total <= 0:
        raise HTTPException(400, "total must be more than zero")
    # the matcher reads a total past its ceiling as unknown; a person
    # typing one would save details that silently never match
    if total > receipt_match.MAX_TOTAL:
        raise HTTPException(400, "total is too large")
    day = _iso_date(str(body.get("date") or "").strip(),
                    "date must be YYYY-MM-DD")
    merchant = body.get("merchant")
    if merchant is not None and not isinstance(merchant, str):
        raise HTTPException(400, "merchant must be text")
    if merchant and len(merchant.strip()) > 80:
        raise HTTPException(400, "merchant is 80 characters at most")
    rid = _receipt_id_or_404(rid)
    conn = _conn(user)
    try:
        row = conn.execute(
            "SELECT created_at FROM receipts WHERE id = %s::uuid"
            " AND kind = 'receipt'", (rid,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such receipt")
        # the matcher only trusts a date on the receipt from the year
        # before it was snapped up to the day after; refuse the rest here
        # rather than store a date that would silently be ignored
        snapped = row["created_at"].date()
        if day > snapped + dt.timedelta(days=1):
            raise HTTPException(400, "that date is after the receipt "
                                     "was snapped")
        if day < snapped - dt.timedelta(days=366):
            raise HTTPException(400, "that date is more than a year "
                                     "before the receipt was snapped")
        got = receipts.set_details(
            conn, rid, total=total, date=day,
            merchant=merchant if merchant is not None else None,
            keep_merchant=merchant is None)
        if got is None:
            raise HTTPException(404, "no such receipt")
        txn_id = got["txn_id"]
        if txn_id is None:
            txn_id = receipt_match.match_receipt(conn, rid)
        _activity.record(conn, user, "receipt", "details", target=txn_id,
                         label=(_activity.txn_label(conn, txn_id)
                                if txn_id else "a waiting receipt"),
                         detail={"receipt_kind": "receipt",
                                 "receipt_id": rid, "total": total})
        return {"id": rid, "txn_id": txn_id,
                **_ser(receipt_match.waiting(conn, today=_today(conn)))}
    finally:
        conn.close()


@router.get("/transactions/{txn_id:path}/receipts")
def receipt_list(txn_id: str, user: dict = Depends(_user())):
    import math

    from ..engine import receipts
    conn = _conn(user)
    try:
        # a pending hold that settled since the client read it is shown as
        # the posted row it became, which is where its receipts went; read
        # from the retired hold, a refetch after an upload would come back
        # empty
        with conn.transaction():
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
        # the charge's own amount and date ride along: the panel ranks the
        # waiting receipts against them, so the one that fits this charge is
        # offered first however many are waiting
        t = conn.execute(
            "SELECT amount, date FROM transactions WHERE id = %s"
            " AND removed = 0", (txn_id,)).fetchone()
        # a row can carry no usable amount (a restore keeps what the file
        # held); then there is nothing to rank against, and the panel gets
        # the charge as unknown — which both clients already handle —
        # rather than the whole panel failing on a NULL
        amount = t["amount"] if t else None
        known = (amount is not None and t["date"] is not None
                 and math.isfinite(float(amount)))
        return {"receipts": [_ser(r) for r in receipts.for_txn(conn, txn_id)],
                "txn": ({"amount": round(float(amount), 2),
                         "date": str(t["date"])[:10]} if known else None)}
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
    # Refused on the demo household: the upload that could put a receipt
    # back is refused there too, so a deleted seeded receipt would be gone
    # for every visitor until the reset. Pairing and unpairing stay open —
    # they are row-level and a visitor can undo them from the same screen.
    demoguard.deny(user)
    from ..engine import receipts
    rid = _receipt_id_or_404(rid)
    conn = _conn(user)
    try:
        # the receipt's row before it goes: the log names the charge
        was = conn.execute("SELECT txn_id, kind FROM receipts "
                           "WHERE id = %s::uuid", (rid,)).fetchone()
        if not receipts.delete(conn, rid):
            raise HTTPException(404, "no such receipt")
        if was:
            _activity.record(conn, user, "receipt", "removed",
                             target=was["txn_id"],
                             label=(_activity.txn_label(conn, was["txn_id"])
                                    if was["txn_id"]
                                    else "the waiting list"),
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
        if receipts.is_manual(conn, rid):
            raise HTTPException(
                409, "its total and date were entered by hand")
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
        w = _csv_writer(buf)
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
    demoguard.deny_shared_grant(user)
    from ..auth import api_tokens
    from .app import _control_conn, _require_elevation
    _owner_only(user)
    # A script token is a long-lived credential that outlives the session
    # that minted it, so a stolen session could mint one for persistence.
    # A password change or reset revokes every token the user minted
    # (password_change, reset.consume), but only once the owner notices
    # and rotates. Password step-up so a hijacked cookie alone can't mint;
    # recovery_code is forwarded, or a passkey-only account could not mint
    # at all. TOTP accounts also owe a live code, exactly like the
    # support-access grant: a token minted on password alone would let a
    # session + password thief sidestep the second factor and keep access
    # until the owner happened to rotate.
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    # push (the default, and what every older client sends) = the import
    # doors; read = the integrations doors. Never both.
    scope = str(body.get("scope") or "push")
    if scope not in api_tokens.SCOPES:
        raise HTTPException(400, "scope must be push or read")
    from .app import _caller_still_signed_in, _lock_user_factors
    with _control_conn() as conn, conn.transaction():
        # a reset landing during the step-up must not be outlived by the
        # token it was meant to revoke
        _lock_user_factors(conn, user["user_id"])
        _caller_still_signed_in(conn, user)
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
            from ..sync import base as sync_base
            conn = tenancy.tenant_connect(tenant_id)
            # the same single-flight lock as the pass after a pull — which
            # is also what an account erasure waits on, so a pass in flight
            # finishes before the rows go and none starts during the wipe
            # a files-only household's first import creates its checking
            # row: with one, the forecast's anchor is pinned here, as a
            # bank's first pull pins it
            try:
                from ..engine import forecast
                forecast.auto_pin_primary_checking(conn)
            except Exception as e:                          # noqa: BLE001
                logging.getLogger("oikonome.import").warning(
                    "post-import primary checking pick failed tenant=%s: %s",
                    tenant_id, e)
            with sync_base.categorize_lock(conn, tenant_id) as held:
                if held:
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
    budget = min(TENANT_BUDGET, bulk_import.MAX_BATCH_BYTES)
    staged: list[tuple[str, bytes]] = []
    total = 0
    for f in files:
        # read_capped streams and bails past MAX_UPLOAD instead of buffering
        # a multi-GB file into RAM first
        data = await read_capped(f)
        total += len(data or b"")
        # The per-FILE cap alone would let 200 × 50 MB sit in this worker's
        # RAM — refuse the batch outright once the AGGREGATE blows the
        # batch budget (sized to fit inside the body ceiling, so this
        # message is reachable rather than pre-empted at the edge)
        if total > budget:
            raise HTTPException(413, bulk_import.BATCH_TOO_LARGE)
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
            from .pages import RESTORE_MAX_UPLOAD
            restore_mb = RESTORE_MAX_UPLOAD // (1024 * 1024)
            for entry, (name, data) in zip(plan["files"], staged):
                if not data and entry.get("kind") != "unsupported":
                    entry.update(action="skip", oversized=True,
                                 note=f"file too large (over {cap_mb} MB)")
                    if name.lower().endswith(".zip"):
                        # not a dead end: the restore door takes the
                        # archive alone, streamed to disk, ten times larger
                        entry.update(
                            restore_door=bulk_import.RESTORE_DOOR,
                            note=f"an export ZIP over {cap_mb} MB is "
                                 f"restored on its own (up to "
                                 f"{restore_mb} MB) — upload it by itself")
            # one export ZIP and nothing else is a restore, whatever its
            # size: say which door takes it
            if (len(staged) == 1
                    and staged[0][0].lower().endswith(".zip")):
                plan["restore_door"] = bulk_import.RESTORE_DOOR
            return plan
        finally:
            conn.close()

    return await run_in_threadpool(_analyze)


# The run door SUBMITS a folder plan: it freezes the plan as a background
# job (jobs/bulk_import_job) and answers at once, so its cost is the
# submit, and the analyze limit already bounds the jobs that do work.
# This bounds the posts that are refused before spending a stage (no
# step-up yet, another folder still importing) and the retries that only
# read a job back.
@router.post("/import/bulk/run",
             dependencies=[Depends(limit("import_bulk_run", 60, 3600))])
def import_bulk_run(user: dict = Depends(_user()), body: dict = Body(...)):
    """Submit an analyzed folder's plan. A client that sends
    `background: true` gets 202 and the job's status at once, and polls
    the job doors below; an older build, which reads `results`, gets 200
    after a short wait with one row per file, those still importing marked
    pending. Posting the same token again answers the same job, whatever
    plan it carries."""
    demoguard.deny(user)
    from . import bulk_import
    # the plan is iterated below: a scalar there is a malformed body (400),
    # never a TypeError escaping as a 500
    files = body.get("files") or []
    if not isinstance(files, list):
        raise HTTPException(400, "files must be a list")

    def step_up():
        # a plan that restores an export ZIP owes the step-up the single
        # upload door asks for (the archive repoints mail and AI
        # endpoints), asked by this request before the stage is spent, so
        # the client's retry after elevating finds its batch — and the
        # shared demonstration login, whose step-up proves nothing, may
        # not land an archive's endpoints at all
        demoguard.deny_shared_grant(user)
        from .app import _require_elevation
        _require_elevation(
            user, password=str(body.get("password") or ""),
            totp_code=str(body.get("totp_code") or ""),
            recovery_code=str(body.get("recovery_code") or ""))

    conn = _conn(user)
    try:
        try:
            out = bulk_import.submit(conn, str(body.get("token") or ""),
                                     list(files), user, step_up=step_up)
        except bulk_import.Busy as e:
            raise HTTPException(409, str(e))
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        # given back before any wait below: the tenant pool is shared by
        # every household on the instance
        conn.close()
    existing = out.pop("existing", False)
    if body.get("background") is True:
        from fastapi.responses import JSONResponse
        return JSONResponse(out, status_code=202)
    from ..jobs import bulk_import_job
    # Only the post that created the job waits for it; a repost of the
    # token reads it back as it stands, so reposting cannot buy a second
    # wait.
    st = out if existing else (bulk_import.wait_for(
        user["tenant_id"], out["job_id"],
        bulk_import.COMPAT_WAIT_SECONDS) or out)
    return {"job_id": st.get("job_id"), "state": st.get("state"),
            "results": bulk_import_job.compat_results(st)}


@router.get("/import/bulk/jobs/current")
def import_bulk_job_current(user: dict = Depends(_user())):
    """The household's folder import in progress, or the one that finished
    in the last hour, or `{state: "idle"}` — read by the Import page on
    load so a reload or a second device finds the job again."""
    from ..jobs import bulk_import_job
    conn = _conn(user)
    try:
        return bulk_import_job.current(conn)
    finally:
        conn.close()


@router.get("/import/bulk/jobs/{job_id}")
def import_bulk_job(job_id: str, user: dict = Depends(_user())):
    """One folder import's progress: N of M files and each file's outcome
    as it settles."""
    from ..jobs import bulk_import_job
    conn = _conn(user)
    try:
        st = bulk_import_job.status(conn, job_id)
    finally:
        conn.close()
    if st is None:
        raise HTTPException(404, "no such import")
    return st


# Cancelling only removes work (the file in hand finishes, the rest are
# skipped), so it asks no step-up; it is bounded like the run door.
@router.post("/import/bulk/jobs/{job_id}/cancel",
             dependencies=[Depends(limit("import_bulk_cancel", 60, 3600))])
def import_bulk_job_cancel(job_id: str, user: dict = Depends(_user())):
    demoguard.deny(user)
    from ..jobs import bulk_import_job
    conn = _conn(user)
    try:
        st = bulk_import_job.cancel(conn, job_id)
    finally:
        conn.close()
    if st is None:
        raise HTTPException(404, "no such import")
    return st


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
                     file: UploadFile = File(...),
                     password: str = Form(""),
                     totp_code: str = Form(""),
                     recovery_code: str = Form(""),
                     ynab_choice: str = Form("")):
    demoguard.deny(user)
    from ..sync import heartbeat
    from .pages import (OVERSIZE_MSG, _dispatch_import_unlocked,
                        read_capped)
    # a client that can ask where each account of a multi-account YNAB
    # register goes says so; one that does not is refused such a file by
    # name rather than handed a card it cannot draw. Passed only when set,
    # so the dispatcher's other callers keep their exact call.
    choice = {"ynab_choice": True} if ynab_choice.strip() in (
        "1", "true", "yes") else {}
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
        # a restore merges the archive's mail relay and AI endpoints over
        # the household's own — the destinations a Settings save may only
        # repoint after a step-up — so it owes the same proof, asked here
        # because the job it starts cannot refuse once it is running. The
        # shared demonstration login's step-up proves nothing, so it is
        # refused outright, as the Settings save refuses it a new endpoint.
        demoguard.deny_shared_grant(user)
        from .app import _require_elevation
        _require_elevation(user, password=password, totp_code=totp_code,
                           recovery_code=recovery_code)
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
            # the collector scripts push statements through this door too,
            # so it joins the push writer lock like the other push doors:
            # an erasure that began after the request authenticated must
            # not have the file's accounts and rows written back under it
            #
            # The sync lock is taken OUTSIDE the push transaction: it is a
            # session lock, and releasing it inside the transaction would
            # free it (and queue a nudged sync) before the rows commit, so
            # that sync would filter its pull against a ledger without the
            # file and insert rows the file already covers.
            from ..sync.base import tenant_sync_lock
            with tenant_sync_lock(conn, str(user["tenant_id"])) as held:
                if not held:
                    # an erasure holds the sync lock too (it is one of the
                    # writer locks it takes after flipping the household's
                    # status), so a push refused here must still answer
                    # the erasure's 409, not "try again in a moment"
                    from .. import erasure
                    if erasure.being_deleted(user["tenant_id"], conn):
                        raise HTTPException(
                            409, "this household is being deleted; "
                                 "nothing was imported")
                    result, mapping_needed = (
                        {"error": "a sync is running for this household — "
                                  "try the import again in a moment"}, None)
                else:
                    with _push_write(conn, user):
                        # the pairing and identity locks before the first
                        # row this transaction writes (see there). A PDF
                        # or OFX/QFX file takes them inside its importer,
                        # after its slow sandboxed read: taken here, every
                        # edit in the household would wait out the slot
                        # wait and the parse.
                        if not filename.lower().endswith(
                                (".pdf", ".ofx", ".qfx")):
                            from .bulk_import import hold_ledger_write_locks
                            hold_ledger_write_locks(conn)
                        dest = account_id
                        # the single-file page can create the destination
                        # account inline, like bulk already could — same
                        # slug-collision-safe helper, so two different
                        # "Chase" accounts never merge
                        if not dest and new_account_name.strip():
                            from .bulk_import import _ensure_account
                            # the sign convention also types the account
                            # (card / investment), as the bulk path does
                            dest = _ensure_account(
                                conn, new_account_name.strip(), amount_sign)
                        result, mapping_needed = _dispatch_import_unlocked(
                            conn, dest, amount_sign, filename, data,
                            role=str(user.get("role") or ""), **choice)
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
    # The token and the column names reach SQL and the CSV engine. Anything
    # but text is a malformed request; refused here, before the staged
    # upload is touched, so a bad body never consumes it.
    token = body.get("token") or ""
    if not isinstance(token, str):
        raise HTTPException(400, "token must be a string")
    for k in ("date_col", "name_col", "amount_col", "debit_col",
              "credit_col", "merchant_col", "category_col"):
        if body.get(k) is not None and not isinstance(body.get(k), str):
            raise HTTPException(400, f"{k} must be a string")
    from ..sync import heartbeat
    from .pages import run_mapped_import
    _watch = heartbeat.AUTO_WATCH.set(bool(user.get("script_token")))
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    conn = _conn(user)
    try:
        result = run_mapped_import(conn, token, body)
        if not (isinstance(result, dict) and result.get("error")):
            _categorize_after_import(str(user["tenant_id"]))
        return {"result": result}
    finally:
        heartbeat.AUTO_WATCH.reset(_watch)
        heartbeat.PUSH_VIA.reset(_via)
        conn.close()


@router.post("/import/ynab-accounts")
def import_ynab_accounts_api(user: dict = Depends(_user()),
                             body: dict = Body(...)):
    """Finish a multi-account YNAB register /api/import parked: the person's
    destination for each YNAB account (an account id, "skip", or
    {"new": name}), and for each, whether rows an earlier import of the
    whole register left on the picked account are to be removed.

    The same gates as the upload door it finishes: the demo refusal, the
    role gate (app.py, by path), the sync lock outside the push writer
    transaction, the erasure 409, the categorize pass after."""
    demoguard.deny(user)
    token = body.get("token")
    accounts = body.get("accounts")
    remove_earlier = body.get("remove_earlier")
    # malformed is refused before the parked upload is touched
    if not isinstance(token, str) or not token:
        raise HTTPException(400, "token must be a string")
    if not isinstance(accounts, dict) or not all(
            isinstance(k, str) and (isinstance(v, str) or (
                isinstance(v, dict) and isinstance(v.get("new"), str)))
            for k, v in accounts.items()):
        raise HTTPException(400, "accounts must map each YNAB account to "
                                 "an account id, \"skip\" or {\"new\": name}")
    if remove_earlier is None:
        remove_earlier = {}
    if not isinstance(remove_earlier, dict) or not all(
            isinstance(v, bool) for v in remove_earlier.values()):
        raise HTTPException(400, "remove_earlier must map YNAB accounts to "
                                 "true or false")
    if user.get("script_token"):
        raise HTTPException(403, "script tokens cannot import a YNAB "
                                 "register of several accounts")
    from ..sync import heartbeat
    from .pages import _run_ynab_import_unlocked
    _watch = heartbeat.AUTO_WATCH.set(False)
    _via = heartbeat.PUSH_VIA.set("app")
    conn = _conn(user)
    try:
        # the sync lock OUTSIDE the push transaction, as /api/import takes
        # it (see there): released inside, it would free a nudged sync to
        # filter its pull against a ledger without this file's rows
        from ..sync.base import tenant_sync_lock
        with tenant_sync_lock(conn, str(user["tenant_id"])) as held:
            if not held:
                from .. import erasure
                if erasure.being_deleted(user["tenant_id"], conn):
                    raise HTTPException(
                        409, "this household is being deleted; "
                             "nothing was imported")
                return {"result": {"error": "a sync is running for this "
                                            "household — try the import "
                                            "again in a moment"}}
            with _push_write(conn, user):
                from .bulk_import import hold_ledger_write_locks
                hold_ledger_write_locks(conn)
                result = _run_ynab_import_unlocked(conn, token, accounts,
                                                   remove_earlier)
        if not result.get("error"):
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
        import httpx

        from ..engine.llm_categorize import LLMError
        conn = _conn(user)
        try:
            try:
                return taxdocs.analyze(conn, filename, data, ctype)
            except ValueError as e:
                raise HTTPException(400, str(e))
            # The W-2 image and 1040 paths ask the AI backend, and a backend
            # that is down, names a model it does not have, or sends back
            # something unusable is a fault in Settings → AI, not in the
            # document. Say so, naming the kind of failure (never the reply),
            # instead of the bare 500 an uncaught error becomes.
            except httpx.TransportError as e:
                raise HTTPException(
                    503, "the AI backend in Settings → AI could not be "
                         f"reached ({type(e).__name__}) — check that the "
                         "endpoint is up")
            except (httpx.HTTPStatusError, LLMError, KeyError,
                    IndexError) as e:
                cause = (f"HTTP {e.response.status_code}"
                         if isinstance(e, httpx.HTTPStatusError)
                         else type(e).__name__)
                raise HTTPException(
                    502, "the AI backend in Settings → AI could not read "
                         f"this document ({cause}) — check the vision model "
                         "and that the endpoint is up")
        finally:
            conn.close()

    return await run_in_threadpool(_analyze)


@router.post("/import/taxdoc/commit")
def taxdoc_commit_api(user: dict = Depends(_user()), body: dict = Body(...)):
    demoguard.deny(user)
    from ..engine import taxdocs
    rows = body.get("rows") or []
    if not isinstance(rows, list):
        raise HTTPException(400, "rows must be a list")
    conn = _conn(user)
    try:
        try:
            return taxdocs.commit(conn, str(body.get("token") or ""),
                                  list(rows))
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


def kept_rows_refusal(user: dict, e) -> JSONResponse:
    """The 409 an undo answers when it would keep rows held only by a note
    naming no feed account. Only the owner may delete those rows
    (force_kept), so only the owner is asked "undo anyway?" — error
    `kept_rows_unconfirmed`. Anyone else gets error `kept_rows`: the same
    count, and a note that says what happened and who can finish it, with
    no question attached, because the retry it would invite is refused."""
    if user.get("role") == "owner" and not user.get("script_token"):
        return JSONResponse(status_code=409, content={
            "detail": str(e), "error": "kept_rows_unconfirmed",
            "kept_transactions": e.kept})
    rows = f"{e.kept} imported row{'s' if e.kept != 1 else ''}"
    return JSONResponse(status_code=409, content={
        "detail": (f"Nothing was undone. {rows} in this import may stand "
                   f"in for charges an older bank feed set aside, so "
                   f"{'they are' if e.kept != 1 else 'it is'} kept for now. "
                   f"Only the household's owner can undo this import."),
        "error": "kept_rows", "kept_transactions": e.kept})


@router.post("/import/rollback")
def import_rollback_api(user: dict = Depends(_user()),
                        body: dict = Body(...)):
    demoguard.deny(user)
    from ..sync import batches
    from ..sync.base import SyncRunning
    batch_id = str(body.get("batch_id") or "")
    # Rows held only by a note naming no feed account may be the only copy
    # of charges an older bank feed set aside; nothing can check whether a
    # feed still relies on them, so the undo asks first (409, with the
    # count) and deletes them only on the owner's literal `force_kept:
    # true` — the account-remove door's contract, for the same rows.
    force_kept = body.get("force_kept") is True
    if force_kept and (user.get("role") != "owner" or user.get("script_token")):
        # the option, not the door, is the owner's: a member may still undo
        raise HTTPException(403, "owner only")
    conn = _conn(user)
    try:
        # An archived business is read-only and its rows are retention
        # records: every door that moves or deletes them refuses, and only
        # the owner's delete-forever may destroy them. Undoing an import
        # hard-deletes the rows it alone owns — with their Schedule C
        # classes — so the rollback refuses while any of those rows
        # belongs to an archived business, looking under its own lock so
        # an archive landing after this request began cannot slip past.
        n = batches.rollback(conn, batch_id, force_kept=force_kept,
                             ask_first=not force_kept)
    except batches.KeptRowsUnconfirmed as e:
        # nothing was changed: the owner is told the count and may answer
        # with force_kept; anyone else is told plainly, with no offer they
        # could not take up
        return kept_rows_refusal(user, e)
    except (SyncRunning, batches.ArchivedBusinessRows) as e:
        # nothing was deleted: the client retries once the sync is done, or
        # once the business is restored
        raise HTTPException(409, str(e))
    finally:
        conn.close()
    return {"ok": True, "deleted": int(n),
            "warning": getattr(n, "warning", None)}


@router.get("/onboarding")
def onboarding_api(user: dict = Depends(_user())):
    """Fresh-instance progress for the SPA's guided checklist."""
    from ..engine import budget
    from ..sync.base import history_months_of
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
        budgets_set = _budgets_set(conn)
        # the accounts the household is asked to choose between: one is
        # pinned by itself, two or more are a question the wizard and the
        # Today checklist ask (closed imports without a balance are not
        # offered next to a live account)
        from ..engine import forecast
        candidates = forecast.pickable_checking_ids(conn, cfg)
        # the pin as the forecast honours it (forecast.pin_is_live): one on
        # a disconnected bank's account reads as unset, so the checklist
        # asks for a new one
        pin_live = forecast.pin_is_live(conn, cfg)
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
            "budgets_set": budgets_set,
            "primary_checking_set": pin_live,
            "primary_checking_id": (cfg.get("checking_account_id") or None)
            if pin_live else None,
            "checking_candidates": candidates,
            # the guided wizard is offered until every step is
            # done or the user dismisses it for good
            "wizard_done": bool(cfg.get("wizard_done")),
            # explicit per-step done/skipped marks (strict flow)
            "wizard_steps": cfg.get("wizard_steps") or {},
            # the setup choices, read-only here, for the wizard's summary
            # (written through the settings save)
            "history_months": history_months_of(cfg),
            "setup_mode": _setup_mode_of(cfg),
            "bill_detection_auto": cfg.get("bill_detection_auto") is not False}


def _setup_mode_of(cfg: dict) -> str:
    """The household's bill setup choice; anything but "manual" is auto."""
    return "manual" if cfg.get("setup_mode") == "manual" else "auto"


# The historical window Plaid backfills after a link, in days — the
# denominator of the wizard's import bar for an Item that predates the
# per-Item record of the window it asked for (those all asked for two years).
BACKFILL_WINDOW_DAYS = 730


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
                  CASE WHEN jsonb_typeof(i.raw->'days_requested') = 'number'
                       THEN (i.raw->>'days_requested')::numeric END
                      AS days_requested,
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
                      AS recent_attempt,
                  EXISTS (SELECT 1 FROM sync_log s WHERE s.item_id = i.id)
                      AS ever_pulled
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
    first_pending = False
    for r in rows:
        # Each Item fills against the window ITS link asked Plaid for, not
        # the household's current choice: the window cannot be changed on
        # an Item once it is made, so an Item linked for two years keeps
        # backfilling two years after the household picks three months,
        # and measured against three months it would read full long before
        # its history is in.
        window_days = (float(r["days_requested"])
                       if r["days_requested"] and r["days_requested"] > 0
                       else BACKFILL_WINDOW_DAYS)
        stalled = (r["status"] or "").startswith("error")
        ready = (r["tx_update_status"] == "HISTORICAL_UPDATE_COMPLETE"
                 or bool(r["legacy_done"]))
        # no pull has finished yet: the Item's first sync is queued or
        # running on the worker (the link saved it and queued the pull),
        # which writes nothing a reader can see until it ends — so the
        # household counts as syncing, and the nudge does not start another
        waits_first = (not r["ever_pulled"] and not r["transactions"]
                       and not ready and not stalled)
        first_pending = first_pending or waits_first
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
            progress = max(0.02, min(0.95, days / window_days))
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
            "active": (r["recent_attempt"] is not None or waits_first)
                      and not ready,
            "ready": ready,
            # a broken connection will not finish its backfill —
            # surfaced so the UI can say "needs attention" instead of
            # waiting forever, and excluded from the all_ready gate
            "stalled": stalled,
        })
    waiting = [r for r, it in zip(rows, items)
               if not it["ready"] and not it["stalled"]]
    all_ready = all(it["ready"] or it["stalled"] for it in items)
    return ({"items": items, "all_ready": all_ready,
             "syncing": running or first_pending},
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
    has not been pulled recently and no sync is running, queue a pull of it
    on the worker. Hosted installs hear HISTORICAL_UPDATE by webhook, but a
    self-host without a public webhook URL would otherwise sit until the
    hourly cron. Owner-only and demo-denied like every other door that
    starts a sync (it pulls new transactions, moves cursors and spends
    aggregator quota). Returns the same status shape as the GET.

    Never pulls in the web process: a first two-year pull can hold a
    pooled web connection and the sync lock for minutes, which is why the
    link poll hands that work to the worker. An Item whose first pull has
    not finished gets that first-sync job queued again under its one job
    id (a no-op while it is queued or running; a re-queue if Redis lost
    it); any other waiting Item gets one pull per minute at most."""
    from ..jobs import worker

    demoguard.deny(user)
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    tid = str(user["tenant_id"])
    conn = _conn(user)
    try:
        payload, waiting = _backfill_status(conn)
    finally:
        conn.close()
    first = [r["id"] for r in waiting
             if not r["ever_pulled"] and not r["transactions"]]
    for iid in first:
        worker.queue_first_sync(tid, iid)
    # 45 s, not 90: the wizard re-nudges every 30 s while it waits, and a
    # 90 s window would decline the first two nudges after a pull and leave
    # the history minutes away; the bank's window is usually ready within
    # a minute of the link
    later = [r for r in waiting
             if r["ever_pulled"] or r["transactions"]]
    if later and not first and not payload["syncing"] \
            and all(r["recent_attempt"] is None for r in later):
        for r in later:
            worker.queue_backfill_sync(tid, r["id"])
        payload["syncing"] = True
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


@router.post("/business/{txn_id:path}/flag")
def business_flag(txn_id: str, user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        _require_business(conn)
        # under the pairing lock, onto the live row (live_edit_ids)
        with conn.transaction():
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
            data.flag_business(conn, txn_id)
        _activity.record(conn, user, "business", "flagged", target=txn_id,
                         label=_activity.txn_label(conn, txn_id))
        return {"ok": True}
    finally:
        conn.close()


@router.post("/business/{txn_id:path}/unflag")
def business_unflag(txn_id: str, user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        _require_business(conn)
        # under the pairing lock, onto the live row (live_edit_ids)
        with conn.transaction():
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
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
    w = _csv_writer(buf)
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
    """Add one member. The owner's door, like correcting or removing one:
    a new member's ownership share changes how a multi-member estimate
    splits the profit just as an edited share does."""
    demoguard.deny(user)
    _owner_only(user)
    from ..engine import entities
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        try:
            with _entity_write(conn, entity_id):
                return entities.add_member(
                    conn, entity_id,
                    member_name=_body_text(body, "member_name"),
                    ownership_pct=body.get("ownership_pct"),
                    is_manager=bool(body.get("is_manager")),
                    is_household=bool(body.get("is_household")))
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()


@contextlib.contextmanager
def _entity_write(conn, entity_id: str):
    """The transaction a write into an entity's own records runs in, opened
    by the active check: the business row is held FOR SHARE until the write
    commits, so an archive landing meanwhile waits for it rather than
    closing the books under it. A door's own check before this, outside any
    transaction, is only the early answer. For writes that lock no
    transaction or account row; those take their row first and check the
    entity after it (entities.require_active)."""
    from ..engine import entities
    with conn.transaction():
        try:
            entities.require_active(conn, entity_id)
        except ValueError as e:
            raise HTTPException(409 if "archived" in str(e) else 404, str(e))
        yield


def _member_write(user: dict, entity_id: str, write):
    """Run one member edit or removal, refusing an archived business. Both
    doors are the owner's (checked in each handler), as adding is:
    ownership shares
    decide how a multi-member estimate splits the profit. The
    business row is held FOR SHARE while the write runs, so an archive
    landing meanwhile waits for it rather than closing the books under it."""
    _require_entity_id(entity_id)
    conn = _conn(user)
    try:
        _require_business(conn)
        try:
            with conn.transaction():
                row = conn.execute(
                    "SELECT status FROM business_entity WHERE id = %s "
                    "FOR SHARE", (entity_id,)).fetchone()
                if row is None:
                    raise HTTPException(404, "no such entity")
                if row["status"] != "active":
                    raise HTTPException(409, "entity is archived (read-only)")
                out = write(conn)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not out:
            raise HTTPException(404, "no such member")
        return out
    finally:
        conn.close()


@router.patch("/business/entities/{entity_id}/members/{member_id}")
def entities_update_member(entity_id: str, member_id: str,
                           user: dict = Depends(_user()),
                           body: dict = Body(...)):
    """Correct one member: any of member_name, ownership_pct (0–100, or
    null for none) and is_manager (true/false). Keys left out keep their
    value. Answers the member as list_members shows it."""
    demoguard.deny(user)
    _owner_only(user)
    from ..engine import entities
    fields = {k: body[k] for k in ("member_name", "ownership_pct",
                                   "is_manager", "is_household") if k in body}
    return _member_write(user, entity_id, lambda conn: entities.update_member(
        conn, entity_id, member_id, **fields))


@router.delete("/business/entities/{entity_id}/members/{member_id}")
def entities_delete_member(entity_id: str, member_id: str,
                           user: dict = Depends(_user())):
    """Remove one member. A capital movement that named them keeps its
    amount and stops naming them."""
    demoguard.deny(user)
    _owner_only(user)
    from ..engine import entities
    _member_write(user, entity_id, lambda conn: entities.delete_member(
        conn, entity_id, member_id))
    return {"ok": True}


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
            with _entity_write(conn, entity_id):
                return equity.record_movement(
                    conn, entity_id, kind=body.get("kind"),
                    amount=body.get("amount"), date=body.get("date"),
                    member_id=_body_text(body, "member_id") or None,
                    form=_body_text(body, "form") or None,
                    note=_body_text(body, "note") or None)
        except equity.FrontedExpenseConflict as e:
            raise HTTPException(409, str(e))
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
    from ..engine import entities, equity
    conn = _conn(user)
    try:
        _require_business(conn)
        # archived entities are read-only, and these are the retention
        # records archiving exists to keep — only delete-forever may go
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        with _entity_write(conn, entity_id):
            if not equity.delete_movement(
                    conn, _require_record_id(movement_id, "movement"),
                    entity_id):
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
        except equity.FrontedExpenseConflict as e:
            # the expense is already explained for another business, or the
            # other way: a conflict with what is recorded, not a bad request
            raise HTTPException(409, str(e))
        except ValueError as e:
            # an archive that landed after the early check above
            raise HTTPException(409 if "archived" in str(e) else 400, str(e))
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
                  year: int | None = None,
                  limit: int | None = Query(None, ge=1, le=1000),
                  offset: int = Query(0, ge=0, le=10_000_000)):
    """The entity's worksheet rows, newest first. With no `limit` the reply
    is every row, the shape older clients read. With `limit` (and
    `offset`) it is one page plus `total`, every row in scope — the
    effective bucket of a refund on the page is still decided against the
    entity's whole history, so paging never changes a row's bucket."""
    _require_entity_id(entity_id)
    _ck_year(year, "year")
    from ..engine import books
    conn = _conn(user)
    try:
        _require_business(conn)
        rows = books.entity_transactions(conn, entity_id, year)
        if limit is None and not offset:
            return {"transactions": rows}
        page = rows[offset:offset + limit] if limit else rows[offset:]
        return {"transactions": page, "total": len(rows),
                "limit": limit, "offset": offset}
    finally:
        conn.close()


@router.post("/business/entities/{entity_id}/transactions/{txn_id:path}/class")
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
        # A body that names `sched_c_line` sets it, and null or "" clears
        # it; a body without the key (the Books tab's bucket change) keeps
        # whatever line is stored.
        names_line = "sched_c_line" in body
        line = _body_text(body, "sched_c_line") or None
        # free text bound straight into the row: anything but a string is
        # the client's mistake (400), never a driver error (500)
        note = _body_text(body, "note") or None
        bucket = body.get("bucket")
        # the request is judged as sent, even when the stored bucket wins
        if not isinstance(bucket, str) or bucket not in books.BUCKETS:
            raise HTTPException(400, f"bucket must be one of {books.BUCKETS}")
        try:
            with conn.transaction():
                # The review queue lists pending holds, and the sync may
                # post one while this request is in flight: the row that
                # stands for it now is found, and locked, under the lock
                # the settlement holds from its carry to its retire. The
                # class then lands on the row every reader counts, never
                # on a retired hold after its one-time carry has run.
                live = entities.live_txn_id(conn, txn_id)
                # The class row is keyed by txn alone, so the URL's entity
                # must own the transaction — otherwise entity A's endpoint
                # could classify (and rewrite) entity B's rows, archived
                # ones included. Checked on the live row, under its lock.
                if live is None or not books.txn_belongs_to_entity(
                        conn, entity_id, live):
                    raise HTTPException(
                        404, "no such transaction for this entity")
                txn_id = live
                # and the entity checked again under this transaction, after
                # the row lock (entities.require_active gives the order), so
                # an archive cannot land between the check and the class
                try:
                    entities.require_active(conn, entity_id)
                except ValueError as _e:
                    raise HTTPException(
                        409 if "archived" in str(_e) else 404, str(_e))
                # A save that carries a Schedule C line is the review
                # queue's confirm, and its bucket is the queue's date-derived
                # default for a row that had none. A bucket chosen since (on
                # the Books tab, which sends a bucket alone, or on another
                # device) is the person's decision and stays. Both saves
                # hold the transaction row's lock (taken by live_txn_id
                # above), so a queue confirm and a bucket change cannot
                # interleave and the later write cannot undo the earlier one.
                if line is not None:
                    have = conn.execute(
                        "SELECT bucket FROM business_txn_class "
                        "WHERE txn_id=%s", (txn_id,)).fetchone()
                    if have is not None and have["bucket"]:
                        bucket = have["bucket"]
                books.classify(
                    conn, txn_id, bucket,
                    sched_c_line=line if names_line else books.KEEP,
                    note=note)
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


def _year_end_sheet(conn, entity_id: str, year: int | None,
                    today: dt.date, lifetime: list[dict]) -> dict:
    """The balance sheet as of December 31 of `year`, for the year-end
    package. With no year, or a year still in progress, it is today's
    sheet and says so.

    No balance history is recorded, so a past year-end balance is rebuilt:
    today's balance with every posted transaction dated after the year end
    undone. That only holds when the account's ledger reaches back to the
    year end, and only for cash and debt — an investment account's value
    moved with the market, which no transaction records. Any other account
    is listed without an amount and left out of the totals, with the
    reason, rather than passed off as a year-end figure.

    The capital account counts the movements dated on or before the year
    end and the net income of every year up to and including it."""
    from ..engine import books, reporting, selfemploy
    if not year or year >= today.year:
        sheet = selfemploy.balance_sheet(conn, entity_id)
        sheet["as_of"] = today.isoformat()
        sheet["as_of_note"] = ("balances as of the download date — the "
                               "year is not over")
        sheet["unknown"] = []
        return sheet
    as_of = dt.date(year, 12, 31)
    rows = conn.execute(
        f"""SELECT COALESCE(a.display_name, a.name) AS name, a.type,
                   a.balance_current AS bal,
                   (SELECT MIN(t.date) FROM transactions t
                     WHERE t.account_id = a.id AND t.removed = 0) AS first,
                   (SELECT COALESCE(SUM(t.amount), 0) FROM transactions t
                     WHERE t.account_id = a.id AND t.removed = 0
                       AND COALESCE(t.pending, 0) = 0
                       AND t.date > %s) AS after
            FROM accounts a WHERE a.entity_id = %s
              AND a.balance_current IS NOT NULL
              {reporting._NOT_SHADOW.format(col="a.id")}""",
        (as_of, entity_id)).fetchall()
    assets, liabilities, unknown = [], [], []
    ta = tl = 0.0
    for r in rows:
        if r["type"] == "investment":
            unknown.append({"name": r["name"], "reason":
                            "market value at year end is not recorded"})
            continue
        if r["first"] is None or r["first"] > as_of:
            unknown.append({"name": r["name"], "reason":
                            "transaction history starts after year end"})
            continue
        # amounts are positive for money out: undoing a later charge puts
        # it back into a cash balance and takes it off a debt
        if r["type"] in ("credit", "loan"):
            bal = float(r["bal"]) - float(r["after"])
            liabilities.append({"name": r["name"], "amount": round(bal, 2)})
            tl += bal
        else:
            bal = float(r["bal"]) + float(r["after"])
            assets.append({"name": r["name"], "amount": round(bal, 2)})
            ta += bal
    from ..engine import equity
    # the same movements the live capital account counts: one fronted for
    # a charge the bank feed withdrew explains a cost the books skip
    by = {m["kind"]: float(m["s"]) for m in conn.execute(
        f"""SELECT m.kind, COALESCE(SUM(m.amount), 0) AS s
              FROM equity_movement m
             WHERE m.entity_id = %s AND m.date <= %s
               AND {equity.COUNTS_IN_CAPITAL}
             GROUP BY m.kind""",
        (entity_id, as_of)).fetchall()}
    upto = [t for t in lifetime
            if (books._row_date(t) or as_of) <= as_of]
    net_income = float(books._pnl_core(conn, entity_id, None, upto,
                                       lifetime=upto)
                       .get("net_operating") or 0)
    capital = (by.get("contribution", 0.0) + net_income
               - by.get("draw", 0.0) - by.get("distribution", 0.0))
    return {"assets": assets, "liabilities": liabilities,
            "total_assets": round(ta, 2), "total_liabilities": round(tl, 2),
            "net": round(ta - tl, 2), "capital_account": round(capital, 2),
            "as_of": as_of.isoformat(),
            "as_of_note": ("year-end balances, rebuilt from today's balance "
                           "and the transactions since"),
            "unknown": unknown}


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
        today = _today(conn)
        # the package is a year's: its balance sheet and capital account
        # stand at that year's end, not at the day it was downloaded
        sheet = _year_end_sheet(
            conn, entity_id, year, today,
            txns if year is None
            else books.entity_transactions(conn, entity_id, None))
        # the 1099 and mileage figures are per tax year; with no year asked
        # for, that is the household's current one, not the instance clock's
        tax_year = year or today.year
        vendors = selfemploy.vendor_totals(conn, entity_id, tax_year)
        trips = selfemploy.list_trips(conn, entity_id, year)
        miles = selfemploy.mileage_deduction(conn, entity_id, tax_year)
    finally:
        conn.close()

    def _csv(rows) -> str:
        buf = _io.StringIO()
        w = _csv_writer(buf)
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
                   "                       bucket and Schedule C line; Net\n"
                   "                       amount (what was not paid back)\n"
                   "                       is what the P&L adds up\n"
                   "  balance-sheet.csv    assets, liabilities, capital account\n"
                   f"                       as of {sheet['as_of']} "
                   f"({sheet['as_of_note']})\n"
                   "  vendors-1099.csv     paid vendors: total paid, the part\n"
                   "                       paid by card (reported on a 1099-K,\n"
                   "                       not a 1099-NEC) and the 1099-NEC\n"
                   "                       amount the threshold is judged on\n"
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
            ["Date", "Payee", "Amount", "Net amount", "Bucket",
             "Schedule C line", "Category"]] + [
            # every free-text cell goes through _csv_safe — `category` carries
            # the user's own category_override verbatim, and this file is built
            # to be handed to an accountant, so the person who opens it in Excel
            # is not the person who typed it. "Net amount" is what the P&L
            # counts (what was not paid back); "Amount" is what the bank
            # recorded — without the net column the rows under a Schedule C
            # line would not add up to that line.
            [str(x.get("date") or ""), _csv_safe(x.get("payee") or ""),
             f'{float(x.get("amount") or 0):.2f}',
             f'{float(books._net(x) or 0):.2f}',
             _csv_safe(x.get("bucket") or ""),
             _csv_safe(x.get("sched_c_line") or "Unclassified"),
             _csv_safe(x.get("category") or "")] for x in txns]))
        z.writestr("balance-sheet.csv", _csv(
            [["As of", sheet["as_of"]], ["Basis", sheet["as_of_note"]], []]
            + [["Assets", "Amount"]]
            + [[_csv_safe(a["name"]), f'{a["amount"]:.2f}']
               for a in sheet.get("assets", [])]
            + [["Total assets", f'{sheet.get("total_assets", 0):.2f}'], []]
            + [["Liabilities", "Amount"]]
            + [[_csv_safe(l["name"]), f'{l["amount"]:.2f}']
               for l in sheet.get("liabilities", [])]
            + [["Total liabilities", f'{sheet.get("total_liabilities", 0):.2f}'],
               [], ["Net", f'{sheet.get("net", 0):.2f}'],
               ["Capital account", f'{sheet.get("capital_account", 0):.2f}']]
            # an account whose year-end balance cannot be rebuilt is named,
            # with why, instead of being counted at today's figure
            + ([[], ["Not included", "Why"]]
               + [[_csv_safe(u["name"]), u["reason"]]
                  for u in sheet["unknown"]] if sheet["unknown"] else [])))
        # No column may be headed as the whole when it holds a part: a
        # preparer reads "Total paid" as everything and subtracts the card
        # column from it. So the total is the true total, and the form
        # amount is its own column.
        z.writestr("vendors-1099.csv", _csv(
            [["Vendor", "Total paid", "Paid by card (not 1099-NEC)",
              "1099-NEC amount", "Marked reportable"]]
            + [[_csv_safe(v.get("merchant") or ""),
                f'{float(v.get("paid") or 0) + float(v.get("paid_by_card") or 0):.2f}',
                f'{float(v.get("paid_by_card") or 0):.2f}',
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
        # a past year's export states the capital account at that year's
        # end, like the year-end package — not today's running balance
        capital = (p["capital"]["capital_balance"] if year is None else
                   _year_end_sheet(conn, entity_id, year, _today(conn),
                                   books.entity_transactions(
                                       conn, entity_id, None))
                   ["capital_account"])
    finally:
        conn.close()
    buf = _io.StringIO()
    w = _csv_writer(buf)
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
    w.writerow(["Capital account balance", f'{capital:.2f}'])
    w.writerow([])
    # "Net amount" is what the summary above counts (a charge less what was
    # paid back); "Amount" is what the bank recorded. Both, so the rows
    # re-foot to the summary and still match the statement.
    w.writerow(["Date", "Payee", "Amount", "Net amount", "Bucket",
                "Schedule C line", "Category"])
    for t in txns:
        # bucket goes through _csv_safe like every other free-text cell (and
        # like the same field in the year-end package): the column is plain
        # TEXT with the three-value taxonomy enforced in application code
        # only, and the full-ledger restore writes it straight from an
        # imported CSV — so what arrives here is not guaranteed to be one of
        # the three, and this file is meant to be opened in a spreadsheet.
        w.writerow([t["date"], _csv_safe(t["payee"]), f'{t["amount"]:.2f}',
                    f'{books._net(t):.2f}',
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
        # the household's own day: on a UTC instance a deadline due today
        # would drop off the list hours early, in the evening of its due date
        return {"obligations": compliance.obligations(
            conn, ent, today=_today(conn))}
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
            with _entity_write(conn, entity_id):
                return compliance.add_obligation(
                    conn, entity_id, title=_body_text(body, "title"),
                    due_date=body.get("due_date"),
                    recurrence=body.get("recurrence", "yearly"),
                    fee=body.get("fee"), url=_body_text(body, "url") or None,
                    note=_body_text(body, "note") or None)
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
    from ..engine import compliance, entities
    conn = _conn(user)
    try:
        _require_business(conn)
        # archived entities are read-only, and these are the retention
        # records archiving exists to keep — only delete-forever may go
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        with _entity_write(conn, entity_id):
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
        body = compliance.ics(conn, ent, today=_today(conn))
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
        # the household's day decides which tax year is in progress and how
        # much of it has elapsed; the instance clock rolls the year early
        return selfemploy.estimated_tax(conn, entity_id, year,
                                        today=_today(conn))
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
                "summary": selfemploy.mileage_deduction(
                    conn, entity_id, year or _today(conn).year)}
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
            with _entity_write(conn, entity_id):
                return selfemploy.add_trip(
                    conn, entity_id, date=body.get("date"),
                    miles=body.get("miles"),
                    purpose=_body_text(body, "purpose") or None,
                    note=_body_text(body, "note") or None)
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
    from ..engine import entities, selfemploy
    conn = _conn(user)
    try:
        _require_business(conn)
        # archived entities are read-only, and these are the retention
        # records archiving exists to keep — only delete-forever may go
        try:
            entities.require_active(conn, entity_id)
        except ValueError as _e:
            raise HTTPException(409 if "archived" in str(_e) else 404, str(_e))
        with _entity_write(conn, entity_id):
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
        # the year's 1099-NEC line, so the panel states the figure its
        # flags were measured against instead of a hard-coded one
        yr = year or _today(conn).year
        return {"vendors": selfemploy.vendor_totals(conn, entity_id, yr),
                "threshold": selfemploy.reportable_1099_threshold(yr)}
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
        merchant = _body_text(body, "merchant")
        if not merchant:
            raise HTTPException(400, "merchant required")
        tin = body.get("tin_last4")
        if tin is not None and (isinstance(tin, bool)
                                or not isinstance(tin, (str, int))):
            raise HTTPException(400, "tin_last4 must be a string")
        try:
            with _entity_write(conn, entity_id):
                selfemploy.mark_vendor(
                    conn, entity_id, merchant,
                    reportable=bool(body.get("reportable", True)),
                    tin_last4=tin)
        except ValueError as e:
            raise HTTPException(400, str(e))
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
        with _entity_write(conn, entity_id):
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
        try:                             # …and so is moving OUT of one
            if not entities.assign_account(conn, account_id, eid):
                raise HTTPException(404, "no such account")
        except ValueError as _e:
            raise HTTPException(409, str(_e))
        return {"ok": True, "account_id": account_id, "entity_id": eid}
    finally:
        conn.close()


def _refuse_moving_a_fronted_expense(conn, txn_id: str,
                                     entity_id: str | None) -> None:
    """A capitalized or reimbursable expense belongs to the one entity its
    movement names. Moving the row elsewhere (or back to personal) would
    leave that entity counting a contribution or a debt for a cost its books
    no longer carry, so the move is refused until the movement is deleted —
    the same rule the capitalize/reimburse doors apply. Takes the row lock
    those doors take, so the check and the move cannot interleave with a
    concurrent pick. Raises equity.FrontedExpenseConflict (a ValueError)."""
    from ..engine import equity
    row = conn.execute(
        "SELECT a.entity_id AS acct FROM transactions t "
        "LEFT JOIN accounts a ON a.id = t.account_id "
        "WHERE t.id = %s FOR UPDATE OF t", (txn_id,)).fetchone()
    if row is None:
        return                        # assign_transaction answers the 404
    after = entity_id or row["acct"]
    after = str(after) if after else None
    p = conn.execute(
        """SELECT m.kind, m.entity_id, e.name AS entity_name
             FROM equity_movement m
             LEFT JOIN business_entity e ON e.id = m.entity_id
            WHERE m.txn_id = %s AND m.kind = ANY(%s)
              AND m.entity_id::text IS DISTINCT FROM %s
            ORDER BY m.created_at, m.id LIMIT 1""",
        (txn_id, list(equity.FRONTED_KINDS), after)).fetchone()
    if p is not None:
        how = ("capitalized" if p["kind"] == "contribution"
               else "marked reimbursable")
        raise equity.FrontedExpenseConflict(
            f"that expense is {how} for "
            f"{p['entity_name'] or 'another business'}: delete that "
            "movement first")


@router.post("/transactions/{txn_id:path}/entity")
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
        try:                             # …and so is moving OUT of one
            with conn.transaction():
                # the pairing lock before the row lock, the order the
                # settlement takes them in; and the check reads the row the
                # move will land on, which for a hold that has since posted
                # is the posted charge
                txn_id = entities.live_txn_id(conn, txn_id) or txn_id
                _refuse_moving_a_fronted_expense(conn, txn_id, eid)
                if not entities.assign_transaction(conn, txn_id, eid):
                    raise HTTPException(404, "no such transaction")
        except ValueError as _e:
            raise HTTPException(409, str(_e))
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
    # the label becomes the account id, which may not hold a comma (the
    # link would succeed and every sync after it fail)
    if "," in str(body["label"]):
        raise HTTPException(400, "label may not contain a comma")
    conn = _conn(user)
    try:
        item_id = coinbase.link(conn, str(body["label"]), body["key_name"],
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
    billed per row, so never automatic; capped per month). Refused to the
    shared demonstration login: its password is handed out, and every run
    bills the Plaid account behind the instance."""
    demoguard.deny_shared_credentials(user)
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


@contextlib.contextmanager
def _push_write(conn, user: dict):
    """The write transaction of a collector push door. The push was
    authenticated before it got here, and an account erasure flips the
    household's status and then wipes its rows: a push landing after that
    would write the rows back under a tenant id nothing ever sweeps again.
    Holding the push writer lock for the transaction makes an erasure that
    starts now wait for this push (and then delete it); one already under
    way answers 409 and nothing is written."""
    from .. import erasure
    with conn.transaction():
        if not erasure.push_may_write(conn, user["tenant_id"]):
            raise HTTPException(
                409, "this household is being deleted; nothing was imported")
        yield


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
            with _push_write(conn, user):
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
            with _push_write(conn, user):
                return amazon_orders.import_orders(
                    conn, body.get("orders") or [])
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
            with _push_write(conn, user):
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
        except (TypeError, ValueError, OverflowError):
            # OverflowError: a JSON integer too wide for a double
            raise HTTPException(400, "balance must be a number")
        import math as _math
        if not _math.isfinite(balance):
            raise HTTPException(400, "balance must be a finite number")
        # finite alone is not enough: two balances near the float ceiling
        # add up to infinity in every net-worth total that carries them
        from ..engine.compat import MAX_AMOUNT
        if abs(balance) > MAX_AMOUNT:
            raise HTTPException(400, "balance is too large")
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
            with _push_write(conn, user):
                r = plan_csv.import_csv(conn, plan, body["csv"],
                                        balance=balance, mask=mask,
                                        provider=provider,
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


def _disconnect_under_sync_lock(conn, tenant_id: str, item_id: str) -> dict:
    """Archive an item only while no sync is running for the tenant.

    A pull that is already in flight re-upserts the item it is syncing, so
    archiving underneath it races the aggregator's own status writes; the
    guards on those writes keep 'archived' from being overwritten, and this
    lock keeps the two from interleaving at all. 409 tells the client to
    try again in a moment rather than leaving it to guess."""
    from ..sync import base as sync_base
    with sync_base.tenant_sync_lock(conn, tenant_id) as held:
        if not held:
            raise HTTPException(409, "a sync is running for this household — "
                                     "try again in a moment")
        try:
            return sync_base.disconnect_item(conn, item_id)
        except KeyError:
            raise HTTPException(404, "no such connection")


@router.post("/connections/{item_id}/disconnect")
def connection_disconnect(item_id: str, user: dict = Depends(_user())):
    """Archive a connection so it stops syncing; transaction history is
    kept. For Plaid this ALSO releases the Item on Plaid's side
    (/item/remove) — that's what frees a slot on limited plans; a local
    archive alone never would. That release cannot be undone, so the
    shared demonstration login may not make it for the household."""
    demoguard.deny_shared_credentials(user)
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    conn = _conn(user)
    try:
        r = _disconnect_under_sync_lock(conn, user["tenant_id"], item_id)
        return {"ok": True, "plaid_released": r["plaid_released"],
                "note": r["note"]}
    finally:
        conn.close()


# Where each other aggregator's disconnected connection comes back from: the
# reconnect door serves MX and single SimpleFIN banks, because for the others
# a new link or token is what brings the bank back. A SimpleFIN bank
# disconnected on its own is the exception among the bridge's: a new token
# brings back only the banks that went down with the bridge.
_RECONNECT_ELSEWHERE = {
    "plaid": "a disconnected Plaid bank comes back by linking it again "
             "from Connect a bank",
    "simplefin": "a disconnected SimpleFIN bridge comes back by connecting "
                 "a new SimpleFIN setup token; the banks that were "
                 "disconnected with it come back too, with what they "
                 "posted while disconnected",
    "coinbase": "a disconnected Coinbase account comes back by linking it "
                "again from Connect a bank",
}


@router.post("/connections/{item_id}/reconnect")
def connection_reconnect(item_id: str, user: dict = Depends(_user())):
    """Bring a disconnected MX or SimpleFIN bank back. The member was left alive at MX,
    so only the owner saying so can tell a bank they want back from one MX
    merely refreshed; the next sync then reaches back over the time it was
    disconnected (mx.reconnect). Taken under the tenant sync lock like the
    disconnect door, so a pull in flight never sees half of it. It
    restarts syncing and billing, so the shared demonstration login is
    refused like every other door that brings a bank in."""
    demoguard.deny_shared_credentials(user)
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    from ..sync import base as sync_base
    from ..sync import mx as mx_mod
    conn = _conn(user)
    try:
        it = conn.execute("SELECT aggregator FROM items WHERE id=%s",
                          (item_id,)).fetchone()
        if it is None:
            raise HTTPException(404, "no such connection")
        if it["aggregator"] == "simplefin-org":
            from . import setup as setup_mod
            with sync_base.tenant_sync_lock(conn, user["tenant_id"]) as held:
                if not held:
                    raise HTTPException(409, "a sync is running for this "
                                             "household — try again in a "
                                             "moment")
                try:
                    return setup_mod.reconnect_bank(conn, item_id)
                except KeyError:
                    raise HTTPException(404, "no such connection")
                except ValueError as e:
                    raise HTTPException(400, str(e))
        if it["aggregator"] != "mx":
            raise HTTPException(400, _RECONNECT_ELSEWHERE.get(
                it["aggregator"] or "",
                "only an MX connection is reconnected here"))
        if mx_mod.creds(conn) is None:
            raise HTTPException(400, "MX keys are not configured — add them "
                                     "again before reconnecting")
        with sync_base.tenant_sync_lock(conn, user["tenant_id"]) as held:
            if not held:
                raise HTTPException(409, "a sync is running for this "
                                         "household — try again in a moment")
            try:
                return mx_mod.reconnect(conn, item_id)
            except KeyError:
                raise HTTPException(404, "no such connection")
            except ValueError as e:
                raise HTTPException(400, str(e))
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
    # the owner's answer to the refusal below: delete the rows kept only
    # by a note that names no feed account
    force_kept = body.get("force_kept") is True
    if mode not in ("hide", "disconnect", "disconnect_purge", "purge"):
        raise HTTPException(
            400, "mode must be hide, disconnect, disconnect_purge, or purge")
    if mode in ("disconnect", "disconnect_purge"):
        # disconnecting releases a Plaid Item for good (and the purge
        # deletes the institution's history); the shared demonstration
        # login may hide an account or purge a manual one, never cut the
        # household's bank links
        demoguard.deny_shared_credentials(user)
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
            r = _disconnect_under_sync_lock(conn, user["tenant_id"], item_id)
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
            # asked before anything changes: the disconnect below commits
            if not force_kept:
                ids = sync_base.item_account_ids(conn, item_id)
                refusal = _kept_without_account_refusal(conn, ids, tuple(ids))
                if refusal is not None:
                    return refusal
            if conn_live:
                r = _disconnect_under_sync_lock(conn, user["tenant_id"], item_id)
                result["plaid_released"] = r["plaid_released"]
                result["note"] = r["note"]
            # all accounts under this item (incl. simplefin children)
            all_ids = sync_base.item_account_ids(conn, item_id)
            result["affected_accounts"] = all_ids
            try:
                result["purged"] = sync_base.purge_item_accounts(
                    conn, item_id, force_kept=force_kept)
            except sync_base.SyncRunning as e:
                # A disconnect made above has already committed; only the
                # delete waits. Say so, so the client reports neither a
                # clean failure nor a purge that did not happen — the
                # data can be deleted once the sync finishes.
                raise HTTPException(
                    409, (f"Disconnected, but the local data was not "
                          f"deleted: {e}") if conn_live else str(e))
            if not result["note"]:
                result["note"] = "Disconnected and deleted local data."
            else:
                result["note"] = (result["note"] + " Local data deleted.")
            result["note"] = _with_kept_rows(result["note"],
                                             result["purged"])
            return result

        # mode == "purge" — single-account hard delete
        if pull_live:
            raise HTTPException(
                400,
                "can't purge a live connected account (sync would recreate it) "
                "— use disconnect_purge for the institution, or hide to keep "
                "the connection")
        if not force_kept:
            refusal = _kept_without_account_refusal(conn, [account_id],
                                                    (account_id,))
            if refusal is not None:
                return refusal
        try:
            result["purged"] = sync_base.purge_account_data(
                conn, account_id, force_kept=force_kept)
        except sync_base.SyncRunning as e:
            raise HTTPException(409, str(e))
        if agg == "simplefin-org":
            # The bridge may still carry this account, so any later pull of
            # the bank would recreate it with its history. The bank is
            # marked so that neither a new setup token (which brings back
            # only banks marked as gone with the bridge) nor the reconnect
            # door brings it back. A bank a new link took over keeps that
            # mark: it is refused by both already, and says why.
            from . import setup as setup_mod
            conn.execute(
                "UPDATE items SET archived_reason=%s WHERE id=%s "
                "AND (archived_reason IS NULL OR archived_reason=%s)",
                (setup_mod.ACCOUNT_PURGED, item_id,
                 sync_base.BRIDGE_DISCONNECT))
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
        result["note"] = _with_kept_rows(
            "Account and its transactions deleted.", result["purged"])
        return result
    finally:
        conn.close()


def _kept_without_account_refusal(conn, account_ids: list[str],
                                  dropping: tuple[str, ...]):
    """A delete whose kept rows are ALL held only by a note naming no feed
    account would keep the account for ever: the note cannot be checked
    against any feed, so no later delete releases it either. Instead of
    that dead end the owner is asked, with the count, and answers with
    `force_kept: true`. None when the delete can simply go ahead."""
    from ..sync import base as sync_base
    kept, accountless = sync_base.kept_without_account(conn, account_ids,
                                                       dropping)
    if not kept or accountless != kept:
        return None
    rows = f"{kept} imported row{'s' if kept != 1 else ''}"
    return JSONResponse(status_code=409, content={
        "detail": (f"{rows} on this account may stand in for charges a "
                   f"bank feed set aside, and the feed may never send "
                   f"those charges again. Delete anyway to remove "
                   f"{'them' if kept != 1 else 'it'} with the account."),
        "error": "kept_rows_unconfirmed",
        "kept_transactions": kept})


def _with_kept_rows(note: str, purged: dict | None) -> str:
    """The purge's own sentence about rows it could not delete — imported
    rows a bank feed relies on, and the account kept to hold them — so the
    reply never claims a delete that only partly happened."""
    if purged and purged.get("kept_transactions") and purged.get("note"):
        return f"{note} {purged['note']}"
    return note


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
    from ..auth import device_tokens as _device_tokens
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
            f"WHERE user_id=%s AND {_push.LIVE}",
            (user["user_id"],)).fetchone()["n"]
        # phones push would actually reach: the delivery fan-out's own
        # target list (notify/push_native.py), so a phone it skips — one
        # the shared demonstration login registered — never makes the
        # clients offer push that nothing would carry
        native = len(_device_tokens.push_targets(cc, user["tenant_id"]))
        vapid_pub = None
        if _push.available():
            try:
                _priv, vapid_pub = _push.vapid_keys(cc)
            except Exception:                    # noqa: BLE001
                vapid_pub = None
    # A number an owner's credential change paused (auth/reset.py
    # _unverify_sms_number) stays on file, unverified, with the moment it
    # was paused — so the clients can say why the texts stopped and offer
    # to confirm it again or remove it. A number that was never verified
    # has no pause time.
    paused_at = None
    if phone.get("number") and not phone.get("verified") \
            and phone.get("unverified_at"):
        raw = str(phone["unverified_at"])
        try:
            paused_at = dt.datetime.fromisoformat(raw).astimezone(
                dt.timezone.utc).isoformat()
        except ValueError:
            paused_at = raw
    return {"sms_available": _sms.configured() and sms_entitled,
            "sms_entitled": sms_entitled,
            "phone": _sms.mask(phone.get("number")),
            "phone_verified": bool(phone.get("verified")),
            "phone_paused_at": paused_at,
            "phone_pending": _sms.mask(pending.get("number")),
            "push_available": _push.available() and vapid_pub is not None,
            "vapid_public_key": vapid_pub,
            "push_subscribed": subs > 0,
            # web push and phone push are separate channels: an instance with
            # no VAPID key can still reach the mobile apps through the relay,
            # so a phone-only household must not be told push is impossible
            "native_push_available": _push_native.available(),
            "native_push_devices": native,
            # mail is on but invitations cannot carry a link: an added
            # recipient is never invited, so never receives the daily email
            "invites_need_base_url": _invites_need_base_url(user)}


def _invites_need_base_url(user) -> bool:
    from ..notify import recipient_invites
    from .app import _tenant_smtp
    try:
        return recipient_invites.need_base_url(
            _tenant_smtp(user["tenant_id"]))
    except Exception:                            # noqa: BLE001
        # a status read must not fail on the mail config it only reports
        import logging
        logging.getLogger("oikonome.api").warning(
            "could not read the mail relay for the notify status",
            exc_info=True)
        return False


@router.post("/notify/phone",
             dependencies=[Depends(limit("notify-phone", 5, 3600))])
def notify_phone_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """Set (or clear) the tenant's SMS number. Setting sends a 6-digit
    verification code to the number — summaries only ever go to a number
    that typed its code back (a typo must not leak a household's budget
    to a stranger). The shared demonstration login can neither set nor
    clear it: a number a visitor verified keeps receiving the household's
    summaries after the visit and after the shared password is rotated."""
    demoguard.deny_shared_grant(user)
    _owner_only(user)
    from ..engine import budget
    from ..notify import sms as _sms
    number = str(body.get("number") or "").strip().replace(" ", "")
    if number:
        # A number is a durable destination for the household's summaries,
        # like a mail relay or a daily-mail recipient, and the code that
        # verifies it goes to the number being set — so a cookie alone
        # would verify a thief's own phone. Setting one is a step-up act;
        # clearing stays free, because it only takes a destination away.
        from .app import _require_elevation
        _require_elevation(
            user, password=str(body.get("password") or ""),
            totp_code=str(body.get("totp_code") or ""),
            recovery_code=str(body.get("recovery_code") or ""))
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
    # the step that actually opens the SMS channel, refused for the same
    # reason as the number itself
    demoguard.deny_shared_grant(user)
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
# The guard lives beside the sender (notify/push.endpoint_allowed), so the
# allowlist is checked with the same reading of the URL the delivery POST
# will use, both here at subscribe time and again before every send.
def _valid_push_endpoint(endpoint: str) -> bool:
    from ..notify import push as _push
    return _push.endpoint_allowed(endpoint)


@router.post("/notify/push/subscribe")
def push_subscribe_api(user: dict = Depends(_user()), body: dict = Body(...)):
    """Store this browser's PushSubscription (upsert on endpoint — a
    re-subscribe refreshes keys instead of duplicating).

    The shared demonstration login is refused: a subscription is a
    channel to one visitor's browser that every later alert for the
    household is sent down, and it stays when that visitor leaves and the
    shared password is rotated."""
    demoguard.deny_shared_grant(user)
    from .app import _control_conn, _require_elevation
    endpoint = str(body.get("endpoint") or "")
    keys = body.get("keys") or {}
    if not isinstance(keys, dict) or not _valid_push_endpoint(endpoint) \
            or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "bad subscription")
    # A subscription lives as long as the browser session that made it
    # (push_subscriptions.session_id cascades from sessions), so a cookie
    # that expires, signs out or is revoked takes its channel with it.
    # Only a browser session can make one: a script or device token has
    # no session for it to die with.
    if not user.get("session_id") or user.get("device_id") \
            or user.get("script_token"):
        raise HTTPException(
            403, "web push is turned on from a signed-in browser")
    # A new destination for the household's verdicts and alerts is a
    # step-up act, like an SMS number, a webhook or a mail recipient: a
    # stolen cookie alone would otherwise add the thief's own browser.
    # The same browser refreshing the subscription it already holds (its
    # endpoint and keys, on this account) is not a new destination.
    with _control_conn() as conn:
        held = conn.execute(
            "SELECT 1 FROM push_subscriptions WHERE endpoint=%s "
            "AND user_id=%s AND p256dh=%s AND auth=%s",
            (endpoint, user["user_id"], str(keys["p256dh"]),
             str(keys["auth"]))).fetchone()
    if held is None:
        _require_elevation(
            user, password=str(body.get("password") or ""),
            totp_code=str(body.get("totp_code") or ""),
            recovery_code=str(body.get("recovery_code") or ""))
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
    from .app import _caller_still_signed_in, _lock_user_factors
    with _control_conn() as conn, conn.transaction():
        # a reset deletes the account's subscriptions; one stored after it
        # would keep receiving the household's alerts with no session, so
        # the row is written under the reset's own lock
        _lock_user_factors(conn, user["user_id"])
        _caller_still_signed_in(conn, user)
        row = conn.execute(
            """INSERT INTO push_subscriptions
                 (user_id, tenant_id, endpoint, p256dh, auth, user_agent,
                  session_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (endpoint) DO UPDATE SET
                 user_id=EXCLUDED.user_id, tenant_id=EXCLUDED.tenant_id,
                 p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth,
                 user_agent=EXCLUDED.user_agent,
                 session_id=EXCLUDED.session_id,
                 dead_at=NULL
               WHERE push_subscriptions.user_id = EXCLUDED.user_id
                  OR (push_subscriptions.p256dh = EXCLUDED.p256dh
                      AND push_subscriptions.auth = EXCLUDED.auth)
               RETURNING id""",
            (user["user_id"], user["tenant_id"], endpoint,
             str(keys["p256dh"]), str(keys["auth"]),
             str(body.get("user_agent") or "")[:200],
             user["session_id"])).fetchone()
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
        # connect, not a held connection: the sends wait on outside push
        # services, and a request holding one of the control pool's few
        # connections across them takes it from everyone's session check
        n = _push.send_tenant(None, user["tenant_id"], "Oikonome test",
                              short, connect=_control_conn)
        n += _native.send_tenant(None, user["tenant_id"], "test",
                                 detail=native_detail,
                                 connect=_control_conn)
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
            # the relay said why it would not deliver (the device is bound
            # to another server, a rate limit, no answer at all) — that
            # sentence is the true story; "enable push" would send the
            # owner to sign in on the phone they are already holding
            if native_detail.get("targets") and native_detail.get("reason"):
                raise HTTPException(
                    400, f"your device is registered, but "
                         f"{native_detail['reason']}")
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
    demoguard.deny_shared_grant(user)
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
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, "hours must be a number")
    if not 1 <= hours <= 168:
        raise HTTPException(400, "hours must be 1–168")
    reason = str(body.get("reason") or "")[:200]
    from .app import _caller_still_signed_in, _lock_user_factors
    with _control_conn() as conn, conn.transaction():
        # a recovery revokes the grants this account made, under the
        # factor lock; a grant that authenticated before it and wrote
        # after would open the window the recovery just closed
        _lock_user_factors(conn, user["user_id"])
        _caller_still_signed_in(conn, user)
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
