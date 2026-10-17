"""Undoing a login-email change from the address it moved away from —
the token half. The door that spends it lives in ``web.app``; migrations
191 and 192 record the why. Everything here runs on the ADMIN connection: the
email change already does (the app role cannot write users.email), and
the restore it pays for is the same write.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

# Long enough for someone who reads that inbox every few days, short
# enough that a mailbox the owner left on purpose stops speaking for the
# account soon after.
REVERT_TTL = dt.timedelta(hours=72)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(admin_conn, user_id, old_email: str, new_email: str) -> str:
    """Mint the link for one change; returns the RAW token.

    Only an earlier unspent link to the SAME old address is burnt (the
    newest link is the one that works). Links to other addresses stand: a
    hijacker who changes the address twice must not be able to spend the
    owner's link by making a second change. The household is read from
    the account as it stands, so the link dies if the row ever moves.

    The new link takes the place of the earliest one it replaces: its
    created_at is that link's, not now. Earliest-wins (``burn_later``)
    orders by created_at, so a fresh stamp would let a hijacker push the
    owner's link behind their own simply by changing the address back
    again — A to B, B to A (their link, to B), A to B (the owner's link
    re-minted after theirs)."""
    token = secrets.token_urlsafe(32)
    admin_conn.execute(
        """WITH burnt AS (
               UPDATE email_change_reverts SET used_at=now()
                WHERE user_id=%s AND lower(old_email)=lower(%s)
                  AND used_at IS NULL
            RETURNING created_at)
           INSERT INTO email_change_reverts
               (user_id, tenant_id, old_email, new_email, token_hash,
                created_at, expires_at)
           SELECT id, tenant_id, %s, %s, %s,
                  COALESCE((SELECT min(created_at) FROM burnt), now()), %s
             FROM users WHERE id=%s""",
        (user_id, old_email, old_email, new_email, _h(token),
         dt.datetime.now(dt.timezone.utc) + REVERT_TTL, user_id))
    return token


def lookup(admin_conn, token: str, *, for_update: bool = False) -> dict | None:
    """The revert IF the token is real, unexpired and unused, and the
    account is not already back on the address it names (then there is
    nothing to undo). Joined to the account's current address and tenant
    so the caller can say what will change."""
    if not token or len(token) > 200:
        return None
    return admin_conn.execute(
        """SELECT r.id, r.user_id, r.old_email, r.new_email,
                  u.email AS current_email, u.tenant_id
             FROM email_change_reverts r JOIN users u ON u.id = r.user_id
            WHERE r.token_hash = %s AND r.used_at IS NULL
              AND r.expires_at > now()
              AND u.tenant_id = r.tenant_id
              AND lower(u.email) <> lower(r.old_email)"""
        + (" FOR UPDATE OF r" if for_update else ""),
        (_h(token),)).fetchone()


def spend(admin_conn, revert_id) -> bool:
    """Burn the token; False when a twin request already did."""
    return admin_conn.execute(
        "UPDATE email_change_reverts SET used_at=now() "
        "WHERE id=%s AND used_at IS NULL", (revert_id,)).rowcount == 1


def burn_later(admin_conn, revert_id) -> None:
    """After `revert_id` is spent: burn every unspent link for the same
    account minted at or after it. The earliest link wins — a later one
    went to an address the account only held after the change the owner
    is undoing, which is where a hijacker's second change sends it.
    Earlier links stand, so a hijacker spending their own later link
    first does not disarm the owner's."""
    admin_conn.execute(
        """UPDATE email_change_reverts l SET used_at=now()
             FROM email_change_reverts s
            WHERE s.id = %s AND l.user_id = s.user_id
              AND l.created_at >= s.created_at AND l.used_at IS NULL""",
        (revert_id,))


def burn_all(admin_conn, user_id) -> None:
    """Every unspent link for the account, for a road that hands the row
    to someone else (a reclaim that moves it into a fresh household)."""
    admin_conn.execute(
        "UPDATE email_change_reverts SET used_at=now() "
        "WHERE user_id=%s AND used_at IS NULL", (user_id,))


def mask(email: str) -> str:
    """An address as a notice may show it to whoever reads the OLD inbox:
    enough to recognise ("was that me?"), not enough to hand a stranger
    the new login name. The domain stays whole — it is usually what tells
    the owner the change was not theirs."""
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"
