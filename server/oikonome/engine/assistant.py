"""A local conversational assistant over your own ledger.

Ask a plain-language question ("how much did I spend on groceries this year?",
"what's my net worth?"); the model ROUTES to a fixed set of READ-ONLY tools —
each a wrapper over an existing engine function — and phrases the answer from
what the code computes. The model never sees free SQL and can never write; it
only picks which of a handful of vetted summaries to fetch. Runs on the same
LLM backend as categorization (bundled Ollama by default, so your financial
data never leaves the box) and is unavailable when no LLM is configured.

An answer can carry charts and tables: the model writes a small JSON spec
in a fenced block, assistant_blocks validates it, and the clients draw it
(and can save the whole answer as a self-contained HTML report).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import uuid as _uuid
from urllib.parse import urlsplit

from ..envnum import env_num
from . import assistant_blocks as _blocks
from . import llm_categorize as _llm

# an answer may carry a chart or table spec beside its prose; a dozen
# months in two series is ~150 tokens, a ten-row table ~200
MAX_TOKENS = 1100
MAX_TOOL_HOPS = 3
# Hop budget spent and the model still asking for data: one more call with
# tools withheld, so it answers from what it has (a thorough model asked
# "by month" wants a lookup per month and would otherwise run out of hops
# holding six results and no answer), and one more when that turn comes
# back empty (_FINAL_RETRIES). Worst case is therefore
# (MAX_TOOL_HOPS + 1 + _FINAL_RETRIES) x _HOP_TIMEOUT per ask.
# The model decides how many tools to call per hop; each call is a ledger
# query on the web thread, so an over-eager (or adversarially prompted)
# reply must not fan out without bound.
MAX_CALLS_PER_HOP = 4
# A tool result is trimmed to this many JSON characters before the model
# sees it — structurally (lists and maps shortened, see _fit_result), never
# cut mid-string: a result sliced at a character count is broken JSON, and
# a model handed broken JSON for the spending summary asks for the same
# figures again one lookup at a time until its hops run out.
_TOOL_RESULT_CAP = 4000
log = logging.getLogger(__name__)

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


class ThreadRemoved(Exception):
    """A follow-up's conversation was deleted while its answer was being
    written: the answer is not kept, so the deleted conversation does not
    come back as the one turn that outlived it."""


def available(conn=None) -> bool:
    return _llm.configured(conn, role="assistant")


def status(conn=None) -> dict:
    """{available, local, host}: whether an assistant backend exists, and
    whether questions (and the ledger figures the tools return) stay on
    this server or its network.

    Judged from the endpoint's address for EVERY backend, the operator's
    included: an operator can point the instance-wide backend at a cloud
    API as easily as at the bundled Ollama, and the clients turn `local`
    into a "nothing leaves the box" promise. Loopback, private-range and
    same-box names are local; anything else is not. This is the same judge
    the off-box consent gate for tax documents uses, so the two cannot
    disagree about one endpoint. A non-local tenant backend is named so the
    UI can say where the answers go; the operator's host never is."""
    from .taxdocs import _endpoint_is_local
    be = _llm._backend(conn, role="assistant")
    url = be.get("url") or ""
    if not url:
        return {"available": False, "local": False, "host": ""}
    local = _endpoint_is_local(url)
    if be.get("source") == "env" or local:
        return {"available": True, "local": local, "host": ""}
    return {"available": True, "local": False,
            "host": urlsplit(url).hostname or ""}


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
        # per part: a split row's part category, else the row's own
        where.append(f"{reporting.PART_CAT} ILIKE %s ESCAPE '\\'")
        params.append(_like(category.replace("_", " ")))
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    if year:
        where.append("EXTRACT(YEAR FROM t.date) = %s")
        params.append(year)
    if not (merchant or category or year):
        return {"error": "give at least one of merchant, category, year"}
    # the Accounts page "excl" toggle, as every Spending-page query applies it
    excl, ex_p = reporting.excluded_accounts_sql(conn)
    params.extend(ex_p)
    if category:
        # By category a hand-split row counts under each of its parts for
        # that part's share, the way the Spending page's category rollup
        # reads it; the whole row would be counted under any one part's
        # category otherwise.
        src = (f"transactions t {MC_JOIN} {reporting.REIMB_PARTIAL_JOIN} "
               f"{reporting.SPLIT_JOIN}")
        amount, count = reporting.PART_NET_JOINED, "COUNT(DISTINCT t.id)"
    else:
        # unsplit by anything else: the parts sum to the row
        src = f"transactions t {MC_JOIN}"
        amount, count = reporting.NET_AMOUNT, "COUNT(*)"
    cond = f"{' AND '.join(where)} {excl}"
    amt2 = reporting._R2.format(f"SUM({amount})")
    tot = conn.execute(
        f"SELECT COALESCE({amt2}, 0) amt, {count} n, MIN(t.date) first, "
        f"MAX(t.date) last FROM {src} WHERE {cond}", params).fetchone()
    by_year = conn.execute(
        f"SELECT to_char(t.date,'YYYY') yr, {amt2} amt, {count} n "
        f"FROM {src} WHERE {cond} GROUP BY yr ORDER BY yr",
        params).fetchall()
    # the most recent 24 months of the filter: "by month this year" is one
    # call, not one per month
    by_month = conn.execute(
        f"SELECT to_char(t.date,'YYYY-MM') ym, {amt2} amt, {count} n "
        f"FROM {src} WHERE {cond} GROUP BY ym ORDER BY ym DESC LIMIT 24",
        params).fetchall()
    top = conn.execute(
        f"SELECT {DISPLAY_MERCHANT} payee, {amt2} amt, {count} n "
        f"FROM {src} WHERE {cond} "
        f"GROUP BY payee ORDER BY amt DESC LIMIT 8", params).fetchall()
    return {
        "filter": {"merchant": merchant or None, "category": category or None,
                   "year": year},
        "total": tot["amt"], "transactions": tot["n"],
        "first": tot["first"], "last": tot["last"],
        "by_year": [[r["yr"], r["amt"], r["n"]] for r in by_year],
        "by_month": [[r["ym"], r["amt"], r["n"]] for r in reversed(by_month)],
        "top_merchants": [[r["payee"], r["amt"], r["n"]] for r in top],
    }


