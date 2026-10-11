"""First-boot setup wizard (self-hosted): while NO user exists, /setup
creates the first account and optionally connects an aggregator in the same
step — SimpleFIN (token claimed + first sync), Plaid (BYO developer keys
stored in tenant config, validated at first link attempt), both, or neither
(file imports only) — plus an optional LLM for smart categorization
(llm_url/llm_model/llm_api_key in tenant config, read by the worker's
nightly llm_categorize run; config wins over the operator's OIKONOME_LLM_*
env vars). Once any user exists the wizard is gone (403) — a fresh
install's single-owner bootstrap, not an open signup (hosted signup is
/api/signup; self-hosted operators add users deliberately later).
"""

from __future__ import annotations

import datetime as dt
import os
from ..envnum import env_flag

from ..auth import passwords
from ..db import crypto, tenancy
from ..engine import budget
from ..sync import simplefin

PLAID_ENVS = ("production", "sandbox")

SIMPLEFIN_ITEM = "sfin-main"


class SimplefinFirstPullFailed(Exception):
    """The setup token was claimed and the bridge saved, but the first pull
    did not finish. The connection is live: the next sync retries it, and
    no new token is needed (the old one is spent)."""


def connect_simplefin(conn, setup_token: str, *, days: int = 90,
                      transport=None) -> dict:
    """Claim a SimpleFIN setup token and pull the first `days` of history.

    The setup token is ONE-TIME: the claim spends it and hands back the
    access URL, which exists nowhere else. It is saved before the first
    pull, because that pull is the slow, failure-prone step (a deep window,
    a bridge having a bad minute) — saved only after it, a pull that timed
    out left the person holding a spent token and no connection, with a
    fresh token from the bridge as the only way back. Raises
    SimplefinFirstPullFailed when the claim held and only the pull failed;
    anything else raised means the claim itself did not happen."""
    from ..sync.base import upsert_item
    access = simplefin.claim_setup_token(setup_token, transport=transport)
    upsert_item(conn, SIMPLEFIN_ITEM, "simplefin", "SimpleFIN bridge", access)
    revived = _revive_disconnected_banks(conn)
    try:
        return simplefin.sync(
            conn, SIMPLEFIN_ITEM, access,
            since=dt.date.today() - dt.timedelta(days=days),
            transport=transport)
    except Exception as e:                                # noqa: BLE001
        raise SimplefinFirstPullFailed(type(e).__name__) from e
    finally:
        # A pull that failed carried no bank, so every revived one goes
        # back to disconnected — the state it was in before this token,
        # still owing its gap. Left live, a bank no longer on the bridge
        # would read as a connection that never syncs, and the routine
        # sync that retries the bridge never re-archives anything.
        _rearchive_banks_not_returned(conn, revived)


def _revive_disconnected_banks(conn) -> dict:
    """A new setup token is the owner connecting the bridge again, and the
    banks that went down with the bridge come back with it. Only those: a
    bank the owner disconnected or purged on its own, or one a new link
    took over, was cut for a reason of its own (a duplicate of another
    feed, history deleted on purpose), and the bridge still carrying it is
    no word from the owner that they want it back. A bank with no accounts
    left has nothing to come back to. Each is marked to reach back
    RESYNC_OVERLAP before its disconnect on the next pull, which the
    routine window never would. Returns {child id: when it was
    disconnected}."""
    from ..sync.base import BRIDGE_DISCONNECT
    rows = conn.execute(
        "SELECT id, archived_at FROM items WHERE aggregator='simplefin-org' "
        "AND id LIKE %s AND status='archived' AND archived_reason=%s "
        "AND EXISTS (SELECT 1 FROM accounts a WHERE a.item_id=items.id)",
        (SIMPLEFIN_ITEM + ":%", BRIDGE_DISCONNECT)).fetchall()
    for r in rows:
        _mark_returning(conn, r["id"], r["archived_at"])
    return {r["id"]: r["archived_at"] for r in rows}


