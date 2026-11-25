"""Notification channels and send-now email.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import datetime as dt
from fastapi import Depends, HTTPException

from . import demoguard
from .. import ext
from .api_common import (_conn, _owner_only, _step_up_proof, _today, _user,
                         router)
from .fields import Fields, json_fields
from .schemas import documented, Ok
from .schemas.notify import (EmailSent, NotifyPhone, NotifyPhoneSet,
                             NotifyPhoneVerify, NotifyStatus, NotifyTest,
                             NotifyTested, PushSubscribe, PushUnsubscribe)
from .security import limit


@router.post("/jobs/email", dependencies=[Depends(limit("jobs-email", 3, 600))])
@documented(response=EmailSent)
def jobs_email_api(user: dict = Depends(_user())):
    """Build and send today's verdict email NOW (the daily email's task
    body, run inline — same recipients rules). Returns the subject line.

    Owner-only (the request gate's list says so too), and refused to the
    shared demonstration login: the mail goes to every recipient, and
    every holder of that password, from any address, could repeat it."""
    demoguard.deny_shared_grant(user)
    _owner_only(user)
    from ..jobs import worker
    try:
        # force_email: this is the explicit "email me now" button — it must
        # send the EMAIL regardless of the daily channel toggle, and must
        # NOT fire the SMS/push side effects or stamp the cadence heartbeat
        # (without this, a user whose daily email
        # is off but SMS is on would click "send test email" and silently
        # get an SMS and no email).
        subject = worker.email_tenant(user["tenant_id"], send_it=True,
                                      force_email=True)
    except worker.NoRecipients as e:
        # nothing was wrong with the mail — there was nobody to send it to,
        # and the button must say so instead of "Sent"
        raise HTTPException(409, str(e))
    except Exception as e:               # noqa: BLE001 — surface SMTP/config
        raise HTTPException(400, f"email failed — {type(e).__name__}: {e}")
    return {"ok": True, "subject": subject}


# ---- notification channels (SMS / web push) ------------------------
# Settings surface for the per-cadence delivery matrix's non-email channels:
# a tenant-level VERIFIED phone for SMS, per-browser push subscriptions, and
# channel availability (so the UI can say WHY a toggle is disabled).


def _caller_push_muted(cc, user) -> bool:
    """Has the caller turned the household's push off for themselves?"""
    row = cc.execute("SELECT push_muted FROM users WHERE id=%s",
                     (user["user_id"],)).fetchone()
    return bool(row and row["push_muted"])


@router.get("/notify")
@documented(response=NotifyStatus)
def notify_status_api(user: dict = Depends(_user())):
    from .. import notify  # noqa: F401
    from ..auth import device_tokens as _device_tokens
    from ..notify import push as _push, push_native as _push_native, \
        sms as _sms
    from .app import _control_conn
    conn = _conn(user)
    try:
        from ..engine import budget
        cfg = budget.load_config(conn)
        sms_entitled = ext.gate.tier_allows(conn, "sms")
    finally:
        conn.close()
    # a restore can plant any shape under these; a non-dict reads as
    # "no number" rather than crashing the status page
    phone = cfg.get("notify_phone")
    if not isinstance(phone, dict):
        phone = {}
    pending = cfg.get("notify_phone_pending")
    if not isinstance(pending, dict):
        pending = {}
    with _control_conn() as cc:
        subs = cc.execute(
            "SELECT count(*) AS n FROM push_subscriptions "
            f"WHERE user_id=%s AND {_push.LIVE}",
            (user["user_id"],)).fetchone()["n"]
        # phones push would actually reach: the delivery fan-out's own
        # target list (notify/push_native.py), so a phone it skips — one
        # the shared demonstration login registered — never makes the
        # clients offer push that nothing would carry
        native = len(_device_tokens.push_targets(cc, user["tenant_id"]))
        # the household fan-out skips a person who turned its push off
        # (users.push_muted); their phones are still registered and still
        # carry the test push and their own account's notices, so they
        # count here as well
        if _caller_push_muted(cc, user):
            native += len(_device_tokens.push_targets(
                cc, user["tenant_id"], user["user_id"]))
        vapid_pub = None
        if _push.available():
            try:
                _priv, vapid_pub = _push.vapid_keys(cc)
            except Exception:                    # noqa: BLE001
                vapid_pub = None
    # A number an owner's credential change paused (auth/reset.py
    # _unverify_sms_number) stays on file, unverified, with the moment it
    # was paused — so the clients can say why the texts stopped and offer
    # to confirm it again or remove it. A number that was never verified
    # has no pause time.
    paused_at = None
    if phone.get("number") and not phone.get("verified") \
            and phone.get("unverified_at"):
        raw = str(phone["unverified_at"])
        try:
            paused_at = dt.datetime.fromisoformat(raw).astimezone(
                dt.timezone.utc).isoformat()
        except ValueError:
            paused_at = raw
    return {"sms_available": _sms.configured() and sms_entitled,
            "sms_entitled": sms_entitled,
            "phone": _sms.mask(phone.get("number")),
            "phone_verified": bool(phone.get("verified")),
            "phone_paused_at": paused_at,
            "phone_pending": _sms.mask(pending.get("number")),
            "push_available": _push.available() and vapid_pub is not None,
            "vapid_public_key": vapid_pub,
            "push_subscribed": subs > 0,
            # web push and phone push are separate channels: an instance with
            # no VAPID key can still reach the mobile apps through the relay,
            # so a phone-only household must not be told push is impossible
            "native_push_available": _push_native.available(),
            "native_push_devices": native,
            # mail is on but invitations cannot carry a link: an added
            # recipient is never invited, so never receives the daily email
            "invites_need_base_url": _invites_need_base_url(user)}


def _invites_need_base_url(user) -> bool:
    from ..notify import recipient_invites
    from .app import _tenant_smtp
    try:
        return recipient_invites.need_base_url(
            _tenant_smtp(user["tenant_id"]))
    except Exception:                            # noqa: BLE001
        # a status read must not fail on the mail config it only reports
        import logging
        logging.getLogger("oikonome.api").warning(
            "could not read the mail relay for the notify status",
            exc_info=True)
        return False


def _consent_programs(body: Fields, known) -> list:
    """The SMS programs a phone body consents to. A list of program names
    is the current clients'; a bare `true` is an older client's and means
    every program. Anything else — `consent: 1`, a string, a list holding
    something that is not a name — is simply not consent, which the door
    answers with its own 400 rather than a type error naming the field."""
    try:
        if body.flag("consent") is True:
            return list(known)
    except HTTPException:
        pass                                # not a boolean: a list, below
    try:
        sent = body.list_of("consent", str, 16) or []
    except HTTPException:
        return []
    return [p for p in sent if p in known]


@router.post("/notify/phone",
             dependencies=[Depends(limit("notify-phone", 5, 3600))])
@documented(response=NotifyPhoneSet, body=NotifyPhone)
def notify_phone_api(user: dict = Depends(_user()),
                     body: Fields = Depends(json_fields)):
    """Set (or clear) the tenant's SMS number. Setting sends a 6-digit
    verification code to the number — summaries only ever go to a number
    that typed its code back (a typo must not leak a household's budget
    to a stranger). The shared demonstration login can neither set nor
    clear it: a number a visitor verified keeps receiving the household's
    summaries after the visit and after the shared password is rotated."""
    demoguard.deny_shared_grant(user)
    _owner_only(user)
    from ..engine import budget
    from ..notify import sms as _sms
    number = body.text("number").replace(" ", "")
    if number:
        # A number is a durable destination for the household's summaries,
        # like a mail relay or a daily-mail recipient, and the code that
        # verifies it goes to the number being set — so a cookie alone
        # would verify a thief's own phone. Setting one is a step-up act;
        # clearing stays free, because it only takes a destination away.
        from .app import _require_elevation
        _require_elevation(user, **_step_up_proof(body))
    conn = _conn(user)
    try:
        if not number:                        # clear
            # Same lock as the send/verify paths below: dropping a phone
            # number is exactly when a
            # concurrent settings save must not put it back.
            with budget.config_txn(conn) as cfg:
                cfg.pop("notify_phone", None)
                cfg.pop("notify_phone_pending", None)
            return {"ok": True, "cleared": True}
        if not _sms.configured():
            raise HTTPException(400, "SMS isn't configured on this instance")
        # SMS is one of the features an installed gate can withhold. Block
        # verification up front so a tenant never spends a verification SMS
        # on a channel they cannot use. With no gate installed nothing is
        # withheld: the operator brings their own SMS provider.
        if not ext.gate.tier_allows(conn, "sms"):
            raise HTTPException(
                402, "Text alerts aren't enabled for this account — your daily "
                     "verdict and alerts arrive by email and push.")
        if not _sms.valid_e164(number):
            raise HTTPException(400, "phone must be E.164, e.g. +12175551234")
        # Consent is parsed BEFORE the rate limiters: a request
        # carrying no valid consent can never send, so letting it burn one of
        # the user's 3 verification SMS per day would let a malformed client
        # lock them out of their own opt-in without a single message going
        # out.
        programs = _consent_programs(body, _sms.PROGRAMS)
        if not programs:
            raise HTTPException(
                400, "SMS consent is required — tick at least one box saying "
                     "which texts you agree to receive")

        # SMS-pump / toll-fraud guard: the
        # per-IP limit is defeated by IP rotation across free accounts, so
        # ALSO cap by account (source-IP-independent) and cool down the
        # destination number, since every send is billed to the instance's
        # Twilio account.
        from .security import _account_limit
        if not _account_limit(("sms-acct", str(user["user_id"])), 3, 86400):
            raise HTTPException(429, "verification SMS limit reached for "
                                     "today — try again tomorrow")
        import hashlib
        if not _account_limit(
                ("sms-dest", hashlib.sha256(number.encode()).hexdigest()),
                3, 86400):
            raise HTTPException(429, "too many codes sent to that number "
                                     "recently")
        from ..auth import otp_code
        code, code_rec = otp_code.issue("sms-phone", user["tenant_id"])
        # TCPA / carrier evidence: the SPA
        # gates the send-code button on an explicit SMS-only consent checkbox,
        # and the timestamp of that affirmative action is recorded HERE, with
        # the number it applied to. A UI-only gate leaves nothing to show a
        # carrier — or a regulator — six months later. Stored, not asserted:
        # if consent is absent the request is refused rather than defaulted.
        # SPLIT per program: one box covering the verdict
        # AND the alerts is a single opt-in spanning two message programs,
        # which carriers reject. `consent` is the list of programs ticked; a
        # bare True is an older client and means both.
        # The whole blob
        # is written back, so a settings save that read BEFORE this one
        # silently drops the pending record — the user then gets a real,
        # unexpired code and is told it is "wrong or expired". config_txn
        # takes the row lock for the read-modify-write.
        with budget.config_txn(conn) as locked:
            locked["notify_phone_pending"] = {
                **code_rec,
                "number": number,
                "consent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "consent_programs": programs}
    finally:
        conn.close()
    if not _sms.send(number, f"Oikonome verification code: {code}"):
        raise HTTPException(502, "couldn't send the verification SMS")
    return {"ok": True, "sent_to": _sms.mask(number)}


@router.post("/notify/phone/verify",
             dependencies=[Depends(limit("notify-verify", 10, 3600))])
@documented(response=Ok, body=NotifyPhoneVerify)
def notify_phone_verify_api(user: dict = Depends(_user()),
                            body: Fields = Depends(json_fields)):
    # the step that actually opens the SMS channel, refused for the same
    # reason as the number itself
    demoguard.deny_shared_grant(user)
    _owner_only(user)
    # the code as typed (the checker drops its spaces); a code that is not
    # text is refused before an attempt is charged or the record touched
    code = body.text("code", 64, strip=False)
    from ..auth import otp_code
    from ..engine import budget
    # the per-address limit above is spent once per address; this one is
    # spent once per account, whichever address the guesses come from
    otp_code.charge_attempt("sms-phone", user["user_id"])
    conn = _conn(user)
    try:
        # Verify reads the pending record, promotes it and drops it
        # — a read-modify-write of the whole blob, so it takes the same lock
        # as the send path above. Without it a concurrent settings save can
        # resurrect the consumed pending record or lose the verified number.
        # A miss is WRITTEN (the counter, or the burned record dropped) and
        # the refusal raised only after the block commits: raising inside
        # it would roll the miss back, and the cap with it.
        with budget.config_txn(conn) as cfg:
            pending = cfg.get("notify_phone_pending") or {}
            verdict = (otp_code.check(pending, code, "sms-phone",
                                      user["tenant_id"])
                       if pending.get("number") else otp_code.INVALID)
            if verdict == otp_code.WRONG:
                cfg["notify_phone_pending"] = pending
            elif verdict != otp_code.OK:
                cfg.pop("notify_phone_pending", None)
            else:
                _promote_verified_phone(cfg, pending)
    finally:
        conn.close()
    if verdict != otp_code.OK:
        raise HTTPException(400, "wrong or expired code")
    return {"ok": True}


def _promote_verified_phone(cfg: dict, pending: dict) -> None:
    # consent_at travels with the verified record: the pending row is
    # dropped below, and losing the consent timestamp there would
    # leave a verified number with no evidence of how it opted in.
    cfg["notify_phone"] = {"number": pending["number"],
                           "verified": True,
                           "consent_at": pending.get("consent_at"),
                           "consent_programs":
                               pending.get("consent_programs"),
                           "verified_at": dt.datetime.now(
                               dt.timezone.utc).isoformat()}
    cfg.pop("notify_phone_pending", None)


# Push endpoints are server-POSTed by the worker fan-out, so an
# attacker-chosen endpoint is an SSRF primitive.
# The guard lives beside the sender (notify/push.endpoint_allowed), so the
# allowlist is checked with the same reading of the URL the delivery POST
# will use, both here at subscribe time and again before every send.
def _valid_push_endpoint(endpoint: str) -> bool:
    from ..notify import push as _push
    return _push.endpoint_allowed(endpoint)


# The browser re-sends the subscription it holds at every page load, so the
# per-address bound sits well above a busy household's reloads; NEW
# destinations are bounded per account, which IP rotation cannot defeat.
_PUSH_NEW_PER_ACCOUNT = (20, 3600)


@router.post("/notify/push/subscribe",
             dependencies=[Depends(limit("push-subscribe", 120, 3600))])
@documented(response=Ok, body=PushSubscribe)
def push_subscribe_api(user: dict = Depends(_user()),
                       body: Fields = Depends(json_fields)):
    """Store this browser's PushSubscription (upsert on endpoint — a
    re-subscribe refreshes keys instead of duplicating).

    The shared demonstration login is refused: a subscription is a
    channel to one visitor's browser that every later alert for the
    household is sent down, and it stays when that visitor leaves and the
    shared password is rotated."""
    demoguard.deny_shared_grant(user)
    from .app import _control_conn, _require_elevation
    # the browser's PushSubscription.toJSON(), read as it is sent: every
    # part a string, any part of the wrong type the same "bad subscription"
    # a missing part is
    _bad = "bad subscription"
    endpoint = body.text("endpoint", strip=False, msg=_bad)
    keys = body.sub("keys", msg=_bad)
    p256dh = keys.ident("p256dh", 512, msg=_bad) if keys else ""
    auth = keys.ident("auth", 512, msg=_bad) if keys else ""
    # a label shown in the device list, cut as it always was; a NUL is
    # dropped, since the column cannot hold one
    user_agent = body.text("user_agent",
                           strip=False)[:200].replace("\x00", "")
    if not _valid_push_endpoint(endpoint) or not p256dh or not auth:
        raise HTTPException(400, _bad)
    # keys no push can be encrypted to would fail inside the sender on
    # every household push, for as long as the row lived
    from ..notify import push as _push
    if not _push.keys_usable(p256dh, auth):
        raise HTTPException(422, "bad subscription keys")
    # A subscription lives as long as the browser session that made it
    # (push_subscriptions.session_id cascades from sessions), so a cookie
    # that expires, signs out or is revoked takes its channel with it.
    # Only a browser session can make one: a script or device token has
    # no session for it to die with.
    if not user.get("session_id") or user.get("device_id") \
            or user.get("script_token"):
        raise HTTPException(
            403, "web push is turned on from a signed-in browser")
    # A new destination for the household's verdicts and alerts is a
    # step-up act, like an SMS number, a webhook or a mail recipient: a
    # stolen cookie alone would otherwise add the thief's own browser.
    # The same browser refreshing the subscription it already holds (its
    # endpoint and keys, on this account) is not a new destination.
    with _control_conn() as conn:
        held = conn.execute(
            "SELECT 1 FROM push_subscriptions WHERE endpoint=%s "
            "AND user_id=%s AND p256dh=%s AND auth=%s",
            (endpoint, user["user_id"], p256dh, auth)).fetchone()
    if held is None:
        # the step-up first: a browser whose subscription lapsed re-posts
        # it unasked on every page load and is refused here, and those
        # refusals must not spend the account's allowance of new
        # destinations — only a request that will create one is counted
        _require_elevation(user, **_step_up_proof(body))
        from .security import account_limit_or_429
        account_limit_or_429(("push-subscribe-new", str(user["user_id"])),
                             *_PUSH_NEW_PER_ACCOUNT,
                             what="too many new push subscriptions")
    # Reassigning user_id/tenant_id on ANY endpoint conflict would let a
    # posted endpoint string that belongs to someone else silently take the
    # row over. push_subscriptions has no RLS (control plane), so nothing
    # else stops it. The victim's alerts then die quietly: the row now carries the attacker's p256dh/auth, which
    # cannot decrypt on the victim's browser, so every send fails and the
    # subscription is marked dead. Silent denial of service on somebody
    # else's notifications, from knowing one URL.
    #
    # Takeover requires proof of possession — the same browser
    # re-subscribing presents the SAME keys, which is exactly the legitimate
    # case (a second person logging in on a shared browser profile). Knowing
    # only the endpoint proves nothing and is refused.
    from .app import _caller_still_signed_in, _lock_user_factors
    with _control_conn() as conn, conn.transaction():
        # a reset deletes the account's subscriptions; one stored after it
        # would keep receiving the household's alerts with no session, so
        # the row is written under the reset's own lock
        _lock_user_factors(conn, user["user_id"])
        _caller_still_signed_in(conn, user)
        row = conn.execute(
            """INSERT INTO push_subscriptions
                 (user_id, tenant_id, endpoint, p256dh, auth, user_agent,
                  session_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (endpoint) DO UPDATE SET
                 user_id=EXCLUDED.user_id, tenant_id=EXCLUDED.tenant_id,
                 p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth,
                 user_agent=EXCLUDED.user_agent,
                 session_id=EXCLUDED.session_id,
                 dead_at=NULL
               WHERE push_subscriptions.user_id = EXCLUDED.user_id
                  OR (push_subscriptions.p256dh = EXCLUDED.p256dh
                      AND push_subscriptions.auth = EXCLUDED.auth)
               RETURNING id""",
            (user["user_id"], user["tenant_id"], endpoint,
             p256dh, auth, user_agent,
             user["session_id"])).fetchone()
        # At most MAX_PER_USER per person: the oldest goes. Evicted, not
        # refused, because nobody can see which stale browser to drop — a
        # browser whose data was cleared subscribes afresh and leaves its
        # old row behind until that session ends — and only the caller's
        # own rows are touched. Rows already dead or out of session go
        # first. Under the factor lock taken above, so two subscribes
        # cannot both overshoot.
        if row is not None:
            conn.execute(
                "DELETE FROM push_subscriptions WHERE id IN ("
                " SELECT id FROM push_subscriptions WHERE user_id=%s"
                "  AND id <> %s"
                f" ORDER BY ({_push.LIVE}) DESC, created_at DESC, id"
                " OFFSET %s)",
                (user["user_id"], row["id"], _push.MAX_PER_USER - 1))
    if row is None:
        raise HTTPException(
            409, "that push endpoint is registered to another account")
    return {"ok": True}


@router.post("/notify/push/unsubscribe")
@documented(response=Ok, body=PushUnsubscribe)
def push_unsubscribe_api(user: dict = Depends(_user()),
                         body: Fields = Depends(json_fields)):
    from .app import _control_conn
    # read the way subscribe stored it, and bounded the way subscribe
    # bounds it: a longer endpoint, or one holding a control character,
    # can never have been stored, so it is refused instead of looked up
    endpoint = body.ident("endpoint", 2048)
    with _control_conn() as conn:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE user_id=%s AND endpoint=%s",
            (user["user_id"], endpoint))
    return {"ok": True}


_NO_PUSH_TARGET = ("no live push subscription — enable push in this "
                   "browser or sign in from the mobile app first")


def _push_targets_exist(user: dict) -> bool:
    """Has the household (or the caller) any browser or device a test push
    could go to? Read before the verdict is built, so a test with nowhere
    to go is refused without it."""
    from ..auth import device_tokens
    from ..notify import push as _push
    from .app import _control_conn
    with _control_conn() as cc:
        tid = user["tenant_id"]
        return bool(_push.live_subscriptions(cc, tid)
                    or _push.live_subscriptions(cc, tid, user["user_id"])
                    or device_tokens.push_targets(cc, tid)
                    or device_tokens.push_targets(cc, tid, user["user_id"]))


@router.post("/notify/test",
             dependencies=[Depends(limit("notify-test", 5, 3600))])
@documented(response=NotifyTested, body=NotifyTest)
def notify_test_api(user: dict = Depends(_user()),
                    body: Fields = Depends(json_fields)):
    """Send today's short verdict through one channel, right now.

    Refused to the shared demonstration login: the text goes to the
    household's verified phone and the push to every member's devices,
    people that login is never shown."""
    demoguard.deny_shared_grant(user)
    _owner_only(user)
    from ..engine import budget
    from ..notify import push as _push, render as _short, sms as _sms
    from .app import _control_conn
    channel = body.text("channel", strip=False)
    # every refusal that needs no verdict is made before one is built:
    # building it refreshes the household's alert log, and a refused
    # request writes nothing
    if channel not in ("sms", "push"):
        raise HTTPException(400, "channel must be sms or push")
    if channel == "push" and not _push_targets_exist(user):
        raise HTTPException(400, _NO_PUSH_TARGET)
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
        # the test-send honors the same SMS tier gate, or it becomes a way
        # around it
        sms_ok = ext.gate.tier_allows(conn, "sms")
        phone = cfg.get("notify_phone") or {}
        if channel == "sms":
            if not sms_ok:
                raise HTTPException(
                    402, "Text alerts aren't enabled for this account — "
                         "alerts arrive by email and push")
            if not (phone.get("verified") and phone.get("number")):
                raise HTTPException(400, "verify a phone number first")
            # the test send IS the summary text, so it rides the summary
            # opt-in — a number that only ticked the alerts box never
            # receives one
            if not _sms.consented(phone, "summary"):
                raise HTTPException(
                    400, "that number didn't opt in to budget-summary texts "
                         "— re-add it and tick that box to receive them")
        from . import report
        d = report.gather(conn, _today(conn))
        short = _short.short_daily(d)
    finally:
        conn.close()
    if channel == "sms":
        if not _sms.send(phone["number"], short):
            raise HTTPException(502, "SMS send failed — check the app log")
        return {"ok": True}
    if channel == "push":
        from ..notify import push_native as _native
        native_detail: dict = {}
        # connect, not a held connection: the sends wait on outside push
        # services, and a request holding one of the control pool's few
        # connections across them takes it from everyone's session check
        n = _push.send_tenant(None, user["tenant_id"], "Oikonome test",
                              short, connect=_control_conn)
        n += _native.send_tenant(None, user["tenant_id"], "test",
                                 detail=native_detail,
                                 connect=_control_conn)
        # The household send skips anyone who turned its push off for
        # themselves. That switch is about the household's notices; the
        # test is the caller checking their own devices, so a muted caller
        # is reached by name too (the household send left them out, so
        # nothing arrives twice).
        with _control_conn() as cc:
            muted = _caller_push_muted(cc, user)
        if muted:
            own_detail: dict = {}
            n += _push.send_tenant(None, user["tenant_id"], "Oikonome test",
                                   short, user_id=user["user_id"],
                                   connect=_control_conn)
            n += _native.send_tenant(None, user["tenant_id"], "test",
                                     detail=own_detail, user_id=user["user_id"],
                                     connect=_control_conn)
            # the failure story below reads one detail: the targets of
            # both sends, and the first reason either gave
            native_detail["targets"] = (native_detail.get("targets", 0)
                                        + own_detail.get("targets", 0))
            for k, v in own_detail.items():
                native_detail.setdefault(k, v)
        if not n:
            # a registered phone whose platform leg isn't live is a
            # CONFIG story, not an account one — say the true reason
            # instead of blaming the sign-in
            if (native_detail.get("targets")
                    and native_detail.get("unconfigured")):
                raise HTTPException(
                    400, "your device is registered, but this server has "
                         "no push relay configured "
                         "(OIKONOME_PUSH_RELAY_URL) — native pushes "
                         "can't be delivered")
            # the relay said why it would not deliver (the device is bound
            # to another server, a rate limit, no answer at all) — that
            # sentence is the true story; "enable push" would send the
            # owner to sign in on the phone they are already holding
            if native_detail.get("targets") and native_detail.get("reason"):
                raise HTTPException(
                    400, f"your device is registered, but "
                         f"{native_detail['reason']}")
            if (native_detail.get("targets")
                    and native_detail.get("unavailable")):
                legs = ", ".join(native_detail["unavailable"])
                raise HTTPException(
                    400, f"your device is registered, but this server's "
                         f"push relay can't deliver to it yet "
                         f"({legs} isn't live)")
            raise HTTPException(400, _NO_PUSH_TARGET)
        return {"ok": True, "delivered": n}
    raise HTTPException(400, "channel must be sms or push")