def _t_recurring_bills(conn, _args) -> dict:
    from . import bills
    health = bills.analyze_bills(conn)
    out = []
    for payee, info in list(health.items())[:25]:
        info = info if isinstance(info, dict) else {}
        withdrawn_on = info.get("withdrawn_on")
        out.append({"payee": payee,
                    "amount": info.get("amount") or info.get("expected"),
                    "health": info.get("health"),
                    # behind a "withdrawn" verdict: the day the bank feed
                    # took the missed cycle's charge back
                    "withdrawn_on": (withdrawn_on.isoformat()
                                     if withdrawn_on else None)})
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
                       "much on Y in 2025', 'what did I spend this year'. "
                       "One call returns the total, the per-year and the "
                       "per-month breakdown and the top merchants for the "
                       "filter: never call it once per month.",
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
                       "amounts and health (ok = matching / drifting / "
                       "stale / withdrawn = the last cycle WAS charged but "
                       "the bank feed took the charge back, on "
                       "withdrawn_on).",
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
    "Write plain prose; **bold** for the key figures and short \"- \" "
    "bullet lists are fine, but no headings and no markdown tables (use a "
    "table block for a table). "
    "Today is {today}.\n\n{charts}"
)


def _fit_result(result, cap: int = _TOOL_RESULT_CAP) -> str:
    """`result` as JSON of at most `cap` characters that is STILL JSON. A
    map that is too long has its largest list or map halved — a `by_…`
    series keeps its most recent half, any other list its first (the
    leaders of a ranking) — until it fits, and says so with
    `truncated: true`. Only a value that cannot be shortened that way is
    ever cut by character count."""
    s = json.dumps(result, default=str)
    if len(s) <= cap or not isinstance(result, dict):
        return s[:cap]
    out = dict(result)
    while len(s) > cap:
        big = [(k, v) for k, v in out.items()
               if isinstance(v, (list, dict)) and len(v) > 1]
        if not big:
            break
        k, v = max(big, key=lambda kv: len(json.dumps(kv[1], default=str)))
        keep = max(1, len(v) // 2)
        if isinstance(v, dict):
            out[k] = dict(list(v.items())[:keep])
        elif k.startswith("by_"):
            out[k] = v[-keep:]
        else:
            out[k] = v[:keep]
        out["truncated"] = True
        s = json.dumps(out, default=str)
    return s[:cap]


# The answer-only turn's nudge. tool_choice "none" alone is not enough:
# a model still reaching for a lookup can spend that turn on a tool call
# the endpoint withholds and hand back an empty message on some
# OpenAI-compatible endpoints, so the turn says in words that the lookups
# are over, and an empty reply earns one more such turn before the
# apology.
_ANSWER_NOW = ("No more lookups are available. Answer the question now "
               "from the results above, with the figures they hold; if "
               "they do not cover it, say what is missing.")
_FINAL_RETRIES = 1


def _run_tool(conn, name: str, args: dict) -> dict:
    impl = _TOOLS_IMPL.get(name)
    if impl is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return impl(conn, args or {})
    except Exception as e:                      # a tool failure is not fatal
        return {"error": f"{type(e).__name__}: {e}"}


def ask(conn, question: str, transport=None, backend: dict | None = None,
        context: list[dict] | None = None) -> dict:
    """Answer a plain-language question about the tenant's ledger. Returns
    {answer, blocks, tools_used}: `answer` is the prose alone, `blocks` the
    ordered text + chart/table pieces the clients render (see
    assistant_blocks). `context` is the thread so far — [{question,
    answer}] oldest first, as thread_context returns it — for a follow-up;
    the model sees those turns before the new question and nothing else
    of the log. Raises AssistantUnavailable when no LLM is set."""
    question = (question or "").strip()
    if not question:
        return {"answer": "", "blocks": [], "tools_used": []}
    be = backend if backend is not None else _llm._backend(conn,
                                                           role="assistant")
    if not (be.get("url")):
        raise AssistantUnavailable()
    if not _IN_FLIGHT.acquire(blocking=False):
        raise AssistantBusy()
    try:
        return _ask(conn, question, be, transport, context or [])
    finally:
        _IN_FLIGHT.release()


_CHART_ONLY = "The answer is in the chart below."


def _context_messages(context: list[dict]) -> list[dict]:
    """The thread so far as alternating user/assistant messages: the
    newest THREAD_CONTEXT_TURNS turns, each answer cut to
    THREAD_CONTEXT_CHARS. The prose only — a chart block is a figure the
    model already quoted in the prose, and re-reading JSON helps nothing."""
    out = []
    for t in list(context)[-THREAD_CONTEXT_TURNS:]:
        q = str(t.get("question") or "").strip()[:1000]
        a = str(t.get("answer") or "").strip()
        if not q or not a:
            continue
        if len(a) > THREAD_CONTEXT_CHARS:
            a = a[:THREAD_CONTEXT_CHARS].rstrip() + " …"
        out.append({"role": "user", "content": q})
        out.append({"role": "assistant", "content": a})
    return out


def _ask(conn, question: str, be: dict, transport,
         context: list[dict] | None = None) -> dict:
    from .. import localtime
    from . import budget as _budget
    # the model's "today" is the household's day, or "this month" in its
    # answers rolls over hours before the household's own calendar does
    today = localtime.now_local(_budget.load_config(conn)).date()
    messages = [{"role": "system",
                 "content": _SYSTEM.format(today=today.isoformat(),
                                           charts=_blocks.guide())},
                *_context_messages(context or []),
                {"role": "user", "content": question[:1000]}]
    used: list[str] = []
    for hop in range(MAX_TOOL_HOPS + 1 + _FINAL_RETRIES):
        final = hop >= MAX_TOOL_HOPS
        if final:
            messages.append({"role": "user", "content": _ANSWER_NOW})
        msg = _llm.chat_tools(messages, TOOLS, MAX_TOKENS,
                              transport=transport, backend=be,
                              timeout=_HOP_TIMEOUT,
                              tool_choice="none" if final else "auto")
        calls = (msg.get("tool_calls") or [])[:MAX_CALLS_PER_HOP]
        if final:
            calls = []
        if not calls:
            content = msg.get("content")
            answer = (content if isinstance(content, str) else "").strip()
            if answer:
                prose, blocks = _blocks.parse(answer)
                if not prose and blocks:
                    # only a chart came back: `answer` is what a client
                    # that draws no blocks shows (and what keeps the turn
                    # in the log), and the raw fence is not a sentence
                    prose = _CHART_ONLY
                return {"answer": prose, "blocks": blocks,
                        "tools_used": used}
            if final:
                log.warning("assistant: the answer-only turn came back "
                            "empty (hop %d, %d tool calls so far)",
                            hop, len(used))
                continue
            break
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
                "content": _fit_result(result),
            })
    return {"answer": "I wasn't able to finish answering that — try "
                      "rephrasing.", "blocks": [], "tools_used": used}