def _mark_returning(conn, child: str, archived_at) -> dt.date | None:
    """Make one disconnected bank live again, owing the days it was away.
    Returns the first day its next pull reaches back to."""
    # on the calendar the pull's own window is counted in (the server's
    # local today)
    since = ((archived_at or dt.datetime.now(dt.timezone.utc)).astimezone()
             - simplefin.RESYNC_OVERLAP).date()
    # a gap an earlier revival marked and no pull has served yet is still
    # owed, so the start never moves later
    row = conn.execute(
        "UPDATE items SET status='ok', archived_at=NULL, "
        "archived_reason=NULL, resync_from=LEAST(resync_from, %s) "
        "WHERE id=%s AND status='archived' RETURNING resync_from",
        (since, child)).fetchone()
    return row["resync_from"] if row else None


# archived_reason on a SimpleFIN bank one of whose accounts the owner
# deleted by itself while the bank was disconnected. The bridge still
# carries that account, so any pull of the bank would bring it back with
# its history: the bank stays out, and the reconnect door says why.
ACCOUNT_PURGED = "account-purged"

# how far the pull a reconnect makes at once reaches on its own; the bank
# coming back reaches further, over the time it was away
RECONNECT_PULL_DAYS = 30


def reconnect_bank(conn, child: str, *, transport=None) -> dict:
    """Bring back one SimpleFIN bank the owner disconnected on its own, on
    their word. A new setup token no longer does that (it brings back only
    what went down with the bridge), so this is the way back for a bank
    cut by itself.

    The bridge is pulled at once, reaching back over the time the bank was
    away. Only that pull can tell whether the bridge still carries the
    bank: one it no longer returns, left live, would read as a connection
    that never syncs, so it goes back to disconnected as it was, and the
    owner is told. Raises KeyError for no such bank, ValueError when it is
    not disconnected, its bridge is not live, nothing of it is left, a new
    link now serves its accounts, one of its accounts was deleted, or the
    bridge does not (or could not) return it."""
    from ..sync.base import get_access_token
    it = conn.execute(
        "SELECT id, status, archived_at, archived_reason FROM items "
        "WHERE id=%s AND aggregator='simplefin-org'", (child,)).fetchone()
    if it is None:
        raise KeyError(child)
    if it["status"] != "archived":
        raise ValueError("this connection is not disconnected")
    bridge_id = child.split(":", 1)[0]
    bridge = conn.execute(
        "SELECT status, access_token FROM items WHERE id=%s",
        (bridge_id,)).fetchone()
    if (bridge is None or not bridge["access_token"]
            or bridge["status"] == "archived"):
        raise ValueError("the SimpleFIN bridge is disconnected — connect a "
                         "new SimpleFIN setup token first")
    if conn.execute("SELECT 1 FROM accounts WHERE item_id=%s LIMIT 1",
                    (child,)).fetchone() is None:
        # its history was deleted on purpose; bringing it back would pull
        # that history in again unasked
        raise ValueError("this bank's data was deleted — add it again at "
                         "bridge.simplefin.org to start it fresh")
    reason = it["archived_reason"] or ""
    if reason.startswith("reconnected"):
        # a new link adopted its accounts; its next pull would re-create
        # each of them beside the one the new link now keeps
        raise ValueError("a newer connection now serves this bank's "
                         "accounts — reconnecting it would bring them in "
                         "twice")
    if reason == ACCOUNT_PURGED:
        # the bridge still carries the deleted account, and the pull
        # would recreate it with its history
        raise ValueError("can't reconnect — an account deleted from this "
                         "bank is still on the bridge, and sync would "
                         "recreate it. Disconnect and delete the bank, then "
                         "add it again at bridge.simplefin.org")
    since = _mark_returning(conn, child, it["archived_at"])
    prior = {child: it["archived_at"]}
    try:
        simplefin.sync(
            conn, bridge_id, get_access_token(conn, bridge_id),
            since=dt.date.today() - dt.timedelta(days=RECONNECT_PULL_DAYS),
            transport=transport)
    except Exception as e:                                # noqa: BLE001
        # nothing showed the bank is still there; it stays as it was,
        # still owing its gap
        _rearchive_banks_not_returned(conn, prior,
                                      reason=it["archived_reason"])
        raise ValueError("SimpleFIN did not answer — try again in a moment"
                         ) from e
    if _rearchive_banks_not_returned(conn, prior,
                                     reason=it["archived_reason"]):
        raise ValueError("SimpleFIN no longer returns this bank — add it "
                         "again at bridge.simplefin.org, then reconnect it")
    return {"ok": True,
            "resync_from": since.isoformat() if since else None}


_BRIDGE_MARK = object()


