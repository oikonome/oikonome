"""Email verification — proves the signup address receives mail
(the daily verdict email is the product; an unverifiable address means a
silent product). Token discipline is password_resets': opaque 256-bit
link token, sha256 at rest, hash-lookup validation, one use — with one
difference, below.

A link speaks for the MAILBOX it was sent to (`sent_to`), not for the
account: it verifies only while the account's address is still that one.
Bound that way, minting a new link has no reason to burn the older ones,
and it does not. Every unexpired, unspent link mailed to the address
verifies it, because each proves exactly the same thing — that this
mailbox receives this instance's mail — and mails arrive out of order: a
resend, the day-7 reminder or the freeze notice minted after the first
mail must not turn the first mail's link, which is the one most people
open, into "already used". Spending stays one-use per link.

users.verified_at is written by consume on an ADMIN connection on
purpose: the app role's users UPDATE is column-scoped to
password_hash + totp_secret precisely so injected SQL can't forge
verified_at — this module must not be the grant that reopens it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

VERIFY_TTL = dt.timedelta(days=7)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(conn, user_id) -> str:
    """Mint a verification token for the user's CURRENT address; returns
    the RAW token. Earlier links stay live (module docstring). App-role
    connection is fine (INSERT granted, like password_resets)."""
    token = secrets.token_urlsafe(32)
    row = conn.execute(
        """INSERT INTO email_verifications (user_id, token_hash, expires_at,
                                            sent_to)
           SELECT id, %s, %s, email FROM users WHERE id=%s
           RETURNING id""",
        (_h(token), dt.datetime.now(dt.timezone.utc) + VERIFY_TTL,
         user_id)).fetchone()
    if row is None:
        raise LookupError("no such user")
    return token


# the link names the account only while the account is still at the
# address the link was mailed to
_SAME_MAILBOX = "lower(u.email) = lower(v.sent_to)"


def lookup(conn, token: str) -> dict | None:
    """The verification row IF the token is real, unexpired, unused, and
    the account is still at the address it was mailed to."""
    if not token or len(token) > 200:
        return None
    return conn.execute(
        """SELECT v.id, v.user_id, u.email
           FROM email_verifications v JOIN users u ON u.id = v.user_id
           WHERE v.token_hash = %s AND v.used_at IS NULL
             AND v.expires_at > now() AND """ + _SAME_MAILBOX,
        (_h(token),)).fetchone()


def lookup_spent(conn, token: str) -> dict | None:
    """The verification row for a token that is real and unexpired but
    ALREADY USED, with whether the user it belongs to is verified. This is
    what makes the emailed link safe to spend on the GET: a mail scanner
    that prefetches it verifies the mailbox (which is exactly what the
    link proves — the address receives our mail), and the human clicking
    afterwards is shown the same "confirmed" page instead of "already
    used", which would strand them."""
    if not token or len(token) > 200:
        return None
    return conn.execute(
        """SELECT v.id, v.user_id, u.email,
                  (u.verified_at IS NOT NULL) AS verified
           FROM email_verifications v JOIN users u ON u.id = v.user_id
           WHERE v.token_hash = %s AND v.used_at IS NOT NULL
             AND v.expires_at > now() AND """ + _SAME_MAILBOX,
        (_h(token),)).fetchone()


def consume(admin_conn, verification_id, user_id) -> bool:
    """Atomically burn the token and stamp users.verified_at. ADMIN
    connection required (see module docstring). Returns False when the
    token was already burned (concurrent submit)."""
    with admin_conn.transaction():
        # the account row first: the nightly freeze locks a household's
        # rows before it asks whether anyone there is verified
        # (jobs/unverified._freeze), so a click and a freeze are ordered —
        # the freeze either sees this stamp or makes the click wait and
        # land on a frozen household's reopen button
        who = admin_conn.execute(
            "SELECT tenant_id FROM users WHERE id=%s FOR UPDATE",
            (user_id,)).fetchone()
        burned = admin_conn.execute(
            "UPDATE email_verifications v SET used_at=now() FROM users u "
            "WHERE v.id=%s AND v.used_at IS NULL AND u.id = v.user_id "
            "AND " + _SAME_MAILBOX, (verification_id,)).rowcount
        if not burned:
            return False
        # first_verified_at is the permanent record that this row once
        # proved a mailbox; nothing that rewrites verified_at later (a
        # reclaim moving the row, say) ever clears it
        stamped = admin_conn.execute(
            "UPDATE users SET verified_at=now(), "
            "first_verified_at=coalesce(first_verified_at, now()) "
            "WHERE id=%s AND verified_at IS NULL", (user_id,)).rowcount
        if stamped:
            from .. import ext
            ext.gate.on_email_verified(admin_conn, who["tenant_id"], user_id)
        return True