# ---- the log of questions and answers --------------------------------------

# about how many answered questions each asker keeps; their oldest go as
# new ones arrive, so the table stays a recent log rather than a
# transcript forever (see _trim)
HISTORY_KEEP = 200
HISTORY_PAGE = 50
# A follow-up is answered with its own thread as context — the earlier
# questions and the prose answers, never the charts and never another
# thread — bounded so a long conversation cannot grow the prompt without
# limit: the newest turns, each answer cut to a readable length.
THREAD_CONTEXT_TURNS = 8
THREAD_CONTEXT_CHARS = 1500


def thread_context(conn, thread_id: str) -> list[dict]:
    """The thread's turns so far, oldest first, as `ask` wants its
    context: [{question, answer}]. Empty when the household has no such
    thread (another household's id reads as none under the row policy)."""
    rows = conn.execute(
        """SELECT question, answer FROM assistant_turns
            WHERE thread_id = %s ORDER BY asked_at, id""",
        (str(_uuid.UUID(str(thread_id))),)).fetchall()
    return [{"question": r["question"], "answer": r["answer"]} for r in rows]


def record_turn(conn, user, question: str, result: dict,
                thread_id: str | None = None) -> str | None:
    """Keep an answered question. Returns the row id, or None when there
    was nothing to keep (an empty answer). A new question opens a thread
    of its own (thread_id = its id); a follow-up names the thread it
    continues, and raises ThreadRemoved (keeping nothing) when that thread
    no longer has a turn. Trims the asker's own share of the household's
    log to about their newest HISTORY_KEEP rows (see _trim)."""
    from . import activity as _activity
    answer = (result.get("answer") or "").strip()
    if not answer:
        return None
    uid, who = _activity.actor_of(user)
    row_id = str(_uuid.uuid4())
    thread = str(_uuid.UUID(str(thread_id))) if thread_id else row_id
    # The check, the insert and the trim are ONE transaction: tenant
    # connections run in autocommit, where a row lock taken by a bare
    # statement is released the moment that statement returns, and a
    # removal could then land between the check and the insert.
    with conn.transaction():
        if uid is not None and conn.execute(
                "SELECT 1 FROM users WHERE id = %s AND tenant_id = "
                "current_setting('app.tenant_id')::uuid FOR SHARE",
                (uid,)).fetchone() is None:
            # The asker left this household while the model was answering
            # — removed, or moved out into a household of their own (their
            # row still exists, elsewhere). Leaving already blanked their
            # rows (forget_asker), so writing their id and address now
            # would name someone who no longer belongs here.
            uid, who = None, ""
            # ... or the whole household was erased meanwhile: the erasure
            # locks the members first, so this check waited for it, and a
            # row written now would be the one thing to outlive it.
            if conn.execute(
                    "SELECT 1 FROM tenants WHERE id = "
                    "current_setting('app.tenant_id')::uuid").fetchone() is None:
                return None
        # A follow-up joins a conversation that must still exist: the
        # model may have been answering for a minute, and the conversation
        # can have been deleted meanwhile. Writing the follow-up anyway
        # would bring the deleted conversation back as a thread whose
        # opening is gone. The share lock holds one of its turns until
        # this transaction commits, so a removal that starts now waits for
        # the insert, and forget_thread's second pass then takes it too.
        if thread != row_id and conn.execute(
                "SELECT 1 FROM assistant_turns WHERE thread_id = %s "
                "LIMIT 1 FOR SHARE", (thread,)).fetchone() is None:
            raise ThreadRemoved()
        # The share lock is held until this transaction commits, so a
        # removal (DELETE) or a move-out (UPDATE of tenant_id) that starts
        # after the check waits for the row below, and its forget_asker
        # then reaches it. A share lock rather than a key-share one,
        # because a move-out is an UPDATE of a non-key column and would
        # not wait for a key-share lock.
        row = conn.execute(
            """INSERT INTO assistant_turns
                   (id, thread_id, asked_by_id, asked_by, question, answer,
                    blocks, tools_used)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s) RETURNING id""",
            (row_id, thread, uid, who, question[:1000], answer,
             json.dumps(result.get("blocks") or [], default=str),
             list(result.get("tools_used") or []))).fetchone()
        # the share is keyed as the rows spell their asker: the id as
        # the database prints it, or the name when there is no id
        _trim(conn, str(_uuid.UUID(uid)) if uid is not None else who)
    # a caller on a non-autocommit connection may already have had a
    # transaction open (the block above was then a savepoint)
    conn.commit()
    return str(row["id"])


