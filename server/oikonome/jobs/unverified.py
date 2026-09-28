"""Signups that never confirmed their address.

A hosted signup opens a working account at once and mails one link; the
account stays usable whether or not the link is ever clicked. Nothing
followed from not clicking it, so an abandoned signup — a typo'd address,
someone trying the product for an afternoon, a squatter parked on an
address they do not own — sat forever: a row the operator counts, a tenant
every sweep visits, an address its real owner can only reach through the
reclaim door. This module is what follows.

Three steps, nightly, hosted only, for households in which NOBODY has ever
confirmed an address:

* day 7 (the first link's lifetime) — one reminder per person, carrying a
  fresh link, sent once;
* day ``reap_days()`` (30) — the household is frozen under STATUS with a
  GRACE_DAYS window, the owner is mailed a fresh link and the date, and the
  frozen state says how to get out (the resend door stays open);
* the window lapses — the ordinary scheduled purge erases it, receipt and
  external release included, exactly like a deletion the operator or the
  owner asked for. There is no second wipe path.

Clicking any of the links confirms the address on the spot (`confirm`);
a frozen household comes back when the person presses the button that
click lands on (`reopen`), which is a POST no mail scanner sends.

"Never confirmed" is decided on ``first_verified_at`` as well as
``verified_at``: a hosted email change clears ``verified_at`` on a
household that once proved a mailbox, and that household is nobody's
abandoned signup. Money attached to the household ends the question too —
a squatter cannot plant a subscription, and a paying household is not
abandoned however unread its mail. Whether the household holds records is
deliberately NOT asked: "unverified" is the normal state of a household
whose mail went to spam, banks linked and all, and that household is
exactly who the reminder, the freeze and the fresh links are for — the
freeze is what finally reaches a person the mail did not.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

from .. import ext
from ..auth import email_verify
from ..db import tenancy
from ..envnum import env_flag
from ..notify import account_mail, delivery

log = logging.getLogger("oikonome.jobs")

# the tenant status of a household frozen for never confirming — the
# product's own, beside 'pending_delete'; app.LOCKOUT_STATUSES carries its
# HTTP answer and the console's restore lifts it like the others
STATUS = "unverified_expired"
# the reminder goes out when the first link dies, which is what it is for
REMIND_AFTER = email_verify.VERIFY_TTL
# the freeze always gives a full week: a "deleted in 0 days" notice is not
# a notice, so the console's own grace setting is not consulted here
GRACE_DAYS = 7
# the single-flight key for the nightly sweep, one for the whole fleet:
# what two overlapping runs duplicate is MAIL, which is per person, not
# per tenant
_SWEEP_LOCK = "oikonome:unverified-sweep"


def reap_days() -> int:
    """Days from signup to the freeze. OIKONOME_UNVERIFIED_REAP_DAYS,
    default 30; 0 turns the whole sweep off (reminders included)."""
    try:
        return max(0, int(os.environ.get("OIKONOME_UNVERIFIED_REAP_DAYS",
                                         "30")))
    except ValueError:
        return 30


def enabled() -> bool:
    # self-host accounts are born verified, so the sweep would find nothing
    # there; gated explicitly all the same, so a self-host row that lost
    # verified_at some other way is never frozen by a policy written for
    # public signup
    return env_flag("OIKONOME_HOSTED") and reap_days() > 0


# ---- the /api/me deadline ----------------------------------------------------


def deadline(conn, tenant_id) -> dt.datetime | None:
    """When this household is frozen if nobody confirms — the SPA prints it
    under the verify nudge. None when the sweep is off or someone in the
    household has ever confirmed."""
    if not enabled():
        return None
    row = conn.execute(
        """SELECT min(created_at) AS since,
                  bool_or(verified_at IS NOT NULL
                          OR first_verified_at IS NOT NULL) AS proven
             FROM users WHERE tenant_id = %s""", (tenant_id,)).fetchone()
    if row is None or row["proven"] or row["since"] is None:
        return None
    return row["since"] + dt.timedelta(days=reap_days())


# ---- the click, and the button behind it ------------------------------------
#
# Confirming an address and reopening a household are two acts, and only
# the first may happen on a GET. Mail security products fetch every link
# in a message before the human sees it, so a GET does whatever the
# scanner decides to do. Proving the mailbox receives this instance's
# mail is safe under that rule — the scanner's fetch proves exactly the
# same thing the human's click would. Bringing a frozen household back
# from the edge of deletion is not: it would hand the scanner a veto over
# the one mechanism that erases signups nobody ever answered for, and on
# a SQUATTED address it is the victim's own provider that would keep the
# squatter's household alive. So the reopen lives behind a button, which
# is a POST, which no scanner sends — the same split /reset, /unlock and
# the signup-reclaim door already use, and the same Origin check guards
# every write in this app.
#
# A household confirmed only by a scanner therefore stays frozen and is
# purged on schedule. The address is verified, which does close it to
# the nightly sweep for good, but the sweep is not what erases it: the
# ordinary scheduled purge reads `status` and `delete_after`, which this
# path never touches.


def confirm(admin, verification_id, user_id) -> bool:
    """Spend the verification token and stamp the address verified.

    Never touches the household's status: see the note above. A click
    landing while the purge is wiping this household spends a token that
    is about to be deleted with its user, which is harmless — nothing
    here claims the household was saved."""
    return email_verify.consume(admin, verification_id, user_id)


def frozen_household(admin, user_id) -> dict | None:
    """The {id, delete_after} of this user's household if it is frozen
    for never confirming, else None — what tells the confirmed page
    whether to offer the reopen button."""
    return admin.execute(
        "SELECT t.id, t.delete_after FROM tenants t "
        "JOIN users u ON u.tenant_id = t.id "
        "WHERE u.id = %s AND t.status = %s", (user_id, STATUS)).fetchone()


def reopen(admin, user_id) -> str | None:
    """Lift the freeze on the household of a user who has just proved
    their address. Returns the tenant id, or None when there was nothing
    frozen to lift.

    The tenant row is locked with the same FOR UPDATE the nightly purge
    takes, and the status is re-read under it: a reopen landing while the
    purge is choosing its victims either wins (and the purge's own locked
    re-read finds nothing to erase) or arrives after the wipe (and the
    user is gone with the household, so there is nothing to reopen).

    Only a VERIFIED user reopens anything, and that is the whole of what
    the token has to have achieved: ANY unexpired link this mailbox was
    sent will do, including one a newer mint superseded, because minting
    burns the older link and nothing can tell that shape from a link a
    scanner spent. All of them were mailed to the same address, none
    outlives its week, and none of them lifts anything until that address
    is proved — so the door is as wide as the mailbox and no wider.
    Demanding the newest link would only strand a person who opens the
    first of two mails. The household is the user's own, so a link for
    one household can never reopen another. The caller tells the
    installed gate about the restore OUTSIDE this transaction — a round
    trip to a billing service is not held across a row lock."""
    with admin.transaction():
        t = admin.execute(
            "SELECT t.id, t.status, t.status_before_delete FROM tenants t "
            "JOIN users u ON u.tenant_id = t.id "
            "WHERE u.id = %s AND u.verified_at IS NOT NULL "
            "FOR UPDATE OF t", (user_id,)).fetchone()
        if t is None or t["status"] != STATUS:
            return None
        admin.execute(
            "UPDATE tenants SET status=%s, delete_after=NULL, "
            "status_before_delete=NULL WHERE id=%s",
            (t["status_before_delete"] or "active", t["id"]))
        from ..web.adminconsole import _audit
        _audit(admin, "tenant_delete_restored", target=str(t["id"]),
               detail="restored_to=active reason=email_confirmed",
               ip="verify-email")
        return str(t["id"])


# ---- the nightly sweep ---------------------------------------------------------


def sweep(now: dt.datetime | None = None) -> dict:
    """The reminder and the freeze, over every active household nobody has
    ever confirmed. Returns {"reminded": [user ids], "scheduled": [tenant
    ids], "held": [tenant ids]} — held = due for the freeze but not frozen
    tonight because the notice could not be sent (retried tomorrow). A
    run that finds another sweep already in flight returns all three
    empty and leaves the work to it."""
    out: dict[str, list] = {"reminded": [], "scheduled": [], "held": []}
    if not enabled():
        return out
    now = now or dt.datetime.now(dt.timezone.utc)
    admin = tenancy.admin_connect()
    try:
        # Single-flight, the same session-level TRY lock every other
        # nightly sweep takes: the console's run-job button enqueues a
        # fresh nightly on every click, so a manual run can overlap the
        # cron's. What overlapping runs duplicate here is MAIL — both read
        # `verify_reminded_at IS NULL` before either stamps it, and both
        # compute and send a final notice before either takes the row lock
        # — so a person gets the same "confirm your email" or "will be
        # deleted" letter twice. Skipping rather than waiting: the run in
        # flight is doing this exact sweep, and queueing behind it to send
        # the second copy anyway would miss the point.
        locked = admin.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (_SWEEP_LOCK,)).fetchone()["ok"]
        if not locked:
            log.info("unverified sweep already running — skipping")
            return out
        try:
            return _sweep_locked(admin, now, out)
        finally:
            tenancy.release_lock(admin, _SWEEP_LOCK)
    finally:
        admin.close()


def _sweep_locked(admin, now: dt.datetime, out: dict) -> dict:
    rows = admin.execute(
        """SELECT t.id, min(u.created_at) AS since
             FROM tenants t JOIN users u ON u.tenant_id = t.id
            WHERE coalesce(t.status, 'active') = 'active'
            GROUP BY t.id
           HAVING bool_and(u.verified_at IS NULL
                           AND u.first_verified_at IS NULL)
              AND min(u.created_at) <= %s
            ORDER BY min(u.created_at)""",
        (now - REMIND_AFTER,)).fetchall()
    freeze_before = now - dt.timedelta(days=reap_days())
    for r in rows:
        tid = str(r["id"])
        try:
            if _skip(admin, tid):
                continue
            if r["since"] <= freeze_before:
                # On the night a household is frozen the final notice IS
                # its mail: it carries a fresh link and the real deletion
                # date, while the day-7 reminder would name a freeze date
                # already in the past. A household that reaches the freeze
                # without ever having been reminded is one the sweep first
                # saw when it was already older than the window — telling
                # it two different stories in one evening helps nobody.
                word = _freeze(admin, tid, now)
                if word:
                    out[word].append(tid)
            else:
                out["reminded"] += _remind(admin, tid, now)
        except Exception as e:                        # noqa: BLE001
            log.error("unverified sweep failed for %s: %s", tid, e)
    return out


def _skip(admin, tid: str) -> bool:
    from ..web import demoguard
    if demoguard.is_demo(tid):
        return True
    # a subscription is the one thing only the household's owner could
    # have attached; also every comped or paying household, whose mail
    # being unread is nobody's business
    return bool(ext.gate.money_attached(admin, tid))


def _remind(admin, tid: str, now: dt.datetime) -> list[str]:
    """One reminder per unconfirmed person, once. A send that fails is
    retried tomorrow (nothing stamped); an address nothing can reach is
    stamped without a send, so it is not retried forever."""
    done: list[str] = []
    people = admin.execute(
        "SELECT id, email, created_at FROM users WHERE tenant_id=%s "
        "AND verified_at IS NULL AND verify_reminded_at IS NULL "
        "AND created_at <= %s", (tid, now - REMIND_AFTER)).fetchall()
    for p in people:
        uid = str(p["id"])
        word = "unreachable"
        if _reachable(p["email"]):
            token = email_verify.create(admin, uid)
            plain, html = _reminder_body(_link(token), p["created_at"],
                                         p["created_at"]
                                         + dt.timedelta(days=reap_days()))
            word = "sent" if account_mail.send(
                p["email"], "Confirm your email to keep your Oikonome "
                "account", plain, html, kind="verify-reminder") else "failed"
        if word == "failed":
            continue
        admin.execute("UPDATE users SET verify_reminded_at=now() WHERE id=%s",
                      (uid,))
        log.info("verify reminder for %s: %s", uid, word)
        done.append(uid)
    return done


def _freeze(admin, tid: str, now: dt.datetime) -> str | None:
    """Notice first, then the freeze: a household is never frozen without
    the mail that says why and carries the way out. An address the
    provider already bounces gets the freeze anyway — the notice cannot
    reach it and the delivery table already says so — and the lockout
    message itself names the way out for whoever does sign in."""
    from ..web.adminconsole import _audit
    owner = tenancy.owner_email(admin, tid)
    delete_after = now + dt.timedelta(days=GRACE_DAYS)
    word = "unreachable"
    if owner and _reachable(owner):
        # the link is minted for the owner OF THIS HOUSEHOLD — scoped to
        # the tenant the notice is about, so the query says what it means
        # rather than leaning on addresses being unique account-wide. A
        # household whose owner row went away between the scan and here
        # has changed under us: leave it for the next run.
        row = admin.execute(
            "SELECT id FROM users WHERE tenant_id=%s AND email=%s",
            (tid, owner)).fetchone()
        if row is None:
            return None
        token = email_verify.create(admin, str(row["id"]))
        plain, html = _final_body(_link(token), delete_after)
        word = "sent" if account_mail.send(
            owner, f"Your Oikonome account will be deleted on "
            f"{notice_date(delete_after)}", plain, html,
            kind="verify-final") else "failed"
    if word == "failed":
        log.warning("unverified freeze of %s held: notice not sent", tid)
        return "held"
    with admin.transaction():
        # re-assert under the row lock: a click since the candidate scan
        # (confirm() takes the same lock) makes this household somebody's
        locked = admin.execute(
            "SELECT 1 FROM tenants WHERE id=%s "
            "AND coalesce(status,'active')='active' FOR UPDATE",
            (tid,)).fetchone()
        proven = admin.execute(
            "SELECT 1 FROM users WHERE tenant_id=%s AND (verified_at IS NOT "
            "NULL OR first_verified_at IS NOT NULL)", (tid,)).fetchone()
        if locked is None or proven is not None:
            return None
        # and re-ask the money question here too. The candidate scan ran
        # before a reminder round and a whole SMTP round trip ago, and a
        # checkout finishing in that window is the household saying it is
        # anyone but abandoned — freezing it anyway would lock a person
        # out of the account they just paid for, and ask the gate to
        # pause the subscription they just started.
        if _skip(admin, tid):
            return None
        admin.execute(
            "UPDATE tenants SET status=%s, status_before_delete='active', "
            "delete_after=%s WHERE id=%s", (STATUS, delete_after, tid))
    # the freeze is local; the installed gate pauses whatever would keep
    # charging a household that cannot open (a trial: nothing). Outside
    # the transaction, like the console's schedule door.
    paused = ext.gate.on_tenant_delete_scheduled(tid)
    _audit(admin, "tenant_delete_scheduled", target=tid,
           detail=f"owner={owner or tid} reason=unverified "
                  f"grace_days={GRACE_DAYS} notice={word} release={paused}",
           ip="worker")
    return "scheduled"


# ---- the mails ------------------------------------------------------------------


def _reachable(email: str) -> bool:
    from ..web import spamdefense
    if spamdefense.recipient_blocked(email):
        return False
    return not delivery.failing([email])


def _link(token: str) -> str:
    # links only under the pinned base URL, never a Host-derived one — the
    # worker has no request anyway; hosted always sets it
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    return f"{base}/verify-email?token={token}"


def notice_date(d: dt.datetime) -> str:
    """The date shape the freeze mails and the reopen page both use, so
    the deadline reads the same wherever a person meets it."""
    return f"{d:%B} {d.day}, {d.year}"


def _reminder_body(url: str, since: dt.datetime,
                   frozen_on: dt.datetime) -> tuple[str, str]:
    """Written for someone who may not remember signing up: what this is,
    what happens if they do nothing, and that doing nothing is fine."""
    plain = (
        f"An Oikonome account was opened with this address on "
        f"{notice_date(since)}, and the address has not been confirmed.\n\n"
        f"To keep the account, confirm it here (link valid 7 days, one "
        f"use):\n\n{url}\n\n"
        f"Unconfirmed accounts are frozen on {notice_date(frozen_on)} and "
        f"deleted {GRACE_DAYS} days later, with everything in them. If you "
        f"did not sign up, do nothing — the account will be removed on its "
        f"own and nobody will write again.\n")
    html = (
        f"<p>An Oikonome account was opened with this address on "
        f"{notice_date(since)}, and the address has not been confirmed.</p>"
        f"<p>To keep the account, <a href=\"{url}\">confirm your email "
        f"address</a> (link valid 7 days, one use).</p>"
        f"<p>Unconfirmed accounts are frozen on {notice_date(frozen_on)} and "
        f"deleted {GRACE_DAYS} days later, with everything in them. If you "
        f"did not sign up, do nothing — the account will be removed on its "
        f"own and nobody will write again.</p>")
    return plain, html


def _final_body(url: str, delete_after: dt.datetime) -> tuple[str, str]:
    """Two steps, said as two steps: the link confirms the address, and
    the page it opens has the button that brings the account back. A mail
    promising the account reopens "the moment you click" would leave
    whoever stops reading at the confirmation thinking they were done."""
    plain = (
        f"This address was never confirmed, so the Oikonome account opened "
        f"with it is now frozen and will be deleted on "
        f"{notice_date(delete_after)}, with everything in it.\n\n"
        f"To keep it, open this link and press “Reopen this account” on "
        f"the page it takes you to (link valid 7 days, one use):"
        f"\n\n{url}\n\n"
        f"If you did not sign up, nothing is needed.\n")
    html = (
        f"<p>This address was never confirmed, so the Oikonome account "
        f"opened with it is now frozen and will be deleted on "
        f"<b>{notice_date(delete_after)}</b>, with everything in it.</p>"
        f"<p>To keep it, <a href=\"{url}\">confirm your email address</a> "
        f"and press <b>Reopen this account</b> on the page that opens "
        f"(link valid 7 days, one use).</p>"
        f"<p>If you did not sign up, nothing is needed.</p>")
    return plain, html
