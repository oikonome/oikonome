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
import logging
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from ..engine.compat import as_date
from .base import DEBT_TYPES, Account, Transaction, \
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
    with httpx.Client(transport=transport or _pinned(), timeout=30) as c:
        r = c.post(post_url, auth=auth)
        r.raise_for_status()
        # The claim RESPONSE is the access URL we will hit on every sync
        # forever — checking only the claim URL guarded the door we knock on,
        # not the address that door hands back. A public claim endpoint could
        # answer "http://169.254.169.254/…" and we would have persisted it.
        return _guard(r.text.strip(), "the SimpleFIN access URL")


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
    with httpx.Client(transport=transport or _pinned(), timeout=60) as c:
        r = c.get(get_url, params=params, auth=auth)
        r.raise_for_status()
        return r.json()


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
    try:
        payload = fetch(access_url, since=since, transport=transport)
        deep = _first_pull_since(conn, item_id, payload, since,
                                 institution_name)
        if deep is not None:
            payload = fetch(access_url, since=deep, transport=transport)
    except Exception as e:                       # noqa: BLE001
        log_sync(conn, item_id, 0, error=f"{type(e).__name__}: {e}")
        # mark the bridge AND its per-org child items (accounts hang off the
        # org items, and links health reads the account's own item) so a
        # dead token fails over immediately instead of after 48h stale
        conn.execute(
            "UPDATE items SET status=%s WHERE id=%s OR id LIKE %s",
            (f"error:{type(e).__name__}", item_id, item_id + ":%"))
        raise
    # One SimpleFIN bridge token returns EVERY institution the user added
    # there — hanging them all under one item makes the first institution
    # swallow every other one's accounts. The bridge item keeps the
    # token + health; each org gets a child item (aggregator
    # 'simplefin-org', no token — the worker's sync loop skips it) and
    # accounts hang off their org, so the UI groups by real institution.
    upsert_item(conn, item_id, "simplefin", "SimpleFIN bridge", access_url)

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
            upsert_item(conn, child, "simplefin-org", name, None)
        return child

    # user-classified accounts: persisted type wins over the name heuristic
    # (both for what upsert keeps and for the balance sign flip below)
    pinned = user_set_types(conn)
    accounts, txns = [], []
    pulled_ids: set[str] = set()
    org_of: dict[str, str] = {}
    for a in payload.get("accounts") or []:
        typ, sub = _classify(a, (account_types or {}).get(a["id"]))
        bal = float(a["balance"]) if a.get("balance") is not None else None
        avail = (float(a["available-balance"])
                 if a.get("available-balance") is not None else None)
        if pinned.get(a["id"], typ) in DEBT_TYPES:
            # SimpleFIN reports debt negative (a card's and a loan's alike);
            # the engine wants the amount owed positive
            bal = -bal if bal is not None else None
            avail = -avail if avail is not None else None
        org_of[a["id"]] = _org_item(a.get("org") or {})
        # SimpleFIN has no mask field, but banks put the last-4 in the
        # account NAME ("Spending Account (1234)") — extract it, or the
        # cross-provider auto-link suggestions (mask + type match) can
        # never see a SimpleFIN account and duplicates go unnoticed
        name = a.get("name") or a["id"]
        digits = re.findall(r"\((\d{2,})\)", name)
        accounts.append(Account(
            id=a["id"], name=name, type=typ, subtype=sub,
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
            txns.append(Transaction(
                id=f"sfin:{a['id']}:{t['id']}",
                account_id=a["id"],
                date=posted_on,
                amount=-float(t["amount"]),      # bank sign → Plaid sign
                name=t.get("description") or "?",
                merchant_name=t.get("payee"),
                pending=bool(t.get("pending")),
                raw=t))
    accounts = [a for a in accounts if org_of[a.id] not in archived]
    txns = [t for t in txns if org_of.get(t.account_id) not in archived]
    for a in accounts:
        upsert_accounts(conn, org_of[a.id], [a])
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
    conn.execute("UPDATE items SET status='ok' WHERE id=%s", (item_id,))
    return {"accounts": len(accounts), "transactions": added,
            "removed": len(retired),
            "skipped_import_duplicates": skipped_imports,
            "errors": payload.get("errors") or []}
