"""MX connector — BYO credentials, same self-host philosophy
as Plaid: the user creates an MX developer account, pastes client_id +
api_key in the GUI (encrypted at rest in tenant config), links banks
through MX's Connect widget, and the hourly worker pulls.

MX Platform API model: one MX *user* per tenant → *members* (bank
connections made in the widget) → accounts → transactions. Each member
becomes an item (id mx-<member_guid>, aggregator 'mx') so the UI groups
by institution and links rank it as the richest source tier
after Plaid.

Environments: production api.mx.com; the free developer sandbox is
int-api.mx.com (test credentials like mxuser/anyname work there).
Auth: HTTP Basic client_id:api_key, versioned Accept header.

Sign convention: MX transactions carry type DEBIT/CREDIT with positive
amounts — engine wants positive = money out, so DEBIT stays positive
and CREDIT flips negative."""

from __future__ import annotations

import datetime as dt
import logging

import httpx

from ..db import crypto
from ..engine.compat import as_date
from .base import Account, Transaction, filter_import_duplicates, log_sync, \
    reconcile_vanished_pendings, upsert_item, upsert_transactions, \
    upsert_accounts

BASES = {"production": "https://api.mx.com",
         "sandbox": "https://int-api.mx.com"}
ACCEPT = "application/vnd.mx.api.v1+json"
PAGE = 250

log = logging.getLogger(__name__)

TYPE_MAP = {  # MX account types → engine (type, subtype)
    "CHECKING": ("depository", "checking"),
    "SAVINGS": ("depository", "savings"),
    "CREDIT_CARD": ("credit", "credit card"),
    "LOAN": ("loan", None), "MORTGAGE": ("loan", "mortgage"),
    "INVESTMENT": ("investment", "brokerage"),
    "RETIREMENT": ("investment", "retirement"),
}


def creds(conn) -> dict | None:
    """BYO MX credentials from tenant config (Settings/wizard writes
    them; api_key encrypted at rest)."""
    from ..engine import budget
    cfg = budget.load_config(conn)
    if not cfg.get("mx_client_id") or not cfg.get("mx_api_key"):
        return None
    return {"client_id": cfg["mx_client_id"],
            "api_key": crypto.decrypt(conn, cfg["mx_api_key"]),
            "env": cfg.get("mx_env") or "production",
            "user_guid": cfg.get("mx_user_guid")}


def _client(c: dict, transport=None) -> httpx.Client:
    return httpx.Client(
        base_url=BASES.get(c["env"], BASES["production"]),
        auth=(c["client_id"], c["api_key"]),
        headers={"Accept": ACCEPT}, timeout=60, transport=transport)


def validate(client_id: str, api_key: str, env: str,
             transport=None) -> bool:
    """Live key check (wizard gate): any authenticated 2xx will do —
    /users is the cheapest."""
    c = {"client_id": client_id, "api_key": api_key, "env": env}
    with _client(c, transport) as h:
        r = h.get("/users", params={"page": 1, "records_per_page": 1})
        if r.status_code == 401:
            return False
        r.raise_for_status()
        return True


def ensure_user(conn, transport=None) -> str:
    """The tenant's MX user (created on first use, guid kept in config)."""
    from ..engine import budget
    c = creds(conn)
    if c is None:
        raise ValueError("MX credentials are not configured")
    if c["user_guid"]:
        return c["user_guid"]
    with _client(c, transport) as h:
        r = h.post("/users", json={"user": {"metadata": "oikonome"}})
        r.raise_for_status()
        guid = r.json()["user"]["guid"]
    # The MX call stays OUTSIDE the lock — holding the settings row for the
    # length of an HTTP round-trip would be the trade config_txn's docstring
    # warns against. Only the write-back is locked: this runs on the first
    # sync after a tenant connects MX, concurrently with that same tenant
    # still clicking through the wizard's later steps.
    #
    # Which is exactly why the guid is re-read INSIDE the lock. Three doors
    # reach here unserialised, and the read at the top of this function is a
    # check-then-act across an HTTP round trip: two of them can both see no
    # guid, both create an MX user, and the second write overwrite the
    # first. The loser is not a wasted API call — it is an ORPHANED MX user
    # holding whatever the widget linked to it, invisible to us afterwards.
    with budget.config_txn(conn) as cfg:
        winner = cfg.get("mx_user_guid")
        if not winner:
            cfg["mx_user_guid"] = guid
            winner = guid
    if winner != guid:
        # somebody else got there first: release the one we just made rather
        # than leaving it behind at MX
        log.warning("mx: concurrent ensure_user — releasing the duplicate "
                    "user %s and keeping %s", guid, winner)
        try:
            with _client(c, transport) as h:
                h.delete(f"/users/{guid}")
        except Exception:                            # noqa: BLE001
            log.exception("mx: could not release duplicate user %s", guid)
    return winner


