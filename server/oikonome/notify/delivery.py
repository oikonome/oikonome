"""does the mail we send actually arrive?

Without this module the answer is "nobody knows". A typo'd domain hard-
bounces, the provider suppresses the address, and every later send is
refused before it leaves — with the only trace a log line in a worker
nobody reads. The person whose money the email is about sees nothing at
all.

What this does, in one sentence: **remember which addresses are failing, so
the app can stop mailing a wall and say so where the owner will see it.**

Three ways a failure gets in here, because no single one sees everything:

  * **The provider webhook** (`web/postmark_webhook.py`) — the authoritative
    signal, and the only one that knows a bounce happened *after* the relay
    accepted the message. Carries the provider's bounce id, which is what
    reactivation needs later.
  * **The send path** (`report.send_each`) — catches what the webhook cannot:
    a send REFUSED at submission time. Once an address is on the provider's
    suppression list this is the only signal left, which is the shape a
    typo'd address takes from its second day onward.
  * **Nothing else.** In particular an open/read is NOT evidence either way —
    a mailbox provider's image proxy prefetches pixels seconds after
    delivery, so an "open" says nothing about a human.

State lives while something is wrong and is DELETED when it is fixed, so
"is this address broken" is one indexed lookup and there is no half-cleared
state. WRITES run on the admin connection and reads on the pooled app one
(migration 073 grants exactly that): a row here pauses an address's mail, so
forging one must stay out of reach of injected app-role SQL, while reading
one exposes nothing that `users` does not already.

Never raises into a caller. A delivery-state failure must not be able to stop
an email from being attempted, or break a webhook, or fail a signup — being
unsure whether mail arrives is strictly better than not sending it.
"""

from __future__ import annotations

import logging
import os
import re

from ..db import tenancy

log = logging.getLogger("oikonome.delivery")

# Provider verdicts that mean THIS ADDRESS IS NOT REACHABLE and re-sending
# will not help. Deliberately narrow: a transient failure (mailbox full,
# greylisting, a relay hiccup) must never pause someone's mail, because the
# cost of a wrong pause is silence — exactly the failure being fixed.
HARD_TYPES = frozenset({
    "HardBounce",           # unknown user / domain — the typo case
    "BadEmailAddress",      # provider rejected the address as malformed
    "SpamNotification",     # recipient hit "this is spam"
    "SpamComplaint",
    "ManuallyDeactivated",  # an operator killed it at the provider
    "Blocked",
})
# Deliberately NOT here: Unsubscribe / SubscriptionChange. Oikonome sends no
# broadcast or marketing mail, so those cannot arrive on this stream — and if
# that ever changes they need a state of their own, because telling someone
# who chose to unsubscribe that "your email isn't arriving" is both wrong and
# a nudge to undo a decision they made on purpose.

# Postmark's "you tried to send to an inactive recipient" — API error 406 on
# the SMTP submission. This is what a suppressed address looks like from the
# send side, and matching it is how the send path learns anything at all.
_INACTIVE_RE = re.compile(r"inactive|error\s*code:?\s*'?406", re.I)
# A PERMANENT SMTP failure at submission (the relay refused outright).
#
# Tight on purpose. A false positive here pauses someone's mail, which is the
# exact silence this whole feature exists to end — so the cost of matching too
# eagerly is worse than the cost of missing one bounce and catching it on the
# next send. A bare `\b55\d\b` is the shape to avoid — any message quoting a
# byte count or a port satisfies it.
#
# What is left matches only things that ARE the reply code: the enhanced
# status (`5.1.1`), smtplib's own repr of a refusal (`(550, b'...')` /
# `{'a@b.com': (550, ...)}`), and the unambiguous phrases.
_PERMANENT_RE = re.compile(
    r"\b5\.\d\.\d\b"                 # enhanced status code
    r"|\(\s*5\d\d\s*,"               # smtplib error tuple: "(550, b'…'"
    r"|unknown user|user unknown|no such user"
    r"|does not exist|recipient (address )?rejected"
    r"|mailbox (unavailable|not found)|address rejected",
    re.I)
