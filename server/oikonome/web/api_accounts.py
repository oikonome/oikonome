"""Accounts, provider keys, connections, links and sync jobs.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import datetime as dt
import os
from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from . import data, demoguard
from ..db import tenancy
from ..engine import activity as _activity
from ..engine import budget as _budget_rules
from ..engine import forecast as _forecast_rules
from ..envnum import env_flag
from ..jobs.sync_row import SYNC_LOCK_HELD_SQL, sync_row_live, SYNC_STALE_AFTER
from .api_common import _conn, _label_field, _literal_true, router, _ser, _user
from .fields import Fields, json_fields
from .schemas import documented, Ok
from .schemas.accounts import (AccountAdd, AccountAdded, AccountBalance,
                               AccountClassify, AccountExclude,
                               AccountExcluded, AccountHidden, AccountHide,
                               AccountLinkCreate, AccountLinkCreated,
                               AccountLinkDismiss, AccountLinkOrder,
                               AccountLinks, AccountRemove, AccountRemoved,
                               AccountRename, AccountRenamed, AccountsData,
                               ConnectionDisconnected, ConnectionReconnect,
                               Connections, ConnectionsExport,
                               ConnectionsImported, Holdings, KeysSaved,
                               MxConnect, MxKeys, MxKeysCleared, MxSynced,
                               PlaidKeys, PlaidKeysCleared, PlaidValidate,
                               SimplefinClaim, SimplefinConnected, SyncRan,
                               SyncStarted, SyncStatus)
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


def goal_destination(r: dict) -> bool:
    """Whether a savings goal can be pointed at this account: open (a
    balance, not hidden), the serving copy of a linked account, and one
    whose ledger can show money arriving — a checking, savings or other
    cash account, or an investment account with ledger rows (a holdings-
    only account never shows a transfer, so a goal on it never moves).
    The goal pickers list these, the one already chosen aside."""
    if r.get("balance_current") is None or r.get("user_removed_at"):
        return False
    link = r.get("link")
    if link and not link.get("primary"):
        return False
    acct_type = str(r.get("kind") or "").split("/", 1)[0]
    if acct_type == "depository":
        return True
    return acct_type == "investment" and bool(r.get("txns"))


@router.get("/accounts")
@documented(response=AccountsData)
def accounts(user: dict = Depends(_user())):
    from ..engine import links
    conn = _conn(user)
    try:
        rows = [_ser(r) for r in data.accounts_detail(conn)]
        from .reporting_api import _household_today
        data.card_due_words(conn, rows, _household_today(conn))
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
            r["goal_destination"] = goal_destination(r)
            # which of the editor's two switches this account is offered,
            # by the rules the money math itself reads: the exclusion
            # wherever a row can be spending, the pin on the type the
            # forecast picks among
            typ = (r.get("kind") or "").split("/")[0]
            r["can_exclude"] = _budget_rules.account_counts_as_spending(typ)
            r["can_pin"] = _forecast_rules.pin_offered(typ)
        # the account the cash forecast starts from — the pin when it is
        # live, else the same fall-through pick — named once here so the
        # ledger can head itself with that balance without a forecast walk
        from ..engine import budget, forecast
        _cfg = budget.load_config(conn)
        primary = forecast.checking_pick(conn, _cfg)[0]
        # why a saved pin is not that account, in the page's words — the
        # clients render this rather than re-derive the forecast's rules
        from ..engine import reporting as _reporting
        return {"accounts": rows, "primary_checking_id": primary,
                "primary_pin_unused": forecast.pin_unused_reason(
                    conn, _cfg, primary),
                # the page's headline and subtotals, server-decided: the
                # headline is Net Worth's Financial accounts figure
                "totals": _reporting.account_totals(conn)}
    finally:
        conn.close()


# ---- provider setup: MX + live key validation ----------

def _step_up_fields(body: Fields) -> dict:
    """The step-up proof a credential door may carry in its body (scripts
    and older builds send it there; current clients elevate first), as
    the keyword arguments of _require_elevation. Read exactly as typed —
    a password is never trimmed."""
    return {k: body.text(k, strip=False)
            for k in ("password", "totp_code", "recovery_code")}


@router.post("/accounts/mx/keys")
@documented(response=KeysSaved, body=MxKeys)
def mx_keys(user: dict = Depends(_user()),
            body: Fields = Depends(json_fields)):
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
    _require_elevation(user, **_step_up_fields(body))
    # the mode answer comes after the step-up: an unelevated caller
    # learns nothing about the instance from this door
    _no_byo_on_hosted()
    from ..sync import mx as mx_mod
    env = body.one_of("env", tuple(mx_mod.BASES), default="production",
                      msg="env must be production or sandbox")
    client_id = body.text("client_id")
    api_key = body.text("api_key")
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
@documented(response=MxConnect)
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
@documented(response=MxSynced)
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
@documented(response=KeysSaved, body=PlaidKeys)
def plaid_keys(user: dict = Depends(_user()),
               body: Fields = Depends(json_fields)):
    """Save BYO Plaid credentials after a LIVE validation —
    the guided flow's final step. Secret encrypted at rest. A step-up
    door written under the factor lock, for the reason the MX one is."""
    demoguard.deny(user)
    demoguard.deny_shared_grant(user)
    from .app import _require_elevation
    _require_elevation(user, **_step_up_fields(body))
    # the mode answer comes after the step-up: an unelevated caller
    # learns nothing about the instance from this door
    _no_byo_on_hosted()
    from .app import _still_signed_in_under_factor_lock
    from ..db import crypto
    from ..engine import budget
    from ..sync import plaid as plaid_mod
    r = plaid_validate(user=user, body=body)     # raises 400 on bad keys
    new_id = body.text("client_id")
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
                    conn, body.text("secret"))
                cfg["plaid_env"] = r["env"]
                # the bank-link hold's banner says when (server-written)
                cfg["plaid_keys_saved_at"] = _keys_saved_now()
    finally:
        conn.close()
    return {"ok": True, "env": r["env"]}


@router.delete("/accounts/plaid/keys")
@documented(response=PlaidKeysCleared)
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
    from ..sync import plaid as plaid_mod

    def _instance_env():
        # never read as production: an unrecognised instance environment is
        # compared as the text it is, so it differs from any real one and
        # the keys count as moved (the straggler release then goes through
        # the release client) — saving a household's own keys, which carry
        # their own environment, is not refused for it
        try:
            return plaid_mod.instance_env()
        except plaid_mod.PlaidError:
            return "unrecognised:" + (os.environ.get("OIKONOME_PLAID_ENV")
                                      or "")
    had_env = ((cur.get("plaid_env") if cur.get("plaid_client_id") else None)
               or _instance_env())
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
        # the release client: a mistyped bank environment must not leave
        # the Items subscribed with no handle left to release them by
        client = plaid_mod._release_client(conn)
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
@documented(response=MxKeysCleared)
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
@documented(body=ConnectionsExport)
def connections_export(user: dict = Depends(_user()),
                       body: Fields = Depends(json_fields)):
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
    _require_elevation(user, fresh_seconds=60, **_step_up_fields(body))
    # In hosted mode every bank link was made under the operator's provider
    # keys: its access tokens must stay on the server, and the import side
    # drops them anyway, so the bundle has no use there. Refused here, not
    # only hidden in the app, because the file cannot be recalled once made.
    if env_flag("OIKONOME_HOSTED"):
        raise HTTPException(
            403, "in hosted mode bank connections run under the operator's "
                 "provider keys and stay on the server — there is no "
                 "connections bundle to export")
    # exactly as typed: the passphrase is the bundle's only lock
    passphrase = body.text("passphrase", strip=False)
    conn = _conn(user)
    try:
        try:
            blob = cb.export_bytes(conn, passphrase)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()
    stamp = f"{dt.datetime.now():%Y%m%d}"
    from .app import _notify_security_change
    _notify_security_change(user, "connections_exported")
    return Response(
        blob, media_type="application/octet-stream",
        headers={"Content-Disposition":
                 f'attachment; filename="oikonome-config-{stamp}.oikx"'})


@router.post("/connections/import",
             dependencies=[Depends(limit("connections_bundle", 5, 3600))])
@documented(response=ConnectionsImported)
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
    # cost from the file, and every address the file names is resolved
    # here, inside one lookup budget, so the write below — which checks
    # them again under the factor lock (netguard.no_lookups) — answers
    # from these lookups.
    from . import netguard as _ng
    try:
        payload = _json.loads(cb._open(data, passphrase))
        if isinstance(payload, dict):
            config, _secrets, _items = cb._checked_shapes(
                copy.deepcopy(payload))
            with _ng.lookup_budget(cb._MAX_BACKENDS + 2 + 8, seconds=8.0):
                cb._check_urls(config)
                cb._clean_backends(copy.deepcopy(payload.get("llm_backends")))
                if not env_flag("OIKONOME_HOSTED"):
                    # hosted drops a bundle's connections unread
                    for _it in _items:
                        if isinstance(_it, dict):
                            cb._check_item(_it)
    except (ValueError, _ng.BlockedURL) as e:
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
    from .app import _notify_security_change
    _notify_security_change(user, "connections_imported")
    return {"ok": True, **summary}


@router.post("/accounts/plaid/validate",
             dependencies=[Depends(limit("plaid_validate", 10, 600))])
@documented(response=KeysSaved, body=PlaidValidate)
def plaid_validate(user: dict = Depends(_user()),
                   body: Fields = Depends(json_fields)):
    """Live Plaid key check for the guided setup flow: one
    cheap authenticated call; nothing is saved here."""
    demoguard.deny(user)
    _no_byo_on_hosted()
    import httpx as _httpx

    from ..sync import plaid as plaid_mod
    env = body.one_of("env", tuple(plaid_mod.ENV_URLS), default="production",
                      msg="env must be production or sandbox")
    base = plaid_mod.ENV_URLS[env]
    client_id = body.text("client_id")
    secret = body.text("secret")
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
@documented(response=AccountLinks)
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


# A link group is a handful of sources for one account; the bound only keeps
# a hostile list from fanning out into per-id work.
_LINK_IDS_MAX = 1000


def _link_account_ids(body: Fields) -> list[str]:
    """The account ids a link door was sent: a list of strings (absent or
    null is empty, and the engine names what is wrong with too few)."""
    return body.list_of("account_ids", str, _LINK_IDS_MAX, blank_ok=True,
                        msg=f"account_ids must be a list of at most "
                            f"{_LINK_IDS_MAX} account ids") or []


@router.post("/accounts/links")
@documented(response=AccountLinkCreated, body=AccountLinkCreate)
def account_links_create(user: dict = Depends(_user()),
                         body: Fields = Depends(json_fields)):
    from ..engine import links
    ids = _link_account_ids(body)
    auto_order = body.flag("auto_order", default=False)
    conn = _conn(user)
    try:
        if auto_order:
            ids = links.default_order(conn, ids)
        try:
            gid = links.create(conn, ids)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"group_id": gid}
    finally:
        conn.close()


@router.delete("/accounts/links/{group_id}")
@documented(response=Ok)
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
@documented(response=Ok, body=AccountLinkOrder)
def account_links_order(group_id: str, user: dict = Depends(_user()),
                        body: Fields = Depends(json_fields)):
    from ..engine import links
    ids = _link_account_ids(body)
    conn = _conn(user)
    try:
        try:
            links.reorder(conn, group_id, ids)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}
    finally:
        conn.close()


@router.post("/accounts/links/dismiss")
@documented(response=Ok, body=AccountLinkDismiss)
def account_links_dismiss(user: dict = Depends(_user()),
                          body: Fields = Depends(json_fields)):
    """Remember a rejected suggestion so it stops nagging — or, with
    `card`, put the standing linked-sources card away (false shows it
    again). Linking and unlinking live in each account's editor, so the
    card carries no action the owner would lose by hiding it."""
    from ..engine import budget
    card = body.flag("card")
    # the suggestion's own key, echoed back exactly as the list gave it
    key = body.text("key", 400, strip=False)
    if card is None and "|" not in key:
        raise HTTPException(422, "key required")
    conn = _conn(user)
    try:
        # the list lives in the settings document every request reads, so it
        # keeps only keys the suggestion list could have offered: two
        # distinct accounts this household has
        if card is None:
            ids = key.split("|")
            n = conn.execute("SELECT count(*) AS n FROM accounts "
                             "WHERE id = ANY(%s)", (ids,)).fetchone()["n"]
            if len(ids) != 2 or ids[0] == ids[1] or n != 2:
                raise HTTPException(404, "no such suggestion")
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


# ---- accounts management (SPA Accounts page) --------------------------------

@router.get("/connections")
@documented(response=Connections)
def connections_api(user: dict = Depends(_user())):
    """Aggregator connections with health (status, last successful pull) +
    the header's relative "synced Xm ago" chip — the Accounts page's
    status dots and last_sync, as JSON for the SPA."""
    from .pages import _connections
    conn = _conn(user)
    try:
        rows = [_ser(r) for r in _connections(conn)]
        # not max(last_ok) over the rows above: the chip also counts the
        # log's bookkeeping rows and items the list leaves out. Written
        # as newest-first LIMIT 1 so the time-ordered partial index
        # answers it without reading the whole log.
        last = conn.execute(
            "SELECT ran_at AS t FROM sync_log WHERE error IS NULL "
            "ORDER BY ran_at DESC LIMIT 1"
        ).fetchone()
        # is a sync running RIGHT NOW, whoever started it. Folded
        # into this endpoint rather than given its own, because the header
        # already polls this one every 60s — a separate /syncing route would
        # have doubled the app's idle request rate to answer a question this
        # response was already 90% of the way to answering.
        #
        # Same test as the claim in /jobs/sync/start — see jobs/sync_row.py.
        # A deploy that restarts the container mid-sync
        # leaves state='running' with no owner, and these two must agree
        # about when that row stops meaning anything: two different tests
        # leave this endpoint reporting no sync running while the other
        # refuses to start one, and a click does nothing with nothing to
        # explain it.
        job = conn.execute(
            "SELECT state, progress, updated_at FROM job_progress "
            "WHERE id='sync'").fetchone()
        job_live = bool(job) and sync_row_live(conn, job)
    finally:
        conn.close()
    syncing, sync_progress = False, None
    if job_live:
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
            "syncing": syncing, "sync_progress": sync_progress,
            # how many need a person's hand — the header chip, the Settings
            # row and the phone all print this one number
            "attention": sum(1 for r in rows if r["needs_attention"])}


@router.get("/holdings")
@documented(response=Holdings)
def holdings_api(user: dict = Depends(_user())):
    """Top investment holdings across accounts — the Accounts page's
    'Top holdings' table (value-ranked, top 60)."""
    from ..engine import budget, links
    conn = _conn(user)
    try:
        # only positions the net-worth figures count: a linked shadow
        # would list every fund twice, a hidden account counts toward
        # nothing, and a business entity's account stays out of the
        # personal view unless the household combines them
        from .reporting_api import _household_today
        rows = conn.execute(
            """SELECT a.id AS account_id,
                      COALESCE(a.display_name, a.name) AS acct,
                      a.balance_current AS bal,
                      h.symbol, h.name AS fund, h.quantity AS qty,
                      h.price, h.value, h.as_of
               FROM holdings h JOIN accounts a ON a.id = h.account_id
               WHERE h.value IS NOT NULL
                 AND NOT (h.account_id = ANY(%s))
                 AND (""" + budget.COMBINE_OR + """a.entity_id IS NULL)
               ORDER BY h.value DESC""",
            (links.shadow_ids(conn),)).fetchall()
        return data.fresh_holdings([dict(r) for r in rows],
                                   _household_today(conn))
    finally:
        conn.close()


@router.post("/accounts/rename")
@documented(response=AccountRenamed, body=AccountRename)
def accounts_rename_api(user: dict = Depends(_user()),
                        body: Fields = Depends(json_fields)):
    """User rename — display_name override that sync never touches. An
    empty name reverts to the aggregator's name."""
    account_id = body.text("account_id")
    if not account_id:
        raise HTTPException(400, "account_id required")
    name = _label_field(body, "name")
    conn = _conn(user)
    try:
        prev = conn.execute(
            "SELECT COALESCE(NULLIF(display_name, ''), name) AS name "
            "FROM accounts WHERE id=%s", (account_id,)).fetchone()
        if not data.set_account_display_name(conn, account_id, name):
            raise HTTPException(404, "account not found")
        now = conn.execute(
            "SELECT COALESCE(NULLIF(display_name, ''), name) AS name "
            "FROM accounts WHERE id=%s", (account_id,)).fetchone()
        if prev and now and prev["name"] != now["name"]:
            # the label before this rename may have been stored by a door
            # that did not cut a typed card number; the activity log is
            # exported, so it is cut here rather than copied whole
            from ..sync.account_sources import (
                stored_account_name as _cut_name)
            before = _cut_name(prev["name"]) if prev["name"] else prev["name"]
            _activity.record(conn, user, "account", "renamed",
                             target=account_id, label=before or account_id,
                             detail={"before": before,
                                     "after": now["name"]})
        # the stored label, which the cut may have changed from the typed
        # one: a client caches and shows this, never what was typed
        return {"ok": True, "name": now["name"] if now else None}
    finally:
        conn.close()


