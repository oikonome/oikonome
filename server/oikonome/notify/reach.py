"""Who a scheduled email actually reaches — decided once, here.

A cadence that is switched on says nothing about whether anyone gets it:
the only address may be muted, every recipient may have turned the kind
off, and push may be ticked with no browser or phone registered. Every
surface that describes the schedule (the Settings list, the cadence
editor, the phone's Settings row, System health) prints what this returns
instead of reading "on" as "arriving" (specs/settings-status-lines.md).

The rules are the senders' own: the addresses are `worker._recipients`
(mutes, per-kind choices, invitations, held addresses), push is the
household fan-out's targets, SMS is the verified, consented number the
worker texts. An address counts only while a mail relay is configured
(report.resolve_smtp, the worker's own check): with none, nothing is
mailed however many addresses there are.
"""
from __future__ import annotations

CADENCES = ("daily", "monthly", "yearly")
NOBODY = "nobody receiving it"
NO_RELAY = "no mail relay"
# a relay saved in Settings that the senders will not use yet: held by an
# owner's credential rotation until they confirm it, or refused by the
# outbound address policy — "no mail relay" would send the owner to a
# card that looks right
RELAY_HELD = "mail relay held until you confirm it"
RELAY_REFUSED = "mail relay refused by the address policy"


def _unused_relay(cfg: dict) -> str:
    """Why the senders have no relay: RELAY_HELD, RELAY_REFUSED or
    NO_RELAY. Called only after report.resolve_smtp said none."""
    if not cfg.get("smtp_host"):
        return NO_RELAY
    from . import outbound_pause
    held = outbound_pause.paused(cfg)
    if held and held["smtp"]:
        return RELAY_HELD
    from ..web.netguard import BlockedURL, check_host
    try:
        check_host(str(cfg["smtp_host"]), what="the configured SMTP host")
    except BlockedURL:
        return RELAY_REFUSED
    return NO_RELAY

_NAMES = {"daily": "daily", "monthly": "monthly report", "yearly": "yearly review"}


def hour_label(h: int) -> str:
    """9 → "9am", 0 → "12am", 13 → "1pm"."""
    return f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"


def _push_targets(tenant_id) -> int:
    """Browsers plus phones the household fan-out would reach."""
    from ..auth import device_tokens
    from ..db.tenancy import control_connect
    from . import push, push_native
    cc = control_connect()
    try:
        n = 0
        if push.available():
            n += len(push.live_subscriptions(cc, tenant_id))
        if push_native.available():
            n += len(device_tokens.push_targets(cc, tenant_id))
        return n
    finally:
        cc.close()


def _owners(tenant_id) -> set[str]:
    # users is read on every request's session check: the pooled
    # control-plane connection serves it
    from ..db import tenancy
    cc = tenancy.control_connect()
    try:
        return {str(r["email"]).lower() for r in cc.execute(
            "SELECT email FROM users WHERE tenant_id=%s AND role='owner'",
            (tenant_id,)).fetchall()}
    finally:
        cc.close()


def cadence_reach(conn, tenant_id, cfg: dict, *,
                  admin=None) -> dict[str, dict]:
    """Per cadence: `on` (any channel scheduled), `email` (addresses the
    worker would mail), `push` (targets, counted only when the cadence's
    push is on), `sms` (a text would go), `receiving` (any of those),
    `warn` (on, and nobody receiving it), and `text` — the Settings list
    line ("daily 9am", "daily 9am · +1 recipient", "daily 9am · nobody
    receiving it", "daily email off").

    `conn` is the household's tenant connection; it is read, never
    written. `admin` is an admin connection the caller already holds (the
    settings read shares one with its recipient grid); without one, this
    opens and closes its own, and only when a cadence emails anyone."""
    from .. import ext
    from ..db import tenancy
    from ..jobs import worker
    from . import delivery
    from . import sms as sms_mod
    from .schedule import effective_email_schedule
    sched = effective_email_schedule(cfg)
    phone = cfg.get("notify_phone")
    phone = phone if isinstance(phone, dict) else {}
    demo = bool(cfg.get("demo_mode"))
    own_admin = None

    relay: dict = {}

    def mailed(cad: str) -> list[str] | None:
        # worker._recipients, on the one admin connection: the addresses
        # after mutes, per-kind choices and invitations, minus held ones.
        # None when no relay is configured: the worker sends nothing then
        nonlocal admin, own_admin
        if "configured" not in relay:
            from ..web import report
            relay["configured"] = bool(report.resolve_smtp(conn)["configured"])
            if not relay["configured"]:
                relay["why"] = _unused_relay(cfg)
        if not relay["configured"]:
            return None
        if admin is None:
            admin = own_admin = tenancy.admin_connect()
        _audible, raw = worker._recipient_lists(conn, tenant_id, cad,
                                                admin=admin)
        return delivery.hold(raw)[0]

    try:
        return _reach_rows(sched, phone, demo, mailed, conn, tenant_id,
                           worker, ext, sms_mod,
                           lambda: relay.get("why", NO_RELAY))
    finally:
        if own_admin is not None:
            own_admin.close()


def _reach_rows(sched, phone, demo, mailed, conn, tenant_id, worker, ext,
                sms_mod, relay_why=lambda: NO_RELAY) -> dict[str, dict]:
    push_n: int | None = None
    owners: set[str] | None = None
    out: dict[str, dict] = {}
    for cad in CADENCES:
        e = worker._sched_entry(sched, cad)
        on = worker._channels_on(e)
        hour = worker._sched_int(e, "hour", 7, 23)
        emails: list[str] = []
        no_relay = False
        push_count = 0
        texts = False
        if on and not demo:
            if e.get("on"):
                got = mailed(cad)
                no_relay = got is None
                emails = got or []
            if e.get("push"):
                if push_n is None:
                    push_n = _push_targets(tenant_id)
                push_count = push_n
            texts = (bool(e.get("sms")) and sms_mod.configured()
                     and ext.gate.tier_allows(conn, "sms")
                     and sms_mod.consented(phone, "summary")
                     and bool(phone.get("verified"))
                     and bool(phone.get("number")))
        receiving = bool(emails) or push_count > 0 or texts
        name = _NAMES[cad]
        if not on:
            text = f"{name} email off" if cad == "daily" else f"{name} off"
        else:
            text = name + (f" {hour_label(hour)}" if hour is not None else "")
            if not receiving and not demo:
                text += f" · {NOBODY}"
                if no_relay:
                    text += f" ({relay_why()})"
            elif emails:
                if owners is None:
                    owners = _owners(tenant_id)
                extra = sum(1 for m in emails if m.lower() not in owners)
                if extra:
                    text += f" · +{extra} recipient{'s' if extra > 1 else ''}"
        out[cad] = {"on": on, "email": len(emails), "push": push_count,
                    "sms": texts, "receiving": receiving,
                    # the demo sends nothing by design, and says so on
                    # System health; it is not a setup to warn about
                    "warn": on and not receiving and not demo,
                    "text": text}
    return out
