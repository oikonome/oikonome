"""Zombie-Item reaper + orphan reconcile (daily).

A Plaid Item bills monthly for as long as it exists at Plaid's side —
disconnect calls /item/remove, but an Item stuck in an unrecoverable
error (login expired, consent revoked) just keeps failing the hourly
sync and keeps billing. The reaper releases Items that have been failing
for OIKONOME_PLAID_REAP_DAYS consecutive days (default 30; 0 disables
the whole job), archives them locally with archived_reason='auto-reap',
and stamps the slot ledger. The user is told twice: a persistent
"reconnect" state on the Accounts page (pages._connections serves
reaped items with status 'reaped') and a one-line notice in the daily
email / Today alert strip (engine.alerts.reaped_connections).

Orphan reconcile, same job: an Item can vanish at Plaid without the
instance hearing (support removal, USER_PERMISSION_REVOKED webhook
lost) — the DB row then fails forever without ever being "in error 30
days" cleanly.
Every active plaid item with no recent successful sync gets a cheap
/item/get; ITEM_NOT_FOUND means it is gone at Plaid → archive locally
(archived_reason='plaid-gone'), no /item/remove needed.

The OTHER direction cannot be reconciled: Plaid has no list-all-Items
endpoint, so a stray Plaid-side Item with no DB row (billing the
operator's Plaid account with nothing to show) cannot be enumerated via
the API. The admin console's
slot ledger (plaid_item_ledger — one append-only row per Item ever
created, now with removed_at) is the audit surface for that direction:
ledger rows without removed_at should match the live Item count on the
Plaid dashboard.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

from ..db import tenancy
from ..sync import plaid
from ..sync.base import get_access_token
from .link_alert import _is_link_issue

log = logging.getLogger("oikonome.jobs")


def reap_days() -> int:
    """Days of consecutive failure before an Item is released.
    OIKONOME_PLAID_REAP_DAYS, default 30; 0 (or negative, or junk)
    disables the reaper AND the orphan reconcile."""
    try:
        return int(os.environ.get("OIKONOME_PLAID_REAP_DAYS", "30"))
    except ValueError:
        return 0


def _unrecoverable(effective_status: str | None) -> bool:
    """True for states only the USER can fix (re-auth / revoked consent)
    — the reap classes. Institution outages and transient errors are
    excluded on purpose: they heal without the user and must never cost
    a re-link."""
    return _is_link_issue(effective_status) \
        or effective_status in ("revoked", "pending_expiration")


def _log_is_link_issue(error: str | None) -> bool:
    """Whether a sync_log.error string represents an UNRECOVERABLE link
    issue (login expired / consent) vs a transient blip (INSTITUTION_DOWN,
    RATE_LIMIT_EXCEEDED, API_ERROR, network errors). The stored text is a
    Plaid error stringified as '<CODE>: <message> [request_id …]', so the
    leading token is the code; reuse _is_link_issue's class check on it."""
    if not error:
        return False
    code = error.split(":", 1)[0].strip()
    return _is_link_issue("error:" + code)


def _failing_since(conn, item_id: str,
                   webhook_at) -> dt.datetime | None:
    """Start of the CURRENT unbroken UNRECOVERABLE failure streak: the
    earliest link-issue sync_log error after the last successful sync (any
    later success resets the clock). Transient-error days (institution
    outage, rate limit) are EXCLUDED — the
    module promises they never cost a re-link, so they must not accrue the
    reap streak. A 30-day outage that then flips to ITEM_LOGIN_REQUIRED
    anchors on the login-required day, giving the full reconnect window.
    Falls back to webhook_status_at for items whose (unrecoverable) error
    arrived out of band before polling caught up."""
    last_ok = conn.execute(
        "SELECT MAX(ran_at) AS t FROM sync_log WHERE item_id=%s "
        "AND error IS NULL", (item_id,)).fetchone()["t"]
    rows = conn.execute(
        "SELECT ran_at, error FROM sync_log WHERE item_id=%s "
        "AND error IS NOT NULL AND (%s::timestamptz IS NULL OR ran_at > %s)",
        (item_id, last_ok, last_ok)).fetchall()
    link = [r["ran_at"] for r in rows if _log_is_link_issue(r["error"])]
    if link:
        return min(link)
    if webhook_at is not None and (last_ok is None or webhook_at > last_ok):
        return webhook_at
    return None


