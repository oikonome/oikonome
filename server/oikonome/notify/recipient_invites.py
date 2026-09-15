"""nobody is added to a household's financial mail without saying yes.

Were typing an address into Settings → Email enough to enrol it, that mailbox
would start receiving balances, spending and a verdict the next morning, and
the first say the person on the other end had in it would come after they had
already read one. So an added address gets an INVITE — one mail explaining
what the daily email is, who added them, and when it arrives — and receives
nothing else until the link in it is opened.

The gate lives in `worker._recipients_raw`, which mails an extra recipient only
when `accepted(...)` is true. Configuring an address and enrolling it are two
different acts, done by two different people.

**Token discipline is `auth/email_verify`'s**, with one structural difference:
there is exactly ONE row per (tenant, address), so a resend ROTATES the token
in place and older links stop working because they no longer exist — not
because some other statement remembered to burn them. Opaque 256-bit token,
sha256 at rest, hash lookup.

A spent token keeps its hash. `accepted_at`/`declined_at` are what make it
unusable (every mutating statement requires both NULL), and KEEPING the hash
is what lets someone who re-opens an old link be told what actually happened
instead of "invalid, expired, or already answered", which would advise a
person who had declined to ask for a fresh invite that `invite` will never
send.

**Everything here runs on the ADMIN connection**, reads included. An
`accepted_at` row is what makes a household's money flow to a mailbox, so
forging one must stay out of reach of injected app-role SQL (migration 074
grants the app role nothing at all). The read side is the Settings page, not a
hot path, so there is no cost to the symmetry.

Never raises into a caller for anything but a genuine programming error: a
household must not be unable to save its settings because the invite table is
having a bad day. A failed invite means the recipient is not enrolled, which
is the safe direction — the mail simply does not start.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets

from ..db import tenancy

log = logging.getLogger("oikonome.recipient_invites")

# 14 days, deliberately longer than the 7 an account's own verification gets.
# That one is answered by someone who just signed up and is sitting in front
# of the app; this one is answered by a third party who was not expecting it,
# may not check that mailbox daily, and has no other reason to come looking.
INVITE_TTL = dt.timedelta(days=14)
# Throttles on the mint-and-mail path itself, so EVERY
# door that reaches it (both settings saves + resend) is bounded — a
# per-address cooldown between re-sends to an unanswered invite, and a
# per-tenant hourly ceiling so rotating fresh addresses can't turn the
# instance into an open mail source.
_RESEND_COOLDOWN = dt.timedelta(minutes=10)
_HOURLY_TENANT_CAP = 30


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _conn():
    return tenancy.admin_connect()


# ---- writes ---------------------------------------------------------------


def invite(tenant_id, email: str, *, invited_by=None) -> str | None:
    """Mint (or rotate) an invite for this address; returns the RAW token,
    or None when no mail should be sent.

    None has three meanings, all of them "do not mail this person":
      * they already accepted — re-adding someone who once said yes does not
        ask them again. Consent is not revoked by being taken off a list.
      * they DECLINED — the one answer that must survive being re-added. A
        recipient who said no and gets re-invited every time the owner edits
        their settings has been given a nuisance, not a choice.
      * the address is empty or the write failed.
    """
    e = _norm(email)
    if not e:
        return None
    token = secrets.token_urlsafe(32)
    now = dt.datetime.now(dt.timezone.utc)
    try:
        conn = _conn()
        try:
            with conn.transaction():
                # The hourly ceiling below is a tenant-WIDE count, but the
                # FOR UPDATE two statements down locks only this one
                # (tenant, email) row — and a FIRST invite has no row to
                # lock at all. Concurrent invites to different fresh
                # addresses therefore all read the same pre-insert count
                # and all pass, overshooting the cap by however many
                # requests arrive together. Serialize the count-and-mint
                # per tenant with an xact-scoped advisory lock taken
                # before any read.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"oikonome:recipient-invite:{tenant_id}",))
                # FOR UPDATE: the answer is read here and acted on below,
                # inside the same transaction as the token rotation. Without
                # the lock a decline committing in that window is invisible
                # — the row says "unanswered", the mint proceeds, and
                # someone who just said no is invited again. Which is the
                # one answer this whole module exists to make stick.
                row = conn.execute(
                    "SELECT accepted_at, declined_at, last_sent_at "
                    "FROM recipient_invites "
                    "WHERE tenant_id=%s AND email=%s FOR UPDATE",
                    (tenant_id, e)).fetchone()
                if row and (row["accepted_at"] or row["declined_at"]):
                    return None
                # Without this, an UNANSWERED address would be
                # re-tokenised and re-mailed every time it reappeared in
                # the settings diff — toggle the list, one mail per
                # request, unbounded, from doors with no limiter. A
                # per-address cooldown makes the settings doors as
                # throttled as the resend door already is; Resend still
                # works after the cooldown, and a FIRST invite (no row)
                # is never delayed.
                if (row and row["last_sent_at"]
                        and row["last_sent_at"] > now - _RESEND_COOLDOWN):
                    return None
                # and a tenant-level hourly ceiling, so rotating through
                # fresh addresses cannot make the instance an open mail
                # source under its own SMTP identity
                sent = conn.execute(
                    "SELECT count(*) AS n FROM recipient_invites "
                    "WHERE tenant_id=%s AND last_sent_at > %s",
                    (tenant_id, now - dt.timedelta(hours=1))).fetchone()["n"]
                if sent >= _HOURLY_TENANT_CAP:
                    log.warning("recipient-invite hourly cap reached for "
                                "tenant %s — not mailing %s", tenant_id, e)
                    return None
                conn.execute(
                    """INSERT INTO recipient_invites
                           (tenant_id, email, token_hash, invited_by,
                            expires_at, last_sent_at, send_count)
                       VALUES (%s,%s,%s,%s,%s,%s,1)
                       ON CONFLICT (tenant_id, email) DO UPDATE SET
                           token_hash   = EXCLUDED.token_hash,
                           invited_by   = EXCLUDED.invited_by,
                           expires_at   = EXCLUDED.expires_at,
                           last_sent_at = EXCLUDED.last_sent_at,
                           send_count   = recipient_invites.send_count + 1""",
                    (tenant_id, e, _h(token), invited_by,
                     now + INVITE_TTL, now))
        finally:
            conn.close()
    except Exception:                                    # noqa: BLE001
        log.exception("could not mint a recipient invite for %s", e)
        return None
    return token


def accept(token: str) -> dict | None:
    """Burn the token and enrol the address. Returns the row on success,
    None when the link is unknown, expired or already answered.

    Atomic by construction: the UPDATE matches only a row that still holds
    this hash, so a double submit (two tabs, a mail client racing a human)
    can only succeed once."""
    if not token:
        return None
    try:
        conn = _conn()
        try:
            with conn.transaction():
                return conn.execute(
                    """UPDATE recipient_invites
                          SET accepted_at = now()
                        WHERE token_hash = %s AND expires_at > now()
                          AND accepted_at IS NULL AND declined_at IS NULL
                    RETURNING id, tenant_id, email""",
                    (_h(token),)).fetchone()
        finally:
            conn.close()
    except Exception:                                    # noqa: BLE001
        log.exception("recipient invite accept failed")
        return None


def decline(token: str) -> dict | None:
    """"No thanks", from the same link. Stamps `declined_at`, which both
    keeps the address off the mail and stops any later re-add from asking
    again (see `invite`)."""
    if not token:
        return None
    try:
        conn = _conn()
        try:
            with conn.transaction():
                return conn.execute(
                    """UPDATE recipient_invites
                          SET declined_at = now()
                        WHERE token_hash = %s AND expires_at > now()
                          AND accepted_at IS NULL AND declined_at IS NULL
                    RETURNING id, tenant_id, email""",
                    (_h(token),)).fetchone()
        finally:
            conn.close()
    except Exception:                                    # noqa: BLE001
        log.exception("recipient invite decline failed")
        return None


# ---- reads ----------------------------------------------------------------


def lookup(token: str) -> dict | None:
    """The invite a link refers to, WITHOUT answering it.

    A mail scanner prefetching the link must
    not be able to accept an invite on the recipient's behalf — that would be
    a machine consenting to receive someone's finances. The GET renders a
    page; only the POST calls `accept`."""
    if not token:
        return None
    try:
        conn = _conn()
        try:
            return conn.execute(
                """SELECT i.id, i.tenant_id, i.email, i.expires_at,
                          i.created_at, u.email AS invited_by_email
                     FROM recipient_invites i
                     LEFT JOIN users u ON u.id = i.invited_by
                    WHERE i.token_hash = %s AND i.expires_at > now()
                      AND i.accepted_at IS NULL AND i.declined_at IS NULL""",
                (_h(token),)).fetchone()
        finally:
            conn.close()
    except Exception:                                    # noqa: BLE001
        log.exception("recipient invite lookup failed")
        return None


def answered(token: str) -> dict | None:
    """The row a SPENT link belongs to — `{email, state}` where state is
    `accepted` or `declined`, or None if the token was never real.

    Exists so a person who re-opens their own invite mail is told what they
    already chose, instead of the generic "invalid, expired, or already
    answered", which is actively misleading: it sends a recipient who has
    DECLINED to ask the household to add them again, which `invite()` refuses
    by design, leaving both parties silently stuck.

    Reveals nothing a holder of the link doesn't have: they were mailed that
    address, and they are the one who answered."""
    if not token:
        return None
    try:
        conn = _conn()
        try:
            r = conn.execute(
                """SELECT email, accepted_at, declined_at
                     FROM recipient_invites
                    WHERE token_hash = %s
                      AND (accepted_at IS NOT NULL
                           OR declined_at IS NOT NULL)""",
                (_h(token),)).fetchone()
        finally:
            conn.close()
    except Exception:                                    # noqa: BLE001
        log.exception("recipient invite answered-lookup failed")
        return None
    if r is None:
        return None
    return {"email": r["email"],
            "state": "accepted" if r["accepted_at"] else "declined"}


def states(tenant_id, emails: list[str]) -> dict[str, dict]:
    """{lowercased address: {state, invited_at, expires_at, ...}} for the
    addresses asked about — the Settings grid's whole server input.

    `state` is one of: `invited` (link sent, unanswered), `expired`,
    `accepted`, `declined`. An address with no row is absent from the map,
    which is how "typed but never saved" stays distinguishable from
    "invited": the app has genuinely never heard of it."""
    wanted = [e for e in {_norm(x) for x in emails} if e]
    if not wanted:
        return {}
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                """SELECT email, created_at, expires_at, accepted_at,
                          declined_at, last_sent_at, send_count
                     FROM recipient_invites
                    WHERE tenant_id = %s AND email = ANY(%s)""",
                (tenant_id, wanted)).fetchall()
        finally:
            conn.close()
    except Exception:                                    # noqa: BLE001
        log.exception("recipient invite states failed")
        return {}
    now = dt.datetime.now(dt.timezone.utc)
    out = {}
    for r in rows:
        if r["accepted_at"]:
            state = "accepted"
        elif r["declined_at"]:
            state = "declined"
        elif r["expires_at"] and r["expires_at"] <= now:
            state = "expired"
        else:
            state = "invited"
        out[r["email"]] = {
            "state": state,
            "invited_at": r["created_at"],
            "expires_at": r["expires_at"],
            "accepted_at": r["accepted_at"],
            "last_sent_at": r["last_sent_at"],
            "send_count": r["send_count"],
        }
    return out


def accepted(tenant_id, emails: list[str]) -> set[str]:
    """The subset of these addresses that agreed to receive the mail —
    lowercased. The worker's gate; everything else here is presentation."""
    st = states(tenant_id, emails)
    return {e for e, v in st.items() if v["state"] == "accepted"}
