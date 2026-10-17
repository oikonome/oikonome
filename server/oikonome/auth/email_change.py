"""A login-email change waiting for its new mailbox — the token half.

On a hosted instance the change door records the wanted address here and
mails the link to that address; the account moves only when the link's
confirm button is pressed (the door and the confirm page live in
``web.app``; migration 196 records the why). Everything here runs on the
ADMIN connection: the table is admin-only, and the move it pays for is a
users.email write the app role cannot make.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

# A day: long enough to fetch mail on another device, short enough that a
# forgotten request does not stand for a week.
CHANGE_TTL = dt.timedelta(hours=24)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def request(admin_conn, user_id, old_email: str, new_email: str, *,
            session_id=None, device_id=None, mail_sent: bool = True) -> str:
    """Record a wanted address; returns the RAW token. Any earlier live
    request of the account is ended: the newest request is what the
    account asked for. `mail_sent`: the caller is about to mail the link
    (False when a send cap withheld it — the caps count only sent mail)."""
    token = secrets.token_urlsafe(32)
    cancel(admin_conn, user_id)
    admin_conn.execute(
        """INSERT INTO email_change_requests
               (user_id, old_email, new_email, session_id, device_id,
                token_hash, expires_at, mail_sent_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,
                   CASE WHEN %s THEN now() END)""",
        (user_id, old_email, new_email, session_id, device_id, _h(token),
         dt.datetime.now(dt.timezone.utc) + CHANGE_TTL, mail_sent))
    return token


def resend(admin_conn, live: dict) -> str:
    """A fresh link for the account's live request (`live()`'s row), to the
    same address from the same credential; returns the RAW token. The
    earlier link ends with it — the newest mail is the one that works —
    and the new row records its own send, so the caps see every mail."""
    return request(admin_conn, live["user_id"], live["old_email"],
                   live["new_email"], session_id=live["session_id"],
                   device_id=live["device_id"], mail_sent=True)


# A request still speaks for the account only while the account is on the
# address it was made from and the credential that made it is signed in:
# a reset, a revert, a reclaim, a sign-out everywhere or a password change
# made from another session each end it without having to know it exists.
_STILL_ASKED = """
    lower(u.email) = lower(r.old_email)
    AND CASE
          WHEN r.session_id IS NOT NULL THEN EXISTS (
              SELECT 1 FROM sessions s
               WHERE s.id = r.session_id AND s.user_id = r.user_id
                 AND s.expires_at > now())
          WHEN r.device_id IS NOT NULL THEN EXISTS (
              SELECT 1 FROM device_tokens d
               WHERE d.id = r.device_id AND d.user_id = r.user_id
                 AND d.revoked_at IS NULL)
          ELSE FALSE
        END"""


def lookup(admin_conn, token: str, *, for_update: bool = False) -> dict | None:
    """The request IF the token is real, unexpired, unused and the account
    still asks for it (see `_STILL_ASKED`)."""
    if not token or len(token) > 200:
        return None
    return admin_conn.execute(
        """SELECT r.id, r.user_id, r.old_email, r.new_email, u.tenant_id
             FROM email_change_requests r JOIN users u ON u.id = r.user_id
            WHERE r.token_hash = %s AND r.used_at IS NULL
              AND r.expires_at > now() AND """ + _STILL_ASKED
        + (" FOR UPDATE OF r" if for_update else ""),
        (_h(token),)).fetchone()


# The migration that added `mail_sent_at`. Before it every recorded request
# had its link mailed (a withheld mail recorded nothing), so a row older
# than that file's arrival counts as mailed although the column is NULL.
_MAIL_SENT_MIGRATION = "197_email_change_requests_record_the_mail_sent.sql"


def pending(conn, user_id) -> dict | None:
    """The account's live request, for the settings card: the address it
    waits on, until when, and `mailed` — whether its link actually went
    out. One a send cap held back has no link anywhere: a card saying
    "waiting for confirmation, the link works until…" would have the person
    wait a day for a mail that was never sent. Such a request still stands
    for the resend door, and asking again once the cap allows replaces it.
    Reads only the columns the app role is granted (never the token), so it
    runs on the caller's pooled control connection."""
    return conn.execute(
        """SELECT r.id, r.new_email, r.expires_at,
                  (r.mail_sent_at IS NOT NULL
                   OR r.created_at < (SELECT m.applied_at
                                        FROM schema_migrations m
                                       WHERE m.name = %s)) IS TRUE AS mailed
             FROM email_change_requests r JOIN users u ON u.id = r.user_id
            WHERE r.user_id = %s AND r.used_at IS NULL
              AND r.expires_at > now() AND """ + _STILL_ASKED
        + " ORDER BY r.created_at DESC LIMIT 1",
        (_MAIL_SENT_MIGRATION, user_id)).fetchone()


def live(admin_conn, user_id, *, for_update: bool = False) -> dict | None:
    """The account's live request with what the resend door needs: who
    asked (session or device) and whether its mail went out."""
    return admin_conn.execute(
        """SELECT r.id, r.user_id, r.old_email, r.new_email, r.session_id,
                  r.device_id, r.mail_sent_at, r.expires_at
             FROM email_change_requests r JOIN users u ON u.id = r.user_id
            WHERE r.user_id = %s AND r.used_at IS NULL
              AND r.expires_at > now() AND """ + _STILL_ASKED
        + " ORDER BY r.created_at DESC LIMIT 1"
        + (" FOR UPDATE OF r" if for_update else ""), (user_id,)).fetchone()


def spend(admin_conn, request_id) -> bool:
    """Burn the token; False when a twin request already did."""
    return admin_conn.execute(
        "UPDATE email_change_requests SET used_at=now() "
        "WHERE id=%s AND used_at IS NULL", (request_id,)).rowcount == 1


def cancel(admin_conn, user_id) -> int:
    """End every live request of the account."""
    return admin_conn.execute(
        "UPDATE email_change_requests SET used_at=now() "
        "WHERE user_id=%s AND used_at IS NULL", (user_id,)).rowcount