def _trim(conn, me: str) -> None:
    """Bound the log: each asker keeps about their newest HISTORY_KEEP
    turns. Three invariants hold, whoever asks and whatever runs at once:

    (i) one asker's trim never deletes another asker's rows. Asking is
        open to every role, a viewer's included, so a trim that reached
        past the asker's own rows would let anyone push everyone else's
        questions out of the log just by asking enough. Each share is
        counted and cut on its own; the log is still bounded, by the
        number of people in the household. Rows whose asker left share
        one share (their name is blank, see forget_asker).
    (ii) a conversation never loses its opening turn while any follow-up
        of it, anyone's, stays: a follow-up would otherwise hand the model
        a conversation that starts in the middle with nothing saying so.
        The opening is the conversation's anchor and goes only as its
        last row; what a long conversation loses is its oldest follow-ups.
    (iii) the trim and a concurrent follow-up cannot leave an orphan.

    The asker's turns are ranked by how recently their CONVERSATION was
    last active, by anyone — a conversation another member keeps going
    is not old — then newest first within it. Turns past the share go,
    an opening only when no other row of its conversation remains.

    Concurrency, under READ COMMITTED. A follow-up holds a share lock on
    one of its conversation's rows from its existence check until it
    commits (record_turn). The trim first locks the rows it means to
    delete FOR UPDATE SKIP LOCKED, and only then, in a NEW statement —
    so a fresh snapshot that sees every follow-up committed meanwhile —
    decides whether an opening still has company. A row a pending
    follow-up holds is skipped, not waited for: it survives this trim
    (and, being a row of the conversation, keeps the opening too); the
    next trim takes it. A follow-up that comes after the lock waits for
    this transaction, then finds the rows deleted and either locks a
    survivor (the opening is then kept) or finds none and keeps nothing
    (ThreadRemoved). Deleting in one statement on a stale snapshot is
    what this replaces: its wait on the follow-up's lock ended with the
    follow-up committed but invisible to it, so the opening went and the
    follow-up stayed behind alone. Never waiting also means two trims,
    or a trim and a follow-up, cannot deadlock."""
    ids = [r["id"] for r in conn.execute(
        """SELECT id FROM assistant_turns
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, COALESCE(asked_by_id::text, asked_by) AS who,
                           row_number() OVER (
                               PARTITION BY COALESCE(asked_by_id::text,
                                                     asked_by)
                               ORDER BY last DESC, thread_id DESC,
                                        asked_at DESC, id DESC) AS n
                      FROM (SELECT *, max(asked_at) OVER (
                                       PARTITION BY thread_id) AS last
                              FROM assistant_turns) t) ranked
                 WHERE who = %(me)s AND n > %(keep)s)
            ORDER BY id FOR UPDATE SKIP LOCKED""",
        {"me": me, "keep": HISTORY_KEEP}).fetchall()]
    if not ids:
        return
    conn.execute(
        """DELETE FROM assistant_turns d
            WHERE d.id = ANY(%(ids)s::uuid[])
              AND (d.id <> d.thread_id
                   OR NOT EXISTS (
                       SELECT 1 FROM assistant_turns o
                        WHERE o.thread_id = d.thread_id AND o.id <> d.id
                          AND NOT o.id = ANY(%(ids)s::uuid[])))""",
        {"ids": ids})


