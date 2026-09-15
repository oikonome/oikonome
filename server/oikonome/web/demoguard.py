"""Demo-instance lockdown.

A tenant whose config carries `demo_mode: true` (set by demo-seed)
is fully explorable and editable, but every DATA DOOR (aggregator links,
file imports, uploads, script tokens) and every ACCOUNT-SECURITY mutation
(password/email, 2FA, passkeys, invites, account delete) is refused — the
instance holds synthetic data only and its printed credentials are shared,
so those features could only mislead or lock people out.

The flag is cached per tenant; every config write path (budget.save_config,
restore_zip's settings merge) calls `invalidate` so a runtime flip —
however it happens — takes effect on the next request, not the next
process restart. The SPA mirrors these 403s by rendering the features
disabled (it reads
`demo` from /api/me); this module is the backstop that makes the UI state
true no matter what is POSTed.
"""

from fastapi import HTTPException

_cache: dict[str, bool] = {}

DENIED = "Not available on the demo instance (synthetic data only)"


def is_demo(tenant_id) -> bool:
    tid = str(tenant_id)
    if tid not in _cache:
        from ..db import tenancy
        from ..engine import budget
        conn = tenancy.tenant_connect(tid)
        try:
            _cache[tid] = bool(budget.load_config(conn).get("demo_mode"))
        finally:
            conn.close()
    return _cache[tid]


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