def release_tenant_user(tenant_id, transport=None) -> str:
    """Tenant erasure: DELETE the platform-side MX user — MX cascades
    the members/accounts/transactions it holds for us, which is real
    financial data living at MX, not just a credential. Called by
    erasure.release_external BEFORE the rows go (the config carrying
    creds + user_guid is only readable while the tenant exists).
    Best-effort, never raises. Returns 'none' / 'deleted' / 'error'."""
    from ..db import tenancy
    try:
        conn = tenancy.tenant_connect(tenant_id)
    except Exception as e:                                 # noqa: BLE001
        log.warning("mx release: no tenant connection for %s: %s",
                    tenant_id, e)
        return "error"
    try:
        try:
            c = creds(conn)
        except Exception as e:                             # noqa: BLE001
            log.warning("mx release: creds unreadable for %s: %s",
                        tenant_id, e)
            return "error"
    finally:
        conn.close()
    if c is None or not c.get("user_guid"):
        return "none"
    try:
        with _client(c, transport) as h:
            r = h.delete(f"/users/{c['user_guid']}")
            if r.status_code == 404:       # already gone — the goal state
                return "deleted"
            r.raise_for_status()
        return "deleted"
    except Exception as e:                                 # noqa: BLE001
        log.warning("mx release: delete user %s failed for %s: %s",
                    c["user_guid"], tenant_id, e)
        return "error"


def connect_widget_url(conn, transport=None) -> str:
    """A fresh Connect-widget URL — the user links (or fixes) banks
    there; the next sync picks the members up."""
    c = creds(conn)
    if c is None:
        raise ValueError("MX credentials are not configured")
    guid = ensure_user(conn, transport)
    with _client(c, transport) as h:
        r = h.post(f"/users/{guid}/widget_urls",
                   json={"widget_url": {"widget_type": "connect_widget"}})
        r.raise_for_status()
        return r.json()["widget_url"]["url"]


def _accounts(h: httpx.Client, guid: str) -> list[dict]:
    out, page = [], 1
    while True:
        r = h.get(f"/users/{guid}/accounts",
                  params={"page": page, "records_per_page": PAGE})
        r.raise_for_status()
        j = r.json()
        out += j.get("accounts") or []
        if page >= (j.get("pagination") or {}).get("total_pages", 1):
            return out
        page += 1


def _transactions(h: httpx.Client, guid: str, since: dt.date) -> list[dict]:
    out, page = [], 1
    while True:
        r = h.get(f"/users/{guid}/transactions",
                  params={"page": page, "records_per_page": PAGE,
                          "from_date": since.isoformat()})
        r.raise_for_status()
        j = r.json()
        out += j.get("transactions") or []
        if page >= (j.get("pagination") or {}).get("total_pages", 1):
            return out
        page += 1


def _members(h: httpx.Client, guid: str) -> dict[str, str]:
    # paginate like _accounts/_transactions — page-1-only would drop the
    # names of every member past the first page.
    out: dict[str, str] = {}
    page = 1
    while True:
        r = h.get(f"/users/{guid}/members",
                  params={"page": page, "records_per_page": PAGE})
        r.raise_for_status()
        j = r.json()
        for m in j.get("members") or []:
            out[m["guid"]] = (m.get("name") or m.get("institution_code")
                              or "MX")
        if page >= (j.get("pagination") or {}).get("total_pages", 1):
            return out
        page += 1


def _default_since(conn, raw_accounts: list[dict]) -> dt.date:
    """The 30-day window is a REFRESH heuristic, not a history policy — on
    a first pull it would silently throw away everything older than a month
    that MX would serve. Deep first sync (2 years — MX serves what it has),
    then 30-day refreshes.

    Scoped per MEMBER, not to the tenant: /users/{guid}/transactions takes
    ONE from_date shared by every member, so keying the check on any mx:
    row existing would leave a SECOND institution connected later without
    its backfill — the first institution's rows would pin the window to 30
    days forever. Any member whose accounts hold no mx: rows yet deepens the
    shared window (a member with a genuinely empty history keeps asking
    deep, which is harmless — MX just returns what it has)."""
    by_member: dict[str, list[str]] = {}
    for a in raw_accounts:
        by_member.setdefault(a.get("member_guid") or "?", []).append(
            f"mx-{a['guid']}")
    deep = not by_member
    for account_ids in by_member.values():
        row = conn.execute(
            "SELECT 1 FROM transactions WHERE id LIKE %s "
            "AND account_id = ANY(%s) LIMIT 1",
            ("mx:%", account_ids)).fetchone()
        if row is None:
            deep = True
            break
    return dt.date.today() - dt.timedelta(days=730 if deep else 30)


