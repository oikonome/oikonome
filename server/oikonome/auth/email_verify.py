"""Email verification — proves the signup address receives mail
(the daily verdict email is the product; an unverifiable address means a
silent product). Token discipline is password_resets': opaque 256-bit
link token, sha256 at rest, hash-lookup validation, one use, and minting
burns older live tokens for the user.

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
    """Mint a verification token for the user; returns the RAW token.
    App-role connection is fine (INSERT granted, like password_resets)."""
    token = secrets.token_urlsafe(32)
    with conn.transaction():
        conn.execute(
            "UPDATE email_verifications SET used_at=now() "
            "WHERE user_id=%s AND used_at IS NULL", (user_id,))
        conn.execute(
            """INSERT INTO email_verifications (user_id, token_hash,
                                                expires_at)
               VALUES (%s,%s,%s)""",
            (user_id, _h(token),
             dt.datetime.now(dt.timezone.utc) + VERIFY_TTL))
    return token


def lookup(conn, token: str) -> dict | None:
    """The verification row IF the token is real, unexpired, and unused."""
    if not token:
        return None
    return conn.execute(
        """SELECT v.id, v.user_id, u.email
           FROM email_verifications v JOIN users u ON u.id = v.user_id
           WHERE v.token_hash = %s AND v.used_at IS NULL
             AND v.expires_at > now()""",
        (_h(token),)).fetchone()


def lookup_spent(conn, token: str) -> dict | None:
    """The verification row for a token that is real and unexpired but
    ALREADY USED, with whether the user it belongs to is verified. This is
    what makes the emailed link safe to spend on the GET: a mail scanner
    that prefetches it verifies the mailbox (which is exactly what the
    link proves — the address receives our mail), and the human clicking
    afterwards is shown the same "confirmed" page instead of "already
    used", which would strand them."""
    if not token:
        return None
    return conn.execute(
        """SELECT v.id, v.user_id, u.email,
                  (u.verified_at IS NOT NULL) AS verified
           FROM email_verifications v JOIN users u ON u.id = v.user_id
           WHERE v.token_hash = %s AND v.used_at IS NOT NULL
             AND v.expires_at > now()""",
        (_h(token),)).fetchone()


def consume(admin_conn, verification_id, user_id) -> bool:
    """Atomically burn the token and stamp users.verified_at. ADMIN
    connection required (see module docstring). Returns False when the
    token was already burned (concurrent submit)."""
    with admin_conn.transaction():
        burned = admin_conn.execute(
            "UPDATE email_verifications SET used_at=now() "
            "WHERE id=%s AND used_at IS NULL", (verification_id,)).rowcount
        if not burned:
            return False
        # first_verified_at is the permanent record that this row once
        # proved a mailbox; an email change clears verified_at, never this
        admin_conn.execute(
            "UPDATE users SET verified_at=now(), "
            "first_verified_at=coalesce(first_verified_at, now()) "
            "WHERE id=%s AND verified_at IS NULL", (user_id,))
        return True