def _rearchive_banks_not_returned(conn, revived: dict, *,
                                  reason=_BRIDGE_MARK) -> list:
    """A revived bank the pull did not carry is no longer on the bridge;
    left live it would read as a connection that never syncs. It goes back
    to disconnected as it was — under the mark it had (a bank a new token
    brought back is marked as gone with the bridge, so a later token can
    bring it back; one the owner brought back keeps its own) — and still
    owing its gap should it be added at the bridge and come back again.
    The pull clears the gap on every bank it carried, so the ones still
    owing are these. A pull that failed carried nothing, and a bank it
    stamped with the error is put back too. Returns the banks re-archived."""
    from ..sync.base import BRIDGE_DISCONNECT
    if reason is _BRIDGE_MARK:
        reason = BRIDGE_DISCONNECT
    back = []
    for child, archived_at in revived.items():
        row = conn.execute(
            "UPDATE items SET status='archived', archived_reason=%s, "
            "archived_at=COALESCE(%s, now()) WHERE id=%s "
            "AND resync_from IS NOT NULL "
            "AND COALESCE(status,'') != 'archived' RETURNING id",
            (reason, archived_at, child)).fetchone()
        if row is not None:
            back.append(child)
    return back


SIMPLEFIN_PULL_RETRYING = ("Bank connected, but the first download from "
                           "SimpleFIN did not finish. It retries on the next "
                           "sync — no new token needed.")


def instance_has_users(conn) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None



# How long the wizard waits for the household's sync lock before its first
# SimpleFIN pull. The household is seconds old, so whatever holds the lock
# is a scheduled pass over an empty household and lets go almost at once.
SETUP_SYNC_LOCK_WAIT_S = 30.0


def _connect_simplefin_locked(conn, tid, token, transport, sleep=None,
                              wait_s=None):
    """The wizard's SimpleFIN connect, under the household's sync lock like
    every other connect door. The bridge item is visible to the worker the
    moment it is written, so without the lock a scheduled sync can start a
    second deep pull of the same item alongside this one, both writing the
    same accounts and transactions from different snapshots. A wizard has no
    "try again later" to answer with, so it waits for the lock instead of
    giving up at once; if it never frees, the connect is reported failed and
    the Accounts page retries it (the token is not claimed until then)."""
    import time

    from ..sync.base import tenant_sync_lock
    sleep = sleep or time.sleep
    deadline = time.monotonic() + (SETUP_SYNC_LOCK_WAIT_S if wait_s is None
                                   else wait_s)
    while True:
        with tenant_sync_lock(conn, str(tid)) as held:
            if held:
                return connect_simplefin(conn, token, transport=transport)
        if time.monotonic() >= deadline:
            raise RuntimeError("the household's sync lock stayed held")
        sleep(0.5)

