"""Plaid connector — an instance's own Plaid app, or a tenant's own
client_id/secret held in its config. Rules:
  * Plaid amounts are ALREADY the engine convention (positive = money out)
    — no flip, unlike SimpleFIN/CSV/OFX.
  * /transactions/sync cursor is persisted ONLY after full pagination —
    a mid-pagination failure must re-run the whole page set (Plaid's
    documented mutation-recovery rule).
  * removed transactions are flagged, never deleted.
  * category_override untouched; personal_finance_category → primary/
    detailed columns; full object in raw.

Credentials resolve per tenant: config plaid_client_id/plaid_secret (the
tenant's own keys) → env OIKONOME_PLAID_CLIENT_ID/SECRET (the instance's
keys). Environment via config/env plaid_env: production | sandbox.
"""

from __future__ import annotations

import logging
import datetime as dt
import os
from ..envnum import env_flag
import time

import httpx

from ..engine import budget
from ..engine.compat import as_date, jsonb
from .base import Account, ImportOverlapGuard, Transaction, \
    check_institution_cap, filter_import_duplicates, get_access_token, \
    log_sync, mark_removed, upsert_accounts, upsert_item, upsert_transactions

log = logging.getLogger(__name__)

DAYS_REQUESTED = 730
ENV_URLS = {"production": "https://production.plaid.com",
            "sandbox": "https://sandbox.plaid.com"}
# Separately-billed per-Item subscriptions: requesting one attaches it.
BILLED_PRODUCTS = ("liabilities", "investments")
# The spelling that turns the extra products OFF. It is a WORD and not the
# empty string because compose declares every key it forwards, so a variable
# the operator left commented out reaches the container as "" — empty and
# absent are one state there, and only one of them can mean "off".
PRODUCTS_OPT_OUT = "none"


def extra_products(conn=None) -> list[str]:
    """The non-transactions Plaid products an Item is linked with.

    Liabilities and Investments are separately-billed per-Item
    subscriptions, so attaching them is a policy decision an installed
    add-on may want to answer per household (``ext.gate.plaid_products``)
    — asked through that hook rather than against any table of its own, so
    core has nothing to keep in step with it. The failure mode when it goes
    wrong is silent: no holdings and no liability detail.

    Falls back to the OIKONOME_PLAID_PRODUCTS_EXTRA env (the
    per-deployment answer) when there's no tenant conn or the add-on
    defers. With OIKONOME_HOSTED unset, a variable nobody set means BOTH:
    this is the one answer the link doors and ``sync_products`` share, and
    that sync pulls liabilities and holdings on the instance's own Plaid
    account. A
    link that did not ask for consent while the sync asks for the product
    every day earns ADDITIONAL_CONSENT_REQUIRED on every Item, which the
    Accounts page then reports as "card due dates not shared" beside a grant
    button whose update session carried the same empty list — a prompt that
    could not be satisfied. ``OIKONOME_PLAID_PRODUCTS_EXTRA=none`` is the
    opt-out, and it silences the sync too, so the two halves cannot
    disagree."""
    if conn is not None:
        try:
            from .. import ext
            plan_products = ext.gate.plaid_products(conn)
            if plan_products is not None:
                return plan_products
        except Exception:                   # noqa: BLE001 — fall back to env
            pass
    # Empty and unset are ONE state here: compose materialises every key it
    # declares, so the documented self-host default — the variable left
    # commented out in the operator's .env — arrives as "". Same rule as
    # `envnum`: empty means "the code's default", and the opt-out is a word.
    raw = (os.environ.get("OIKONOME_PLAID_PRODUCTS_EXTRA") or "").strip()
    if not raw:
        return [] if env_flag("OIKONOME_HOSTED") else list(BILLED_PRODUCTS)
    if raw.lower() == PRODUCTS_OPT_OUT:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


class PlaidError(Exception):
    def __init__(self, payload: dict, status: int):
        self.code = payload.get("error_code", "UNKNOWN")
        self.type = payload.get("error_type", "UNKNOWN")
        self.message = payload.get("error_message", str(payload))
        # request_id is what Plaid support asks for — carry it on every
        # error so it lands in sync_log / logs alongside the failure
        self.request_id = payload.get("request_id")
        self.status = status
        rid = f" [request_id {self.request_id}]" if self.request_id else ""
        super().__init__(f"{self.code}: {self.message}{rid}")


def credentials(conn) -> tuple[str, str, str]:
    """(client_id, secret, base_url) for the current tenant: BYO config
    first, product env second."""
    from ..db import crypto
    cfg = budget.load_config(conn)
    client_id = cfg.get("plaid_client_id") or os.environ.get("OIKONOME_PLAID_CLIENT_ID")
    # secret is encrypted at rest (Settings + connections-bundle import store
    # enc:v1:… ); decrypt on read. crypto.decrypt passes untagged values
    # through untouched, so env creds and legacy/dev-mode plaintext still work.
    secret = crypto.decrypt(conn, cfg.get("plaid_secret")) \
        or os.environ.get("OIKONOME_PLAID_SECRET")
    # The tenant's own environment choice applies only to the tenant's own
    # keys. With the instance's credentials the instance's environment
    # rules: a restored export can carry plaid_env=sandbox (secrets are
    # scrubbed on restore, this key is not), and production keys must never
    # be sent to sandbox.plaid.com.
    byo = bool(cfg.get("plaid_client_id"))
    env = ((cfg.get("plaid_env") if byo else None)
           or os.environ.get("OIKONOME_PLAID_ENV", "production"))
    if not client_id or not secret:
        raise PlaidError({"error_code": "NO_CREDENTIALS", "error_type": "CONFIG",
                          "error_message": "no Plaid credentials configured"}, 0)
    return client_id, secret, ENV_URLS.get(env, ENV_URLS["production"])


