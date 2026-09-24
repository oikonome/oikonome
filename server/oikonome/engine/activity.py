"""The household's activity log: who changed what, and when.

Two adults keep one set of books. The ledger records the CHANGE — a
category override, a bill's new amount, a note — and, until this table,
nothing about the person: a member's edit and the owner's read the same.
Every door that takes a hand-authored change calls `record` after the
write and the row reads back as one sentence on the Users page (web) and
the Activity screen (mobile): "sam@… changed the category of Corner Store
$123.45 on 01/15/25 from Shopping to Groceries".

What is logged, and what is not: the acts a PERSON takes — categories,
splits, notes, bills and the offers about them, rules, receipts,
reimbursements, business flags, merchant names, the budget settings.
Machine writes (the sync, the bill pass stamping its rows, the nightly
categorizer, the store matchers) are absent on purpose: the log answers
"who did this", and for a machine write the answer is always the app.

The sentence is composed HERE, at write time, so every client renders the
same words and a row keeps saying what it said even if a later version
would phrase it differently. The structured facts (kind, action, target,
before/after in `detail`) sit beside it for filters and tests.

Values a sentence names are the household's own labels — a merchant, a
category, an amount, a payee — never a secret: the settings entry names
the KEYS that changed and no value at all.
"""
from __future__ import annotations

import copy
import datetime as dt
import logging

from .compat import jsonb

log = logging.getLogger("oikonome.activity")

# what the clients may filter by; the order is the order of a picker
KINDS = ("category", "split", "note", "bill", "rule", "receipt",
         "reimbursement", "business", "merchant", "account", "settings")

# a summary sentence is bounded so a pathological note or payee cannot
# make the log unreadable
SUMMARY_MAX = 300
LABEL_MAX = 120
QUOTE_MAX = 80

# rows older than this are pruned by the nightly job: "who changed this"
# is a question about the recent past, and a two-year window is the
# longest a household's memory of an edit plausibly runs
RETENTION_DAYS = 730

# what a page reads at a time
PAGE = 50
PAGE_MAX = 200


def actor_of(user) -> tuple[str | None, str]:
    """(user id, actor text) from a request's user dict, or from a bare
    address (the email-link door acts for an address, not a session)."""
    if isinstance(user, str):
        return None, user.strip().lower()[:LABEL_MAX]
    email = str(user.get("email") or "").strip()
    if user.get("script_token"):
        # current_user spells a collector as "script:<name>"; keep that
        # spelling so the row says a script did it and which one
        return None, email[:LABEL_MAX] or "script"
    uid = user.get("user_id")
    return (str(uid) if uid else None), email.lower()[:LABEL_MAX]


def _clip(s, n: int = QUOTE_MAX) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _cat(stored) -> str:
    # the SPA's catLabel: the stored key with its underscores opened up
    return (str(stored or "")).replace("_", " ") or "uncategorized"


def money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "$0.00"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _mmddyy(d) -> str:
    if isinstance(d, str):
        d = d[:10]
        try:
            d = dt.date.fromisoformat(d)
        except ValueError:
            return d
    if isinstance(d, dt.datetime):
        d = d.date()
    if not isinstance(d, dt.date):
        return ""
    return d.strftime("%m/%d/%y")


def txn_label(conn, txn_id: str) -> str:
    """The transaction as a person reads it: merchant, amount, date."""
    from . import merchant_sql
    r = conn.execute(
        f"""SELECT {merchant_sql.DISPLAY_MERCHANT} AS merchant, t.name,
                   t.amount, t.date
              FROM transactions t
              {merchant_sql.MC_JOIN}
             WHERE t.id = %s""", (txn_id,)).fetchone()
    if not r:
        return "a transaction"
    who = _clip(r["merchant"] or r["name"] or "a charge", 48)
    return f"{who} {money(r['amount'])} on {_mmddyy(r['date'])}"


# ---- sentences ------------------------------------------------------------