# Positive evidence the reply was TRANSIENT (4xx). Postfix's
# standard greylist/deferral text — "450 4.7.1 <addr>: Recipient address
# rejected: ..." — carries a phrase from the permanent set, so one greylist
# event would write a HardBounce row the hold then makes permanent by
# construction (clear() only runs on a successful send, which the hold
# itself prevents). A 4.x.x enhanced status or a (4xx, smtplib tuple
# vetoes the phrase branch; same bare-number discipline as above.
_TRANSIENT_RE = re.compile(r"\b4\.\d\.\d\b|\(\s*4\d\d\s*,", re.I)


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _write_conn():
    """Writes are admin-only: a row here PAUSES an address's mail, so an
    injected app-role INSERT would be a way to silence someone."""
    return tenancy.admin_connect()


def _read_conn():
    """Reads come off the POOLED app-role connection. `state_for` is on
    /api/me, which runs on every page load, and `admin_connect` opens a
    fresh unpooled connection per call — putting that on the hot path would
    trade a visibility fix for a connection-budget problem."""
    return tenancy.control_connect()


# ---- writes ---------------------------------------------------------------


def record_failure(email: str, *, state: str = "bouncing",
                   bounce_type: str | None = None, reason: str | None = None,
                   provider_id: str | None = None,
                   suppressed: bool = False) -> None:
    """Remember that mail to this address failed. Idempotent per address:
    a repeat bumps the count and the timestamp rather than piling up rows,
    and a later report that carries a provider_id or a suppression flag
    fills those in without losing the first sighting."""
    e = _norm(email)
    if not e:
        return
    try:
        conn = _write_conn()
        try:
            conn.execute(
                """INSERT INTO email_delivery_state
                       (email, state, bounce_type, reason, provider_id,
                        suppressed)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (email) DO UPDATE SET
                       state       = EXCLUDED.state,
                       bounce_type = COALESCE(EXCLUDED.bounce_type,
                                              email_delivery_state.bounce_type),
                       reason      = COALESCE(EXCLUDED.reason,
                                              email_delivery_state.reason),
                       provider_id = COALESCE(EXCLUDED.provider_id,
                                              email_delivery_state.provider_id),
                       suppressed  = email_delivery_state.suppressed
                                     OR EXCLUDED.suppressed,
                       fail_count  = email_delivery_state.fail_count + 1,
                       last_seen   = now()""",
                (e, state, bounce_type, (reason or "")[:500] or None,
                 provider_id, suppressed))
        finally:
            conn.close()
        log.warning("delivery: %s is %s (%s)", e, state,
                    bounce_type or reason or "no detail")
    except Exception:                              # noqa: BLE001
        log.exception("delivery: could not record failure for %s", e)


def clear(email: str, *, reactivate: bool = True) -> None:
    """This address works again — forget the failure.

    Called on a successful send and on a fresh verification. `reactivate`
    also asks the provider to lift its own suppression: without that a
    corrected address stays dead upstream however clean our table is.
    """
    e = _norm(email)
    if not e:
        return
    row = None
    try:
        conn = _write_conn()
        try:
            row = conn.execute(
                "DELETE FROM email_delivery_state WHERE email=%s "
                "RETURNING provider_id, suppressed", (e,)).fetchone()
        finally:
            conn.close()
    except Exception:                              # noqa: BLE001
        log.exception("delivery: could not clear state for %s", e)
        return
    if row:
        log.info("delivery: %s cleared", e)
    if reactivate and row and row["suppressed"] and row["provider_id"]:
        _reactivate(e, row["provider_id"])


def record_send_failure(email: str, error: str) -> None:
    """Classify an exception raised by the send path. Only a PERMANENT
    failure is recorded — a timeout or a connection reset says nothing
    about the address, and pausing someone's mail over one is worse than
    the problem."""
    text = error or ""
    if _INACTIVE_RE.search(text):
        record_failure(email, bounce_type="Suppressed", reason=text,
                       suppressed=True)
    elif _PERMANENT_RE.search(text) and not _TRANSIENT_RE.search(text):
        # A phrase match with 4xx evidence alongside it is a deferral
        # (greylisting, reject_unverified_recipient) — say nothing and let
        # the next scheduled send find out
        record_failure(email, bounce_type="HardBounce", reason=text)
    # anything else: transient until the provider says otherwise


# ---- reads ----------------------------------------------------------------


def state_for(email: str) -> dict | None:
    """The failure row for one address, or None when there is no reason to
    think it is broken."""
    e = _norm(email)
    if not e:
        return None
    try:
        conn = _read_conn()
        try:
            return conn.execute(
                """SELECT email, state, bounce_type, reason, suppressed,
                          fail_count, first_seen, last_seen
                   FROM email_delivery_state WHERE email=%s""",
                (e,)).fetchone()
        finally:
            conn.close()
    except Exception:                              # noqa: BLE001
        log.exception("delivery: lookup failed for %s", e)
        return None