def parse_cursor(before: str | None, before_id: str | None = None
                 ) -> tuple[_dt.datetime | None, str | None]:
    """The page cursor as (asked_at, id). Raises ValueError for one that
    is not a timestamp (or an id that is not a uuid), so a bad link is
    refused before it reaches the database."""
    if not before:
        if before_id:
            raise ValueError("before_id needs before")
        return None, None
    at = _dt.datetime.fromisoformat(str(before).strip())
    if at.tzinfo is None:
        # asked_at is handed out with its offset; a bare one is read as UTC
        at = at.replace(tzinfo=_dt.timezone.utc)
    rid = str(_uuid.UUID(str(before_id))) if before_id else None
    return at, rid


def history(conn, limit: int = HISTORY_PAGE, before: str | None = None,
            before_id: str | None = None) -> dict:
    """The household's answered questions, newest first. The next page
    starts after (`before`, `before_id`) — the last row's asked_at and id,
    handed back as `next` — so rows sharing one timestamp are neither
    skipped nor repeated. `before` alone (no id) pages by the time only.
    Raises ValueError for a malformed cursor."""
    limit = max(1, min(int(limit or HISTORY_PAGE), HISTORY_PAGE))
    at, rid = parse_cursor(before, before_id)
    where, args = "", []
    if at is not None and rid is not None:
        where, args = "WHERE (asked_at, id) < (%s, %s::uuid)", [at, rid]
    elif at is not None:
        where, args = "WHERE asked_at < %s", [at]
    rows = conn.execute(
        f"""SELECT id, thread_id, asked_at, asked_by, question, answer, blocks,
                   tools_used
            FROM assistant_turns {where}
            ORDER BY asked_at DESC, id DESC LIMIT %s""",
        (*args, limit + 1)).fetchall()
    more = len(rows) > limit
    turns = [{
        "id": str(r["id"]),
        "thread_id": str(r["thread_id"]),
        "asked_at": r["asked_at"].isoformat(),
        "asked_by": r["asked_by"],
        "question": r["question"],
        "answer": r["answer"],
        "blocks": r["blocks"] or [],
        "tools_used": list(r["tools_used"] or []),
    } for r in rows[:limit]]
    nxt = ({"before": turns[-1]["asked_at"], "before_id": turns[-1]["id"]}
           if more and turns else None)
    return {"turns": turns, "more": more, "next": nxt}