def _archive(conn, item_id: str, reason: str) -> None:
    # NULL the access_token on release: an archived row with a live token is
    # the "maybe still billing at Plaid" signal (release_tenant_items and the
    # straggler retry pass key on it), so a released Item must shed it or it gets
    # re-probed every run.
    conn.execute(
        "UPDATE items SET status='archived', archived_reason=%s, "
        "archived_at=now(), access_token=NULL WHERE id=%s", (reason, item_id))


# The slot-ledger removal stamp is a shared plaid helper so every
# release path (reaper, user disconnect, tenant erasure) records the audit.
_ledger_removed = plaid.stamp_ledger_removed


def reap(conn, client: plaid.Client | None = None,
         now: dt.datetime | None = None) -> dict:
    """One tenant's reap + reconcile pass on a tenant-scoped conn.
    Returns {'reaped': [...], 'orphaned': [...], 'disabled': bool}."""
    days = reap_days()
    if days <= 0:
        return {"reaped": [], "orphaned": [], "disabled": True}
    now = now or dt.datetime.now(dt.timezone.utc)
    out: dict = {"reaped": [], "orphaned": [], "released_archived": [],
                 "disabled": False}
    items = conn.execute(
        "SELECT id, institution_name, status, webhook_status, "
        "webhook_status_at FROM items WHERE aggregator='plaid' "
        "AND COALESCE(status,'') != 'archived' "
        "AND access_token IS NOT NULL").fetchall()
    # Archived rows that still hold a live token — a user disconnect
    # whose /item/remove failed archived the row but left the Item billing at
    # Plaid, and the candidate query above skips archived. Without this retry
    # it bills indefinitely (only full erasure ever released it).
    stragglers = conn.execute(
        "SELECT id, institution_name FROM items WHERE aggregator='plaid' "
        "AND status='archived' AND access_token IS NOT NULL").fetchall()
    if not items and not stragglers:
        return out
    if client is None:
        try:
            client = plaid.Client.for_tenant(conn)
        except plaid.PlaidError:       # no creds — nothing billable here
            return out
    reaped_ids = set()
    for it in items:
        effective = (None if (it["status"] or "ok") == "ok"
                     else it["status"]) or it["webhook_status"]
        if not _unrecoverable(effective):
            continue
        since = _failing_since(conn, it["id"], it["webhook_status_at"])
        if since is None or (now - since) < dt.timedelta(days=days):
            continue
        # The batch SELECT above is a snapshot. A LOGIN_REPAIRED webhook
        # (plaid_webhook._clear_item_status, writing on its own connection)
        # can heal the Item between that snapshot and this item's turn in
        # the loop — and acting on the stale copy would release at Plaid and
        # archive a connection the person only moments ago finished
        # re-linking, with nothing in the UI to explain it. Re-read the live
        # row FOR UPDATE and re-check before releasing; the row lock also
        # blocks a concurrent heal from landing between this check and the
        # archive below (a plain UPDATE waits on the row lock even though the
        # webhook does not itself say FOR UPDATE).
        with conn.transaction():
            fresh = conn.execute(
                "SELECT status, webhook_status FROM items "
                "WHERE id=%s FOR UPDATE", (it["id"],)).fetchone()
            if fresh is None or (fresh["status"] or "") == "archived":
                continue
            eff = (None if (fresh["status"] or "ok") == "ok"
                   else fresh["status"]) or fresh["webhook_status"]
            if not _unrecoverable(eff):
                continue                          # healed since the snapshot
            # release at Plaid FIRST (this is what stops the billing); a
            # failed release leaves the item for the next daily run rather
            # than archiving a row whose Item still bills
            try:
                token = get_access_token(conn, it["id"])
                if token:
                    try:
                        client.remove_item(token)
                    except plaid.PlaidError as e:
                        if e.code != "ITEM_NOT_FOUND":   # already gone = done
                            raise
            except Exception as e:                         # noqa: BLE001
                log.warning("plaid reap release failed item=%s: %s",
                            it["id"], e)
                continue
            _archive(conn, it["id"], "auto-reap")
        _ledger_removed(it["id"], "auto-reap")
        reaped_ids.add(it["id"])
        out["reaped"].append(it["institution_name"] or it["id"])
        log.info("plaid reap: released item=%s (%s) after %s+ days of %s",
                 it["id"], it["institution_name"], days, effective)
    # ---- orphan reconcile (DB → Plaid direction only; see module doc) ----
    # A recent successful sync proves the Item exists — only quiet items
    # pay the /item/get (a free endpoint), so the healthy steady state
    # makes zero extra API calls.
    for it in items:
        if it["id"] in reaped_ids:
            continue
        last_ok = conn.execute(
            "SELECT MAX(ran_at) AS t FROM sync_log WHERE item_id=%s "
            "AND error IS NULL", (it["id"],)).fetchone()["t"]
        if last_ok is not None and (now - last_ok) < dt.timedelta(days=1):
            continue
        token = get_access_token(conn, it["id"])
        if not token:
            continue
        try:
            client.get_item(token)
        except plaid.PlaidError as e:
            if e.code == "ITEM_NOT_FOUND":
                _archive(conn, it["id"], "plaid-gone")
                _ledger_removed(it["id"], "plaid-gone")
                out["orphaned"].append(it["institution_name"] or it["id"])
                log.info("plaid reconcile: item=%s (%s) gone at Plaid — "
                         "archived", it["id"], it["institution_name"])
            # any other error: the hourly sync's problem, not ours
        except Exception as e:                             # noqa: BLE001
            log.warning("plaid reconcile probe failed item=%s: %s",
                        it["id"], e)
    # ---- retry release for archived items that still hold a token -----------
    for it in stragglers:
        token = get_access_token(conn, it["id"])
        if not token:
            continue
        try:
            try:
                client.remove_item(token)
            except plaid.PlaidError as e:
                if e.code != "ITEM_NOT_FOUND":     # already gone = done
                    raise
        except Exception as e:                             # noqa: BLE001
            log.warning("plaid archived-release retry failed item=%s: %s",
                        it["id"], e)
            continue
        # success (or already gone): shed the token + stamp the audit ledger
        conn.execute("UPDATE items SET access_token=NULL WHERE id=%s",
                     (it["id"],))
        _ledger_removed(it["id"], "disconnect-retry")
        out["released_archived"].append(it["institution_name"] or it["id"])
        log.info("plaid archived-release: freed stray token item=%s (%s)",
                 it["id"], it["institution_name"])
    return out