class Client:
    """Thin Plaid HTTP client.

    COST RULE: NEVER call /transactions/refresh — Plaid bills each call
    PER REQUEST, on top of the per-Item subscription. Every sync,
    including any future "sync now" button, uses /transactions/sync only
    (included in the Transactions subscription). test_plaid_product_gating
    asserts this module has no code path touching the endpoint — keep it
    that way."""

    def __init__(self, client_id: str, secret: str,
                 base_url: str = ENV_URLS["production"], transport=None):
        self._auth = {"client_id": client_id, "secret": secret}
        self._http = httpx.Client(base_url=base_url, transport=transport,
                                  timeout=httpx.Timeout(120, connect=10))

    @classmethod
    def for_tenant(cls, conn, transport=None) -> "Client":
        cid, secret, url = credentials(conn)
        return cls(cid, secret, url, transport=transport)

    # RATE_LIMIT_EXCEEDED is the one error class Plaid documents as purely
    # transient on OUR side — brief in-run backoff beats failing the item
    # and waiting a whole poll cycle. Bounded (2 retries); everything else
    # (institution outages, re-auth states) is the hourly sweep's job.
    RETRY_SLEEPS = (1.0, 2.0)
    _sleep = staticmethod(time.sleep)                   # test-injectable

    def post(self, path: str, body: dict | None = None) -> dict:
        for attempt in range(len(self.RETRY_SLEEPS) + 1):
            r = self._http.post(path, json={**self._auth, **(body or {})})
            try:
                payload = r.json()
            except ValueError:         # non-JSON body from a 502/504 gateway
                raise PlaidError({"error_code": f"HTTP_{r.status_code}",
                                  "error_type": "API_ERROR",
                                  "error_message": r.text[:200]},
                                 r.status_code)
            if r.status_code == 200:
                return payload
            err = PlaidError(payload, r.status_code)
            if err.type == "RATE_LIMIT_EXCEEDED" \
                    and attempt < len(self.RETRY_SLEEPS):
                self._sleep(self.RETRY_SLEEPS[attempt])
                continue
            raise err
        raise err                      # unreachable; keeps linters honest

    # -- linking ----------------------------------------------------------

    def create_hosted_link(self, tenant_id: str, *, client_name: str,
                           access_token: str | None = None,
                           account_selection: bool = False,
                           completion_redirect: str | None = None,
                           conn=None) -> dict:
        """Hosted Link session (no local web server needed). With an
        access_token, runs in update/re-auth mode;
        account_selection additionally shows the shared-accounts checklist,
        which is the ONLY lever that stops Plaid billing a per-account
        subscription product for an unwanted account — a local hide cannot
        change what the Item shares."""
        body = {
            "client_name": client_name,
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": tenant_id},
            "hosted_link": {"url_lifetime_seconds": 1800},
        }
        if completion_redirect:
            body["hosted_link"]["completion_redirect_uri"] = completion_redirect
        # webhook-driven sync: when the deployment exposes the receiver
        # (web/plaid_webhook.py), every new/re-linked Item registers it —
        # SYNC_UPDATES_AVAILABLE then triggers a scoped sync instead of
        # waiting for the hourly poll. Unset = polling only (unchanged).
        webhook_url = os.environ.get("OIKONOME_PLAID_WEBHOOK_URL")
        if webhook_url:
            body["webhook"] = webhook_url
        if access_token:
            body["access_token"] = access_token
            if account_selection:
                body["update"] = {"account_selection_enabled": True}
            # an Item linked before a billed product was asked for has
            # no consent for it, and every later /liabilities/get answers
            # ADDITIONAL_CONSENT_REQUIRED forever — update mode is the only
            # door that can add the consent, so ask for it here. Only for
            # products the institution supports: unlike link creation's
            # required_if_supported_products, update mode hard-rejects an
            # unsupported one (INVALID_FIELD, e.g. investments at a
            # card-only issuer)
            extra = extra_products(conn)
            if extra:
                try:
                    item = self.get_item(access_token)
                    inst = self.get_institution(
                        item["item"]["institution_id"])
                    supported = set(
                        inst["institution"].get("products") or [])
                    extra = [p for p in extra if p in supported]
                except Exception:
                    # lookup failure keeps the full request — the Link
                    # error names the product, which beats silently never
                    # repairing a missing consent
                    pass
            if extra:
                body["additional_consented_products"] = extra
        else:
            body["products"] = ["transactions"]
            body["transactions"] = {"days_requested": DAYS_REQUESTED}
            # extra billed products attach only when extra_products says
            # so (an add-on's answer, or OIKONOME_PLAID_PRODUCTS_EXTRA).
            extra = extra_products(conn)
            if extra:
                body["required_if_supported_products"] = extra
        return self.post("/link/token/create", body)

    def get_link_session(self, link_token: str) -> dict:
        return self.post("/link/token/get", {"link_token": link_token})

    def exchange_public_token(self, public_token: str) -> dict:
        return self.post("/item/public_token/exchange", {"public_token": public_token})

    # -- data (beyond balances/transactions) --------------------------------

    def get_item(self, access_token: str) -> dict:
        return self.post("/item/get", {"access_token": access_token})

    def remove_item(self, access_token: str) -> dict:
        """Releases the Item on Plaid's side — this is what frees a slot
        on limited plans; deleting locally alone never does."""
        return self.post("/item/remove", {"access_token": access_token})

    def get_institution(self, institution_id: str, *, branding: bool = False) -> dict:
        """`branding=True` asks for the optional metadata — logo (base64
        PNG), primary_color, url — for the Accounts page's institution
        chip; off by default because it makes the payload much larger."""
        body = {"institution_id": institution_id, "country_codes": ["US"]}
        if branding:
            body["options"] = {"include_optional_metadata": True}
        return self.post("/institutions/get_by_id", body)

    def get_liabilities(self, access_token: str) -> dict:
        return self.post("/liabilities/get", {"access_token": access_token})

    def get_investment_holdings(self, access_token: str) -> dict:
        return self.post("/investments/holdings/get",
                         {"access_token": access_token})


def stamp_ledger_removed(item_id: str, reason: str) -> None:
    """Stamp plaid_item_ledger.removed_at/removed_reason for an Item just
    released at Plaid. Written via the ADMIN role — the ledger is append-only
    for the app role by design (migrate.py revokes app UPDATE). Best-effort:
    a ledger write must never fail the release it records. Idempotent via
    'removed_at IS NULL'. The documented audit invariant (rows without
    removed_at should match the live Plaid dashboard count) then holds on
    EVERY release path — reaper, user disconnect, and tenant erasure."""
    from ..db import tenancy
    try:
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE plaid_item_ledger SET removed_at=now(), "
                "removed_reason=%s WHERE item_id=%s AND removed_at IS NULL",
                (reason, item_id))
        finally:
            admin.close()
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid ledger removal stamp failed item=%s: %s",
                    item_id, e)


class ReleaseCount(int):
    """How many Items /item/remove actually released — an int, because the
    audit lines and every caller count it as one — that also carries what
    the count alone hid: how many Items there WERE (`expected`) and the ids
    of the ones that did not release (`failed_ids`). With only the number,
    a Plaid outage at delete-time would read as "released 0", the rows (and
    the only access tokens) would be wiped anyway, and the Items would keep
    billing with no in-DB handle. The failed ids ride into the
    pending-erasure marker."""
    expected: int = 0
    failed_ids: tuple = ()

    def __new__(cls, released: int, expected: int = 0,
                failed_ids: tuple | list = ()):
        self = super().__new__(cls, released)
        self.expected = int(expected)
        self.failed_ids = tuple(failed_ids)
        return self


def release_tenant_items(tenant_id) -> ReleaseCount:
    """Best-effort /item/remove for every Plaid Item of a tenant — the
    CCPA delete-account door and the admin console's tenant-delete call
    this BEFORE wiping rows, so no live access_token (and no Plaid
    billing) outlives the account. Includes archived items: a disconnect
    whose remove call failed still holds a token server-side, and
    /item/remove on an already-removed Item is a harmless error. Per-Item
    failures never raise — releasing must not block an erasure the user is
    owed — but a failure to even open the tenant connection DOES propagate:
    swallowed, it would read as ReleaseCount(0) with expected=0, which the
    caller cannot tell apart from "the tenant held no Items", and the wipe
    would go ahead with no failed-service marker. The caller's outer except turns the
    raise into plaid_failed='all'. The return is an int (released count)
    that also names the Items that did NOT release, so the caller can
    leave a retry handle before the wipe."""
    from ..db import tenancy
    released = 0
    failed: list = []
    conn = tenancy.tenant_connect(tenant_id)
    try:
        rows = conn.execute(
            "SELECT id FROM items WHERE aggregator='plaid' "
            "AND access_token IS NOT NULL").fetchall()
        if not rows:
            return ReleaseCount(0)
        try:
            client = Client.for_tenant(conn)
        except PlaidError:
            # no creds configured — nothing can be released, and every
            # Item that still holds a token is a straggler to report
            return ReleaseCount(0, len(rows), [r["id"] for r in rows])
        for r in rows:
            try:
                token = get_access_token(conn, r["id"])
                if not token:
                    continue
                client.remove_item(token)
                released += 1
                # stamp the audit ledger on the erasure release path too
                stamp_ledger_removed(r["id"], "erasure")
            except Exception as e:                         # noqa: BLE001
                failed.append(r["id"])
                log.warning("plaid release failed tenant=%s item=%s: %s",
                            tenant_id, r["id"], e)
    finally:
        conn.close()
    return ReleaseCount(released, len(rows), failed)