def sentence(kind: str, action: str, label: str, detail: dict | None) -> str:
    """The words for one act. Third person, no subject — the client puts
    the actor in front. Every branch reads as "<actor> <sentence>"."""
    d = detail if isinstance(detail, dict) else {}
    before, after = d.get("before"), d.get("after")
    if kind == "category":
        if action == "cleared":
            return (f"cleared the category of {label}"
                    + (f" (was {_cat(before)})" if before else ""))
        if action == "renamed":
            return f"renamed the category {_cat(before)} to {_cat(after)}"
        if action == "bulk":
            n = d.get("count") or 0
            return (f"filed {n} transaction{'s' if n != 1 else ''} under "
                    f"{_cat(after)}")
        tail = ""
        if d.get("scope") == "all" and d.get("merchant"):
            tail = f" and taught the merchant {_clip(d['merchant'], 48)}"
        if before and before != after:
            return (f"changed the category of {label} from {_cat(before)} "
                    f"to {_cat(after)}{tail}")
        return f"filed {label} under {_cat(after)}{tail}"
    if kind == "split":
        if action == "cleared":
            return f"removed the split on {label}"
        parts = d.get("parts") or []
        words = ", ".join(f"{_cat(p.get('category'))} {money(p.get('amount'))}"
                          for p in parts[:6])
        more = f" and {len(parts) - 6} more" if len(parts) > 6 else ""
        return f"split {label} {len(parts)} ways: {words}{more}"
    if kind == "note":
        if action == "cleared":
            return f"removed the note on {label}"
        if action == "edited":
            return f"changed the note on {label} to “{_clip(after)}”"
        return f"added a note on {label}: “{_clip(after)}”"
    if kind == "bill":
        if action == "added":
            return f"added the bill {label}{_bill_facts(d)}"
        if action == "edited":
            return f"edited the bill {label}{_bill_changes(d)}"
        if action == "archived":
            return f"archived the bill {label}"
        if action == "deleted":
            return f"deleted the bill {label}"
        if action == "restored":
            return f"restored the bill {label}"
        if action == "paused":
            return f"paused the bill {label} (left out of the budget)"
        if action == "resumed":
            return f"resumed the bill {label}"
        if action == "confirmed":
            what = "paycheck" if d.get("income") else "bill"
            return f"confirmed {label} as a {what}"
        if action == "dismissed":
            return f"dismissed the offer to track {label}"
        if action == "applied":
            return f"applied the offered change to {label}"
        if action == "kept":
            return f"kept {label} as it is"
    if kind == "rule":
        if action == "set":
            n = d.get("count")
            rows = (f" ({n} row{'s' if n != 1 else ''})"
                    if isinstance(n, int) else "")
            return f"set the rule {label} → {_cat(after)}{rows}"
        if action == "undone":
            return f"undid the rule {label}"
        if action == "disabled":
            return f"turned off the rule {label}"
        if action == "enabled":
            return f"turned on the rule {label}"
        if action == "deleted":
            return f"deleted the rule {label}"
    if kind == "receipt":
        what = "check image" if d.get("receipt_kind") == "check" else "receipt"
        if action == "removed":
            return f"removed a {what} from {label}"
        return f"attached a {what} to {label}"
    if kind == "reimbursement":
        if action == "flagged":
            exp = d.get("expected")
            return (f"flagged {label} as awaiting reimbursement"
                    + (f" ({money(exp)} expected)" if exp else ""))
        if action == "unflagged":
            return f"cleared the reimbursement flag on {label}"
        if action == "linked":
            n = d.get("count") or 1
            return (f"matched {label} with {n} "
                    f"repayment{'s' if n != 1 else ''}")
        if action == "unlinked":
            return f"unmatched a repayment from {label}"
    if kind == "business":
        if action == "flagged":
            return f"flagged {label} as a business expense"
        if action == "unflagged":
            return f"unflagged {label} as a business expense"
    if kind == "merchant":
        if action == "renamed":
            return f"renamed the merchant {label} to {_clip(after, 48)}"
        if action == "merged":
            return f"merged the merchant {label} into {_clip(after, 48)}"
        if action == "kept_apart":
            return f"kept the merchants {label} apart"
        if action == "undone":
            return f"undid a merchant name change ({label})"
    if kind == "account":
        if action == "renamed":
            return f"renamed the account {label} to {_clip(after, 48)}"
        if action == "excluded":
            return f"left the account {label} out of the budget"
        if action == "included":
            return f"put the account {label} back in the budget"
    if kind == "settings":
        keys = d.get("keys") or []
        if action == "debt_plan":
            return "saved the debt payoff plan"
        if action == "continuity_downloaded":
            return "downloaded the continuity packet"
        if action == "continuity_emailed":
            return f"emailed the continuity packet to {label}"
        shown = ", ".join(_setting_name(k) for k in keys[:6])
        more = f" and {len(keys) - 6} more" if len(keys) > 6 else ""
        return f"changed settings: {shown}{more}"
    # a kind/action this version does not know how to phrase still logs —
    # the facts are in the row, the sentence is the plainest possible
    return f"{action} {kind} {label}".strip()