def sync(conn, since: dt.date | None = None, transport=None) -> dict:
    """Full pull for the tenant's MX user → normalized upserts. One item
    per member (institution)."""
    c = creds(conn)
    if c is None:
        raise ValueError("MX credentials are not configured")
    guid = ensure_user(conn, transport)
    try:
        with _client(c, transport) as h:
            raw_accounts = _accounts(h, guid)
            if since is None:
                since = _default_since(conn, raw_accounts)
            raw_txns = _transactions(h, guid, since)
            # member names are cosmetic — a failed _members call
            # must NOT error the whole MX sync (and flip healthy members to
            # error). Best-effort; fall back to "MX" labels.
            try:
                members = _members(h, guid)
            except Exception:                     # noqa: BLE001
                members = {}
    except Exception as e:                        # noqa: BLE001
        # a real auth failure (401/403) → 'login_required' so the
        # link_alert email fires (like Plaid ITEM_LOGIN_REQUIRED); transient
        # network/HTTP blips stay 'error:<type>' which link_alert treats as
        # self-healing (no break/recover spam per blip).
        status = ("login_required"
                  if (isinstance(e, httpx.HTTPStatusError)
                      and e.response.status_code in (401, 403))
                  else f"error:{type(e).__name__}")
        conn.execute(
            "UPDATE items SET status=%s WHERE aggregator='mx'", (status,))
        raise

    accounts_by_member: dict[str, list[Account]] = {}
    for a in raw_accounts:
        typ, sub = TYPE_MAP.get(str(a.get("type") or "").upper(),
                                ("depository", "checking"))
        accounts_by_member.setdefault(a.get("member_guid") or "?", []).append(
            Account(
                id=f"mx-{a['guid']}", name=a.get("name") or a["guid"],
                type=typ, subtype=sub,
                mask=(a.get("account_number") or "")[-4:] or None,
                balance_current=a.get("balance"),
                balance_available=a.get("available_balance"),
                currency=a.get("currency_code"),
                # data minimization: the last-4 mask is all we surface, so the
                # full account/routing number must never land in accounts.raw
                # (plaintext JSONB that rides /export + the portable archive).
                raw={k: v for k, v in a.items()
                     if k not in ("account_number", "routing_number")}))
    for member_guid, accts in accounts_by_member.items():
        item_id = f"mx-{member_guid}"
        upsert_item(conn, item_id, "mx",
                    members.get(member_guid, "MX"), None)
        upsert_accounts(conn, item_id, accts)
        conn.execute("UPDATE items SET status='ok' WHERE id=%s", (item_id,))

    # what the FEED carried, keyed BEFORE any local parsing or filtering:
    # a row skipped below for a transient parse glitch, or dropped by the
    # import guard, was still present at the source — treating it as
    # vanished would retire a real pending hold
    pulled_ids = {f"mx:{t['guid']}" for t in raw_txns if t.get("guid")}
    txns = []
    for t in raw_txns:
        if not t.get("account_guid"):
            continue
        # one malformed row (a missing/garbage date, a non-numeric amount)
        # must not abort the whole member's sync and flip healthy items to
        # error — skip it, keep the rest, and say so
        try:
            txns.append(Transaction(
                id=f"mx:{t['guid']}", account_id=f"mx-{t['account_guid']}",
                # as_date also refuses a year the app cannot read (0001,
                # 9999) as a ValueError, so such a row is skipped below
                date=as_date(str(t.get("transacted_at")
                                 or t.get("date"))[:10]),
                # MX: positive amounts + DEBIT/CREDIT type →
                # engine positive = out
                amount=(float(t.get("amount") or 0)
                        * (-1 if str(t.get("type")).upper() == "CREDIT"
                           else 1)),
                name=t.get("description") or "?",
                # `guid and description` yields False (a bool) when guid
                # is falsy — downstream expects str | None.
                merchant_name=(t.get("description") if t.get("merchant_guid")
                               else None),
                pending=str(t.get("status") or "").upper() == "PENDING",
                category_primary=(t.get("top_level_category") or "").upper()
                                 .replace(" ", "_") or None,
                raw=t))
        except (ValueError, TypeError, KeyError):
            log.warning("MX row skipped (unparseable): guid=%s",
                        t.get("guid"))
    txns, skipped = filter_import_duplicates(conn, txns)
    added = upsert_transactions(conn, txns)
    # MX is a stateless full pull with no removal delta: a pending hold
    # the feed no longer carries was cancelled/expired at the bank —
    # retire it or it double-counts spend forever.
    retired = reconcile_vanished_pendings(
        conn, [f"mx-{a['guid']}" for a in raw_accounts],
        pulled_ids, "mx:", since)
    # log the count PER member, not the batch total for everyone — a
    # shared `added` makes Doctor/history overstate each institution's pull.
    member_of = {a["guid"]: (a.get("member_guid") or "?") for a in raw_accounts}
    per_member: dict[str, int] = {}
    for t in txns:
        mg = member_of.get(t.account_id[len("mx-"):], "?")
        per_member[mg] = per_member.get(mg, 0) + 1
    removed_per_member: dict[str, int] = {}
    for r in retired:
        mg = member_of.get(r["account_id"][len("mx-"):], "?")
        removed_per_member[mg] = removed_per_member.get(mg, 0) + 1
    for member_guid in accounts_by_member:
        log_sync(conn, f"mx-{member_guid}", per_member.get(member_guid, 0),
                 removed=removed_per_member.get(member_guid, 0))
    return {"members": len(accounts_by_member),
            "accounts": len(raw_accounts), "transactions": added,
            "removed": len(retired),
            "skipped_import_duplicates": skipped}
