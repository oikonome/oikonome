"""What every JSON API module shares: the one router they all register on,
and the request helpers more than one area uses (who is asking, their
household connection, their household's day, may they edit).

web/api.py was one module; it is split by area into web/api_<area>.py
(specs/api-routers.md). Each area module imports from HERE and never from
another area module or from api.py, so there is no import cycle and no area
depends on another. api.py imports the area modules in the order their
routes used to appear, which keeps the route table — and so which pattern
Starlette tries first — exactly as it was.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException

from ..db import tenancy
from . import permissions
from . import security as _security
from .fields import Fields
from .session_user import current_user
from fastapi import Request
import re
from ..engine import activity as _activity
from . import data

# The one router every API module registers on. Its routes are matched in
# the order they were added, which is the order api.py imports the modules.
router = APIRouter(prefix="/api")


def _user():
    # The dependency lives apart from the app so any API module can be the
    # first thing a process imports (web/session_user.py).
    return current_user


def _conn(user):
    return tenancy.tenant_connect(user["tenant_id"])


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


def _is_owner(user: dict) -> bool:
    """The household owner signed in as themself — never a script token,
    which acts for the owner but is not them."""
    return user.get("role") == "owner" and not user.get("script_token")


def _owner_only(user: dict) -> None:
    if not _is_owner(user):
        raise HTTPException(403, "owner only")


def _scalar_text(body: Fields, key: str) -> str:
    """A credential or name field as the step-up and token doors have
    always read it: str() of whatever scalar was sent, "" for a falsy one.
    Scripts mint and revoke tokens too, and one sending its TOTP code or
    the token id as a JSON number must keep working; a list or an object
    was never a usable credential and is a 400 naming the field."""
    try:
        return body.text(key, strip=False)
    except HTTPException:
        pass
    try:
        v = body.flag(key)
    except HTTPException:
        try:
            v = body.int_in(key)
        except HTTPException:
            v = body.money(key, msg=f"{key} must be a string")
    return str(v or "")


def _ser(row: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v)
            for k, v in dict(row).items()}


def _step_up_proof(body: Fields) -> dict:
    """The step-up proof a typed door's body may carry, for
    `_require_elevation`. Each is kept exactly as typed — a password may
    begin or end with a space — and a value that is not a string is a 400,
    never str()'d into a guess that cannot match."""
    return {k: body.text(k, strip=False)
            for k in ("password", "totp_code", "recovery_code")}


def _literal_true(body: Fields, key: str) -> bool:
    """Whether the body sent the JSON literal `true` for a destructive
    option. Anything else — false, "yes", 1, a list — leaves the option
    off and the door asks first, rather than being refused: the safe
    reading of a mistyped "delete anyway" is not to delete."""
    try:
        return body.flag(key) is True
    except HTTPException:
        return False