def release_live_items(tenant_id, reason: str) -> int:
    """/item/remove every LIVE Plaid Item of a tenant and archive the rows
    (status archived, archived_reason=reason, access_token cleared, slot
    ledger stamped) — the shape an instance needs when it must stop
    syncing a household but keep its books: the accounts and history
    stay, the connections are gone and stop billing,
    the Accounts page shows them as removed-pending-reconnect. Returns the
    number released; an Item that fails to release keeps its token so the
    reaper's straggler pass finds it. Never raises."""
    from ..db import tenancy
    released = 0
    try:
        conn = tenancy.tenant_connect(tenant_id)
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid release: no tenant connection for %s: %s",
                    tenant_id, e)
        return 0
    try:
        rows = conn.execute(
            "SELECT id FROM items WHERE aggregator='plaid' "
            "AND COALESCE(status,'') != 'archived' "
            "AND access_token IS NOT NULL").fetchall()
        if not rows:
            return 0
        try:
            client = Client.for_tenant(conn)
        except PlaidError:
            return 0
        for r in rows:
            try:
                token = get_access_token(conn, r["id"])
                if token:
                    try:
                        client.remove_item(token)
                    except PlaidError as e:
                        # Already gone at Plaid = released. A prior partial
                        # run (removed at Plaid, then the local UPDATE
                        # failed), a user's own disconnect, or a duplicate
                        # cancellation can all leave a token whose Item no
                        # longer exists; the sweep runs nightly, so treating
                        # that as a failure repeats forever and the row
                        # never converges to archived. Same tolerance as
                        # the reaper's release paths.
                        if e.code != "ITEM_NOT_FOUND":
                            raise
                conn.execute(
                    "UPDATE items SET status='archived', archived_reason=%s, "
                    "archived_at=now(), access_token=NULL WHERE id=%s",
                    (reason, r["id"]))
                stamp_ledger_removed(r["id"], reason)
                released += 1
            except Exception as e:                         # noqa: BLE001
                log.warning("plaid release (%s) failed tenant=%s item=%s: %s",
                            reason, tenant_id, r["id"], e)
    finally:
        conn.close()
    return released


# ---- normalization ---------------------------------------------------------


def _norm_accounts(payload: dict) -> list[Account]:
    out = []
    for a in payload.get("accounts") or []:
        b = a.get("balances") or {}
        out.append(Account(
            id=a["account_id"], name=a.get("name") or a["account_id"],
            type=a.get("type") or "depository", subtype=a.get("subtype"),
            mask=a.get("mask"), balance_current=b.get("current"),
            balance_available=b.get("available"),
            currency=b.get("iso_currency_code"), raw=a))
    return out


def _norm_txn(t: dict) -> Transaction:
    pfc = t.get("personal_finance_category") or {}
    primary = pfc.get("primary")
    detailed = pfc.get("detailed")
    conf = pfc.get("confidence_level")
    return Transaction(
        id=t["transaction_id"], account_id=t.get("account_id"),
        date=as_date(t.get("date")),
        amount=t.get("amount"),               # Plaid sign IS the engine sign
        name=t.get("name") or "?",
        merchant_name=t.get("merchant_name"),
        pending=bool(t.get("pending")),
        category_primary=primary,
        category_detailed=detailed,
        # Immutable-by-us mirror of PFC (sync refreshes; seed/LLM do not)
        category_plaid=primary,
        category_plaid_detailed=detailed,
        category_plaid_confidence=conf,
        raw=t)


# ---- item lifecycle --------------------------------------------------------


def link_item(conn, public_token: str, institution_name: str = "",
              link_session_id: str | None = None, transport=None) -> str:
    """Exchange a public token (from a completed Link session) → item row
    (access token encrypted at rest) → first sync. With no caller-supplied
    institution_name, resolves the real name via /item/get +
    /institutions/get_by_id — best-effort, falls back to 'Plaid'.

    Plaid launch-checklist identifier retention: institution_id and the
    Hosted Link session's link_session_id persist on the item row — with
    item_id and the request_ids in sync_log these are what Plaid support
    and the dashboard Activity Log key on."""
    client = Client.for_tenant(conn, transport=transport)
    ex = client.exchange_public_token(public_token)
    item_id = ex["item_id"]
    inst_id = ""
    if not institution_name:
        inst_id, institution_name = _institution_ids(client,
                                                     ex["access_token"])
    # The hosted institution cap is enforced HERE, where
    # the item row is actually created. Checking it only when the Link session
    # OPENS is advisory — a tenant sitting at the cap can open several
    # sessions before completing any, then complete them all and land well
    # over. An item_id we already hold is a re-auth/update, exempt exactly as
    # it is at the start door.
    #
    # Count and insert together under a per-tenant advisory lock, or two
    # exchanges finishing at once both read the pre-insert count and both pass.
    #
    # Refusing means handing the Item BACK: the token exchange has already
    # happened, so the Item is live and billing at Plaid, and abandoning it
    # would burn a plan slot (plaid_item_ledger never refunds) for a
    # connection the tenant never received.
    if conn.execute("SELECT 1 FROM items WHERE id=%s", (item_id,)).fetchone():
        upsert_item(conn, item_id, "plaid", institution_name or "Plaid",
                    ex["access_token"])
    else:
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id', true) AS t"
        ).fetchone()["t"]
        try:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                             (f"oikonome:instcap:{tid}",))
                check_institution_cap(conn)
                upsert_item(conn, item_id, "plaid",
                            institution_name or "Plaid", ex["access_token"])
        except ValueError:
            try:
                client.remove_item(ex["access_token"])
            except Exception as e:            # noqa: BLE001 - best effort
                log.warning("item/remove after cap refusal failed item=%s: %s",
                            item_id, e)
            raise
    conn.execute(
        "UPDATE items SET institution_id=COALESCE(NULLIF(%s,''), "
        "institution_id), link_session_id=COALESCE(%s, link_session_id) "
        "WHERE id=%s", (inst_id, link_session_id, item_id))
    refresh_branding(conn, item_id, client=client)
    _record_slot(conn, item_id)
    # First sync is best-effort: the Item is already live (and billing). A
    # product/API miss must not strand the waiting page in a 500 poll loop —
    # hourly sync / "sync now" will retry. link_item still returns the id.
    try:
        sync(conn, item_id, transport=transport)
    except PlaidError as e:
        log.warning("plaid first sync failed item=%s: %s", item_id, e)
    # Holdings/liabilities are a separate daily product pull, but a brand-
    # new brokerage Item must not wait up to 20h for the first positions.
    # Best-effort — never fail the link on product miss.
    try:
        sync_products(conn, item_id, transport=transport)
    except PlaidError as e:
        log.warning("plaid first products failed item=%s: %s", item_id, e)
    return item_id


def _record_slot(conn, item_id: str) -> None:
    """Append one plaid_item_ledger row (slot accounting): Plaid's
    limited plans consume a slot per Item CREATED and deletion never
    refunds it, so the burn count must survive the item row vanishing
    (tenant deletion cascades). The ledger is append-only (app role has no
    UPDATE/DELETE); the admin console counts it against
    OIKONOME_PLAID_PLAN_ITEMS. Best-effort — ledger trouble must never
    fail a link."""
    cfg = budget.load_config(conn)
    env = cfg.get("plaid_env") or os.environ.get("OIKONOME_PLAID_ENV",
                                                 "production")
    source = "byo" if cfg.get("plaid_client_id") else "platform"
    try:
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id', true) AS t"
        ).fetchone()["t"]
        conn.execute(
            "INSERT INTO plaid_item_ledger (item_id, environment, "
            "credential_source, tenant) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (item_id) DO NOTHING",
            (item_id, env, source, tid))
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid slot ledger append failed item=%s: %s",
                    item_id, e)