def forget_asker(conn, tenant_id, user_id) -> int:
    """A member is leaving the household: their questions and the answers
    stay — they are the household's record — but the log stops naming
    them. Runs inside the caller's removal transaction, on whichever
    connection that is (the tenant is pinned for the transaction, so an
    app-role connection passes the row policy). Returns rows changed."""
    conn.execute("SELECT set_config('app.tenant_id', %s, true)",
                 (str(tenant_id),))
    return conn.execute(
        "UPDATE assistant_turns SET asked_by_id = NULL, asked_by = '' "
        "WHERE tenant_id = %s AND asked_by_id = %s",
        (str(tenant_id), str(user_id))).rowcount


# why the one-turn door kept an opening: the clients show it beside the
# 200 they already treat as success
OPENING_KEPT = ("This question opens a conversation that still has "
                "follow-ups; remove the whole conversation, or its "
                "follow-ups first.")


def forget_turn(conn, turn_id: str) -> dict:
    """Drop one turn — the question and answer named by its own id — and
    nothing else of its conversation. Returns {"deleted": n} and, when an
    opening was kept, {"reason": OPENING_KEPT}.

    An opening (its id is its thread's id) goes only as the last row of its
    conversation, the rule the trim keeps: dropped while follow-ups remain
    it would leave them headless — rows that answer a question nobody can
    see. Refused, not widened to the whole conversation, because the
    clients already in people's hands send this id meaning that one answer,
    and a follow-up they never chose to remove must not go with it.

    The turn is locked first. A follow-up being written holds a share lock
    on one of the thread's turns (record_turn), so the lock waits for it to
    commit, and the check that runs after sees the follow-up it wrote. A
    follow-up arriving after the lock waits on it, and finds the opening
    gone (ThreadRemoved) once this commits."""
    with conn.transaction():
        row = conn.execute(
            "SELECT id, thread_id FROM assistant_turns WHERE id = %s "
            "FOR UPDATE", (turn_id,)).fetchone()
        if row is None:
            out = {"deleted": 0}
        elif row["id"] == row["thread_id"] and conn.execute(
                "SELECT 1 FROM assistant_turns WHERE thread_id = %s "
                "AND id <> %s LIMIT 1", (row["thread_id"], row["id"])
                ).fetchone() is not None:
            out = {"deleted": 0, "reason": OPENING_KEPT}
        else:
            out = {"deleted": conn.execute(
                "DELETE FROM assistant_turns WHERE id = %s",
                (row["id"],)).rowcount}
    conn.commit()
    return out