@router.post("/accounts/classify")
@documented(response=Ok, body=AccountClassify)
def accounts_classify_api(user: dict = Depends(_user()),
                          body: Fields = Depends(json_fields)):
    from ..engine import budget
    from ..sync import base as sync_base
    kind = body.text("kind")
    account_id = body.text("account_id")
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
        # absent = leave the anchor alone; null, as before, unpins
        pin = (bool(body.flag("primary_checking", default=False))
               if body.present("primary_checking") else None)
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
            # a pin the forecast will not honour (a disconnected bank, a
            # business account outside the combined view, an excluded or
            # backup account) is refused, not saved — the one rule every
            # pin door answers with (forecast.pin_refusal)
            if pin:
                from ..engine import forecast as _forecast
                why = _forecast.pin_refusal(conn, cfg, account_id)
                if why:
                    raise HTTPException(400, why)
            sync_base.mark_type_user_set(conn, account_id, typ, sub or None)
            if pin:
                cfg["checking_account_id"] = account_id
            elif pin is False and cfg.get("checking_account_id") == account_id:
                cfg.pop("checking_account_id")
        return {"ok": True}
    finally:
        conn.close()


@router.post("/accounts/exclude")
@documented(response=AccountExcluded, body=AccountExclude)
def accounts_exclude_api(user: dict = Depends(_user()),
                         body: Fields = Depends(json_fields)):
    """Toggle an account's budget exclusion — config 'excluded_accounts'
    (account-id list); the engine's _spend_rows skips those accounts, so
    their transactions never count as spend (the Accounts page's 'excl'
    toggle)."""
    from ..engine import budget
    aid = body.text("account_id", strip=False)
    if not aid:
        raise HTTPException(400, "account_id required")
    # sent (null included) = that state; absent = flip the current one
    want_sent = (bool(body.flag("excluded", default=False))
                 if body.present("excluded") else None)
    conn = _conn(user)
    try:
        with budget.config_txn(conn) as cfg:
            # the existence check sits inside the settings transaction and
            # locks the row (settings first, then accounts — the order
            # every door takes): a reconnect adoption renaming this id
            # holds the row, and once it commits the id is gone. Checked
            # outside, the door read the rewritten list, appended the dead
            # id and answered excluded while the renamed account counted on.
            if not conn.execute(
                    "SELECT 1 FROM accounts WHERE id=%s FOR UPDATE",
                    (aid,)).fetchone():
                raise HTTPException(404, "no such account")
            cur = list(cfg.get("excluded_accounts") or [])
            want = want_sent if want_sent is not None else aid not in cur
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
@documented(response=AccountAdded, body=AccountAdd)
def accounts_add_api(user: dict = Depends(_user()),
                     body: Fields = Depends(json_fields)):
    name = _label_field(body, "name")
    if not name:
        raise HTTPException(400, "name required")
    kind = body.text("kind", default="depository/checking")
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
    bal = body.money("balance", default=0.0)       # "" and absent open at 0
    # a name spelling a full card number ("Visa 4111…") keeps only its
    # last four in the stored name, and the id is opaque, minted under the
    # household's account lock (bulk_import.create_manual_account)
    from .bulk_import import create_manual_account
    conn = _conn(user)
    try:
        aid = create_manual_account(conn, name, typ, sub or None, bal)
        return {"ok": True, "account_id": aid}
    finally:
        conn.close()