def states(emails: list[str]) -> dict[str, dict]:
    """{lowercased address: its failure row} for the addresses asked about.

    One query for the whole list. `failing()` already fetched the set in a
    single round trip and then `state_for()` was called PER failing address,
    each opening its own connection — so a household with four bouncing
    recipients paid five connections to render one Settings card. An address
    with no failure is simply absent."""
    wanted = [_norm(e) for e in emails if _norm(e)]
    if not wanted:
        return {}
    try:
        conn = _read_conn()
        try:
            rows = conn.execute(
                """SELECT email, state, bounce_type, reason, suppressed,
                          fail_count, first_seen, last_seen
                   FROM email_delivery_state WHERE email = ANY(%s)""",
                (wanted,)).fetchall()
        finally:
            conn.close()
        return {r["email"]: r for r in rows}
    except Exception:                              # noqa: BLE001
        # same posture as `failing`: unreadable means "no reason to think
        # anything is broken", so mail keeps flowing
        log.exception("delivery: bulk state lookup failed")
        return {}


def failing(emails: list[str]) -> set[str]:
    """Which of these addresses are known-broken (lowercased). Used to hold
    the scheduled cadences back; a lookup failure returns the EMPTY set, so
    the mail still goes out when this table is unreadable."""
    wanted = [_norm(e) for e in emails if _norm(e)]
    if not wanted:
        return set()
    try:
        conn = _read_conn()
        try:
            rows = conn.execute(
                "SELECT email FROM email_delivery_state WHERE email = ANY(%s)",
                (wanted,)).fetchall()
        finally:
            conn.close()
        return {r["email"] for r in rows}
    except Exception:                              # noqa: BLE001
        log.exception("delivery: bulk lookup failed")
        return set()


def hold(emails: list[str]) -> tuple[list[str], list[str]]:
    """Split a recipient list into (send, held). The held ones are addresses
    the provider has already told us are unreachable — mailing them again
    produces a rejected send every morning and teaches nobody anything.

    Only the SCHEDULED cadences use this. Transactional mail (verification,
    password reset, sign-in unlock) is always attempted, because those are
    the messages a person needs when they are in the middle of FIXING the
    address, and a locked door is worse than a wasted send."""
    bad = failing(emails)
    send = [e for e in emails if _norm(e) not in bad]
    held = [e for e in emails if _norm(e) in bad]
    return send, held


# ---- provider reactivation ------------------------------------------------


def _postmark_token() -> str | None:
    """The Postmark server token. It is already in the environment as the
    SMTP credential — Postmark uses the server token as both the SMTP
    username and password — so reactivation needs no new secret."""
    tok = (os.environ.get("OIKONOME_POSTMARK_TOKEN") or "").strip()
    if tok:
        return tok
    host = (os.environ.get("OIKONOME_SMTP_HOST") or "").lower()
    if "postmark" in host:
        return (os.environ.get("OIKONOME_SMTP_USER") or "").strip() or None
    return None


def _reactivate(email: str, provider_id: str) -> None:
    """Lift the provider's own suppression. Best-effort and quiet: if this
    fails the local state is still cleared, and the next send failing will
    simply write the row again — which is the correct outcome, because the
    address really is still dead upstream."""
    token = _postmark_token()
    if not token:
        log.info("delivery: no provider token; %s cleared locally only", email)
        return
    # the id lands in the URL path with our server token attached — refuse
    # anything but Postmark's numeric bounce id (rows stored before the
    # webhook validated the shape can still carry junk)
    if not provider_id.isdigit():
        log.warning("delivery: non-numeric provider id for %s; "
                    "cleared locally only", email)
        return
    try:
        import httpx
        r = httpx.put(
            f"https://api.postmarkapp.com/bounces/{provider_id}/activate",
            headers={"Accept": "application/json",
                     "X-Postmark-Server-Token": token},
            timeout=10.0)
        if r.status_code == 200:
            log.info("delivery: reactivated %s upstream", email)
        else:
            log.warning("delivery: reactivating %s returned %s: %s",
                        email, r.status_code, r.text[:200])
    except Exception:                              # noqa: BLE001
        log.exception("delivery: reactivation call failed for %s", email)