def reap_tenant(tenant_id: str) -> dict:
    """Worker entry point — one tenant, own connection.

    Takes the SAME per-tenant lock `sync_tenant` holds. Both
    sweeps write `items.status` and both read `access_token`, and
    `nightly_all` (04:00) fires at the same instant as the hourly
    `sync_all` (:00). Interleaved, the reaper releases an Item at Plaid and
    archives the row while an in-flight sync is mid-pull on that same Item —
    and the sync's own error-status write then lands ON TOP of 'archived',
    un-archiving a connection that no longer exists upstream. It would then
    be picked up by every subsequent sweep and fail forever.

    A TRY lock, skipping rather than waiting: reaping is a daily best-effort
    sweep of Items already dead 30+ days, so deferring a tenant to tomorrow
    costs one extra day of Plaid billing on an Item that has been dead a
    month. Blocking a worker thread behind a multi-minute sync costs more."""
    conn = tenancy.tenant_connect(tenant_id)
    try:
        if not conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                            (f"oikonome:sync:{tenant_id}",)).fetchone()["ok"]:
            log.info("sync in flight for tenant %s — deferring reap",
                     tenant_id)
            return {"skipped": "sync-in-flight"}
        try:
            return reap(conn)
        finally:
            tenancy.release_lock(conn, f"oikonome:sync:{tenant_id}")
    finally:
        conn.close()
