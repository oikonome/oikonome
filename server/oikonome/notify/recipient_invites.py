"""Nobody is added to a household's financial mail without saying yes.

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

    None means "do not mail this person", for any of these reasons:
      * they already accepted — re-adding someone who once said yes does not
        ask them again. Consent is not revoked by being taken off a list.
      * they DECLINED — the one answer that must survive being re-added. A
        recipient who said no and gets re-invited every time the owner edits
        their settings has been given a nuisance, not a choice.
      * the per-address cooldown or the per-tenant hourly cap is in force.
      * the address is empty or the write failed.

    `resend` is the door that needs to know WHICH of those it was."""
    return _mint(tenant_id, email, invited_by=invited_by)[0]


def _mint(tenant_id, email: str, *, invited_by=None,
          recheck_held: bool = False,
          can_link: bool = True) -> tuple[str | None, str, dict | None]:
    """(raw token or None, reason, previous link). Reasons: `minted`,
    `recheck`, `accepted`, `declined`, `cooldown`, `cap`, `no_mail`,
    `invalid`, `error`.

    The previous link is the `token_hash` / `expires_at` a `minted` answer
    replaced (None on a first invite), for `keep_previous_link`: a rotated
    link whose mail never left must not be the only one that works.

    `can_link=False` says no invitation could be mailed right now, so
    where a fresh link would be minted the answer is `no_mail` and nothing
    is written: rotating the token then would only kill the link the
    person may already hold, and stamping the cooldown would block the
    Resend that works once the install is fixed.

    With `recheck_held`, an accepted address whose delivery is failing is
    not a no-op: the slot is stamped against the same cooldown and hourly
    cap an invite uses and the answer is `recheck`, telling the caller to
    send a delivery check. The token is NOT rotated, so the link they
    already answered keeps saying what they chose."""
    e = _norm(email)
    if not e:
        return None, "invalid", None
    held = False
    if recheck_held:
        from . import delivery
        held = e in delivery.failing([e])
    token = secrets.token_urlsafe(32)
    now = dt.datetime.now(dt.timezone.utc)
    prev = None
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
                    "SELECT accepted_at, declined_at, last_sent_at, "
                    "token_hash, expires_at "
                    "FROM recipient_invites "
                    "WHERE tenant_id=%s AND email=%s FOR UPDATE",
                    (tenant_id, e)).fetchone()
                if row:
                    prev = {"token_hash": row["token_hash"],
                            "expires_at": row["expires_at"]}
                if row and row["declined_at"]:
                    return None, "declined", None
                if row and row["accepted_at"] and not held:
                    return None, "accepted", None
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
                    return None, "cooldown", None
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
                    return None, "cap", None
                if row and row["accepted_at"]:
                    # a delivery check counts against the same throttles
                    # as an invite, so Resend on a dead address cannot be
                    # used to hammer it either
                    conn.execute(
                        "UPDATE recipient_invites SET last_sent_at=%s, "
                        "send_count = send_count + 1 "
                        "WHERE tenant_id=%s AND email=%s", (now, tenant_id, e))
                    return None, "recheck", None
                if not can_link:
                    return None, "no_mail", None
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
        return None, "error", None
    return token, "minted", prev


def keep_previous_link(tenant_id, email: str, token: str,
                       prev: dict | None) -> None:
    """The mail carrying `token` never went out, so the link it replaced
    goes back: a refused or failed Resend must not leave the person
    holding a dead link while the live one went to the server log. Only
    the row still carrying the unsent token is touched, so a newer mint
    is never undone; the send stamp stays, because the relay was asked.
    A first invite has nothing to go back to."""
    if not prev or not prev.get("token_hash"):
        return
    e = _norm(email)
    try:
        conn = _conn()
        try:
            conn.execute(
                "UPDATE recipient_invites SET token_hash=%s, expires_at=%s "
                "WHERE tenant_id=%s AND email=%s AND token_hash=%s",
                (prev["token_hash"], prev["expires_at"], tenant_id, e,
                 _h(token)))
        finally:
            conn.close()
    except Exception:                                    # noqa: BLE001
        log.exception("could not keep the previous invite link for %s", e)