def recurring_streams(conn, item_id: str, transport=None) -> list[dict]:
    """Plaid's own recurring-transaction detection for one item —
    /transactions/recurring/get — as a flat list of streams, both directions.
    A cross-check for our detection: Plaid sees the merchant entity across
    spellings and its cadence model has more history than one instance's
    lookback. Best-effort: an item whose plan/products cannot serve it (or
    any Plaid error) yields [] and a log line, never a failed nightly."""
    token = get_access_token(conn, item_id)
    if not token:
        return []
    client = Client.for_tenant(conn, transport=transport)
    try:
        r = client.post("/transactions/recurring/get", {"access_token": token})
    except Exception as e:                                   # noqa: BLE001
        # "never a failed nightly" has to mean the network too: a reset
        # connection or a read timeout is exactly as survivable as a Plaid
        # error.
        log.info("recurring streams unavailable item=%s: %s", item_id, e)
        return []
    out = []
    for direction, key in (("outflow", "outflow_streams"), ("inflow", "inflow_streams")):
        for st in r.get(key) or []:
            avg = (st.get("average_amount") or {}).get("amount")
            last = (st.get("last_amount") or {}).get("amount")
            amt = abs(float(avg if avg is not None else (last or 0)))
            pfc = st.get("personal_finance_category") or {}
            out.append({
                "stream_id": st.get("stream_id"), "direction": direction,
                "merchant": st.get("merchant_name") or st.get("description") or "",
                "description": st.get("description") or "",
                "amount": round(amt, 2),
                "frequency": st.get("frequency"),
                "first_date": st.get("first_date"), "last_date": st.get("last_date"),
                "predicted_next_date": st.get("predicted_next_date"),
                "is_active": bool(st.get("is_active", True)),
                "status": st.get("status") or "",
                "category": pfc.get("primary"),
                "transaction_ids": list(st.get("transaction_ids") or []),
                "item_id": item_id,
            })
    return out


# ---- Plaid Enrich for imported history ------------------------------------
# History imported from files and SimpleFIN has none of Plaid's merchant
# resolution. /transactions/enrich returns the same
# entity id, logo, website, counterparties, location, channel, check
# number and PFC for an arbitrary description — billed per transaction, so
# it runs only when a person asks (Settings → Connections), inside a
# monthly cap, newest rows first.

ENRICH_BATCH = 100
ENRICH_CAP_DEFAULT = 2000          # rows per calendar month, per tenant


# Rows Plaid never described. A Plaid-linked account is excluded on
# purpose: /transactions/sync already returns the merchant entity, logo and
# PFC for everything it carries, so a Plaid row that arrived without a
# category is one Plaid itself could not resolve — paying per row to hand
# the same descriptor back to the same resolver buys nothing. Everything
# else (files, SimpleFIN, OFX, the collectors) is what Enrich is for.
_CANDIDATE_WHERE = """
            WHERE t.removed = 0 AND t.amount <> 0
              AND a.type IN ('depository', 'credit')
              AND NOT (t.raw ? 'personal_finance_category')
              AND (t.raw->>'_enriched_at') IS NULL
              AND NOT EXISTS (SELECT 1 FROM items i
                               WHERE i.id = a.item_id
                                 AND i.aggregator = 'plaid')"""


def enrich_candidates(conn, limit: int | None = None) -> list[dict]:
    """Rows Plaid never described: no PFC in raw and not enriched yet, on a
    depository/credit account that is not itself a Plaid connection, newest
    first."""
    q = ("""SELECT t.id, t.name, t.merchant_name, t.amount, t.date,
                   a.type AS acct_type
              FROM transactions t JOIN accounts a ON a.id = t.account_id"""
         + _CANDIDATE_WHERE + " ORDER BY t.date DESC, t.id")
    args: list = []
    if limit is not None:
        q += " LIMIT %s"; args.append(int(limit))
    return conn.execute(q, args).fetchall()


def enrich_candidate_count(conn) -> int:
    """How many rows Enrich could still describe — the Settings card's
    number. Shares `_CANDIDATE_WHERE` with the selection itself so the
    figure a person is shown can never be a count of a different set than
    the one their click would send."""
    return conn.execute(
        "SELECT count(*) AS n FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id" + _CANDIDATE_WHERE
    ).fetchone()["n"]


def _cap_from(cfg) -> int:
    # 0 is a real answer ("off"), so only an ABSENT key falls back to the
    # default — `or` would silently turn the operator's 0 back into 2000.
    # Clamped at READ time to the same ceiling the settings door enforces:
    # a legacy oversized value written before the door clamped would
    # otherwise keep authorizing the operator's Plaid spend until the
    # tenant happened to save settings again.
    v = cfg.get("plaid_enrich_cap")
    if v is None:
        return ENRICH_CAP_DEFAULT
    from ..web.api import _enrich_cap_ceiling
    return min(int(v), _enrich_cap_ceiling())


def enrich_usage(conn) -> dict:
    """{cap, used, month, remaining} from config (plaid_enrich_cap /
    plaid_enrich_used[YYYY-MM])."""
    cfg = budget.load_config(conn)
    month = dt.date.today().strftime("%Y-%m")
    cap = _cap_from(cfg)
    used = int((cfg.get("plaid_enrich_used") or {}).get(month) or 0)
    return {"cap": cap, "used": used, "month": month,
            "remaining": max(0, cap - used)}


def _enrich_reserve(conn, month: str, want: int) -> int:
    """Charge `want` rows to this month's cap BEFORE Plaid is asked, and
    return how many of them the cap actually had room for.

    The read of what is left and the write of what is spent have to be one
    atomic step. Read-then-spend let two callers who clicked Enrich at the
    same moment each see the whole month's cap as free and each spend it —
    real money, twice the ceiling, and the ceiling is the only thing
    standing between a large imported ledger and a large Plaid bill.
    `config_txn` takes the settings row lock, so the second caller reads
    what the first already reserved.
    """
    with budget.config_txn(conn) as cfg:
        cap = _cap_from(cfg)
        used = dict(cfg.get("plaid_enrich_used") or {})
        have = int(used.get(month) or 0)
        room = max(0, min(int(want), cap - have))
        if room:
            used[month] = have + room
            cfg["plaid_enrich_used"] = used
    return room


def _enrich_refund(conn, month: str, n: int) -> None:
    """Give back rows that were reserved but never left — a chunk aborted
    by a Plaid or network error was never billed, so it must not stay
    charged against the month."""
    if n <= 0:
        return
    with budget.config_txn(conn) as cfg:
        used = dict(cfg.get("plaid_enrich_used") or {})
        used[month] = max(0, int(used.get(month) or 0) - int(n))
        cfg["plaid_enrich_used"] = used


def enrich_rows(conn, *, limit: int = ENRICH_BATCH, transport=None) -> dict:
    """Send up to `limit` unenriched imported rows to Plaid Enrich, merge
    what comes back into each row's raw record in Plaid's own shape, then
    let the resolver and the enrichment columns pick it up. Returns
    counters; a Plaid error is reported, not raised.

    Plaid bills per row SENT, not per row it manages to describe, so the
    cap is charged on the way out and only the rows that never left are
    refunded. One run per tenant at a time (advisory lock): two clicks
    would otherwise select the same candidates — nothing marks a row until
    the answer comes back — and pay for both."""
    stats = {"sent": 0, "enriched": 0, "cap_hit": False, "error": None}
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]
    lock = f"oikonome:enrich:{tid}"
    if not conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                        (lock,)).fetchone()["ok"]:
        stats["error"] = "already running"
        return stats
    try:
        return _enrich_locked(conn, limit, transport, stats)
    finally:
        # Session-level, so it outlives the statement and MUST be released:
        # tenant connections are pooled, and a lock left on one is inherited
        # by whoever borrows it next.
        # a connection that goes back to the pool still holding the lock
        # answers "already running" to every later Enrich for this tenant
        # until the backend dies — release_lock drops it instead
        from ..db import tenancy
        tenancy.release_lock(conn, lock)