# The most items a body's list is read for where the door itself never
# bounded one (a report's turns, a whole settings list the cleaner below
# slices). A JSON array spends at least two bytes on each item, so no body
# the edge's size limit lets in carries more: the bound states the limit
# already in force and refuses nothing a client could send before it.
_BODY_ROWS_MAX = _security.BODY_LIMIT // 2


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

    `pages.py:_csv_cell` carries the same rule for the full-data export.

    A cell that already opens with quotes in front of such a lead gets one
    more: the household's own apostrophe then survives (a spreadsheet hides
    exactly one), and a restore can take exactly one off — the mark is
    reversible because text that looks guarded is always guarded again."""
    if not isinstance(v, str) or not v:
        return v
    head = v.lstrip("".join(_CSV_INVISIBLE_LEAD) + "'")
    if v[:1] in _CSV_INVISIBLE_LEAD + _CSV_FORMULA_LEAD + ("'",) \
            and head[:1] in _CSV_FORMULA_LEAD:
        return "'" + v
    if v.lstrip("'")[:1] in ("\t", "\r"):
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
    then the first visible character of every piece that reaches it. An
    apostrophe already there keeps the flag too, so a piece the household
    typed as "'=x" gets one more and a restore takes exactly one off. A
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
        elif ch not in _CSV_INVISIBLE and ch not in ('"', "'"):
            # a double quote keeps the flag: csv doubles it, and a reader
            # that splits on the break opens a quoted field there, closes it
            # at the doubled quote and reads what follows as the cell
            lead = False
    if not changed:
        return v
    out.reverse()
    return "".join(out)


def _csv_piece_unsafe(v):
    """Reverse _csv_piece_safe for a cell read back from an export (the
    full-data ZIP is also the restore input, so a round trip must give
    the household's text back byte for byte). Exactly one quote comes out
    where the export put one: right after a piece break, with a formula
    character next once quotes, invisible characters (a break excepted)
    and double quotes are passed. The export adds one there whenever the
    piece already looks like that, so the step loses nothing; the result
    is kept only when guarding it again gives exactly the cell read."""
    if not isinstance(v, str) or "'" not in v:
        return v
    out = []
    changed = False
    n = len(v)
    for i, ch in enumerate(v):
        if ch == "'" and i and v[i - 1] in _CSV_PIECE_BREAK:
            j = i + 1
            while j < n and (v[j] in _CSV_INVISIBLE or v[j] in ('"', "'")) \
                    and v[j] not in _CSV_PIECE_BREAK:
                j += 1
            if j < n and v[j] in _CSV_FORMULA:
                changed = True
                continue
        out.append(ch)
    if not changed:
        return v
    cand = "".join(out)
    return cand if _csv_piece_safe(cand) == v else v


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


def _ck_ym(y: int, m: int) -> None:
    """Bound a caller-supplied year/month BEFORE it reaches dt.date().

    Out of range they would reach the engine as an unhandled ValueError
    from `dt.date(y, m, 1)` — a 500 instead of a 400. Written inline, the
    guard would be re-implemented per endpoint and every later endpoint that
    takes a y/m would re-open the same hole. One spelling, shared."""
    if not (1900 <= y <= 2100 and 1 <= m <= 12):
        raise HTTPException(400, "y/m out of range")


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


def _budgets_set(conn) -> bool:
    """Has the household a budget of its own? (budget.budgets_set — the
    stored row, never the provisional seed.)"""
    from ..engine import budget
    return budget.budgets_set(conn)


_SOURCE_WORDS = {
    "plaid": "your bank's aggregator (Plaid)", "import": "the file it was imported from",
    "flow": "the ledger's own transfer / payment rules", "fuel": "the fuel-arm outlet",
    "mcc": "the merchant's registered line of business (MCC)",
    "rule_user": "your rule for this merchant", "rule_llm": "the AI's merchant rule",
    "rule_seed": "the built-in merchant rule", "rule_model": "the learned merchant rule",
    "rule": "a merchant rule",
    "check": "a written check the bank called a transfer: it counts as spending until its payee says more",
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


def _ck_year(y: int | None, field: str = "y") -> None:
    """Same, for endpoints that take a bare year.

    EVERY endpoint taking a `year` has to call this, not just the ones that
    obviously build a date: a year eventually reaches `dt.date(year, 1, 1)`,
    `year + 1` (the 1040-ES Q4 deadline), or a bigint bind, and each of those
    turns year=0 or year=99999 into an opaque 500 instead of a 400."""
    if y is not None and not 1900 <= y <= 2100:
        raise HTTPException(400, f"{field} out of range")


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
    # the first bill that matches names the row, so the bills are read in
    # one readable order (the bill set up first, then by name), never the
    # order the table happens to return (specs/today-figures.md, "Row
    # order")
    active = conn.execute("SELECT payee, merchant, amount, raw "
                          "FROM bills WHERE active=1 "
                          "ORDER BY created_on NULLS FIRST, lower(payee), "
                          "uid").fetchall()
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


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _setup_mode_of(cfg: dict) -> str:
    """The household's bill setup choice; anything but "manual" is auto."""
    return "manual" if cfg.get("setup_mode") == "manual" else "auto"


# the longest merchant name a write may store (_merchant_name_or_400)
MERCHANT_NAME_MAX = 200


def _merchant_name_or_400(field: str, val: str,
                          max_len: int | None = MERCHANT_NAME_MAX) -> None:
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


def _category_name(body: Fields, key: str, *, user: dict) -> str:
    """A category NAME a request is about to write: the text field plus
    the one length every category door keeps for a NEW name. A category is
    stored on every row, rule and split it touches and rendered in every
    picker and email, so a name with no bound is a cheap way to bloat the
    ledger by the kilobyte per row."""
    return _new_category_or_400(body.text(key), user)


def _new_category_or_400(v: str, user: dict) -> str:
    if len(v) > data.CATEGORY_MAX_LEN and not _category_long_ok(user, v):
        raise HTTPException(
            400, f"a category name is at most {data.CATEGORY_MAX_LEN} "
                 f"characters")
    return v


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


def _label_field(body: Fields, key: str) -> str:
    """An account name or owner label from a JSON body, held to the
    merchant-name rule. These are not stored once and read once: the
    account label and the owner are selected onto every ledger and spend
    row of the account, so an unbounded one multiplies into every page,
    export and email that reads those rows. A new manual account's name
    also becomes its primary-key id, which overflows the index long before
    the body limit."""
    v = body.text(key)
    _merchant_name_or_400(key, v)
    return v