def need_base_url(smtp) -> bool:
    """True when mail goes out but no invitation can: there is a relay,
    and no pinned OIKONOME_BASE_URL to build the link under (a link is
    never built from request headers). An added recipient then never
    receives an invitation, so never accepts, so never gets the daily
    email — the status door says so, so the clients can name the fix."""
    import os
    return bool(smtp and smtp.get("configured")
                and not os.environ.get("OIKONOME_BASE_URL", "").rstrip("/"))


def resend(tenant_id, email: str, *, invited_by=None,
           inviter: str | None = None) -> dict:
    """The Resend button: `{"sent": bool, "reason": str}`, synchronously.

    `sent` is true only when mail was handed to the relay (or, for an
    invite on a mail-less install, to the server log that stands in for
    it). Reasons when something went out: `invited` (a fresh link) and
    `delivery_check`. Reasons when nothing did: `accepted` (already
    receiving the mail, nothing to recover), `declined`, `complained`,
    `cooldown`, `cap`, `undelivered` (the relay refused the mail),
    `no_mail` (no relay configured for a check, or a relay but no
    OIKONOME_BASE_URL to build an invitation link under), `blocked`,
    `invalid`, `error`.

    An accepted recipient whose mail is held gets a delivery check rather
    than a new invitation: they already said yes, and asking again would
    revoke nothing but would make the mail wait on a second click. The
    check goes through the household send path, which lifts the hold when
    the relay accepts it and records the refusal when it does not."""
    e = _norm(email)
    if e:
        from . import delivery
        st = delivery.state_for(e)
        if st and st["state"] == "complained":
            # a spam complaint is the recipient refusing the mail itself;
            # an owner's Resend must not override it
            return {"sent": False, "reason": "complained"}
    from ..web.app import (_deliver_recipient_invite, _tenant_smtp,
                           record_unbuildable_link, smtp_unresolved)
    smtp = _tenant_smtp(tenant_id)
    if smtp_unresolved(smtp):
        # the relay settings could not be read: that is not an install
        # without mail, where the log line is the delivery — nothing was
        # sent, the row is untouched, and the owner is told so
        return {"sent": False, "reason": "error"}
    # a delivery check carries no link and is unaffected
    can_link = not need_base_url(smtp)
    token, reason, prev = _mint(tenant_id, email, invited_by=invited_by,
                                recheck_held=True, can_link=can_link)
    if reason == "minted":
        delivered, how = _deliver_recipient_invite(
            email, token, tenant_id, inviter, smtp=smtp, resolved=True)
        if delivered:
            return {"sent": True, "reason": "invited"}
        keep_previous_link(tenant_id, email, token, prev)
        return {"sent": False, "reason": how}
    if reason == "recheck":
        return _send_delivery_check(tenant_id, email, inviter)
    if reason == "no_mail" and not can_link:
        # the same record the settings save writes for a link it could
        # not build, so the row says "not arriving — set OIKONOME_BASE_URL"
        # instead of a bare "not sent" beside an "invite sent" badge
        record_unbuildable_link(email)
    return {"sent": False, "reason": reason}


def _delivery_check_body(inviter: str | None) -> tuple[str, str]:
    import html as _html
    who = inviter or "A household"
    plain = (
        f"{who} asked Oikonome to check that mail reaches this address.\n\n"
        f"You agreed to receive their daily summary, and recent messages "
        f"to you did not arrive, so it was paused. This message arriving "
        f"means it can start again. There is nothing to do.\n\n"
        f"To stop receiving the summary, ask them to remove you.\n")
    w = _html.escape(who)
    htm = (
        f"<p><b>{w}</b> asked Oikonome to check that mail reaches this "
        f"address.</p>"
        f"<p>You agreed to receive their daily summary, and recent messages "
        f"to you did not arrive, so it was paused. This message arriving "
        f"means it can start again. There is nothing to do.</p>"
        f"<p style=\"color:#666\">To stop receiving the summary, ask them to "
        f"remove you.</p>")
    return plain, htm


