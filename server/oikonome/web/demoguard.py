"""Demo-instance lockdown.

A tenant whose config carries `demo_mode: true` (set by demo-seed)
is fully explorable and editable, but every DATA DOOR (aggregator links,
file imports, uploads, script tokens) and every ACCOUNT-SECURITY mutation
(password/email, 2FA, passkeys, invites, account delete) is refused — the
instance holds synthetic data only and its printed credentials are shared,
so those features could only mislead or lock people out.

The flag is cached per tenant; every config write path (budget.save_config,
restore_zip's settings merge) calls `invalidate` so a runtime flip in this
process takes effect on the next request, and a "not a demo" answer
expires on its own so a flip written by another process (the worker's
hourly reset, the seed CLI) lands within seconds. The SPA mirrors these 403s by rendering the features
disabled (it reads
`demo` from /api/me); this module is the backstop that makes the UI state
true no matter what is POSTed.
"""

import time

from fastapi import HTTPException

_cache: dict[str, bool] = {}
# tenant id -> monotonic time a "not a demo" answer was read
_checked_at: dict[str, float] = {}

# `invalidate` only reaches the process that wrote the config; the demo
# seed and the hourly reset write it from the worker or the CLI. So a
# "not a demo" answer is only trusted briefly — a household that turns
# into a demo in another process is locked within this many seconds —
# while "demo" is kept until this process sees a write, the safe side to
# be stale on.
_NOT_DEMO_TTL = 30.0

DENIED = "Not available on the demo instance (synthetic data only)"


def is_demo(tenant_id) -> bool:
    tid = str(tenant_id)
    hit = _cache.get(tid)
    if hit or (hit is not None and
               time.monotonic() - _checked_at.get(tid, 0.0) < _NOT_DEMO_TTL):
        return hit
    from ..db import tenancy
    from ..engine import budget
    conn = tenancy.tenant_connect(tid)
    try:
        flag = bool(budget.load_config(conn).get("demo_mode"))
    finally:
        conn.close()
    _cache[tid] = flag
    _checked_at[tid] = time.monotonic()
    return flag


def invalidate(tenant_id=None) -> None:
    """Config just changed — forget the cached flag (one tenant, or all
    when the caller doesn't know which tenant wrote). Repopulating is one
    config read per tenant, so clearing broadly is cheap."""
    if tenant_id is None:
        _cache.clear()
    else:
        _cache.pop(str(tenant_id), None)


def deny(user: dict) -> None:
    """Raise 403 when the requesting user's tenant is a demo instance."""
    if is_demo(user["tenant_id"]):
        raise HTTPException(403, DENIED)


SHARED_DENIED = ("This is a shared demonstration login — its password, "
                 "email, sign-in factors and sessions can't be changed")


def deny_shared_login(user: dict) -> None:
    """`deny`, plus the shared demonstration login wherever it lives.

    A demonstration household can also be a real tenant (one whose login
    signs in like any other, so `deny` lets it through) with a single shared
    login whose second factor is waived — the waiver is written only by the
    demonstration seed. Any holder of that login who changes the password
    or the email, enrols a factor, or revokes the other sessions locks every
    other holder out. (Removing a factor locks nobody out, so those doors
    keep plain `deny`.)
    The account-posture doors call this instead of `deny`."""
    deny(user)
    if user.get("second_factor_waived"):
        raise HTTPException(403, SHARED_DENIED)


def roster_hidden(user: dict) -> bool:
    """True when the caller's own session and device rosters must not be
    served: on a demo instance, and for the shared demonstration login in
    a real tenant. Every visitor holds that one login, so its rosters list
    strangers — their addresses, browsers and phones. Both roster doors
    read this one predicate so neither can drift from the other."""
    return is_demo(user["tenant_id"]) or bool(user.get("second_factor_waived"))