@router.post("/accounts/balance")
@documented(response=Ok, body=AccountBalance)
def accounts_balance_api(user: dict = Depends(_user()),
                         body: Fields = Depends(json_fields)):
    aid = body.text("account_id", strip=False)
    conn = _conn(user)
    try:
        # an id a migrate moved off a full number reaches the account it
        # moved to, as the import door's does (bulk_import.moved_account)
        from .bulk_import import moved_account
        found = moved_account(conn, aid) if aid else []
        if len(found) == 1:
            aid = found[0]
        row = conn.execute(
            "SELECT i.aggregator FROM accounts a JOIN items i ON i.id=a.item_id "
            "WHERE a.id=%s", (aid,)).fetchone()
        if not row:
            raise HTTPException(404, "no such account")
        if row["aggregator"] != "manual":
            raise HTTPException(400, "balance is synced for connected accounts")
        # blank reads as 0, as a typed money field always has; null was
        # never a balance here
        if body.json_type("balance") == "null":
            raise HTTPException(400, "balance must be a number")
        val = body.money("balance", default=0.0)
        # a balance typed in is as fresh as a pull (the form door stamps
        # it too): the cash forecast grades a night by it
        conn.execute("UPDATE accounts SET balance_current=%s, "
                     "balance_available=%s, updated_at=now() WHERE id=%s",
                     (val, val, aid))
        return {"ok": True}
    finally:
        conn.close()