def bootstrap(email: str, password: str, simplefin_token: str = "",
              plaid_client_id: str = "", plaid_secret: str = "",
              plaid_env: str = "production", llm_url: str = "",
              llm_model: str = "", llm_api_key: str = "",
              transport=None) -> dict:
    """Create tenant + first user; claim + sync SimpleFIN when a setup token
    is supplied; store BYO Plaid keys in tenant config when supplied (no
    validation here — the first link attempt validates, same as the
    Accounts-page flow); store optional smart-categorization LLM settings
    in tenant config (llm_url + llm_model required together, key optional
    and encrypted at rest — local servers need none). Returns {tenant_id,
    user_id, sync, sync_error, plaid_saved, llm_saved}. Raises ValueError
    on bad input BEFORE any user is created; the ROUTE enforces the
    no-users gate."""
    email = email.strip().lower()
    from .mailguard import valid_address
    if not valid_address(email):
        raise ValueError("enter a valid email address")
    err = passwords.password_error(password)
    if err:
        raise ValueError(err)
    plaid_client_id = plaid_client_id.strip()
    plaid_secret = plaid_secret.strip()
    if bool(plaid_client_id) != bool(plaid_secret):
        raise ValueError("enter both the Plaid client_id and secret, "
                         "or leave both blank")
    llm_url = llm_url.strip()
    llm_model = llm_model.strip()
    llm_api_key = llm_api_key.strip()
    if bool(llm_url) != bool(llm_model):
        raise ValueError("enter both the LLM endpoint URL and model name, "
                         "or leave both blank")
    if llm_api_key and not llm_url:
        raise ValueError("an LLM API key needs an endpoint URL and model "
                         "name to go with it")
    if llm_url:
        from .netguard import BlockedURL, check_url
        try:
            check_url(llm_url, what="the LLM endpoint")
        except BlockedURL as e:
            raise ValueError(str(e))
    # hashed before the claim lock below: argon2 is slow by design, and
    # nothing slow runs while a lock other requests queue on is held
    pw_hash = passwords.hash_password(password)
    admin = tenancy.admin_connect()
    try:
        # The route's instance_has_users() check and this insert are a
        # TOCTOU window — two parallel /setup posts (or /setup racing
        # /api/signup) during first boot could each pass the check and mint a
        # second owner/tenant. Serialize the claim: one transaction-scoped
        # advisory lock, re-check inside it, so exactly one racer creates the
        # first account and the rest get a clear 403 from the route's retry.
        with admin.transaction():
            admin.execute("SELECT pg_advisory_xact_lock(hashtext('oikonome:setup-claim'))")
            # re-check under the lock closes the TOCTOU. DEV_MODE exempt: the
            # test suite bootstraps many tenants against one control plane
            # (which is the hosted shape); prod self-host never sets it.
            if not env_flag("OIKONOME_HOSTED") \
                    and os.environ.get("OIKONOME_DEV") != "1" \
                    and instance_has_users(admin):
                raise ValueError("this instance is already set up")
            tid = tenancy.create_tenant(admin, email)
            row = admin.execute(
                """INSERT INTO users (tenant_id, email, password_hash, verified_at)
                   VALUES (%s,%s,%s,now()) RETURNING id""",
                (tid, email, pw_hash)).fetchone()
    finally:
        admin.close()
    sync_result = None
    sync_error = None
    if simplefin_token.strip():
        # The user row already exists at this point, so a bad token must NOT
        # raise — that would end the one-shot wizard with no account usable.
        # Finish setup and surface the error instead; the Accounts page
        # offers a retry form.
        conn = tenancy.tenant_connect(tid)
        try:
            sync_result = _connect_simplefin_locked(conn, tid,
                                                    simplefin_token,
                                                    transport)
        except SimplefinFirstPullFailed:
            import logging
            logging.getLogger("oikonome.simplefin").exception(
                "setup simplefin first pull failed")
            sync_error = SIMPLEFIN_PULL_RETRYING
        except Exception:  # noqa: BLE001 — wizard must complete
            import logging
            logging.getLogger("oikonome.simplefin").exception(
                "setup simplefin connect failed")
            sync_error = ("Bank connection failed. Your account was created "
                          "— retry the token from the Accounts page.")
        finally:
            conn.close()
    plaid_saved = False
    if plaid_client_id and plaid_secret:
        # Stored exactly where the Accounts-page link flow reads them
        # (sync/plaid.credentials: tenant config keys), AFTER the SimpleFIN
        # sync so first-run budget seeding sees any synced history. Never
        # log the values — they are credentials.
        conn = tenancy.tenant_connect(tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["plaid_client_id"] = plaid_client_id
                # encrypt at rest (same as llm_api_key below and the Settings
                # path); sync/plaid.credentials decrypts on read
                cfg["plaid_secret"] = crypto.encrypt(conn, plaid_secret)
                cfg["plaid_env"] = (plaid_env if plaid_env in PLAID_ENVS
                                    else "production")
                # when the keys were saved, for the bank-link hold's banner
                # (written by the server only)
                cfg["plaid_keys_saved_at"] = dt.datetime.now(
                    dt.timezone.utc).isoformat()
        finally:
            conn.close()
        plaid_saved = True
    llm_saved = False
    if llm_url:
        # Read by the worker's nightly llm_categorize run via
        # budget.load_config; tenant config wins over the operator's
        # OIKONOME_LLM_* env. The key is a credential: encrypted at rest
        # (db/crypto, same as aggregator tokens) and never logged.
        conn = tenancy.tenant_connect(tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["llm_url"] = llm_url
                cfg["llm_model"] = llm_model
                if llm_api_key:
                    cfg["llm_api_key"] = crypto.encrypt(conn, llm_api_key)
        finally:
            conn.close()
        llm_saved = True
    return {"tenant_id": tid, "user_id": str(row["id"]), "sync": sync_result,
            "sync_error": sync_error, "plaid_saved": plaid_saved,
            "llm_saved": llm_saved}
