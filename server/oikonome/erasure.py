"""External-service release on tenant erasure (the CCPA/GDPR sweep).

Both erasure doors — the self-serve ``POST /api/account/delete`` and the
admin console's tenant-delete — call :func:`release_external` BEFORE
``tenancy.delete_tenant_rows``, so nothing long-lived outlives the
tenant at a third party once the instance's own rows are gone:

- **Plaid** — ``/item/remove`` on every Item, live and archived
  (``sync/plaid.release_tenant_items``). Also what stops per-Item billing.
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
    results: dict = {}
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
# with the tenant; Plaid retry is dashboard-side, per the erasure contract —
# the marker records the item ids for that lookup, and names the ones that
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
                           # the Items that did not release, by id — what an
                           # operator removes from the Plaid dashboard (the
                           # access tokens die with the rows, so nothing
                           # server-side can call /item/remove for them)
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