def _enrich_locked(conn, limit, transport, stats) -> dict:
    """The body of `enrich_rows`, under the per-tenant single-flight lock."""
    from ..engine import categories, merchant_identity
    from .base import enrichment_from_raw
    _jsonb = jsonb
    month = dt.date.today().strftime("%Y-%m")
    rows = enrich_candidates(conn, int(limit))
    if not rows:
        return stats
    reserved = _enrich_reserve(conn, month, len(rows))
    if reserved <= 0:
        stats["cap_hit"] = True
        return stats
    try:
        if reserved < len(rows):
            stats["cap_hit"] = True
            rows = rows[:reserved]
        client = Client.for_tenant(conn, transport=transport)
        by_type: dict[str, list] = {}
        for r in rows:
            by_type.setdefault(r["acct_type"], []).append(r)
        touched: list[str] = []
        for acct_type, group in by_type.items():
            for i in range(0, len(group), ENRICH_BATCH):
                chunk = group[i:i + ENRICH_BATCH]
                body = {"account_type": acct_type, "transactions": [
                    {"id": r["id"],
                     "description": (r["merchant_name"] or r["name"] or "")[:1000],
                     "amount": abs(float(r["amount"])),
                     "direction": "OUTFLOW" if float(r["amount"]) > 0 else "INFLOW",
                     "iso_currency_code": "USD",
                     "date_posted": r["date"].isoformat() if r["date"] else None}
                    for r in chunk]}
                try:
                    resp = client.post("/transactions/enrich", body)
                except Exception as e:                           # noqa: BLE001
                    # "reported, not raised" covers the network too: a reset
                    # connection or a read timeout must not escape as a 500
                    # and lose the record of the chunks that HAD gone out and
                    # been billed.
                    stats["error"] = str(e) or e.__class__.__name__
                    break
                stats["sent"] += len(chunk)
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                for et in resp.get("enriched_transactions") or []:
                    en = et.get("enrichments") or {}
                    add = {"_enriched_at": now}
                    for src, dst in (("merchant_name", "merchant_name"), ("website", "website"),
                                     ("logo_url", "logo_url"), ("check_number", "check_number"),
                                     ("payment_channel", "payment_channel"),
                                     ("entity_id", "merchant_entity_id"),
                                     ("counterparties", "counterparties"),
                                     ("location", "location"),
                                     ("personal_finance_category", "personal_finance_category")):
                        if en.get(src) not in (None, "", [], {}):
                            add[dst] = en[src]
                    pfc = en.get("personal_finance_category") or {}
                    conn.execute(
                        """UPDATE transactions
                              SET raw = COALESCE(raw, '{}'::jsonb) || %s::jsonb,
                                  category_plaid = COALESCE(category_plaid, %s),
                                  category_plaid_detailed = COALESCE(category_plaid_detailed, %s),
                                  category_plaid_confidence = COALESCE(category_plaid_confidence, %s)
                            WHERE id = %s""",
                        (_jsonb(add), pfc.get("primary"), pfc.get("detailed"),
                         pfc.get("confidence_level"), et.get("id")))
                    # an imported row with no category (or a generic import one)
                    # takes Plaid's — the machine layers refine from there.
                    # Only a label from the known primaries, though: every other
                    # writer and the category picker work from that set, so a
                    # value Enrich invents outside it would be a bucket nothing
                    # can budget, group or offer as a choice. The mirror column
                    # above keeps whatever Plaid actually said.
                    if pfc.get("primary") in categories.PLAID_PRIMARIES:
                        conn.execute(
                            """UPDATE transactions
                                  SET category_primary = %s, category_detailed = %s,
                                      category_source = 'plaid'
                                WHERE id = %s AND category_override IS NULL
                                  AND (category_primary IS NULL
                                       OR category_source IN ('import') OR category_source IS NULL)
                                  AND COALESCE(category_primary, '')
                                      NOT IN ('TRANSFER_IN','TRANSFER_OUT','LOAN_PAYMENTS','INCOME')""",
                            (pfc.get("primary"), pfc.get("detailed"), et.get("id")))
                    touched.append(et.get("id"))
                    stats["enriched"] += 1
            if stats["error"]:
                break
        if touched:
            # the columns and the merchant row follow the new raw facts
            for r in conn.execute("SELECT id, raw FROM transactions WHERE id = ANY(%s)",
                                  (touched,)).fetchall():
                en = enrichment_from_raw(r["raw"])
                conn.execute(
                    """UPDATE transactions SET
                           check_number = COALESCE(check_number, %s),
                           payment_channel = COALESCE(payment_channel, %s),
                           payment_processor = COALESCE(payment_processor, %s),
                           location_city = COALESCE(location_city, %s),
                           location_region = COALESCE(location_region, %s),
                           location_address = COALESCE(location_address, %s),
                           location_postal = COALESCE(location_postal, %s),
                           location_lat = COALESCE(location_lat, %s),
                           location_lon = COALESCE(location_lon, %s),
                           location_store = COALESCE(location_store, %s)
                     WHERE id = %s""",
                    (en["check_number"], en["payment_channel"], en["payment_processor"],
                     en["location_city"], en["location_region"], en["location_address"],
                     en["location_postal"], en["location_lat"], en["location_lon"],
                     en["location_store"], r["id"]))
            merchant_identity.resolve(conn, txn_ids=touched, only_unresolved=False)
    finally:
        # The spend was reserved up front; give back only what never went out.
        # A batch Plaid answers with no enrichments at all — the ordinary
        # answer for descriptors it cannot resolve — is still billed, so usage
        # follows rows sent, not rows touched. Give it back even when the
        # post-send bookkeeping (column updates, the merchant resolve)
        # raises — the rows never sent must not stay charged for the rest of
        # the month.
        _enrich_refund(conn, month, reserved - stats["sent"])
    return stats


def refresh_branding(conn, item_id: str, *, client: Client | None = None) -> bool:
    """Store the institution's logo / brand colour / url on the item —
    Plaid's optional institution metadata. Best-effort, never raises: an
    item without branding just shows a monogram. Skips items whose
    institution_id is unknown or that already carry a logo."""
    row = conn.execute(
        "SELECT institution_id, logo FROM items WHERE id=%s", (item_id,)).fetchone()
    if not row or not row["institution_id"] or row["logo"]:
        return False
    try:
        client = client or Client.for_tenant(conn)
        inst = client.get_institution(row["institution_id"], branding=True)["institution"]
    except Exception as e:                                   # noqa: BLE001
        log.info("institution branding unavailable item=%s: %s", item_id, e)
        return False
    conn.execute(
        "UPDATE items SET logo=%s, brand_color=%s, url=%s WHERE id=%s",
        (inst.get("logo"), inst.get("primary_color"), inst.get("url"), item_id))
    return True


def _institution_ids(client: Client, access_token: str) -> tuple[str, str]:
    """Best-effort (institution_id, institution_name) for a fresh item
    (never raises)."""
    try:
        item = client.get_item(access_token).get("item") or {}
        inst_id = item.get("institution_id")
        if not inst_id:
            return "", ""
        try:
            return inst_id, \
                client.get_institution(inst_id)["institution"]["name"]
        except PlaidError:
            return inst_id, inst_id              # id beats nothing
    except PlaidError:
        return "", ""


def _filter_adopted(conn, txns: list) -> tuple[list, int]:
    """Per-account pass of the reconnect guard over one page of a sync.

    Grouped by account because the cutoff is a property of the account —
    one bank's chequing may have been restored to last Tuesday while its
    savings stops a year earlier."""
    if not txns:
        return txns, 0
    from . import adopt as _adopt
    by_account: dict = {}
    for t in txns:
        by_account.setdefault(getattr(t, "account_id", None), []).append(t)
    kept, dropped = [], 0
    for aid, group in by_account.items():
        if not aid:
            kept.extend(group)
            continue
        try:
            k, stats = _adopt.filter_incoming(conn, aid, group)
        except Exception:                                # noqa: BLE001
            log.warning("reconnect guard failed account=%s", aid,
                        exc_info=True)
            kept.extend(group)
            continue
        dropped += (stats.get("dropped_before_cutoff", 0)
                    + stats.get("deduped_overlap", 0))
        kept.extend(k)
    return kept, dropped


