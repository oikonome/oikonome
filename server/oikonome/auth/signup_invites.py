"""Hosted signup invites — the gate between OIKONOME_HOSTED and the open
internet. An invite is operator-minted (`oikonome invite <email>`),
single-use, expiring, and bound to ONE email address, so an invite-only
instance admits exactly the people the operator typed and nobody else.

At-rest discipline matches password_resets: the link carries an opaque
256-bit token, the DB stores its sha256, validation is a hash lookup (no
timing side channel on the token bytes). Control-plane table, no RLS —
and deliberately no INSERT for the app role: minting is an admin-role
act, so SQL injected through the web tier can't issue itself an invite.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from psycopg.types.json import Jsonb

INVITE_TTL_DAYS = 14


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bounded_days(days) -> int:
    """The lifetime an invite asked for `days` will ACTUALLY get.

    `mint` clamps, because every caller hands it a number someone typed
    (see there for why). That makes the typed number the wrong thing to
    report back: `--days 999999` mints a 3650-day invite, `--days 0` mints
    a 1-day one, and an operator told otherwise is being told a number the
    system does not honour. Public so the doors that PRINT or AUDIT a
    lifetime — the CLI's confirmation line, the console's audit row — say
    what was minted rather than what was asked for."""
    return max(1, min(3650, int(days)))


def mint(admin_conn, email: str, days: int = INVITE_TTL_DAYS,
         note: str | None = None, options: dict | None = None) -> str:
    """Mint an invite for `email`; returns the RAW token (goes in the
    link). Admin connection required (the app role has no INSERT here).
    Minting burns every older unused invite for the address — re-inviting
    someone is a resend, and the newest link is the only one that works —
    the same rule every other one-time link in the app follows.

    `options` is whatever an installed add-on wants the new account born
    with; the core knows only that the object rides the invite and is
    handed back at redemption."""
    # `days` becomes timedelta(days=days) three lines down, and every
    # caller hands it a number someone typed — a console form field, the
    # CLI's `--days`. Zero or negative mints a link that is already expired
    # (the operator sends an invite that can never be claimed and has no
    # way to tell), and anything past timedelta's ~2.7-million-day ceiling
    # raises OverflowError. Bounded HERE so every door inherits it, rather
    # than in whichever caller was written most recently.
    days = bounded_days(days)
    token = secrets.token_urlsafe(32)
    email = email.strip().lower()
    with admin_conn.transaction():
        admin_conn.execute(
            "UPDATE signup_invites SET used_at=now() "
            "WHERE email=%s AND used_at IS NULL", (email,))
        admin_conn.execute(
            """INSERT INTO signup_invites (token_hash, email, note,
                                           expires_at, options)
               VALUES (%s,%s,%s,%s,%s)""",
            (_h(token), email, note,
             dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days),
             Jsonb(dict(options or {}))))
    return token


def lookup(conn, token: str, email: str) -> dict | None:
    """The invite row IF the token is real, unused, unexpired AND bound to
    exactly this email. One lookup answers every failure mode identically
    — the signup door turns any None into one generic 403, so a probe
    learns nothing about which check failed."""
    if not token or not email:
        return None
    return conn.execute(
        """SELECT id, email, options FROM signup_invites
           WHERE token_hash = %s AND used_at IS NULL
             AND expires_at > now() AND email = %s""",
        (_h(token), email.strip().lower())).fetchone()


def consume(conn, invite_id, tenant_id) -> bool:
    """Burn the invite against the tenant it minted. Conditional on
    used_at IS NULL so one-time-use holds under concurrent signups; the
    caller runs inside the signup transaction, so a False return rolls
    the whole tenant back."""
    return conn.execute(
        """UPDATE signup_invites SET used_at=now(), used_by_tenant=%s
           WHERE id=%s AND used_at IS NULL""",
        (tenant_id, invite_id)).rowcount == 1