def _send_delivery_check(tenant_id, email: str, inviter) -> dict:
    """One check mail through `report.send_each`, the path the cadences
    use, so healing and recording are the same code as a real send: a
    relay that accepts the check is what lifts the hold, and a permanent
    refusal records itself. The hold is NOT lifted beforehand. Lifting it
    on speculation and putting it back on failure lost both ways: a
    timeout records nothing, so the hold had to be restored by hand, and
    that restore also undid a scheduled send to the same address that
    succeeded while the check was in flight. Left in place, the row is
    exactly as true after a failed check as before it.

    A provider-side suppression the app's own sends caused is lifted
    first, or the provider would refuse the check before it left (never
    one an operator or the provider decided on). A complaint is never
    cleared, including one that arrived after resend() first looked."""
    from ..web import report, spamdefense
    from ..web.app import _tenant_smtp, smtp_unresolved
    from . import delivery
    if spamdefense.recipient_blocked(email):
        return {"sent": False, "reason": "blocked"}
    smtp = _tenant_smtp(tenant_id)
    if smtp_unresolved(smtp):
        return {"sent": False, "reason": "error"}
    if not smtp or not smtp.get("configured"):
        return {"sent": False, "reason": "no_mail"}
    st = delivery.state_for(email)
    if st and st["state"] == "complained":
        return {"sent": False, "reason": "complained"}
    # An address the operator deactivated at the provider, or the provider
    # blocked, stays as it is: the household's Resend must not reverse a
    # decision it did not make, with the operator's provider credential.
    if st and st.get("bounce_type") in delivery.PROVIDER_DECISIONS:
        return {"sent": False, "reason": "blocked"}
    if st and st.get("suppressed") and st.get("provider_id"):
        delivery._reactivate(delivery._norm(email), st["provider_id"])
        # the row now says what the provider says; the send path's own
        # clear on success then has nothing left to lift upstream
        delivery.mark_reactivated(email)
    plain, htm = _delivery_check_body(inviter)
    subject = (f"{inviter} is checking that their Oikonome summary reaches "
               f"you" if inviter else
               "Checking that your Oikonome summary reaches you")
    try:
        res = report.send_each(subject, plain, htm, [email], smtp=smtp)
    except Exception:                                    # noqa: BLE001
        log.exception("delivery check to %s failed", email)
        res = None
    if res and res[0].get("ok"):
        return {"sent": True, "reason": "delivery_check"}
    return {"sent": False, "reason": "undelivered" if res else "error"}


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


def carry_acceptance(tenant_id, entries) -> None:
    """A stored entry in display form (`Name <a@x.example>`) names one
    person, and a consent recorded under that whole string is theirs. When
    a save rewrites the entry to its bare mailbox, the consent goes with
    it — otherwise the person who already said yes stops getting mail and
    is asked again. Only a one-mailbox entry carries: a joined `a;b` is two
    people, and one answer is not both of theirs. The bare mailbox's own
    answer, a decline above all, is never overwritten."""
    from ..web.mailguard import split_mailboxes
    pairs = []
    for entry in (entries if isinstance(entries, (list, tuple))
                  else [entries]):
        raw = _norm(str(entry or ""))
        good, _bad = split_mailboxes([entry])
        if raw and len(good) == 1 and _norm(good[0]) != raw:
            pairs.append((raw, _norm(good[0])))
    if not pairs:
        return
    try:
        conn = _conn()
        try:
            with conn.transaction():
                for raw, box in pairs:
                    conn.execute(
                        """INSERT INTO recipient_invites
                               (tenant_id, email, accepted_at)
                           SELECT tenant_id, %s, accepted_at
                             FROM recipient_invites
                            WHERE tenant_id = %s AND email = %s
                              AND accepted_at IS NOT NULL
                           ON CONFLICT (tenant_id, email) DO UPDATE
                              SET accepted_at = EXCLUDED.accepted_at
                            WHERE recipient_invites.accepted_at IS NULL
                              AND recipient_invites.declined_at IS NULL""",
                        (box, tenant_id, raw))
        finally:
            conn.close()
    except Exception:                                    # noqa: BLE001
        log.exception("could not carry a recipient's consent to its "
                      "mailbox")


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