def _cadence_words(freq, interval) -> str:
    f = (freq or "").upper()
    # the bills table spells a cadence the aggregator's way (EVERY_MONTH);
    # the save door spells it MONTHLY — one word for both
    f = {"EVERY_DAY": "DAILY", "EVERY_WEEK": "WEEKLY",
         "EVERY_MONTH": "MONTHLY", "EVERY_YEAR": "YEARLY"}.get(f, f)
    try:
        n = int(interval or 1)
    except (TypeError, ValueError):
        n = 1
    if f == "ENVELOPE":
        return "annual envelope" if n >= 12 else "envelope"
    if not f or f == "ONE_TIME":
        return "one-time"
    base = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month",
            "YEARLY": "year"}.get(f, f.lower())
    if n == 1:
        return {"day": "daily", "week": "weekly", "month": "monthly",
                "year": "yearly"}.get(base, base)
    return f"every {n} {base}s"


def _bill_facts(d: dict) -> str:
    a = d.get("after") or {}
    if not isinstance(a, dict):
        return ""
    bits = []
    if a.get("amount") is not None:
        bits.append(money(a["amount"]))
    if a.get("frequency") is not None or a.get("interval") is not None:
        bits.append(_cadence_words(a.get("frequency"), a.get("interval")))
    if a.get("due_on"):
        bits.append(f"due {_mmddyy(a['due_on'])}")
    return f" ({', '.join(bits)})" if bits else ""


def _bill_changes(d: dict) -> str:
    b, a = d.get("before") or {}, d.get("after") or {}
    if not isinstance(b, dict) or not isinstance(a, dict):
        return ""
    bits = []
    if b.get("amount") != a.get("amount") and a.get("amount") is not None:
        bits.append(f"amount {money(b.get('amount'))} → {money(a['amount'])}")
    if ((b.get("frequency"), b.get("interval"))
            != (a.get("frequency"), a.get("interval"))):
        bits.append(f"cadence {_cadence_words(b.get('frequency'), b.get('interval'))}"
                    f" → {_cadence_words(a.get('frequency'), a.get('interval'))}")
    if b.get("due_on") != a.get("due_on") and a.get("due_on"):
        bits.append(f"due {_mmddyy(b.get('due_on')) or '—'} → {_mmddyy(a['due_on'])}")
    if b.get("category") != a.get("category"):
        bits.append(f"category {_cat(b.get('category'))} → {_cat(a.get('category'))}")
    if b.get("payee") and a.get("payee") and b["payee"] != a["payee"]:
        bits.append(f"renamed from {_clip(b['payee'], 40)}")
    return f": {', '.join(bits)}" if bits else ""


_SETTING_NAMES = {
    "continuity_note": "continuity packet note",
    "continuity_for": "continuity packet recipient",
    "budget": "budget", "custom_buckets": "budget categories",
    "savings_goals": "savings goals", "excluded_accounts": "budget accounts",
    "disabled_bills": "paused bills", "occurrence_caps": "bill caps",
    "email_recipients": "email recipients", "email_hour": "email time",
    "timezone": "time zone", "llm_url": "AI backend", "smtp_host": "mail relay",
    "retirement": "retirement plan", "income_scenarios": "income scenarios",
    "debt_plan": "debt payoff plan", "home_page": "home page",
}


def _setting_name(key: str) -> str:
    return _SETTING_NAMES.get(key, str(key).replace("_", " "))


# ---- writing ---------------------------------------------------------------

