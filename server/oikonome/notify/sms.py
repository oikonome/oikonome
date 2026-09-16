"""SMS delivery via Twilio's Messages API — plain HTTP (httpx is already a
dependency; no vendor SDK). An instance brings its own Twilio
credentials or simply leaves SMS off.

Env (all three required for the channel to exist):
  OIKONOME_TWILIO_ACCOUNT_SID   the ACxxxx ACCOUNT SID (the REST path and
                                Basic-auth user are both this account SID;
                                a standalone API-key SID would break the
                                request URL, so use the account SID)
  OIKONOME_TWILIO_AUTH_TOKEN    the account's auth token
  OIKONOME_TWILIO_FROM          an E.164 number, or an MG… Messaging
                                Service SID

The phone number a tenant receives at lives in tenant config
(notify_phone {number, verified}) and must be VERIFIED (a code sent to
that number, typed back) before any summary goes out — a typo must not
leak a household's budget to a stranger."""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("oikonome.notify.sms")

_API_VERSION = "2010-04-01"            # Twilio's pinned REST API version
_API = ("https://api.twilio.com/" + _API_VERSION
        + "/Accounts/{sid}/Messages.json")


def configured() -> bool:
    return all(os.environ.get(k) for k in (
        "OIKONOME_TWILIO_ACCOUNT_SID", "OIKONOME_TWILIO_AUTH_TOKEN",
        "OIKONOME_TWILIO_FROM"))


def valid_e164(number: str) -> bool:
    return bool(re.fullmatch(r"\+[1-9]\d{6,14}", number or ""))


# The message programs a number can opt in to, each with its OWN checkbox in
# the SPA. Carriers do not accept one consent statement covering several
# programs (a single opt-in cannot cover multiple use cases), so every
# program is its own opt-in, recorded separately:
#   summary — the scheduled budget verdict (daily/weekly/monthly/yearly)
#
# Only programs a sender actually exists for belong in this tuple. A name
# here is an affirmative promise — to the person who ticks the box, and to
# the carrier reading the consent copy in a toll-free verification — so a
# program with no send() call site leaves whoever opted in to it in silence
# forever. Add a program in the SAME change that adds its sender, never
# before.
PROGRAMS = ("summary",)


def consented(phone: dict | None, program: str) -> bool:
    """Did this number opt in to `program`? A record with no list was taken
    under a combined consent covering every program, so it stays opted in to
    all of them rather than silently going dark."""
    progs = (phone or {}).get("consent_programs")
    return program in progs if isinstance(progs, list) else True


def mask(number: str | None) -> str | None:
    """+12175551234 → +1•••••1234 — settings display only."""
    if not number:
        return None
    return number[:2] + "•" * max(0, len(number) - 6) + number[-4:]


def send(to: str, body: str) -> bool:
    """One outbound message. Returns False (and logs) on any failure —
    a summary SMS is never worth failing the email sweep over."""
    if not configured():
        return False
    if not valid_e164(to):
        log.warning("sms: refusing non-E.164 destination")
        return False
    import httpx
    sid = os.environ["OIKONOME_TWILIO_ACCOUNT_SID"]
    frm = os.environ["OIKONOME_TWILIO_FROM"]
    data = {"To": to, "Body": body}
    # a Messaging Service SID rides a different field than a raw number
    data["MessagingServiceSid" if frm.startswith("MG") else "From"] = frm
    try:
        r = httpx.post(_API.format(sid=sid), data=data,
                       auth=(sid, os.environ["OIKONOME_TWILIO_AUTH_TOKEN"]),
                       timeout=15)
        if r.status_code // 100 != 2:
            # log only Twilio's error code, never the raw body (it echoes the
            # recipient's phone number) — mask the destination for context
            code = ""
            try:
                code = str((r.json() or {}).get("code", ""))
            except Exception:                        # noqa: BLE001
                pass
            log.warning("sms: twilio %s (code %s) to %s",
                        r.status_code, code or "?", mask(to))
            return False
        return True
    except Exception as e:                       # noqa: BLE001
        log.warning("sms send failed: %s", e)
        return False
