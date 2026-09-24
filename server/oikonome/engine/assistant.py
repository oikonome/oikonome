"""A local conversational assistant over your own ledger.

Ask a plain-language question ("how much did I spend on groceries this year?",
"what's my net worth?"); the model ROUTES to a fixed set of READ-ONLY tools —
each a wrapper over an existing engine function — and phrases the answer from
what the code computes. The model never sees free SQL and can never write; it
only picks which of a handful of vetted summaries to fetch. Runs on the same
LLM backend as categorization (bundled Ollama by default, so your financial
data never leaves the box) and is unavailable when no LLM is configured.
"""

from __future__ import annotations

import ipaddress
import json
import threading
from urllib.parse import urlsplit

from ..envnum import env_num
from . import llm_categorize as _llm

MAX_TOKENS = 600
MAX_TOOL_HOPS = 3
# The model decides how many tools to call per hop; each call is a ledger
# query on the web thread, so an over-eager (or adversarially prompted)
# reply must not fan out without bound.
MAX_CALLS_PER_HOP = 4
_TOOL_RESULT_CAP = 2000
# Process-wide in-flight cap. Each ask holds a web threadpool thread for up
# to MAX_TOOL_HOPS x _HOP_TIMEOUT; the per-IP rate limit does not stop many
# tenants (or one tenant behind many addresses) from pinning every thread
# at once. Overflow is refused immediately (the route answers 503) rather
# than queued — a queued ask would still hold the thread it is meant to
# spare.
_IN_FLIGHT = threading.BoundedSemaphore(
    int(env_num("OIKONOME_ASSISTANT_CONCURRENCY", "3")))
# Bound each LLM hop so the whole ask can't hold a web threadpool thread for
# MAX_TOOL_HOPS x REQUEST_TIMEOUT (600s = 40 min) — a thread-exhaustion DoS.
# 150s comfortably covers a slow bundled CPU model; worst case is
# MAX_TOOL_HOPS x 150 = 7.5 min, and the endpoint is rate-limited 30/hr.
_HOP_TIMEOUT = 150


class AssistantUnavailable(Exception):
    """No LLM backend configured — the assistant hides entirely."""


class AssistantBusy(Exception):
    """Every in-flight slot is taken — try again shortly."""


def available(conn=None) -> bool:
    return _llm.configured(conn, role="assistant")


# Hostnames that mean "the model runs beside this server": the loopback
# names, the compose service of the bundled Ollama, and the container-to-
# host aliases a self-host uses to reach a model on the same machine.
_LOCAL_HOSTS = frozenset({"localhost", "ollama", "host.docker.internal",
                          "host.containers.internal"})


def _host_is_local(host: str) -> bool:
    h = (host or "").strip("[]").lower()
    if not h:
        return False
    if h in _LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def status(conn=None) -> dict:
    """{available, local, host}: whether an assistant backend exists, and
    whether questions (and the ledger figures the tools return) stay on
    this server. The operator's env/bundled backend counts as local — it
    is the standard install's Ollama sidecar. A tenant-configured backend
    is local only when its host is loopback or a same-box alias; anything
    else is named so the UI can say where the answers go, rather than
    making a fixed "nothing leaves the box" claim for every backend."""
    be = _llm._backend(conn, role="assistant")
    url = be.get("url") or ""
    if not url:
        return {"available": False, "local": False, "host": ""}
    host = urlsplit(url).hostname or ""
    if be.get("source") == "env":
        return {"available": True, "local": True, "host": ""}
    local = _host_is_local(host)
    return {"available": True, "local": local,
            "host": "" if local else host}


# ---- read-only tools (each wraps a vetted engine function) -----------------

def _t_net_worth(conn, _args) -> dict:
    from . import reporting
    nw = reporting.compute_networth(conn)
    return {
        "liquid_and_investments_total": nw.get("current_total"),
        "including_property_total": nw.get("full_total"),
        "by_asset_class": (nw.get("by_asset_class") or [])[:8],
    }