def bill_snapshot(row) -> dict | None:
    """The facts of a bill row the log compares: what a person would
    notice changing. None for no row."""
    if row is None:
        return None
    from .compat import as_dict
    raw = as_dict(row.get("raw"))
    freq = row.get("frequency")
    interval = raw.get("interval") or 1
    if raw.get("bill_type") == "envelope" or (row.get("type") or "") == "ENVELOPE":
        freq = "ENVELOPE"
        interval = raw.get("interval") or raw.get("envelope_months") or 1
    due = row.get("due_on")
    return {"payee": row.get("payee"), "amount": abs(float(row.get("amount") or 0)),
            "frequency": freq, "interval": interval,
            "due_on": due.isoformat() if isinstance(due, dt.date) else due,
            "category": row.get("category"),
            "income": (row.get("type") or "").upper() == "INCOME"}


def record(conn, user, kind: str, action: str, *, target: str | None = None,
           label: str = "", detail: dict | None = None) -> str | None:
    """Write one row. `user` is the request's user dict or a bare address.

    Never raises into the door that called it: a log that could fail the
    write it describes would make every edit slightly less reliable for
    the sake of a record of it. The failure is logged instead.
    """
    uid, actor = actor_of(user)
    if not actor:
        actor = "someone"
    label = _clip(label, LABEL_MAX)
    words = sentence(kind, action, label, detail)[:SUMMARY_MAX]
    d = copy.deepcopy(detail) if detail else None
    try:
        r = conn.execute(
            """INSERT INTO activity_log (actor_user_id, actor, kind, action,
                                         target, label, summary, detail)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (uid, actor, kind, action, target, label, words,
             jsonb(d) if d is not None else None)).fetchone()
    except Exception as e:                                     # noqa: BLE001
        log.warning("activity: could not record %s/%s: %s", kind, action, e)
        return None
    rid = str(r["id"]) if r else None
    # the household's webhooks, if any, hear the same sentence — queued,
    # never sent from inside the door that made the change
    from ..notify import webhooks
    webhooks.emit(conn, "activity.logged", {
        "id": rid, "actor": actor, "kind": kind, "action": action,
        "target": target, "label": label, "summary": words, "detail": d})
    return rid


# ---- reading ---------------------------------------------------------------

def _row(r) -> dict:
    d = dict(r)
    d["id"] = str(d["id"])
    at = d.get("at")
    d["at"] = at.isoformat() if isinstance(at, dt.datetime) else at
    if d.get("actor_user_id") is not None:
        d["actor_user_id"] = str(d["actor_user_id"])
    from .compat import as_dict
    d["detail"] = as_dict(d.get("detail")) if d.get("detail") else None
    return d


def list_activity(conn, *, limit: int = PAGE, before: str | None = None,
                  kind: str | None = None, actor: str | None = None,
                  target: str | None = None) -> dict:
    """Newest first. `before` is the `at` of the last row a page showed
    (an ISO timestamp): the next page is everything strictly older. Rows
    sharing a timestamp to the microsecond are ordered by id, so a page
    boundary between two such rows can drop one — a microsecond tie at a
    page edge is an acceptable price for a cursor that is one string."""
    limit = max(1, min(int(limit or PAGE), PAGE_MAX))
    where, args = [], []
    if before:
        try:
            cut = dt.datetime.fromisoformat(str(before))
        except ValueError:
            cut = None
        if cut is not None:
            where.append("at < %s")
            args.append(cut)
    if kind:
        where.append("kind = %s")
        args.append(kind)
    if actor:
        where.append("actor = %s")
        args.append(actor.strip().lower())
    if target:
        where.append("target = %s")
        args.append(target)
    sql = "SELECT * FROM activity_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY at DESC, id DESC LIMIT %s"
    args.append(limit + 1)
    rows = conn.execute(sql, args).fetchall()
    more = len(rows) > limit
    rows = [_row(r) for r in rows[:limit]]
    actors = [r["actor"] for r in conn.execute(
        "SELECT actor FROM activity_log GROUP BY actor ORDER BY max(at) DESC"
    ).fetchall()]
    return {"rows": rows, "more": more,
            "next": rows[-1]["at"] if more and rows else None,
            "actors": actors, "kinds": list(KINDS)}


def for_target(conn, target: str, limit: int = 10) -> list[dict]:
    """The history of one thing — a transaction's rows, newest first."""
    return list_activity(conn, target=target, limit=limit)["rows"]


def prune(conn, days: int = RETENTION_DAYS) -> int:
    """Drop rows older than the retention window. Nightly."""
    return conn.execute(
        "DELETE FROM activity_log WHERE at < now() - make_interval(days => %s)",
        (int(days),)).rowcount