def sync(conn, item_id: str, transport=None) -> dict:
    """Balances + cursor-based incremental transactions for one item.

    Account list + balances come from ``/accounts/get`` (included with the
    Transactions product). Never use ``/accounts/balance/get`` here — that
    is the separately-billed Balance product; production clients without
    Balance authorized get INVALID_PRODUCT and the link waiting page 500s.
    Cached balances on /accounts/get are sufficient for budgeting; real-time
    Balance is only needed for payment rails we do not run."""
    client = Client.for_tenant(conn, transport=transport)
    token = get_access_token(conn, item_id)
    if not token:
        raise PlaidError({"error_code": "NO_TOKEN", "error_type": "CONFIG",
                          "error_message": f"item {item_id} has no token"}, 0)
    try:
        bal = client.post("/accounts/get", {"access_token": token})
        # Reconnecting after a restore: adopt the accounts the backup left
        # behind BEFORE they are upserted, so the rename lands the ordinary
        # upsert on the same row instead of creating a second copy of an
        # account the tenant already has. A no-op for every ordinary link.
        try:
            from . import adopt as _adopt
            inst = conn.execute(
                "SELECT institution_id, institution_name FROM items "
                "WHERE id=%s", (item_id,)).fetchone() or {}
            _adopt.adopt_accounts(
                conn, item_id=item_id,
                institution_id=inst.get("institution_id"),
                institution_name=inst.get("institution_name"),
                incoming=[{"account_id": a.get("account_id"),
                           "mask": a.get("mask"), "type": a.get("type")}
                          for a in (bal.get("accounts") or [])])
        except Exception:                                # noqa: BLE001
            # adoption is an optimisation over correctness-by-duplication:
            # if it fails the link still works, it just leaves the second
            # account the user can merge by hand
            log.warning("reconnect adoption failed item=%s", item_id,
                        exc_info=True)
        upsert_accounts(conn, item_id, _norm_accounts(bal))

        cursor = conn.execute("SELECT tx_cursor FROM items WHERE id=%s",
                              (item_id,)).fetchone()["tx_cursor"]
        added = modified = removed_n = 0
        skipped_adopted = 0
        next_cursor = cursor
        last_request_id = None
        update_status = None
        skipped_imports = 0
        # one overlap guard for the whole run: its one-to-one consumption
        # of legacy import rows must span the pages (see
        # filter_import_duplicates)
        overlap = ImportOverlapGuard(conn)
        pages = 0
        while True:
            # bounded: a response that kept saying has_more would otherwise
            # hold this tenant's sync lock for ever (250 rows × 400 pages is
            # far past any real two-year backfill)
            pages += 1
            if pages > 400:
                raise PlaidError({"error_code": "PAGINATION_RUNAWAY",
                                  "error_type": "SYNC",
                                  "error_message": "sync did not converge"}, 0)

            # 250 a page, applied PAGE BY PAGE. Plaid delivers the two-year
            # backfill in one go, and applying it after the last page would
            # land it as one silent jump — the setup wizard's import bar
            # sitting still and then filling, instead of the steady progression
            # an import should show. Each page is upserted as it
            # arrives so the poll sees the count climb; a mid-pagination
            # failure leaves rows that the retry simply upserts again by
            # id, because the CURSOR still moves only after the last page
            # (the invariant Plaid asks for).
            page = client.post("/transactions/sync", {
                "access_token": token, "count": 250,
                **({"cursor": next_cursor} if next_cursor else {}),
                "options": {"include_original_description": True}})
            # HISTORICAL_UPDATE_COMPLETE means the full days_requested
            # window has been delivered; before that the item is still
            # backfilling and detection-quality features should wait.
            update_status = (page.get("transactions_update_status")
                             or update_status)
            page_txns = [_norm_txn(t) for t in page.get("added") or []]
            page_txns += [_norm_txn(t) for t in page.get("modified") or []]
            page_removed = [r["transaction_id"]
                            for r in page.get("removed") or []]
            added += len(page.get("added") or [])
            modified += len(page.get("modified") or [])
            removed_n += len(page.get("removed") or [])
            # Reverse dedup first: rows already covered by earlier FILE
            # IMPORTS (window-scoped, one-to-one — base.ImportOverlapGuard)
            # must not re-insert; existing plaid ids (updates) always pass.
            page_txns, skipped = filter_import_duplicates(conn, page_txns,
                                                          guard=overlap)
            skipped_imports += skipped
            # Same idea one door along: on an account this item ADOPTED
            # from a restore, the backfill replays charges the restored
            # history already holds under their old ids. Drop what we
            # already have rather than shipping the tenant two of it.
            page_txns, adopted_skips = _filter_adopted(conn, page_txns)
            skipped_adopted += adopted_skips
            upsert_transactions(conn, page_txns)
            mark_removed(conn, page_removed)
            next_cursor = page.get("next_cursor")
            last_request_id = page.get("request_id") or last_request_id
            if not page.get("has_more"):
                break
        # the cursor is persisted ONLY after full pagination succeeded
        # a clean sync also clears any out-of-band webhook flag (ITEM
        # ERROR / PENDING_EXPIRATION / …) — the connection demonstrably works
        # …but never resurrect an archived item: disconnect can land while
        # a sync is in flight, and 'archived' must win over both the 'ok'
        # here and the error stamp below
        conn.execute("UPDATE items SET tx_cursor=%s, status='ok', "
                     "tx_update_status=COALESCE(%s, tx_update_status), "
                     "webhook_status=NULL, webhook_status_at=NULL "
                     "WHERE id=%s AND COALESCE(status,'') != 'archived'",
                     (next_cursor, update_status, item_id))
        log_sync(conn, item_id, added, request_id=last_request_id,
                 modified=modified, removed=removed_n)
        # Close the reconnect-adoption guard on demonstrated success: the
        # backfill window is fully delivered AND a complete pagination ran
        # without the seam matcher claiming anything — the re-delivery has
        # dried up, so keeping the duplicate matcher pointed at live data
        # for the rest of the 14-day clock is pure false-positive risk:
        # a genuine settlement can be absorbed as a "duplicate" of its own
        # pending twin and silently dropped. The clock stays as the
        # backstop for an item that never gets here.
        if (skipped_adopted == 0
                and (update_status or conn.execute(
                    "SELECT tx_update_status FROM items WHERE id=%s",
                    (item_id,)).fetchone()["tx_update_status"])
                == "HISTORICAL_UPDATE_COMPLETE"):
            from . import adopt as _adopt
            _adopt.mark_reconciled_if_settled(conn, item_id)
        # …and, from the same successful run, when the BANK last fed Plaid.
        # A sync that adds nothing is the common case, and this is the only
        # figure that says whether that means "nothing happened" or "Plaid
        # has not heard from your bank since this morning".
        record_bank_update(conn, client, token, item_id)
        record_institution_health(conn, client, item_id)
        # The guard is NOT closed here. Closing it after the first full
        # pagination would let duplicates through: Plaid keeps delivering
        # for days, and a charge that was pending when the backup was taken
        # returns later as a different transaction. The guard expires on
        # its own after adopt.RECONCILE_DAYS, which
        # is the width of that overlap rather than the width of one sync.
        return {"added": added, "modified": modified, "removed": removed_n,
                "skipped_import_duplicates": skipped_imports,
                "skipped_already_restored": skipped_adopted}
    except PlaidError as e:
        conn.execute("UPDATE items SET status=%s WHERE id=%s "
                     "AND COALESCE(status,'') != 'archived'",
                     (f"error:{e.code}", item_id))
        log_sync(conn, item_id, 0, error=str(e), request_id=e.request_id)
        raise