def _t_spending_summary(conn, _args) -> dict:
    from . import budget, reporting
    from .. import localtime
    # the household's day, as reporting_api passes it, never the
    # container clock
    today = localtime.now_local(budget.load_config(conn)).date()
    sp = reporting.compute_spending(conn, today=today)
    # The keys say what the numbers ARE. `category_totals` is the trailing
    # twelve months, not all time — labelled "alltime" it would have the
    # model answer "this year" questions from a window that is neither,
    # with figures that appear nowhere in the ledger. The year × category
    # matrix is what "how much on X in <year>" actually needs.
    years = sp.get("yoy_years") or []
    matrix = sp.get("yoy_matrix") or []
    by_year_cat = {}
    for row in matrix[:12]:
        cat, *amts = row
        by_year_cat[cat] = {y: a for y, a in zip(years, amts) if a}
    return {
        "by_category_trailing_12_months": (sp.get("category_totals") or [])[:12],
        "by_year_and_category": by_year_cat,
        "top_merchants_trailing_12_months": (sp.get("top_merchants") or [])[:8],
        "total_by_year": (sp.get("by_year") or [])[-4:],
    }


def _like(term: str) -> str:
    """A question's literal fragment as a `%term%` LIKE pattern, with the
    wildcards neutralised. The model relays whatever the person typed, so
    asking about a shop called "100%" or a label with an underscore in it
    must match those characters instead of every row on the ledger. Call
    sites pair this with an explicit ESCAPE so the pattern's meaning does
    not depend on a server setting. (web/data.py, engine/savings.py and
    engine/receipts.py each keep their own copy so those modules stay
    independent of one another.)"""
    t = term.replace("\\", "\\\\")
    return "%" + t.replace("%", "\\%").replace("_", "\\_") + "%"


def _t_spend_lookup(conn, args) -> dict:
    """Spend filtered by merchant and/or category and/or year — the tool for
    any question naming a shop, a category, or a period. Same predicate the
    Spending page uses (personal ledger, no transfers, no card payments), so
    the assistant's number is the report's number."""
    from . import reporting
    from .merchant_dedup import DISPLAY_MERCHANT, MC_JOIN
    merchant = str(args.get("merchant") or "").strip()[:100]
    category = str(args.get("category") or "").strip()[:100]
    year = args.get("year")
    where = [reporting.SPEND_WHERE]
    params: list = []
    if merchant:
        where.append(f"{DISPLAY_MERCHANT} ILIKE %s ESCAPE '\\'")
        params.append(_like(merchant))
    if category:
        # the row's own category, or any part of a hand split
        where.append(
            f"({reporting.EFF_CAT} ILIKE %s ESCAPE '\\' OR EXISTS ("
            "SELECT 1 FROM transaction_splits sp WHERE sp.txn_id = t.id "
            "AND REPLACE(sp.category, '_', ' ') ILIKE %s ESCAPE '\\'))")
        params.extend([_like(category.replace("_", " "))] * 2)
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    if year:
        where.append("EXTRACT(YEAR FROM t.date) = %s")
        params.append(year)
    if not (merchant or category or year):
        return {"error": "give at least one of merchant, category, year"}
    amt2 = reporting._R2.format(f"SUM({reporting.NET_AMOUNT})")
    tot = conn.execute(
        f"SELECT COALESCE({amt2}, 0) amt, COUNT(*) n, MIN(t.date) first, "
        f"MAX(t.date) last FROM transactions t {MC_JOIN} "
        f"WHERE {' AND '.join(where)}", params).fetchone()
    by_year = conn.execute(
        f"SELECT to_char(t.date,'YYYY') yr, {amt2} amt, COUNT(*) n "
        f"FROM transactions t {MC_JOIN} WHERE {' AND '.join(where)} "
        f"GROUP BY yr ORDER BY yr", params).fetchall()
    top = conn.execute(
        f"SELECT {DISPLAY_MERCHANT} payee, {amt2} amt, COUNT(*) n "
        f"FROM transactions t {MC_JOIN} WHERE {' AND '.join(where)} "
        f"GROUP BY payee ORDER BY amt DESC LIMIT 8", params).fetchall()
    return {
        "filter": {"merchant": merchant or None, "category": category or None,
                   "year": year},
        "total": tot["amt"], "transactions": tot["n"],
        "first": tot["first"], "last": tot["last"],
        "by_year": [[r["yr"], r["amt"], r["n"]] for r in by_year],
        "top_merchants": [[r["payee"], r["amt"], r["n"]] for r in top],
    }


def _t_recurring_bills(conn, _args) -> dict:
    from . import bills
    health = bills.analyze_bills(conn)
    out = []
    for payee, info in list(health.items())[:25]:
        info = info if isinstance(info, dict) else {}
        out.append({"payee": payee,
                    "amount": info.get("amount") or info.get("expected"),
                    "health": info.get("health")})
    return {"count": len(health), "bills": out}


