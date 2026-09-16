"""The provider telling us a message did not arrive.

POST /api/email/webhook has no session auth — the mail provider calls it —
so authenticity is a shared secret set by the operator as
`OIKONOME_EMAIL_WEBHOOK_TOKEN` and carried either as HTTP Basic credentials
(Postmark lets you embed them in the webhook URL, which is its documented
way to authenticate a receiver) or as `?token=`. Compared in constant time.

**Unset token ⇒ the route 404s**, exactly like the admin console. A
self-hosted install with a local relay has no webhook to receive and should
not expose an endpoint for one; nothing about this feature is required for
the app to work, and an always-on unauthenticated door would be a worse
trade than the visibility it buys.

What arrives is Postmark's Bounce payload. Only two fields decide anything —
`Email` and `Type` — and only the types in `delivery.HARD_TYPES` pause
anything. `Inactive` records that the provider is now suppressing the
address, which is what makes every LATER send fail at submission with no
bounce of its own; storing `ID` alongside it is what lets a corrected
address be reactivated upstream later.

Deliberately NOT handled: Delivery, Open, Click, SpamComplaint-as-analytics.
Oikonome does not track opens — a recorded "open" is as likely to be a
mailbox proxy prefetching a pixel as a person — and a feature that tells
you your mail is broken has no business also building a reading profile.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from ..notify import delivery
from .security import limit

log = logging.getLogger("oikonome.delivery")

router = APIRouter()

# Bounce payloads are small; cap before buffering (the Plaid webhook
# precedent — an unauthed endpoint must not read request.body() unbounded).
_MAX_BODY = 64 * 1024


def _expected_token() -> str:
    return (os.environ.get("OIKONOME_EMAIL_WEBHOOK_TOKEN") or "").strip()


def _digest(s: str) -> bytes:
    """A trap on an UNAUTHENTICATED door: hmac.compare_digest raises
    TypeError on non-ASCII str, and the Basic branch below manufactures
    U+FFFD from arbitrary bytes via .decode('utf-8','replace') — so
    ?token=%C3%BC would 500 on every request (120/min per IP), each with a
    full traceback into the feedback ring buffer. Hashing both sides first
    makes every compare fixed-length and total."""
    import hashlib
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).digest()


def _authorized(request: Request, token_param: str) -> bool:
    expected = _expected_token()
    if not expected:
        return False
    if token_param and hmac.compare_digest(_digest(token_param),
                                           _digest(expected)):
        return True
    # HTTP Basic, as Postmark embeds it in the webhook URL. Either half may
    # carry the secret (user:token or token:anything) — operators write it
    # both ways and neither is wrong.
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        import base64
        try:
            raw = base64.b64decode(auth[6:].strip()).decode("utf-8", "replace")
        except Exception:                          # noqa: BLE001
            return False
        user, _, pw = raw.partition(":")
        return (hmac.compare_digest(_digest(pw), _digest(expected))
                or hmac.compare_digest(_digest(user), _digest(expected)))
    return False


@router.post("/api/email/webhook",
             dependencies=[Depends(limit("email_webhook", 120, 60))])
async def email_webhook(request: Request, token: str = ""):
    """Provider → us. A 404 when the feature is not configured, a 401 when
    the secret is wrong, and 200 for anything we understood — including
    events we deliberately ignore, because a provider that gets a 4xx for a
    Delivery event will retry it forever."""
    if not _expected_token():
        raise HTTPException(404, "not found")
    if not _authorized(request, token):
        raise HTTPException(401, "unauthorized")
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > _MAX_BODY:
        raise HTTPException(413, "payload too large")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY:
            raise HTTPException(413, "payload too large")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks) or b"{}")
    except ValueError:
        raise HTTPException(400, "malformed payload")
    if not isinstance(payload, dict):
        raise HTTPException(400, "malformed payload")
    return _handle(payload)


# Every field we read is a string in Postmark's schema. A number, list or
# object in one of these slots is not an event we can act on — it is not an
# address, not a bounce type, and never will be on a retry.
_TEXT_FIELDS = ("RecordType", "Email", "Recipient", "Type", "Description",
                "Details")


def _handle(payload: dict) -> dict:
    """Split out so tests can drive the logic without an HTTP round trip."""
    # Shape first: `.strip()` on a non-string raises, and an uncaught
    # AttributeError here is a 500 — which the provider reads as "we are
    # down" and retries, forever, for a payload that can never become
    # valid. Answer 200-with-ignored instead: the event is understood well
    # enough to know there is nothing in it to act on. (A body that isn't
    # even a JSON object is different — that one still 400s at the route,
    # because it isn't an event at all.)
    for k in _TEXT_FIELDS:
        v = payload.get(k)
        if v is not None and not isinstance(v, str):
            log.warning("delivery: webhook field %s is %s, not a string",
                        k, type(v).__name__)
            return {"ok": True, "ignored": "malformed field"}
    record_type = (payload.get("RecordType") or "").strip()
    email = (payload.get("Email") or payload.get("Recipient") or "").strip()
    btype = (payload.get("Type") or "").strip()
    if record_type and record_type not in ("Bounce", "SpamComplaint"):
        return {"ok": True, "ignored": record_type}
    if not email:
        return {"ok": True, "ignored": "no recipient"}
    inactive = bool(payload.get("Inactive"))
    if btype in delivery.HARD_TYPES or inactive:
        delivery.record_failure(
            email,
            state=("complained" if btype in ("SpamNotification",
                                             "SpamComplaint")
                   else "bouncing"),
            bounce_type=btype or record_type or None,
            reason=(payload.get("Description") or payload.get("Details")
                    or None),
            # Postmark bounce IDs are numeric. The value is later
            # interpolated into the reactivation URL path
            # (/bounces/<id>/activate) with our server token attached, so
            # anything non-numeric from the webhook body must not be
            # stored as an id at all
            provider_id=(str(payload["ID"])
                         if re.fullmatch(r"\d+", str(payload.get("ID") or ""))
                         else None),
            suppressed=inactive)
        return {"ok": True, "recorded": email}
    # A soft bounce is real but says nothing durable about the address —
    # logged so an operator can see it, never acted on.
    log.info("delivery: soft failure for %s (%s)", email, btype or record_type)
    return {"ok": True, "soft": email}