# ---- non-transaction products: liabilities + investment holdings -----------
#
# Holdings only, deliberately: investment TRANSACTIONS are budget-excluded
# anyway, and holdings alone back Net Worth / Top holdings / the fees
# estimate.
#
# Cadence: the worker's hourly sweep calls sync_products roughly once per
# DAY per tenant (job_runs heartbeat 'plaid-products', >20h stale ⇒ due):
# due dates and positions move daily at most, and /liabilities/get +
# /investments/holdings/get are per-request billed products.


# A failing connection is not one kind of thing: not every status that is
# not 'ok' deserves a red dot, "sync failing", and a Link update-mode
# button. Plaid documents
# these as transient — the bank is down or slow, and the next poll fixes
# it with no human involved — while only a second group actually needs
# the person to sign in again. Sending someone through re-auth for a bank
# that is merely down teaches them the warning means nothing, which is
# worse than showing no warning at all.
TRANSIENT_CODES = frozenset({
    "INSTITUTION_DOWN", "INSTITUTION_NOT_RESPONDING",
    "INSTITUTION_NOT_AVAILABLE", "INSTITUTION_STATUS_UNAVAILABLE",
    "INTERNAL_SERVER_ERROR", "PLANNED_MAINTENANCE", "RATE_LIMIT_EXCEEDED",
    "PRODUCT_NOT_READY",
})

# These end at Link's update mode (or, for a revoked consent, a fresh
# link). Plaid's own guidance per webhook code.
REAUTH_CODES = frozenset({
    "ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT",
    "USER_PERMISSION_REVOKED", "ACCESS_NOT_GRANTED", "ITEM_LOCKED",
    "NEW_ACCOUNTS_AVAILABLE",
})

# The webhook receiver stores its own vocabulary on webhook_status; map
# both it and an `error:CODE` sync status through one function so the
# clients never have to know which door wrote the string.
_WEBHOOK_WORDS = {
    "pending_expiration": "PENDING_EXPIRATION",
    "pending_disconnect": "PENDING_DISCONNECT",
    "revoked": "USER_PERMISSION_REVOKED",
    "new_accounts": "NEW_ACCOUNTS_AVAILABLE",
}


def status_kind(status: str | None) -> str:
    """What a connection's status MEANS to the person looking at it.

    'ok'        — working.
    'retrying'  — the bank or Plaid is having trouble; the next poll
                  clears it and there is nothing to do. Never offer a fix
                  button here: update mode cannot repair an institution
                  outage, and the click that appears to work is what
                  makes the next real warning ignorable.
    'reauth'    — the person must sign in again (or share new accounts)
                  through Link's update mode.
    'gone'      — the Item no longer exists at Plaid; only a fresh link
                  brings it back.
    'attention' — an error we have not classified. Deliberately the
                  fallback so a new Plaid code surfaces rather than
                  silently reading as healthy.
    """
    st = (status or "ok").strip()
    if st in ("", "ok"):
        return "ok"
    if st in ("reaped", "plaid-gone"):
        return "gone"
    if st in ("restored", "stale"):
        return "retrying"
    code = _WEBHOOK_WORDS.get(st) or (
        st.split(":", 1)[1] if st.startswith("error:") else st.upper())
    if code in TRANSIENT_CODES:
        return "retrying"
    if code in REAUTH_CODES:
        return "reauth"
    if code in ("ITEM_NOT_FOUND", "NO_TOKEN"):
        return "gone"
    return "attention"


# How stale the cached institution health may get before the hourly sync
# spends a Plaid call refreshing it. Well under the 24 h the clients stop
# trusting it at, so the displayed value is never the throttle's fault.
HEALTH_MAX_AGE_H = 6


def _fresher_than(stamp: str, hours: int) -> bool:
    """True when an ISO stamp is younger than `hours`. An unreadable or
    future-dated stamp is treated as stale — refreshing once too often
    beats never refreshing again on one bad write."""
    try:
        seen = dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - seen
    return dt.timedelta(0) <= age < dt.timedelta(hours=hours)


def record_institution_health(conn, client: "Client", item_id: str) -> None:
    """Cache Plaid's institution-wide login health on the item.

    The status dot is honest about the ITEM — but when a bank's own OAuth
    is degraded fleet-wide, the item still syncs and stays green while a
    reconnect attempt dies on the bank's error page with no context. The
    hourly sync stamps `institution_health` into items.raw so the clients
    can show "the bank is having trouble" without any new live call.
    Best-effort like record_bank_update: health that cannot be read must
    never fail a sync that landed rows."""
    try:
        row = conn.execute("SELECT institution_id, raw FROM items "
                           "WHERE id=%s", (item_id,)).fetchone()
        inst_id = row and row["institution_id"]
        if not inst_id:
            return
        # Throttled, because the sync it hangs off is hourly and this is
        # not: the display side only trusts the stored value for a day, so
        # re-fetching it twelve times a day buys nothing and adds one
        # /institutions/get_by_id per item per hour to the same client_id
        # rate limit the sweep's bucketing exists to protect. One local
        # read of what we already stored decides.
        prev = (row["raw"] or {}) if isinstance(row["raw"], dict) else {}
        as_of = ((prev.get("institution_health") or {}).get("as_of")
                 if isinstance(prev.get("institution_health"), dict) else None)
        if as_of and _fresher_than(as_of, HEALTH_MAX_AGE_H):
            return
        r = client.post("/institutions/get_by_id",
                        {"institution_id": inst_id, "country_codes": ["US"],
                         "options": {"include_status": True}})
        st = (((r.get("institution") or {}).get("status") or {})
              .get("item_logins") or {})
        if not st.get("status"):
            return
        conn.execute(
            "UPDATE items SET raw = COALESCE(raw,'{}'::jsonb) || %s "
            "WHERE id=%s",
            (jsonb({"institution_health": {
                "logins": st["status"],
                "as_of": dt.datetime.now(dt.timezone.utc).isoformat()}}),
             item_id))
    except Exception:                                    # noqa: BLE001
        log.debug("institution health read failed item=%s", item_id,
                  exc_info=True)


def record_bank_update(conn, client: "Client", token: str, item_id: str) -> None:
    """Stamp when the BANK last gave Plaid new data for this item.

    The distinction the UI could not draw before: `sync_log.ran_at` is when
    WE last asked Plaid, and Plaid answers out of its own copy. The copy
    refreshes on Plaid's schedule (a few times a day for most
    institutions), so a sync that returns nothing is usually correct
    rather than broken — the bank simply has not sent anything since.
    /item/get carries the real timestamp and is included in the
    Transactions subscription, unlike /transactions/refresh, which would
    force a live pull and is billed per request (never called; see the
    Client docstring).

    Best-effort by construction: a sync that landed rows must not be
    reported as a failure because a cosmetic timestamp could not be read.
    """
    try:
        got = client.get_item(token)
        st = (got.get("status") or {})
        tx = st.get("transactions") or {}
        conn.execute("UPDATE items SET bank_updated_at=%s, bank_failed_at=%s "
                     "WHERE id=%s AND COALESCE(status,'') != 'archived'",
                     (tx.get("last_successful_update"),
                      tx.get("last_failed_update"), item_id))
        # Same call, second job: an Item's webhook is registered on the
        # LINK TOKEN, so an Item linked before this deployment had a
        # webhook URL — or before the operator set one at all — never
        # receives SYNC_UPDATES_AVAILABLE and falls back to the hourly
        # poll forever, silently. Nothing else would ever notice. Since
        # /item/get already tells us what is registered, reconcile it
        # here rather than adding a sweep.
        want = os.environ.get("OIKONOME_PLAID_WEBHOOK_URL") or ""
        have = (got.get("item") or {}).get("webhook") or ""
        if want and want != have:
            client.post("/item/webhook/update",
                        {"access_token": token, "webhook": want})
            log.info("item %s webhook re-registered (%s → %s)",
                     item_id, have or "none", want)
    except Exception:                                    # noqa: BLE001
        log.debug("item status unavailable item=%s", item_id, exc_info=True)