_TOOLS_IMPL = {
    "net_worth": _t_net_worth,
    "spending_summary": _t_spending_summary,
    "spend_lookup": _t_spend_lookup,
    "recurring_bills": _t_recurring_bills,
}

TOOLS = [
    {"type": "function", "function": {
        "name": "net_worth",
        "description": "The user's current net worth: liquid + investments, "
                       "the total including property, and a breakdown by asset "
                       "class.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "spending_summary",
        "description": "Overview of the user's spending: trailing-12-month "
                       "totals by category, a year-by-category table, top "
                       "merchants, and per-year totals. Use for broad "
                       "'where does my money go' questions.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "spend_lookup",
        "description": "Spend filtered by a merchant name and/or a category "
                       "and/or a calendar year. Use whenever the question "
                       "names a specific shop, category (e.g. groceries = "
                       "'food and drink'), or year — 'how much at X', 'how "
                       "much on Y in 2025', 'what did I spend this year'.",
        "parameters": {"type": "object", "properties": {
            "merchant": {"type": "string",
                         "description": "part of the merchant name"},
            "category": {"type": "string",
                         "description": "part of the category name"},
            "year": {"type": "integer", "description": "calendar year"},
        }},
    }},
    {"type": "function", "function": {
        "name": "recurring_bills",
        "description": "The user's recurring bills and subscriptions with their "
                       "amounts and health (matching / drifting / stale).",
        "parameters": {"type": "object", "properties": {}},
    }},
]

_SYSTEM = (
    "You are Oikonome's assistant. Answer the user's question about THEIR OWN "
    "finances using ONLY the tools provided — never invent numbers, and only "
    "quote figures that appear in a tool result. Call a tool to get data, "
    "then answer concisely in plain language with the actual figures (US "
    "dollars). Say which period a figure covers. If the tools don't cover "
    "the question, say so briefly. Do not mention the tools or JSON. "
    "Today is {today}."
)


def _run_tool(conn, name: str, args: dict) -> dict:
    impl = _TOOLS_IMPL.get(name)
    if impl is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return impl(conn, args or {})
    except Exception as e:                      # a tool failure is not fatal
        return {"error": f"{type(e).__name__}: {e}"}


def ask(conn, question: str, transport=None, backend: dict | None = None) -> dict:
    """Answer a plain-language question about the tenant's ledger. Returns
    {answer, tools_used}. Raises AssistantUnavailable when no LLM is set."""
    question = (question or "").strip()
    if not question:
        return {"answer": "", "tools_used": []}
    be = backend if backend is not None else _llm._backend(conn,
                                                           role="assistant")
    if not (be.get("url")):
        raise AssistantUnavailable()
    if not _IN_FLIGHT.acquire(blocking=False):
        raise AssistantBusy()
    try:
        return _ask(conn, question, be, transport)
    finally:
        _IN_FLIGHT.release()


def _ask(conn, question: str, be: dict, transport) -> dict:
    from .. import localtime
    from . import budget as _budget
    # the model's "today" is the household's day, or "this month" in its
    # answers rolls over hours before the household's own calendar does
    today = localtime.now_local(_budget.load_config(conn)).date()
    messages = [{"role": "system",
                 "content": _SYSTEM.format(today=today.isoformat())},
                {"role": "user", "content": question[:1000]}]
    used: list[str] = []
    for _ in range(MAX_TOOL_HOPS):
        msg = _llm.chat_tools(messages, TOOLS, MAX_TOKENS,
                              transport=transport, backend=be,
                              timeout=_HOP_TIMEOUT)
        calls = (msg.get("tool_calls") or [])[:MAX_CALLS_PER_HOP]
        if not calls:
            return {"answer": (msg.get("content") or "").strip(),
                    "tools_used": used}
        messages.append({"role": "assistant",
                         "content": msg.get("content") or "",
                         "tool_calls": calls})
        for tc in calls:
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            result = _run_tool(conn, name, args)
            used.append(name)
            messages.append({
                "role": "tool", "tool_call_id": tc.get("id", ""),
                "content": json.dumps(result, default=str)[:_TOOL_RESULT_CAP],
            })
    return {"answer": "I wasn't able to finish answering that — try "
                      "rephrasing.", "tools_used": used}
