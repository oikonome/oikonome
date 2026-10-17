"""SimpleFIN Bridge connector (US/CA; the user holds their own
bridge.simplefin.org subscription; the app never touches bank credentials).

Protocol (https://www.simplefin.org/protocol.html):
  1. user pastes a one-time SETUP TOKEN (base64 of a claim URL)
  2. POST the claim URL once → the permanent ACCESS URL
     (https://user:pass@bridge.../simplefin) — this is the item's
     access_token, stored encrypted
  3. GET {access}/accounts?start-date=<unix> → accounts + transactions

Conventions handled at this boundary:
  * SimpleFIN amounts are bank-signed strings (negative = money out) →
    flipped to the engine's Plaid convention (positive = out).
  * txn ids namespaced "sfin:<account>:<id>".
  * balances: SimpleFIN reports debt as a NEGATIVE balance; the engine
    expects the amount owed positive (Plaid) → flipped for every debt type
    (base.DEBT_TYPES: cards and loans), heuristically detected from the
    account name until the user classifies (setup wizard asks). Once the
    user has classified the account (accounts.type_user_set), the persisted
    type decides the flip — the name heuristic never says "loan", so a
    mortgage is only ever signed right because that classification is
    honoured here as well as at the classify door.

Testable: pass `transport=` (httpx.MockTransport) — no live HTTP in tests.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import math
import re
import time
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from ..engine.compat import MAX_AMOUNT, as_date
from .adopt import stored_account_id
from .base import DEBT_TYPES, Account, Transaction, _finite_or_none, \
    bound_first_pull, \
    filter_import_duplicates, log_sync, reconcile_vanished_pendings, \
    upsert_accounts, upsert_item, upsert_transactions, user_set_types

log = logging.getLogger(__name__)


def _url_auth(url: str) -> tuple[str, tuple[str, str] | None]:
    """Split a SimpleFIN credentialed URL (https://user:pass@host/...) into a
    clean URL + a Basic-auth tuple. The bearer secret is the URL userinfo, and
    httpx echoes request.url verbatim in EVERY transport/status exception — so
    leaving it in the URL leaks it into logs, redirect query strings and
    tracebacks, one sink at a time. Carrying it in an Authorization header
    instead means no exception, log line, or redirect can ever contain it —
    the credential is removed at the source."""
    p = urlsplit(url)
    if not p.username:
        return url, None
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    clean = urlunsplit((p.scheme, host, p.path, p.query, p.fragment))
    return clean, (unquote(p.username), unquote(p.password or ""))

CREDIT_HINTS = ("credit", "card", "visa", "mastercard", "amex",
                "american express")
# whole-word match so "card" doesn't classify "Cardinal Savings" as credit
_CREDIT_RE = re.compile(r"\b(" + "|".join(CREDIT_HINTS) + r")\b")


def _guard(url: str, what: str) -> str:
    """Netguard a SimpleFIN URL, raising ValueError the callers already
    handle. Both URLs in this flow are attacker-influenced."""
    from ..web.netguard import BlockedURL, check_url
    try:
        check_url(url, what=what)
    except BlockedURL as e:
        raise ValueError(str(e))
    return url


# Both SimpleFIN servers are whichever the setup token names, so a reply is
# read with a ceiling on its size and on the whole exchange, not only on
# each read: a server sending gigabytes, or one byte a minute, would
# otherwise hold the hourly sync (and every household queued behind it) or
# the worker's memory. The claim answers with one URL; a fetch carries up
# to two years of every bank on the bridge.
CLAIM_READ_MAX = 16 * 1024
CLAIM_DEADLINE = 30.0
FETCH_READ_MAX = 64 * 1024 * 1024
FETCH_DEADLINE = 180.0


class ReplyTooLarge(ValueError):
    """The bridge's reply ran past its ceiling."""


def _client(transport, deadline: float) -> httpx.Client:
    """A client whose every socket operation shares one wall-clock
    deadline (the webhook sender's own mechanism). A test's transport is
    used as given."""
    c = httpx.Client(transport=transport or _pinned(),
                     timeout=min(60.0, deadline - time.monotonic()))
    if transport is None:
        from ..notify.webhooks import _with_deadline
        _with_deadline(c, deadline)
    return c


def _read(r: httpx.Response, cap: int, deadline: float, what: str) -> bytes:
    """The decoded body, at most `cap` bytes (decoded, so a compressed
    reply cannot inflate past it) and only while the deadline lasts."""
    got = bytearray()
    for chunk in r.iter_bytes():
        got += chunk
        if len(got) > cap:
            raise ReplyTooLarge(f"{what} is larger than {cap} bytes")
        if time.monotonic() > deadline:
            raise ValueError(f"{what} took too long to arrive")
    return bytes(got)


def _pinned():
    """both SimpleFIN URLs are tenant-supplied, so on hosted the
    fetch pins DNS (see netguard.pinned_transport). Explicit `transport=`
    (tests) wins at every call site."""
    from ..web.netguard import pinned_transport
    return pinned_transport()


def claim_setup_token(setup_token: str, transport=None) -> str:
    """One-time exchange: setup token → permanent access URL."""
    claim_url = base64.b64decode(setup_token.strip()).decode()
    # The token decodes to a URL we POST to — on hosted, block claim
    # URLs that resolve to internal addresses (SSRF). Self-host allows a
    # private SimpleFIN bridge; the metadata IP is always blocked.
    _guard(claim_url, "the SimpleFIN claim URL")
    post_url, auth = _url_auth(claim_url)
    deadline = time.monotonic() + CLAIM_DEADLINE
    with _client(transport, deadline) as c:
        # The claim secret is a PATH segment of the claim URL, and every
        # httpx error names the URL — a refused or unreachable bridge would
        # otherwise put a still-unclaimed token into the app log (and the
        # feedback bundle built from it) through the callers' traceback.
        # The replacement names the status only, and `from None` keeps the
        # original exception out of the chained traceback too.
        try:
            with c.stream("POST", post_url, auth=auth) as r:
                if r.is_error:
                    raise ValueError("the SimpleFIN bridge refused the claim "
                                     f"(HTTP {r.status_code})")
                body = _read(r, CLAIM_READ_MAX, deadline,
                             "the SimpleFIN claim reply")
        except httpx.HTTPError as e:
            raise ValueError("the SimpleFIN claim request failed "
                             f"({type(e).__name__})") from None
        # The claim RESPONSE is the access URL we will hit on every sync
        # forever — checking only the claim URL guarded the door we knock on,
        # not the address that door hands back. A public claim endpoint could
        # answer "http://169.254.169.254/…" and we would have persisted it.
        return _guard(body.decode("utf-8", "replace").strip(),
                      "the SimpleFIN access URL")


def _classify(acct: dict, type_hint: str | None) -> tuple[str, str | None]:
    if type_hint:
        return (type_hint, "credit card" if type_hint == "credit" else None)
    # SimpleFIN carries no account type — heuristic on the account name AND
    # the institution/org name: a card account is often named "Alex Rivera (1234)"
    # while its org is "Discover Credit Card" / "American Express", so the
    # org name is the stronger signal for credit/investment.
    org = (acct.get("org") or {})
    name = " ".join((
        acct.get("name") or "",
        org.get("name") or "", org.get("domain") or "")).lower()
    if _CREDIT_RE.search(name):
        return "credit", "credit card"
    sub = _investment_subtype(name)
    if sub:
        return "investment", sub
    return "depository", "checking"


# name token → the subtype the engine keys tax treatment on (the retirement
# sim's buckets, the net-worth asset classes). Roth decides first whatever
# the plan wrapper; a plan that names no wrapper is still pre-tax money.
#
# Each hint is a TOKEN pattern, never a bare substring, because the two
# coincidences banks actually produce are "ira" inside an ordinary word
# (Vanguard's Admiral share class; a holder's first name, "Amira Patel
# (1234)") and a plan number inside a masked last-4 ("Brokerage ...4013").
# The subtype decides whether the retirement sim withdraws the money
# pre-tax, Roth or taxable, so a hit on a coincidence is a wrong tax bill,
# not a wrong label. Plan numbers therefore accept a following letter or
# bracket (401A, 401(k), 403B) but refuse a further digit — the extra digit
# is exactly what distinguishes an account mask from a plan number. The
# boundaries are drawn against the token's OWN kind of character: a word
# token may not sit inside letters, a plan number may not sit inside
# digits — but letters and digits may touch, because administrators glue
# them ("Roth401k", "Retirement401k"), and `\b` sees no boundary there.
_W, _WE = r"(?<![a-z])", r"(?![a-z])"          # word token: not inside letters
_D, _DE = r"(?<!\d)", r"(?!\d)"                # plan number: not inside digits
_SUBTYPE_TOKENS = ((_W + r"roth" + _WE, "roth"), (_D + r"401" + _DE, "401k"),
                   (_D + r"403" + _DE, "403b"), (_D + r"457" + _DE, "457b"),
                   (_W + r"iras?" + _WE, "ira"), (_W + r"hsa" + _WE, "hsa"),
                   (_D + r"529" + _DE, "529"), (_W + r"brokerage" + _WE, "brokerage"),
                   (_W + r"ret\.?\s*plan" + _WE, "retirement"),
                   (_W + r"retirement" + _WE, "retirement"),
                   (_W + r"pension" + _WE, "retirement"))
_SUBTYPE_RE = tuple((re.compile(p), sub) for p, sub in _SUBTYPE_TOKENS)


def _investment_subtype(name: str) -> str | None:
    """SimpleFIN carries no subtype, so the account's own name must say
    how the money is taxed. A flat stamp would put a pre-tax 401(k) in
    the taxable bucket, where every withdrawal takes a capital-gains
    haircut on ordinary-income money.

    None when the name says nothing about a wrapper — which is also how
    `_classify` decides the account is not an investment at all, so the
    type and the subtype can never disagree about what the name said. A
    name with an investment word but no wrapper ("Vanguard Brokerage")
    still answers here; one with neither falls through to depository, where
    the user's own classification is the fix and is sacred once made."""
    for pattern, sub in _SUBTYPE_RE:
        if pattern.search(name):
            return sub
    return None


def fetch(access_url: str, since: dt.date | None = None,
          transport=None) -> dict:
    """GET the accounts payload (accounts + transactions since `since`).

    Re-guards the URL on EVERY request, not just at claim time — the
    access URL is persisted and replayed hourly, and it can also arrive from
    a restore or a `.oikx` import that never passed through claim_setup_token.
    """
    _guard(access_url, "the SimpleFIN access URL")
    params = {}
    if since:
        params["start-date"] = int(dt.datetime.combine(
            since, dt.time.min, dt.timezone.utc).timestamp())
    get_url, auth = _url_auth(access_url.rstrip("/") + "/accounts")
    deadline = time.monotonic() + FETCH_DEADLINE
    with _client(transport, deadline) as c:
        with c.stream("GET", get_url, params=params, auth=auth) as r:
            # A refusal is raised on its status alone and its body is never
            # read: reading it decodes it whole, and a compressed error
            # page could inflate past any memory the worker has.
            r.raise_for_status()
            body = _read(r, FETCH_READ_MAX, deadline,
                         "the SimpleFIN bridge reply")
    try:
        payload = json.loads(body)
    except ValueError:
        raise ValueError("the SimpleFIN bridge reply is not JSON") from None
    return _well_formed(payload)


def _text(v) -> str | None:
    """A bridge text field as a string: a number is spelled as one (an id
    of 42 is the id "42"), anything else (an object, a list, a boolean) is
    absent. Everything downstream — the last-4 regex, the type heuristic,
    the text columns — assumes a string, and a bridge URL is the tenant's
    own, so the reply's types are not ours to trust."""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def _texts(d: dict, keys) -> dict:
    """`d` with each of `keys` it carries passed through _text."""
    return {**d, **{k: _text(d[k]) for k in keys if k in d}}


def _well_formed(payload) -> dict:
    """The reply with every account that has no id, and every account or
    row that is not an object, left out, and every text field it reads
    spelled as a string or dropped. One bridge token carries every bank,
    so one malformed entry must cost that entry, not the pull."""
    if not isinstance(payload, dict):
        raise ValueError("the SimpleFIN bridge reply is not an object")
    accounts = []
    raw_accounts = payload.get("accounts")
    for a in raw_accounts if isinstance(raw_accounts, list) else []:
        aid = _text(a.get("id")) if isinstance(a, dict) else None
        if not aid:
            log.warning("SimpleFIN account skipped (malformed)")
            continue
        rows = a.get("transactions")
        rows = [_texts(t, ("id", "description", "payee"))
                for t in rows if isinstance(t, dict)] \
            if isinstance(rows, list) else []
        a = {**_texts(a, ("name", "currency")), "id": aid,
             "transactions": rows}
        org = a.get("org")
        a["org"] = (_texts(org, ("name", "domain"))
                    if isinstance(org, dict) else {})
        accounts.append(a)
    return {**payload, "accounts": accounts}


def _amount(v) -> float | None:
    """A bridge balance as a finite number, or None (unknown) for anything
    else: a non-numeric string is not a reason to fail the pull, and NaN or
    infinity would poison every sum and reply that reads the account."""
    if v is None or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: a JSON integer too wide for a double
        return None
    return n if math.isfinite(n) else None


# Depth of an institution's FIRST pull — the same window the claim-time
# call sites (setup wizard, connect/reconnect doors) request. One bridge
# token returns EVERY institution the user added at bridge.simplefin.org,
# and a bank added there later just appears in the payload of a routine
# sync — no new claim happens on our side. Without a one-time deep pull,
# that bank's history older than the routine window (30 days) would never
# be fetched by anything, ever.
FIRST_PULL_DAYS = 90


def _child_item(item_id: str, org: dict,
                institution_name: str) -> tuple[str, str]:
    """(child item id, display name) for an org in the payload — the one
    definition both the new-org detection and sync()'s upsert use, so the
    two can never disagree about which orgs are new."""
    name = (org or {}).get("name") or (org or {}).get("domain") \
        or institution_name
    slug = "".join(c if c.isalnum() else "-" for c in name.lower())[:40]
    return f"{item_id}:{slug}", name


def _first_pull_since(conn, item_id: str, payload: dict,
                      since: dt.date | None,
                      institution_name: str) -> dt.date | None:
    """Detect a brand-new institution under an already-connected bridge.

    Returns the deep first-pull date when the payload carries an org whose
    child item does not exist yet (its first appearance here), and the
    requested window is shallower than the first-pull depth. Returns None
    when every org is already known — including archived (disconnected)
    ones, which must not trigger a deep re-pull — or when the caller
    already asked for a full/deep window."""
    if since is None:
        return None                       # unbounded pull is already deep
    deep = dt.date.today() - dt.timedelta(days=FIRST_PULL_DAYS)
    if since <= deep:
        return None                       # caller's window is deep enough
    known = {r["id"] for r in conn.execute(
        "SELECT id FROM items WHERE id LIKE %s",
        (item_id + ":%",)).fetchall()}
    for a in payload.get("accounts") or []:
        if _child_item(item_id, a.get("org") or {},
                       institution_name)[0] not in known:
            return deep
    return None


# How far before a disconnect a bank brought back by a new setup token
# reaches on its next pull, so the days either side of the gap overlap.
# The mark is items.resync_from. The migration that added that column
# describes only MX's reconnect door, which wrote it first; both
# aggregators write it now, each reading back only its own items.
RESYNC_OVERLAP = dt.timedelta(days=7)


def _resync_owed(conn, item_id: str, payload: dict,
                 institution_name: str) -> tuple[dt.date | None, list[str]]:
    """The earliest day a bank brought back by a new token still needs, and
    which of its children asked — only those this payload actually
    carries. The bridge takes one start-date for every bank, so a gap owed
    to a bank the bridge no longer returns must not deepen every routine
    pull for ever; it waits until that bank is back in the payload."""
    rows = conn.execute(
        "SELECT id, resync_from FROM items WHERE aggregator='simplefin-org' "
        "AND id LIKE %s AND resync_from IS NOT NULL "
        "AND COALESCE(status,'') != 'archived'",
        (item_id + ":%",)).fetchall()
    if not rows:
        return None, []
    present = {_child_item(item_id, a.get("org") or {}, institution_name)[0]
               for a in payload.get("accounts") or []}
    owed = [r for r in rows if r["id"] in present]
    if not owed:
        return None, []
    return min(r["resync_from"] for r in owed), [r["id"] for r in owed]


def _write_bank_signed_balances(conn, account_id: str, bal, avail) -> None:
    """Store the bank-signed balances under the account's type AS IT IS
    NOW, decided in the statement that writes them.

    The sign the pull computes above comes from a read of the classified
    types taken at the start of the pull. A person classifying the account
    while the pull runs re-signs the stored balance in the classify
    statement; a balance this pull then wrote with the sign it chose from
    its earlier read would undo that, leaving a card read as an asset (or
    a deposit as a debt) until the next pull. Decided here, from the row's
    type at write time, the two orders give one answer: a classify that
    commits first is read here, and one that commits after re-signs what
    this wrote. A hidden account keeps no balance, as upsert_accounts
    keeps none."""
    bal = _finite_or_none(bal, account_id)
    avail = _finite_or_none(avail, account_id)
    conn.execute(
        "UPDATE accounts SET "
        "balance_current = CASE WHEN type = ANY(%(debt)s) "
        "    THEN -(%(bal)s::double precision) "
        "    ELSE %(bal)s::double precision END, "
        "balance_available = CASE WHEN type = ANY(%(debt)s) "
        "    THEN -(%(avail)s::double precision) "
        "    ELSE %(avail)s::double precision END "
        "WHERE id = %(id)s AND user_removed_at IS NULL",
        {"debt": list(DEBT_TYPES), "bal": bal, "avail": avail,
         "id": account_id})


def sync(conn, item_id: str, access_url: str, *, since: dt.date | None = None,
         institution_name: str = "SimpleFIN",
         account_types: dict[str, str] | None = None,
         transport=None) -> dict:
    """Full pull → normalized upserts. `account_types` = optional user
    classification {account_id: 'depository'|'credit'|...} from the setup
    wizard; unclassified accounts get the name heuristic.

    A routine (30-day) sync that turns out to be an institution's very
    first appearance re-fetches ONCE with the deep first-pull window, so a
    bank added later at the bridge still gets its initial history. The
    upserts are idempotent, so the extra depth for already-known orgs in
    the same payload is harmless."""
    # the household's history window bounds a deep pull (the claim's first
    # pull, an unbounded one, a bank's first appearance), as it bounds a
    # Plaid link's backfill; a routine refresh is left as asked
    since = bound_first_pull(conn, since)
    try:
        payload = fetch(access_url, since=since, transport=transport)
        deep = _first_pull_since(conn, item_id, payload, since,
                                 institution_name)
        if deep is not None:
            deep = bound_first_pull(conn, deep)
            if since is not None and deep >= since:
                deep = None         # the window is no deeper than this pull
        # a bank a new token brought back reaches back over the time it was
        # disconnected, which the routine window never would
        owed, owed_items = _resync_owed(conn, item_id, payload,
                                        institution_name)
        # the bridge takes one start date for every bank, so the gap is
        # filled only as far back as the household's history window: past
        # it, every other bank's pre-window rows would land as well
        if owed is not None:
            owed = bound_first_pull(conn, owed)
        if (owed is not None and since is not None
                and owed < (deep or since)):
            deep = owed
        if deep is not None:
            payload = fetch(access_url, since=deep, transport=transport)
    except Exception as e:                       # noqa: BLE001
        log_sync(conn, item_id, 0, error=f"{type(e).__name__}: {e}")
        # mark the bridge AND its per-org child items (accounts hang off the
        # org items, and links health reads the account's own item) so a
        # dead token fails over immediately instead of after 48h stale.
        # A disconnected (archived) child or bridge keeps its status: an
        # error stamp would drop it out of the archived set the next good
        # pull skips, and that pull would bring the bank back with its
        # history. A restored shell keeps its status for the reason the
        # worker's own error write gives.
        conn.execute(
            "UPDATE items SET status=%s WHERE (id=%s OR id LIKE %s) "
            "AND COALESCE(status,'') NOT IN ('archived', 'restored')",
            (f"error:{type(e).__name__}", item_id, item_id + ":%"))
        raise
    # One SimpleFIN bridge token returns EVERY institution the user added
    # there — hanging them all under one item makes the first institution
    # swallow every other one's accounts. The bridge item keeps the
    # token + health; each org gets a child item (aggregator
    # 'simplefin-org', no token — the worker's sync loop skips it) and
    # accounts hang off their org, so the UI groups by real institution.
    upsert_item(conn, item_id, "simplefin", "SimpleFIN bridge", access_url,
                keep_archived=True)

    # a DISCONNECTED per-bank child stays disconnected: upsert_item resets
    # status='ok', so archived children must be skipped entirely (no item
    # resurrect, no accounts, no transactions) even though the bridge
    # payload still carries their data
    archived = {r["id"] for r in conn.execute(
        "SELECT id FROM items WHERE aggregator='simplefin-org' "
        "AND status='archived'").fetchall()}

    def _org_item(org: dict) -> str:
        child, name = _child_item(item_id, org, institution_name)
        if child not in archived:
            upsert_item(conn, child, "simplefin-org", name, None,
                        keep_archived=True)
        return child

    # user-classified accounts: persisted type wins over the name heuristic
    # (both for what upsert keeps and for the balance sign flip below)
    pinned = user_set_types(conn)
    accounts, txns = [], []
    pulled_ids: set[str] = set()
    org_of: dict[str, str] = {}
    renamed_ids: dict[str, str] = {}
    # the balances as the bank signed them, written last by
    # _write_bank_signed_balances, which decides the sign at write time
    bank_signed: dict[str, tuple[float | None, float | None]] = {}
    for a in payload.get("accounts") or []:
        # the bridge's id is any string; a comma would be refused by the
        # accounts table and fail this whole pull, so it is spelled the way
        # the account is stored (adopt.stored_account_id). The transaction
        # ids below keep the bridge's own spelling — they are what the next
        # pull dedups on.
        aid = stored_account_id(conn, str(a["id"]), renamed_ids)
        types = account_types or {}
        typ, sub = _classify(a, types.get(aid, types.get(a["id"])))
        bal = _amount(a.get("balance"))
        avail = _amount(a.get("available-balance"))
        bank_signed[aid] = (bal, avail)
        if pinned.get(aid, typ) in DEBT_TYPES:
            # SimpleFIN reports debt negative (a card's and a loan's alike);
            # the engine wants the amount owed positive
            bal = -bal if bal is not None else None
            avail = -avail if avail is not None else None
        org_of[aid] = _org_item(a.get("org") or {})
        # SimpleFIN has no mask field, but banks put the last-4 in the
        # account NAME ("Spending Account (1234)") — extract it, or the
        # cross-provider auto-link suggestions (mask + type match) can
        # never see a SimpleFIN account and duplicates go unnoticed
        name = a.get("name") or a["id"]
        digits = re.findall(r"\((\d{2,})\)", name)
        accounts.append(Account(
            id=aid, name=name, type=typ, subtype=sub,
            mask=digits[-1][-4:] if digits else None,
            balance_current=bal, balance_available=avail,
            currency=a.get("currency"),
            raw={k: v for k, v in a.items() if k != "transactions"}))
        for t in a.get("transactions") or []:
            # the FEED carried this row whatever happens below — recorded
            # before the date check so an undated-but-present pending is
            # never mistaken for one the source dropped
            if t.get("id"):
                pulled_ids.add(f"sfin:{a['id']}:{t['id']}")
            posted = t.get("posted") or t.get("transacted_at")
            if not posted:
                # legal for a SimpleFIN pending; epoch-0 would date it
                # 1970-01-01 and poison every "history starts" MIN(date)
                log.warning("SimpleFIN row skipped (no date): %s/%s",
                            a["id"], t.get("id"))
                continue
            # a timestamp beyond what the platform can convert (Overflow/
            # OSError) or a year the app cannot read (0001, 9999) is one bad
            # row: skip it rather than abort the pull or store a date every
            # later reader raises on
            try:
                posted_on = as_date(dt.datetime.fromtimestamp(
                    int(posted), dt.timezone.utc).date())
            except (ValueError, OverflowError, OSError):
                log.warning("SimpleFIN row skipped (date out of range): "
                            "%s/%s", a["id"], t.get("id"))
                continue
            # one row with no id or no usable amount is one skipped row:
            # raised out of here it would fail the pull for every bank on
            # the bridge, every hour, for as long as the row stays in the
            # window (the same isolation the MX syncer gives its rows)
            try:
                if t.get("id") in (None, ""):
                    raise KeyError("id")
                amount = -float(t["amount"])     # bank sign → Plaid sign
                # a value too wide for a double raises OverflowError; one
                # that converts to infinity or past any real sum would fail
                # every cents conversion later in the pull
                if not math.isfinite(amount) or abs(amount) > MAX_AMOUNT:
                    raise ValueError("amount")
            except (KeyError, TypeError, ValueError, OverflowError):
                log.warning("SimpleFIN row skipped (no id or bad amount): "
                            "%s/%s", a["id"], t.get("id"))
                continue
            txns.append(Transaction(
                id=f"sfin:{a['id']}:{t['id']}",
                account_id=aid,
                date=posted_on,
                amount=amount,
                name=t.get("description") or "?",
                merchant_name=t.get("payee"),
                pending=bool(t.get("pending")),
                raw=t))
    accounts = [a for a in accounts if org_of[a.id] not in archived]
    txns = [t for t in txns if org_of.get(t.account_id) not in archived]
    for a in accounts:
        upsert_accounts(conn, org_of[a.id], [a])
        _write_bank_signed_balances(conn, a.id, *bank_signed[a.id])
    # reverse dedup: rows already covered by earlier FILE IMPORTS (window-
    # scoped, one-to-one — see base.ImportOverlapGuard) must not re-insert
    txns, skipped_imports = filter_import_duplicates(conn, txns)
    added = upsert_transactions(conn, txns)
    # SimpleFIN is a stateless full pull with no removal delta: a pending
    # hold the feed no longer carries was cancelled/expired at the bank —
    # retire it or it double-counts spend forever. The window is whatever
    # this pull actually covered; with no dated rows at all the window is
    # unknowable, so nothing is touched.
    eff_since = deep or since or (min((t.date for t in txns), default=None))
    retired = (reconcile_vanished_pendings(
        conn, [a.id for a in accounts], pulled_ids, "sfin:", eff_since)
        if eff_since else [])
    log_sync(conn, item_id, added, removed=len(retired))
    # stamp the per-institution child items too — the Accounts page
    # and the wizard show last-sync per CONNECTION (the org children), and
    # with only the bridge stamped every SimpleFIN bank read "never synced"
    per_child: dict[str, int] = {}
    for t in txns:
        cid = org_of.get(t.account_id)
        if cid:
            per_child[cid] = per_child.get(cid, 0) + 1
    removed_per_child: dict[str, int] = {}
    for r in retired:
        cid = org_of.get(r["account_id"])
        if cid:
            removed_per_child[cid] = removed_per_child.get(cid, 0) + 1
    for cid in set(org_of.values()) - archived:
        log_sync(conn, cid, per_child.get(cid, 0),
                 removed=removed_per_child.get(cid, 0))
    # the owner may have disconnected the bridge while this pull ran
    conn.execute("UPDATE items SET status='ok' WHERE id=%s "
                 "AND COALESCE(status,'') != 'archived'", (item_id,))
    if owed_items:
        # the gap is filled; later pulls go back to the usual window
        conn.execute("UPDATE items SET resync_from=NULL WHERE id = ANY(%s) "
                     "AND COALESCE(status,'') != 'archived'", (owed_items,))
    return {"accounts": len(accounts), "transactions": added,
            "removed": len(retired),
            "skipped_import_duplicates": skipped_imports,
            "errors": payload.get("errors") or []}
