"""External-service release on tenant erasure (the CCPA/GDPR sweep).

Both erasure doors — the self-serve ``POST /api/account/delete`` and the
admin console's tenant-delete — call :func:`release_external` BEFORE
``tenancy.delete_tenant_rows``, so nothing long-lived outlives the
tenant at a third party once the instance's own rows are gone:

- **Plaid** — ``/item/remove`` on every Item, live and archived
  (``sync/plaid.release_tenant_items``). Also what stops per-Item billing.
  An Item whose removal fails is queued with its access token (encrypted
  under the master key) in ``plaid_release_queue`` before the rows go, and
  the nightly ``jobs.reaper.retry_queued_releases`` keeps trying — the
  token dies with the rows otherwise, and nothing could remove the Item.
  Before that, every bank link still open is finished and the Items it
  made are handed straight back
  (``jobs/plaid_links.hand_back_open_sessions``): the session rows go with
  the household, and an Item nobody exchanged would bill unremovably.
- **MX** — delete the platform-side MX *user* (MX cascades its members/
  accounts/transactions — real financial data held at MX)
  (``sync/mx.release_tenant_user``).
- **SimpleFIN / Coinbase / script collectors** — BYO bearer credentials
  with no server-side registration and no revoke API; the encrypted
  copies die with the tenant rows (``api_tokens`` and tenant config are
  both tenant_id-scoped, so ``delete_tenant_rows`` sweeps them), and the
  user revokes at the source. Nothing external to call.
- **SMS / Twilio** — nothing to release, on purpose. The tenant's number
  and its verification/consent record live in tenant config
  (``notify_phone``), which ``delete_tenant_rows`` sweeps. Twilio holds no
  per-tenant object: no Verify service, no number binding, no
  Messaging Service membership (the sender is the platform's, not the
  tenant's), and message SIDs are not stored, so there is no handle to
  redact by. Twilio's own opt-out (STOP) list is keyed by recipient number
  and is a carrier-compliance record that must NOT be cleared by the app. If
  a per-tenant Twilio object is ever created (a Verify service, a
  redaction by stored SID), its release joins this list in the same
  change; the audit line names the channel so its absence is visible.
- **An installed add-on** — anything it holds for the tenant outside the
  database goes through its own release step
  (``ext.gate.release_tenant``), and it contributes its own paragraphs to
  the erasure receipt (``ext.gate.erasure_receipt``) for whatever the
  erasure could not undo on the person's behalf. Identifiers of that kind
  belong to the add-on, so core never reads them.

Every release is best-effort and never raises: a vendor outage must not
block an erasure the user is legally owed. The per-service results come
back as a dict for the caller's audit line.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# Every per-tenant advisory lock a background WRITER holds while it works:
# the sync (hourly sweep, webhook, the manual doors, restore, reaper), the
# categorize pass that runs after a pull or an import, and the nightly
# sweep — and the three hourly/nightly runs that write on their own: the
# email send (it records the run and its verdict line after sending), the
# net-worth snapshot, and the stale-collector alert. The domain tables have
# no FK to tenants, so a writer already in flight when the rows go writes
# them straight back for a tenant that no longer exists. An erasure blocks
# on ALL of them — waiting on the sync lock alone lets any of the others
# carry on writing through the wipe. Each writer only TRY-locks its own,
# so a writer that arrives while the erasure holds them skips, and none
# waits on another's while holding its own, so the fixed order here cannot
# deadlock against them.
# The collector push doors (crypto, plan activity, store orders and
# receipts) write on their own too, from a request authenticated before the
# erasure flipped the tenant's status; `push_may_write` is how they join.
#
# Those try-locks never wait. The locks a writer DOES wait on, inside its
# transaction, are always taken in one order, because two writers taking
# any pair of them the other way round can each hold what the other wants,
# and Postgres then kills one of them:
#   1. the reimbursement-pairing lock (sync.reanchor.hold_pairing_lock);
#   2. the household merchant-identity lock
#      (engine.merchant_identity._lock_identity) — AFTER the pairing lock
#      when a writer needs both, as a ledger import or a crypto push does
#      (the importer pairs receipts and names merchants), and never asked
#      for the first time once a transaction row is held: a rename holds
#      identity while it moves rows, so a writer that restated a row and
#      then asked for identity would wait on a rename waiting on that row;
#   3. the settings row before any accounts row (engine.budget.config_txn),
#      and before the household-member lock (oikonome:tenant-members), which
#      comes before the inviter's invite rows and then users rows: an
#      owner's rotation or recovery pauses the outbound settings
#      (notify.outbound_pause) and only then waits out the invite claims
#      (auth.reset._cancel_invites_and_read_invitees). A user's own
#      per-user factor lock comes first of all, in that user's own door;
#      a REMOVED member's factor lock comes after the household-member
#      lock, taken under it just before that member's rows
#      (auth.reset._remove_members). So only an owner's rotation or
#      recovery ever takes the household-member lock: a member's own door
#      already holds its factor lock and removes nobody, and asking for
#      the member lock there would close a cycle with an owner removing
#      that member;
#   4. bill offers, then bills, then transaction rows
#      (engine.bills._rename_locks);
#   5. a merchant-wide category write and a rename both touch category
#      rules and the rows those rules file, in opposite orders (the write:
#      rule, then rows; the rename: rows, then rules), so each holds the
#      identity lock first and they take turns on it
#      (web.data.set_merchant_category, engine.merchant_dedup.rename).
# A writer that needs a later lock takes the earlier ones it will need
# first, even when it only reaches them near the end of its work.
PUSH_LOCK = "oikonome:push:{}"
WRITER_LOCKS = ("oikonome:sync:{}", "oikonome:sync-categorize:{}",
                "oikonome:nightly:{}", "oikonome:email:{}",
                "oikonome:snapshot:{}", "oikonome:script-alert:{}",
                PUSH_LOCK)


def writer_lock_keys(tenant_id) -> list[str]:
    return [k.format(tenant_id) for k in WRITER_LOCKS]


def hold_writers(conn, tenant_id, *, xact: bool = False) -> None:
    """Wait for every in-flight writer of this tenant to finish, and keep
    new ones out: session-level (released by :func:`release_writers`), or
    held to the end of the current transaction with ``xact=True``. Always
    taken in the same order, so two erasures cannot deadlock each other."""
    fn = "pg_advisory_xact_lock" if xact else "pg_advisory_lock"
    for key in writer_lock_keys(tenant_id):
        conn.execute(f"SELECT {fn}(hashtext(%s))", (key,))


def being_deleted(tenant_id, conn=None) -> bool:
    """True once an erasure of this household has begun: its status was
    flipped to pending_delete, or its row is already gone — or, given a
    connection, the push writer lock is held exclusively, which only an
    erasure does. Read from the control plane, so it is current even
    inside another transaction."""
    if conn is not None:
        got = conn.execute(
            "SELECT pg_try_advisory_lock_shared(hashtext(%s)) AS ok",
            (PUSH_LOCK.format(tenant_id),)).fetchone()
        if not (got["ok"] if isinstance(got, dict) else got[0]):
            return True
        conn.execute("SELECT pg_advisory_unlock_shared(hashtext(%s))",
                     (PUSH_LOCK.format(tenant_id),))
    from .db import tenancy
    cc = tenancy.control_connect()
    try:
        row = cc.execute("SELECT status FROM tenants WHERE id=%s",
                         (str(tenant_id),)).fetchone()
    finally:
        cc.close()
    return not row or row["status"] == "pending_delete"


def push_may_write(conn, tenant_id) -> bool:
    """Call inside a push door's write transaction, before its first write.
    Takes the push writer lock SHARED for the rest of that transaction —
    pushes for one household coexist (a slow store import must not turn
    the next collector's push into a false "being deleted"), while an
    erasure, which takes it exclusive, waits for every push to commit and
    then deletes what it wrote — and answers False (the door replies 409 and writes
    nothing) when an erasure holds the lock or has already begun: the
    tenant's status was flipped to pending_delete, or its row is gone. The
    status is read after the lock is held, because the request was
    authenticated earlier and an erasure flips the status before it takes
    the writer locks; without the re-read, a push authenticated while the
    tenant was active writes the household's rows back under a tenant id
    nothing will ever sweep again (the domain tables have no FK to
    tenants)."""
    got = conn.execute(
        "SELECT pg_try_advisory_xact_lock_shared(hashtext(%s)) AS ok",
        (PUSH_LOCK.format(tenant_id),)).fetchone()
    if not (got["ok"] if isinstance(got, dict) else got[0]):
        return False
    return not being_deleted(tenant_id)


def release_writers(conn, tenant_id) -> None:
    """Undo a session-level :func:`hold_writers`, best-effort per key."""
    for key in reversed(writer_lock_keys(tenant_id)):
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        except Exception:                                # noqa: BLE001
            log.warning("erasure: could not release %s", key)


def release_external(tenant_id) -> dict:
    """Release everything a tenant holds at external services. Returns
    ``{"plaid_items": int, "released": str (the add-on's release word),
    "mx_user": str, "sms": str}`` for the audit trail — plus
    ``"plaid_failed"`` (item ids, or ``"all"``
    when the release itself blew up) whenever fewer Items released than
    the tenant held, so the pending marker gets a retry handle before the
    rows go. Never raises."""
    from . import ext
    from .sync import mx, plaid
    from .jobs import plaid_links
    results: dict = {}
    # first, the bank links still open: an Item the person finished at
    # Plaid but nobody exchanged yet would outlive the session rows the
    # wipe deletes, with nothing able to remove it
    try:
        open_links = plaid_links.hand_back_open_sessions(tenant_id)
        if any(w in ("busy", "error") for w in open_links.values()):
            log.warning("erasure: open bank links of %s not all handed "
                        "back: %s", tenant_id, sorted(open_links.values()))
    except Exception as e:                                 # noqa: BLE001
        log.warning("erasure: open bank links of %s not handed back: %s",
                    tenant_id, e)
    try:
        n = plaid.release_tenant_items(tenant_id)
        results["plaid_items"] = int(n)
        # release_tenant_items swallows per-Item errors and reports a
        # count; the count alone hid a partial failure (released < held),
        # which the wipe then made permanent — the tokens die with the
        # rows, and Plaid keeps billing Items nobody can find. Surface it.
        failed = list(getattr(n, "failed_ids", ()) or ())
        expected = int(getattr(n, "expected", 0) or 0)
        if failed or int(n) < expected:
            results["plaid_failed"] = failed or "all"
    except Exception as e:                                 # noqa: BLE001
        log.warning("erasure: plaid release failed for %s: %s", tenant_id, e)
        results["plaid_items"] = 0
        results["plaid_failed"] = "all"
    try:
        results["released"] = ext.gate.release_tenant(tenant_id)
    except Exception as e:                                 # noqa: BLE001
        log.warning("erasure: add-on release failed for %s: %s", tenant_id, e)
        results["released"] = "error"
    try:
        results["mx_user"] = mx.release_tenant_user(tenant_id)
    except Exception as e:                                 # noqa: BLE001
        log.warning("erasure: mx release failed for %s: %s", tenant_id, e)
        results["mx_user"] = "error"
    # SMS: no per-tenant Twilio object exists to release (module docstring
    # has the full reasoning) — recorded so the audit line shows the
    # channel was considered, not forgotten.
    results["sms"] = "no_external_state"
    return results


# release_external is best-effort, so its RESULT has to be recorded: with
# it discarded and the rows wiped anyway, a vendor outage at delete-time
# leaves live state at a third party with no in-DB identifier, no audit
# line and no retry. So, on BOTH doors,
# (a) audit the per-service result durably (admin_audit survives the wipe),
# and (b) when a vendor release failed, leave a queryable PENDING marker
# carrying the non-secret identifiers an operator/retry needs — before the
# rows (the only copy) are gone. (Plaid access tokens are secrets that die
# with the tenant rows, so the release queues an encrypted copy of each
# failed Item's token in plaid_release_queue for the nightly retry; the
# marker records the item ids for the operator, and names the ones that
# actually failed when the release was partial.)


def _failed_services(results: dict) -> list[str]:
    """Which external releases did NOT cleanly succeed (retry candidates).
    Plaid counts when release_external reports a shortfall (fewer Items
    released than held, or the call itself failed) — without it, a Plaid
    outage at delete-time leaves no marker, and with it no way to find the
    still-billing Items after the wipe."""
    failed = []
    if results.get("plaid_failed"):
        failed.append("plaid")
    addon = str(results.get("released", ""))
    if addon == "error" or "_failed" in addon:
        failed.append("addon")
    if str(results.get("mx_user", "")) == "error":
        failed.append("mx")
    return failed


def _plaid_failed_ids(results: dict, retry_ids: dict | None) -> list:
    failed = results.get("plaid_failed")
    if isinstance(failed, list):
        return failed
    if failed:                       # "all": the release never got going
        return list((retry_ids or {}).get("plaid_item_ids") or [])
    return []


def collect_retry_ids(tenant_id) -> dict:
    """Non-secret identifiers for retrying a failed external release, read
    while the tenant rows still exist. Plaid item ids only, and never an
    access token (a secret). An add-on that holds its own identifiers reads
    them in its ``ext.gate.erasure_receipt``/``on_tenant_purge`` hooks,
    which also run before the rows go."""
    from .db import tenancy
    out: dict = {"plaid_item_ids": []}
    try:
        conn = tenancy.tenant_connect(tenant_id)
    except Exception as e:                                 # noqa: BLE001
        log.warning("erasure: retry-id read failed for %s: %s", tenant_id, e)
        return out
    try:
        rows = conn.execute(
            "SELECT id FROM items WHERE aggregator='plaid'").fetchall()
        out["plaid_item_ids"] = [r["id"] for r in rows]
    except Exception as e:                                 # noqa: BLE001
        log.warning("erasure: retry-id read failed for %s: %s", tenant_id, e)
    finally:
        conn.close()
    return out


def _write_pending_marker(admin, tenant_id, results: dict,
                          retry_ids: dict | None) -> bool:
    """Write the 'external_release_pending' marker (with retry identifiers)
    IFF a vendor release failed. Single source of truth for the marker's SQL
    and JSON shape — the self-serve and admin-console paths must stay in sync
    (the admin console reads this back). Returns True only when a marker was
    actually written, False when there was nothing to record OR the write
    failed (best-effort: never raise)."""
    import json as _json
    failed = _failed_services(results)
    if not failed:
        return False
    try:
        admin.execute(
            "INSERT INTO admin_audit (action, target, detail, ip) "
            "VALUES ('external_release_pending', %s, %s::jsonb, NULL)",
            (str(tenant_id), _json.dumps(
                {"failed": failed, "results": results,
                 "retry": {**(retry_ids or {}),
                           # the Items that did not release, by id. The
                           # release queued each one it held a token for
                           # (plaid_release_queue) and the nightly retry
                           # removes them; an id the queue does not hold
                           # (no token could be read) is one an operator
                           # has to follow up at Plaid
                           "plaid_failed_item_ids":
                               _plaid_failed_ids(results, retry_ids)}})))
        log.warning("erasure: external release incomplete for %s (%s) — "
                    "pending marker recorded", tenant_id, ",".join(failed))
        return True
    except Exception as e:                                 # noqa: BLE001
        log.warning("erasure: pending marker write failed for %s: %s",
                    tenant_id, e)
        return False


def audit_release(admin, tenant_id, results: dict,
                  retry_ids: dict | None = None) -> None:
    """Durably record an erasure's external-release outcome on the admin
    connection (admin_audit is control-plane, no RLS — it survives the
    tenant wipe). Writes a 'self_erase' result row and, when a vendor
    release failed, a distinct 'external_release_pending' marker carrying the
    retry identifiers. Best-effort: an audit failure must not block the
    erasure the user is owed."""
    import json as _json
    try:
        admin.execute(
            "INSERT INTO admin_audit (action, target, detail, ip) "
            "VALUES ('self_erase', %s, %s::jsonb, NULL)",
            (str(tenant_id), _json.dumps({"results": results})))
    except Exception as e:                                 # noqa: BLE001
        log.warning("erasure: audit write failed for %s: %s", tenant_id, e)
    _write_pending_marker(admin, tenant_id, results, retry_ids)


def record_pending_release(admin, tenant_id, results: dict,
                           retry_ids: dict | None = None) -> bool:
    """Write ONLY the pending marker (for the admin console path, which
    already audits the summary itself). Returns True when a marker was
    written, False when nothing was pending or the write failed."""
    return _write_pending_marker(admin, tenant_id, results, retry_ids)