@router.post("/accounts/simplefin")
@documented(response=SimplefinConnected, body=SimplefinClaim)
def accounts_simplefin_api(user: dict = Depends(_user()),
                           body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    _no_byo_on_hosted()
    token = body.text("token")
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


# ---- maintenance jobs (SPA Settings "Maintenance" card) ---------------------
# On-demand job triggers: the worker's per-tenant task bodies, run
# synchronously for the caller's own tenant. Lightly rate-limited — these
# hit aggregators / send real mail.


@router.post("/jobs/sync", dependencies=[Depends(limit("jobs-sync", 6, 600))])
@documented(response=SyncRan)
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
@documented(response=ConnectionDisconnected)
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
@documented(response=ConnectionReconnect)
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
@documented(response=AccountHidden, body=AccountHide)
def account_set_hidden(account_id: str, user: dict = Depends(_user()),
                       body: Fields = Depends(json_fields)):
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
    # absent or null unhides, as it always has; a string is refused rather
    # than read as truthy, so "false" can never hide an account
    hidden = bool(body.flag("hidden", default=False))
    conn = _conn(user)
    try:
        # check and write in ONE transaction under the row lock: a
        # reconnect adoption renaming this id holds the row, and once it
        # commits the old id is gone. A bare check first let the UPDATE
        # match nothing after the rename and still answer "hidden" while
        # the renamed account went on counting.
        with conn.transaction():
            row = conn.execute(
                "SELECT id, name FROM accounts WHERE id=%s FOR UPDATE",
                (account_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "no such account")
            refills = _account_balance_refills(conn, account_id)
            if hidden:
                _hide_account(conn, account_id)
            else:
                # a fed account's balances stay NULL until the next sync
                # refreshes them — the aggregator is the only source of
                # truth for a live balance. A manual/imported one was
                # never nulled, so it comes back with the number it had.
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
@documented(response=AccountRemoved, body=AccountRemove)
def accounts_remove_api(account_id: str, user: dict = Depends(_user()),
                        body: Fields = Depends(json_fields)):
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
    mode = body.text("mode").lower()
    # the owner's answer to the refusal below: delete the rows kept only
    # by a note that names no feed account
    force_kept = _literal_true(body, "force_kept")
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
            # check and write under the row lock, as /hidden does: a
            # reconnect adoption renaming this id commits with the old row
            # gone, and an unlocked write would match nothing yet answer
            # "hidden" while the renamed account went on counting
            with conn.transaction():
                if conn.execute("SELECT 1 FROM accounts WHERE id=%s "
                                "FOR UPDATE", (account_id,)).fetchone() is None:
                    raise HTTPException(404, "no such account")
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


# jobs/sync_row.py says when a job_progress row has lost its owner: unheard
# from for SYNC_STALE_AFTER and nobody holding the sync lock. ONE test, because
# the claim below and the liveness reads must agree: two different tests leave
# /api/connections reporting no sync running while this endpoint refuses to
# start one, because the same orphaned row still looks alive to it. A click
# does nothing, with nothing to say why.
#
# The claim is still not the real guard. sync_tenant holds a Postgres advisory
# lock for the whole pull, and a second run that gets past this check returns
# immediately on that lock.
# This row is bookkeeping; the lock is the mutex.


@router.post("/jobs/sync/start",
             dependencies=[Depends(limit("jobs-sync", 6, 600))])
@documented(response=SyncStarted)
def jobs_sync_start(user: dict = Depends(_user())):
    """Kick off the full-tenant sync in the background; returns at once.
    One running job per tenant — a stale 'running' row (no update for
    SYNC_STALE_AFTER and its lock gone: a died worker) is reclaimed."""
    demoguard.deny(user)
    import threading

    from ..engine.compat import jsonb
    tid = str(user["tenant_id"])
    conn = _conn(user)

    def claim():
        return conn.execute(
            f"""INSERT INTO job_progress (id, state, progress)
               VALUES ('sync', 'running', '{{}}'::jsonb)
               ON CONFLICT (tenant_id, id) DO UPDATE SET
                   state='running', progress='{{}}'::jsonb,
                   started_at=now(), updated_at=now()
               WHERE job_progress.state != 'running'
                  OR (job_progress.updated_at < now() - %s::interval
                      AND NOT {SYNC_LOCK_HELD_SQL})
               RETURNING id""", (SYNC_STALE_AFTER,)).fetchone()
    try:
        claimed = claim()
        if claimed is None and conn.execute(
                "SELECT 1 FROM job_progress WHERE id='sync' "
                "AND updated_at < now() - %s::interval",
                (SYNC_STALE_AFTER,)).fetchone():
            # A quiet row kept only by the lock: its holder may be no sync
            # at all (an import, a disconnect, a restore), which lets go
            # without pulling. Leave the ask behind, as sync_tenant does
            # when it finds the lock held, so the holder queues it.
            from ..jobs import worker
            worker._nudge_sync(conn)
            # The holder answers only AFTER it lets go, so one that let go
            # between the claim and the nudge never sees it: look again,
            # and with the lock free take the ask back and claim the row.
            if not conn.execute(
                    f"SELECT {SYNC_LOCK_HELD_SQL} AS held").fetchone()["held"]:
                # only an ask still there is ours to take back: one the
                # holder already answered means it queued the sync itself
                if worker._claim_sync_nudge(conn):
                    claimed = claim()
                    if claimed is None:
                        # someone took the lock in between: the ask stands
                        worker._nudge_sync(conn)
    finally:
        conn.close()
    if claimed is None:
        return {"ok": True, "started": False, "reason": "already running"}

    def _run():
        from ..jobs import worker
        snap: dict = {"items": [], "categorize": None}
        order: dict[str, int] = {}
        wconn = tenancy.tenant_connect(tid)
        # The started_at this run's own pull stamped (sync_tenant resets it
        # under the lock, before the first callback). The settle below runs
        # after the lock is let go, when a newer run may already own the
        # row; scoping to this value leaves that run's row alone.
        own: dict = {}

        def _mine() -> tuple[str, tuple]:
            if "started_at" in own:
                return " AND started_at=%s", (own["started_at"],)
            return "", ()

        def _write():
            # MERGE, don't assign. sync_tenant writes {done,total} into this
            # same row so that any sync — cron included — can be reported as
            # live; a plain assignment here fires on every item callback and
            # wipes those keys within a second of them being written, so the
            # header chip could only ever say "syncing…" and never
            # "syncing 3 of 7".
            where, args = _mine()
            row = wconn.execute(
                "UPDATE job_progress "
                "SET progress=COALESCE(progress,'{}'::jsonb) || %s::jsonb, "
                "    updated_at=now() WHERE id='sync'" + where
                + " RETURNING started_at", (jsonb(snap), *args)).fetchone()
            if row is not None:
                own.setdefault("started_at", row["started_at"])

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
            where, args = _mine()
            wconn.execute(
                "UPDATE job_progress SET state='done', progress=%s, "
                "updated_at=now() WHERE id='sync'" + where,
                (jsonb({**snap, "ok": ok, "results": results}), *args))
        except Exception as e:                   # noqa: BLE001
            try:
                where, args = _mine()
                wconn.execute(
                    "UPDATE job_progress SET state='error', progress=%s, "
                    "updated_at=now() WHERE id='sync'" + where,
                    (jsonb({**snap,
                            "error": f"{type(e).__name__}: {e}"}), *args))
            except Exception:                    # noqa: BLE001
                pass
        finally:
            wconn.close()

    threading.Thread(target=_run, daemon=True,
                     name=f"wizard-sync-{tid[:8]}").start()
    return {"ok": True, "started": True}


@router.get("/jobs/sync/status")
@documented(response=SyncStatus)
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
            # never synced: the documented shape, with no times to give
            return {"state": "none", "progress": {}, "started_at": None,
                    "updated_at": None}
        progress = as_dict(row["progress"]) or {}
        state = row["state"]
        if state == "running" and not sync_row_live(conn, row):
            # the run died without settling its row, and the start door will
            # already take a new one: "running" here would hold the client's
            # button disabled over a sync that no longer exists
            state = "error"
            progress = {**progress,
                        "error": "it stopped before finishing — sync again"}
        items = progress.get("items") or []
        if items and state in ("done", "error"):
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
        return {"state": state,
                "progress": progress,
                "started_at": row["started_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat()}
    finally:
        conn.close()
