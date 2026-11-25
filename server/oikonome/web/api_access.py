"""Script tokens and consented support access.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import uuid
from fastapi import Depends, HTTPException

from . import demoguard
from .api_common import (_owner_only, _scalar_text, _ser, _step_up_proof,
                         _user, router)
from .fields import Fields, json_fields
from .schemas import documented, Ok
from .schemas.access import (ApiTokenMint, ApiTokenMinted, ApiTokenRevoke,
                             ApiTokens, SupportAccess, SupportAccessGrant,
                             SupportAccessGranted)
from .security import limit


# ---- script tokens -------------------------------------------------
# Bearer credentials for host-side collector scripts: minted once, shown
# once, revocable; the auth layer restricts them to the import doors.

@router.get("/tokens")
@documented(response=ApiTokens)
def tokens_list_api(user: dict = Depends(_user())):
    from ..auth import api_tokens
    from .app import _control_conn
    _owner_only(user)
    with _control_conn() as conn:
        rows = api_tokens.list_for_tenant(conn, user["tenant_id"])
    return {"tokens": [_ser(r) for r in rows]}


@router.post("/tokens", dependencies=[Depends(limit("tokens", 10, 3600))])
@documented(response=ApiTokenMinted, body=ApiTokenMint)
def tokens_mint_api(user: dict = Depends(_user()),
                    body: Fields = Depends(json_fields)):
    demoguard.deny_shared_grant(user)
    from ..auth import api_tokens
    from .app import _control_conn, _require_elevation
    _owner_only(user)
    # A script token is a long-lived credential that outlives the session
    # that minted it, so a stolen session could mint one for persistence.
    # A password change or reset revokes every token the user minted
    # (password_change, reset.consume), but only once the owner notices
    # and rotates. Password step-up so a hijacked cookie alone can't mint;
    # recovery_code is forwarded, or a passkey-only account could not mint
    # at all. TOTP accounts also owe a live code, exactly like the
    # support-access grant: a token minted on password alone would let a
    # session + password thief sidestep the second factor and keep access
    # until the owner happened to rotate.
    _require_elevation(user, password=_scalar_text(body, "password"),
                       totp_code=_scalar_text(body, "totp_code"),
                       recovery_code=_scalar_text(body, "recovery_code"))
    # push (the default, and what every older client sends) = the import
    # doors; read = the integrations doors. Never both.
    scope = _scalar_text(body, "scope") or "push"
    if scope not in api_tokens.SCOPES:
        raise HTTPException(400, "scope must be push or read")
    from .app import _caller_still_signed_in, _lock_user_factors
    with _control_conn() as conn, conn.transaction():
        # a reset landing during the step-up must not be outlived by the
        # token it was meant to revoke
        _lock_user_factors(conn, user["user_id"])
        _caller_still_signed_in(conn, user)
        token, row = api_tokens.mint(conn, user["tenant_id"],
                                     user.get("user_id"),
                                     _scalar_text(body, "name"), scope)
    from .app import _notify_security_change
    _notify_security_change(user, "api_token_created")
    return {"token": token, **_ser(row)}


@router.post("/tokens/revoke",
             dependencies=[Depends(limit("tokens_revoke", 10, 3600))])
@documented(response=Ok, body=ApiTokenRevoke)
def tokens_revoke_api(user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    from ..auth import api_tokens
    from .app import _control_conn, _require_elevation
    _owner_only(user)
    # Mint steps up; revoke gets the same — a stolen cookie shouldn't be
    # able to silently cut every collector off, and the symmetric door means
    # one rule to reason about, not two. The recovery code is forwarded for
    # passkey-only accounts, same as mint.
    # and the same live TOTP code the mint door demands — symmetric doors,
    # one rule; a hijacked cookie + password must not silence every
    # collector without the second factor.
    _require_elevation(user, password=_scalar_text(body, "password"),
                       totp_code=_scalar_text(body, "totp_code"),
                       recovery_code=_scalar_text(body, "recovery_code"))
    try:
        token_id = str(uuid.UUID(_scalar_text(body, "id")))
    except ValueError:
        raise HTTPException(404, "no such token")
    with _control_conn() as conn:
        ok = api_tokens.revoke(conn, user["tenant_id"], token_id)
    if not ok:
        raise HTTPException(404, "no such token")
    return {"ok": True}


# ---- consented support access ---------------------------------------
# A tenant grants a time-boxed, revocable window during which an operator's
# data-touching console actions (export today; any future data view) are
# permitted-and-audited. Default is NO access — the console shows no tenant
# data without this. Owner-only; the grant rides the tenant's own session.

@router.get("/support-access")
@documented(response=SupportAccess)
def support_access_status_api(user: dict = Depends(_user())):
    from .. import tenant_export as _exp
    from .app import _control_conn
    with _control_conn() as cc:
        st = _exp.consent_status(cc, user["tenant_id"])
    return {"granted": st is not None,
            "expires_at": st["expires_at"].isoformat() if st else None,
            "reason": (st or {}).get("reason")}


@router.post("/support-access",
             dependencies=[Depends(limit("support-access", 10, 3600))])
@documented(response=SupportAccessGranted, body=SupportAccessGrant)
def support_access_grant_api(user: dict = Depends(_user()),
                             body: Fields = Depends(json_fields)):
    """Grant support access for a bounded window (hours, 1–168, default
    24). Revokes any prior live grant first so there is at most one."""
    demoguard.deny_shared_grant(user)
    _owner_only(user)
    # Session alone must not open a support window (stolen cookie
    # → operator data access). Same posture as invites/tokens/passkeys.
    # And a durable grant (up to 168h operator data access)
    # also owes a LIVE TOTP when enrolled — password+session without the
    # authenticator must not be enough to open the support door.
    from .app import _control_conn, _require_elevation
    _require_elevation(user, **_step_up_proof(body))
    # a number, or digits in a string; a fractional one is cut to its whole
    # part as it always was. Absent is the default window; a null or blank
    # one was always refused and still is.
    hours = body.int_in("hours", text_ok=True, truncate=True,
                        msg="hours must be a number")
    if hours is None:
        if body.present("hours"):
            raise HTTPException(400, "hours must be a number")
        hours = 24
    if not 1 <= hours <= 168:
        raise HTTPException(400, "hours must be 1–168")
    # cut, not refused, as it always was; a NUL is dropped, since the
    # column cannot hold one
    reason = body.text("reason", strip=False)[:200].replace("\x00", "")
    from .app import _caller_still_signed_in, _lock_user_factors
    with _control_conn() as conn, conn.transaction():
        # a recovery revokes the grants this account made, under the
        # factor lock; a grant that authenticated before it and wrote
        # after would open the window the recovery just closed
        _lock_user_factors(conn, user["user_id"])
        _caller_still_signed_in(conn, user)
        conn.execute(
            "UPDATE support_consents SET revoked_at=now() "
            "WHERE tenant_id=%s AND revoked_at IS NULL", (user["tenant_id"],))
        conn.execute(
            "INSERT INTO support_consents (tenant_id, granted_by, reason, "
            "expires_at) VALUES (%s, %s, %s, now() + make_interval(hours => %s))",
            (user["tenant_id"], user["user_id"], reason, hours))
    return {"ok": True, "hours": hours}


@router.post("/support-access/revoke")
@documented(response=Ok)
def support_access_revoke_api(user: dict = Depends(_user())):
    demoguard.deny(user)
    _owner_only(user)
    from .app import _control_conn
    with _control_conn() as conn:
        conn.execute(
            "UPDATE support_consents SET revoked_at=now() "
            "WHERE tenant_id=%s AND revoked_at IS NULL", (user["tenant_id"],))
    return {"ok": True}