def forget(conn, turn_id: str | None = None) -> int:
    """Drop one turn (see forget_turn) or, with no id, the whole log.
    Returns rows gone.

    The whole log is deleted in two passes in one transaction, as
    forget_thread does and for the same reason: the first pass waits for a
    follow-up holding its share lock on a turn, but its view of the table
    was fixed before that follow-up's row existed and would leave it
    behind, headless, in a log the household asked to empty. The second
    pass sees it."""
    if turn_id:
        return forget_turn(conn, turn_id)["deleted"]
    gone = 0
    with conn.transaction():
        for _ in range(2):
            gone += conn.execute("DELETE FROM assistant_turns").rowcount
    conn.commit()
    return gone


def forget_thread(conn, thread_id: str) -> int:
    """Drop one conversation: every turn of the thread. Returns rows gone.

    Two passes in one transaction. A follow-up being written holds one of
    the thread's turns (record_turn's share lock), so the first delete
    waits for it — but that delete's view of the table was fixed when it
    started, before the follow-up's row existed, and would leave that row
    behind as the deleted conversation's one survivor. The second pass
    sees it. A follow-up arriving after the first pass finds the thread's
    turns locked, waits, then finds them gone and keeps nothing."""
    gone = 0
    with conn.transaction():
        for _ in range(2):
            gone += conn.execute(
                "DELETE FROM assistant_turns WHERE thread_id = %s",
                (thread_id,)).rowcount
    conn.commit()
    return gone
