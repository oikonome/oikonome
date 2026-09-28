"""Taking back a squatted address — the token half.

A signup for an address that exists but was never verified creates
nothing; it parks the signup under a one-shot token and mails the
address. The click proves the mailbox, and only then does
``web.app`` perform the reclaim. Everything here runs on the ADMIN
connection: signup already does (it creates tenants), and the table
is admin-only by design. See migration 117 for the why.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

RECLAIM_TTL = dt.timedelta(hours=1)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(admin_conn, user_id, email: str,
           invite: str | None, ref: str | None) -> str:
    """Park a signup's INTENT under a fresh token; returns the RAW token.
    The credential is deliberately NOT parked: whoever filled the form
    and whoever clicks the link are different parties in the case this
    feature exists for, so the clicker sets the password on the confirm
    page (migration 118). Any earlier unspent token for the same row is
    burnt — the newest link is the only one that works (the reset-token
    doctrine)."""
    token = secrets.token_urlsafe(32)
    admin_conn.execute(
        "UPDATE signup_reclaims SET used_at=now() "
        "WHERE user_id=%s AND used_at IS NULL", (user_id,))
    admin_conn.execute(
        """INSERT INTO signup_reclaims (user_id, email, token_hash,
                                        invite, ref, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (user_id, email, _h(token), invite or None, ref or None,
         dt.datetime.now(dt.timezone.utc) + RECLAIM_TTL))
    return token


def lookup(admin_conn, token: str) -> dict | None:
    """The parked signup IF the token is real, unexpired and unused —
    joined to the row it names, so the caller can see what the address
    has become since (verified in the meantime = the owner already got
    in some other way; a reclaim would then be the takeover).

    The join is on the user id AND the address the token was mailed to.
    The id alone is not an honest binding: `users.email` is mutable (the
    account-settings email-change door rewrites it), so a link minted for
    old@example.com would keep resolving to the same user row after that row
    moved to new@example.com — re-arming a stale link against an address
    its holder never proved. Matching the address as well makes the token
    mean what its mail said it meant: it speaks for ONE mailbox, and the
    moment the account leaves that mailbox the token names nothing.
    """
    if not token or len(token) > 200:
        return None
    return admin_conn.execute(
        """SELECT r.id, r.user_id, r.email, r.invite, r.ref,
                  u.tenant_id, u.role, u.verified_at
             FROM signup_reclaims r
             JOIN users u ON u.id = r.user_id AND lower(u.email) = lower(r.email)
            WHERE r.token_hash = %s AND r.used_at IS NULL
              AND r.expires_at > now()""", (_h(token),)).fetchone()


def spend(admin_conn, reclaim_id) -> bool:
    """Burn the token; False when a twin request already did."""
    return admin_conn.execute(
        "UPDATE signup_reclaims SET used_at=now() "
        "WHERE id=%s AND used_at IS NULL", (reclaim_id,)).rowcount == 1