def _product_unavailable(e: PlaidError) -> bool:
    """True for 'this item/institution simply doesn't do that product'
    errors — skip quietly. Everything else raises."""
    return (e.type in ("INVALID_INPUT", "ITEM_ERROR")
            or "NOT_SUPPORTED" in e.code or "CONSENT" in e.code)


def _record_product_issue(conn, item_id: str, product: str,
                          e: "PlaidError | None") -> None:
    """Remember (or clear) WHY a billed product returns nothing for this
    item — items.raw → product_issues → {product: {cause, code}}.

    _product_unavailable lumps two very different refusals together: a
    CONSENT error means the user never granted the product and a re-link
    in update mode can attach it; anything else (NOT_SUPPORTED and kin)
    means the institution cannot provide it. The sync rightly skips both —
    but the forecast then quietly draws no autopay line for the card, and
    no surface could say which cause it was, or that anything was missing
    at all. The connection card reads this and says so. Cleared (pass
    e=None) the moment the product answers, so a granted consent heals
    the notice without a new code path."""
    if e is None:
        conn.execute(
            "UPDATE items SET raw = raw #- ARRAY['product_issues', %s] "
            "WHERE id=%s AND raw->'product_issues' ? %s",
            (product, item_id, product))
        return
    cause = "consent" if "CONSENT" in e.code else "not_supported"
    # `product_issues` is a map keyed by product, and the clear path above
    # deletes a key out of it — on any other shape that delete raises and
    # the connection can never be marked healthy again. COALESCE only
    # substitutes for a MISSING value, so the shape is checked as well:
    # a value that is not a map is replaced rather than concatenated onto.
    conn.execute(
        "UPDATE items SET raw = jsonb_set(COALESCE(raw,'{}'::jsonb), "
        "ARRAY['product_issues'], CASE WHEN jsonb_typeof("
        "raw->'product_issues') = 'object' THEN raw->'product_issues' "
        "ELSE '{}'::jsonb END || %s) WHERE id=%s",
        (jsonb({product: {"cause": cause, "code": e.code}}), item_id))


def sync_liabilities(conn, item_id: str, client: Client | None = None,
                     token: str | None = None, transport=None) -> int:
    """/liabilities/get → upsert one row per account into `liabilities`
    (raw keeps Plaid's full object; forecast.py reads
    next_payment_due_date / last_statement_balance out of it). Returns rows
    written; 0 when the product is unavailable for this item."""
    client = client or Client.for_tenant(conn, transport=transport)
    token = token or get_access_token(conn, item_id)
    if not token:
        return 0
    try:
        data = client.get_liabilities(token)
    except PlaidError as e:
        if _product_unavailable(e):
            _record_product_issue(conn, item_id, "liabilities", e)
            return 0
        raise
    _record_product_issue(conn, item_id, "liabilities", None)
    n = 0
    for kind in ("credit", "mortgage", "student"):
        for entry in (data.get("liabilities") or {}).get(kind) or []:
            if not entry.get("account_id"):
                continue
            conn.execute(
                """INSERT INTO liabilities (account_id, as_of, raw)
                   VALUES (%s, now(), %s)
                   ON CONFLICT (tenant_id, account_id)
                   DO UPDATE SET as_of=now(), raw=EXCLUDED.raw""",
                # the loan's own number is a credential-shaped value Plaid
                # documents on the liability entry; it has no use here and
                # would otherwise sit in plaintext and ride the export
                (entry["account_id"],
                 jsonb({k: v for k, v in entry.items()
                        if k not in ("account_number",)})))
            n += 1
    return n


def sync_holdings(conn, item_id: str, client: Client | None = None,
                  token: str | None = None, transport=None) -> int:
    """/investments/holdings/get → replace the `holdings` rows of this
    item's investment accounts (delete-then-insert per account: a sold
    position must disappear). Skips items with no
    investment accounts without an API call. Returns holdings written."""
    # the item's investment accounts, plus any account of the item that
    # holds positions from an earlier sync whatever its type — those are
    # the rows this replace owns, and a sold position on one must go
    ours = {r["id"] for r in conn.execute(
        """SELECT a.id FROM accounts a WHERE a.item_id=%s
              AND (a.type='investment' OR EXISTS (
                    SELECT 1 FROM holdings h WHERE h.account_id = a.id))""",
        (item_id,))}
    if not ours:
        return 0
    client = client or Client.for_tenant(conn, transport=transport)
    token = token or get_access_token(conn, item_id)
    if not token:
        return 0
    try:
        hold = client.get_investment_holdings(token)
    except PlaidError as e:
        if _product_unavailable(e):
            _record_product_issue(conn, item_id, "investments", e)
            return 0
        raise
    _record_product_issue(conn, item_id, "investments", None)
    secs = {s["security_id"]: s for s in hold.get("securities") or []}

    def sym_name(sid):
        s = secs.get(sid, {})
        return (s.get("ticker_symbol") or s.get("cusip") or sid,
                s.get("name") or s.get("ticker_symbol") or sid)

    inv_accts = {a["account_id"] for a in hold.get("accounts") or []
                 if a.get("type") == "investment"}
    # The rows are built in full BEFORE the replace, and the replace is
    # one transaction: on an autocommit connection a per-account DELETE
    # would commit on its own, so a bad holding further down the
    # response would leave the account with no positions at all until the
    # next successful run, and every net-worth read in between would see $0
    rows = []
    for h in hold.get("holdings") or []:
        if not isinstance(h, dict) or not h.get("account_id"):
            continue
        sym, nm = sym_name(h.get("security_id"))
        rows.append((h["account_id"], sym, nm, h.get("quantity"),
                     h.get("institution_price"), h.get("institution_value"),
                     jsonb(h)))
    # Replace covers every account we are about to WRITE, not just the
    # ones the response calls investment: a holding whose account Plaid
    # types as something else (or omits from `accounts` altogether) would
    # be inserted and then never deleted, so a position sold from it would
    # stay in net worth forever. Our own investment accounts join the set too,
    # so one that sold everything is cleared rather than left frozen.
    with conn.transaction():
        for aid in ours | inv_accts | {r[0] for r in rows}:
            conn.execute("DELETE FROM holdings WHERE account_id=%s", (aid,))
        for row in rows:
            conn.execute(
                """INSERT INTO holdings (account_id, symbol, name, quantity,
                       price, value, as_of, raw)
                   VALUES (%s,%s,%s,%s,%s,%s,now(),%s)
                   ON CONFLICT (tenant_id, account_id, symbol)
                   DO UPDATE SET name=EXCLUDED.name, quantity=EXCLUDED.quantity,
                       price=EXCLUDED.price, value=EXCLUDED.value,
                       as_of=now(), raw=EXCLUDED.raw""", row)
    return len(rows)


def sync_products(conn, item_id: str, transport=None) -> dict:
    """Liabilities + holdings for one item, one shared client/token.
    The worker calls this daily; the per-item manual sync route calls it
    too so 'sync now' refreshes everything.

    What to pull is ``extra_products`` and nothing else — requesting
    /liabilities/get or /investments/holdings/get can ATTACH the billed
    product to the Item, so an instance that does not want them must never
    touch the endpoints, and a link door that asks the same question must
    get the same answer. With no add-on to narrow it, that answer is both
    products unless the owner opts out (their keys, their Plaid bill), so
    the sync never pulls products no link asked consent for."""
    extra = extra_products(conn)
    want_liab = "liabilities" in extra
    want_inv = "investments" in extra
    if not (want_liab or want_inv):
        return {"liabilities": 0, "holdings": 0}
    client = Client.for_tenant(conn, transport=transport)
    token = get_access_token(conn, item_id)
    if not token:
        return {"liabilities": 0, "holdings": 0}
    return {
        "liabilities": sync_liabilities(conn, item_id, client, token)
        if want_liab else 0,
        "holdings": sync_holdings(conn, item_id, client, token)
        if want_inv else 0,
    }
