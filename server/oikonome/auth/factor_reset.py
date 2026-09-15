"""The way back in when password, authenticator AND recovery codes are all
gone.

The mailed reset (`reset.py`) strips every factor, so on an account with a
strong factor enrolled it also demands a recovery code — unconditionally,
because otherwise "I control the inbox" would equal "I own the account".
That rule is right and it stays. It also means a person missing all three
has no door, which is what this module adds, in two shapes:

* **Cooling-off reset (self-serve).** From the reset page, holding a live
  reset link (so the inbox is proven), the person asks for a factor-clearing
  reset. Nothing changes yet: a request is recorded that LANDS after
  `COOLING_OFF`. The account is told on every channel it has — email with a
  one-click cancel link, web push, native push, SMS when a verified number
  is enrolled — and any live sign-in cancels it too. Once it has landed, the
  next reset link works without a recovery code, and that reset clears the
  factors exactly as a coded one would. The delay plus the cancel is what
  keeps inbox control insufficient on its own: a hijacker who reads the
  mail has to keep the real owner from noticing for a week, on every
  channel at once.

* **Operator clear (out of band).** The console action and the host-side
  CLI void TOTP, passkeys and recovery codes at once, evict every session
  and device exactly like a reset does, and mail the account that it
  happened. The operator verifies identity out of band first; the code
  only records that they did.

Both doors leave the password alone: the person sets a new one through the
ordinary reset afterwards (the operator door hands them the reset link).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets

log = logging.getLogger("oikonome.auth.factor_reset")

COOLING_OFF = dt.timedelta(days=7)

# How long a LANDED request stays usable before it goes stale.
#
# A request that has matured is spent by the next reset link. One that is
# never used must not stand open indefinitely: an exemption from the
# recovery-code rule is only meaningful while the account still remembers
# asking for it, and every channel was told a door would open in a week,
# not that one would stay open for good. A month past maturity is far
# longer than any honest recovery takes and short enough that a forgotten
# request is never a permanent one. Asking again is free — it restarts the
# seven days, with the account told on every channel all over again, which
# is exactly the protection the cooling-off exists to give.
LANDED_TTL = dt.timedelta(days=30)

# Advisory-lock namespace for the per-account clock (arbitrary constant, the
# first half of the (space, account) pair so these locks cannot collide with
# any other advisory lock taken against the same database).
_CLOCK_LOCK_SPACE = 74210092


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def pending(conn, user_id) -> dict | None:
    """The live request for this user — asked for, not cancelled, not yet
    spent — whether or not it has landed.

    Deliberately unbounded, unlike `matured`: this answers "is a clock
    running / did one run", which stays true of a stale request, and the
    cancel link goes on stopping it. `matured` is the one that grants an
    exemption, so `matured` is the one that expires.
    """
    return conn.execute(
        "SELECT id, user_id, requested_at, lands_at "
        "FROM factor_resets WHERE user_id=%s AND cancelled_at IS NULL "
        "AND consumed_at IS NULL ORDER BY requested_at DESC LIMIT 1",
        (user_id,)).fetchone()


def matured(conn, user_id) -> dict | None:
    """The live request, inside the window it is good for: its cooling-off
    has run out and it has not gone stale (`LANDED_TTL`). This is the ONE
    thing that lets a reset proceed without a recovery code, which is why
    it is bounded at both ends — see the constant for what an unbounded
    one left standing."""
    return conn.execute(
        "SELECT id, user_id, requested_at, lands_at "
        "FROM factor_resets WHERE user_id=%s AND cancelled_at IS NULL "
        "AND consumed_at IS NULL AND lands_at <= now() "
        "AND lands_at > now() - make_interval(days => %s) "
        "ORDER BY requested_at DESC LIMIT 1",
        (user_id, LANDED_TTL.days)).fetchone()


def request(conn, user_id, ip: str = "",
            cooling_off: dt.timedelta = COOLING_OFF) -> tuple[dict, str]:
    """Record a cooling-off request; returns (row, raw cancel token).

    Any earlier live request is superseded rather than kept alongside:
    two clocks running for one account would let the older, nearer one
    land while the person watched the newer date. Re-requesting therefore
    RESTARTS the wait — asking again never makes it shorter.

    Superseding is check-then-act, so it has to be serialized per account
    or it supersedes nothing: of two requests arriving together, each
    reads a table with no COMMITTED live row, cancels nothing, and commits
    a clock of its own — exactly the two clocks the rule exists to
    prevent, one of them invisible to whoever cancels the other. A row lock cannot close
    that window, because on the first request there is no row to lock; the
    lock is therefore taken on the ACCOUNT, held to the end of this
    transaction, so the second request blocks until the first is committed
    and visible and then supersedes it in the ordinary way. The partial
    unique index on the live rows is the same rule stated where no future
    caller can route around it — belt to this braces."""
    token = secrets.token_urlsafe(32)
    lands = dt.datetime.now(dt.timezone.utc) + cooling_off
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                     (_CLOCK_LOCK_SPACE, str(user_id)))
        conn.execute(
            "UPDATE factor_resets SET cancelled_at=now(), "
            "cancel_reason='superseded' WHERE user_id=%s "
            "AND cancelled_at IS NULL AND consumed_at IS NULL", (user_id,))
        row = conn.execute(
            "INSERT INTO factor_resets (user_id, lands_at, cancel_token_hash, "
            "requested_ip) VALUES (%s,%s,%s,%s) "
            "RETURNING id, user_id, requested_at, lands_at",
            (user_id, lands, _h(token), ip or None)).fetchone()
    return row, token


# How far a cancel link reaches. It answers for the ACCOUNT, not only for
# the request it was minted for, but only while that request still has
# standing — two clauses, each load-bearing:
#
#   * the minting request is itself still live: the ordinary one-shot
#     cancel, honoured however old the link is, including after the clock
#     has landed. A landed request stays live until a reset spends it, and
#     stopping it then is precisely when stopping it matters most, so an
#     age limit here would kill the link at the worst possible moment.
#   * or a LATER request superseded it and the minting request's own
#     cooling-off has not yet run out: the restart. Asking again restarts
#     the wait, and a person opening the older mail inside the window it
#     promised is asking for the account's reset to stop — answering "this
#     link has already done its work" while a clock they never opened ran
#     on would be the cancel promise broken by a technicality.
#
# The window is the bound, and the bound is the point. These tokens carry
# no expiry of their own, so a reach that ignored it would turn every link
# ever mailed into a standing veto over every future recovery: an old
# mailbox, a forwarded message, a screenshot, a previous owner of the
# address — any of them could deny the account the one door back in, for
# good, and the account holder would see only a cancel they did not make.
# Bounded, an older link outlives exactly the restart it exists for.
#
# A link whose clock a sign-in, a coded reset or the link itself already
# ended reaches nothing later either: it is spent, which is what the mail
# says it is. Whoever asked next was mailed a link of their own.
_STILL_ANSWERS = (
    "((m.cancelled_at IS NULL AND m.consumed_at IS NULL) "
    "OR (m.cancel_reason = 'superseded' AND m.lands_at > now()))")


def lookup_cancel(conn, token: str) -> dict | None:
    """The clock a cancel link stops, if one is still running and this link
    still answers for it (see `_STILL_ANSWERS`). Nothing to stop answers
    None so the page can say the link has done its work rather than pretend
    to cancel again."""
    if not token:
        return None
    return conn.execute(
        "SELECT r.id, r.user_id, r.lands_at, u.email "
        "FROM factor_resets m "
        "JOIN factor_resets r ON r.user_id = m.user_id "
        "JOIN users u ON u.id = r.user_id "
        "WHERE m.cancel_token_hash = %s "
        "AND r.cancelled_at IS NULL AND r.consumed_at IS NULL "
        "AND " + _STILL_ANSWERS + " "
        "ORDER BY r.requested_at DESC LIMIT 1", (_h(token),)).fetchone()


def cancel_by_token(conn, token: str) -> dict | None:
    """One click on the mailed link stops the account's clock — whichever
    request is live, not merely the one this link was minted for, for as
    long as this link still answers for the account (see `_STILL_ANSWERS`).
    Returns a cancelled row, or None when there was nothing left to stop or
    nothing this link may stop."""
    if not token:
        return None
    return conn.execute(
        "UPDATE factor_resets r SET cancelled_at=now(), cancel_reason='link' "
        "FROM factor_resets m "
        "WHERE m.cancel_token_hash = %s AND r.user_id = m.user_id "
        "AND r.cancelled_at IS NULL AND r.consumed_at IS NULL "
        "AND " + _STILL_ANSWERS + " "
        "RETURNING r.id, r.user_id, r.lands_at", (_h(token),)).fetchone()


def cancel_for_user(conn, user_id, reason: str = "sign-in") -> int:
    """Any live sign-in cancels the clock: whoever can sign in does not
    need a factor-clearing reset, and if it was not them who asked, the
    request must not land. Returns how many requests were cancelled."""
    return conn.execute(
        "UPDATE factor_resets SET cancelled_at=now(), cancel_reason=%s "
        "WHERE user_id=%s AND cancelled_at IS NULL AND consumed_at IS NULL",
        (reason, user_id)).rowcount


def consume(conn, request_id) -> None:
    """The matured request has been spent by a reset."""
    conn.execute("UPDATE factor_resets SET consumed_at=now() "
                 "WHERE id=%s AND consumed_at IS NULL", (request_id,))


def clear_second_factor(conn, user_id) -> dict:
    """The operator door: void every factor and every foothold, keep the
    password. Returns what was removed so the audit row can say it.

    Uses the same eviction as a password reset — `reset.revoke_everything`
    — because the reasoning is the same: a factor that survives is a
    factor a hijacker may have planted, and the person is about to prove
    themselves again from nothing anyway."""
    from . import reset as reset_mod
    before = conn.execute(
        "SELECT (totp_secret IS NOT NULL) AS totp, "
        "(SELECT count(*) FROM passkeys WHERE user_id=%s) AS passkeys, "
        "(SELECT count(*) FROM sessions WHERE user_id=%s) AS sessions "
        "FROM users WHERE id=%s", (user_id, user_id, user_id)).fetchone()
    if before is None:
        raise LookupError("no such user")
    with conn.transaction():
        reset_mod.revoke_everything(conn, user_id)
    reset_mod.drop_recovery_codes(user_id)
    # a pending cooling-off request is moot once the operator has cleared
    # the factors by hand — leaving it live would let it land later, after
    # the person had already re-enrolled
    cancel_for_user(conn, user_id, reason="operator")
    return {"totp": bool(before["totp"]), "passkeys": int(before["passkeys"]),
            "sessions": int(before["sessions"])}
