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


def is_shared_login(user: dict) -> bool:
    """True when the caller is the shared demonstration login: the one
    login whose second factor the demonstration seed waived. The refusing
    helpers below and the flag /api/me hands the clients both read this,
    so a control the clients hide and a door the server refuses can never
    disagree about who the shared login is."""
    return bool(user.get("second_factor_waived"))


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
    if is_shared_login(user):
        raise HTTPException(403, SHARED_DENIED)


SHARED_GRANT_DENIED = ("This is a shared demonstration login — it can't add "
                       "people, script tokens, support access or new "
                       "outbound destinations to this household")


def deny_shared_grant(user: dict) -> None:
    """`deny`, plus the shared demonstration login, at every door that mints
    a principal or a channel which outlives the login that made it: an
    invite (a member account), a role change or removal of a member, a
    script token, a support-access window, a webhook destination, a new
    mail relay, AI endpoint or report recipient.

    The step-up in front of those doors exists so that a password alone
    cannot make a durable grant; for this login the second factor is waived
    and the password is handed out, so the step-up proves nothing. And the
    only remedy the operator has, rotating the shared password, removes
    what the login itself holds — not a member account someone claimed
    with it, nor a token or a hook that belongs to the household."""
    deny(user)
    if is_shared_login(user):
        raise HTTPException(403, SHARED_GRANT_DENIED)


SHARED_BUNDLE_DENIED = ("This is a shared demonstration login — it can't "
                        "connect, disconnect or reconnect a bank or "
                        "exchange, save aggregator keys, take the "
                        "credential bundle or manage the household's plan, "
                        "since those outlive the login.")


def deny_shared_credentials(user: dict) -> None:
    """`deny`, plus the shared demonstration login, at every door that
    puts a live credential into the household, ends or restarts one, or
    hands one out: linking a bank or an exchange, disconnecting or
    reconnecting one, saving an aggregator's keys, the sealed bundle of
    every credential the household holds, and the payment account behind
    its plan.

    Each of these outlives the login that used it. A bank someone links
    with the handed-out password is read by every later holder of that
    password, keeps syncing (and billing the operator) every night, and
    stays when the password is rotated, because it is the household's and
    not the login's; a bundle once downloaded cannot be recalled; a Stripe
    customer opened by one visitor is a card and an address the next one
    is shown. The step-up in front of these doors proves nothing for a
    login whose password is handed out and whose second factor is waived.

    A disconnect is in the family too: releasing a Plaid Item cannot be
    undone, and a reconnect restarts syncing and billing — neither is the
    shared login's to decide for the household.

    The request gate calls this for every write to the bank-connection
    door family — the prefixes in permissions.SHARED_LOGIN_CREDENTIAL_DOORS
    and the per-connection disconnect and reconnect paths in
    permissions.SHARED_LOGIN_CREDENTIAL_PATTERNS — so a door added there
    later is covered without anyone remembering to. The billing doors and
    the helpers behind the connection doors call it themselves, so a
    caller that does not pass through that gate is refused as well."""
    deny(user)
    if is_shared_login(user):
        raise HTTPException(403, SHARED_BUNDLE_DENIED)


def roster_hidden(user: dict) -> bool:
    """True when the caller's own session and device rosters must not be
    served: on a demo instance, and for the shared demonstration login in
    a real tenant. Every visitor holds that one login, so its rosters list
    strangers — their addresses, browsers and phones. Both roster doors
    read this one predicate so neither can drift from the other."""
    return is_demo(user["tenant_id"]) or bool(user.get("second_factor_waived"))


# what another member's address reads as to the shared demonstration login
SOMEONE = "a household member"


def people_hidden(user: dict) -> bool:
    """True when the caller must not be shown other members' addresses:
    the shared demonstration login. Every visitor holds that one login, so
    anything it reads about the household's people — the roster, the mail
    recipients, who made an edit, who an invite was for — reaches each
    stranger who signs in with it. Every door that returns a member's
    address reads this one predicate, so none can drift from the others."""
    return bool(user.get("second_factor_waived"))


def own_address_only(user: dict, address) -> str:
    """`address` as the caller may see it: unchanged when it is the
    caller's own or people are not hidden from them, else SOMEONE."""
    if not people_hidden(user) or not address:
        return address
    mine = str(user.get("email") or "").strip().lower()
    return address if str(address).strip().lower() == mine else SOMEONE


def own_addresses_only(user: dict, addresses) -> list:
    """`addresses` cut to the caller's own when people are hidden from it:
    a list of members is dropped to the one row the caller already knows,
    rather than filled with anonymised entries that name nobody."""
    addresses = list(addresses or [])
    if not people_hidden(user):
        return addresses
    mine = str(user.get("email") or "").strip().lower()
    return [a for a in addresses if str(a or "").strip().lower() == mine]


SHARED_PEOPLE_DENIED = ("This is a shared demonstration login — the "
                        "household's own notes about its people are not "
                        "shown to it or changed by it")
