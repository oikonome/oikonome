"""Native (mobile app) push, delivered through the Oikonome push relay.

Phones can only be pushed through the platform services (FCM for
Android, APNs for iOS), and those require the app publisher's
credentials — which is why a self-hosted instance cannot push directly
and a relay exists at all. The relay holds the platform keys; every
instance, hosted or self-hosted, forwards through it.

The payload is NEARLY content-free: the instance sends the relay
`{push token, event kind, badge}` and, for the daily verdict only, one
display line (`text`) — "OVER budget by $120 · Spend at most $23/day
food …" — so the lock screen says the verdict rather than "your verdict
is ready". That line is the whole of what the relay (and Apple/Google)
can see; no names, no merchants, no balances, and nothing at all for
the other events. The app still wakes on the push and pulls the real
content from its own server over its authenticated channel. An operator
who wants a routing-only payload sets `OIKONOME_PUSH_CONTENT_FREE=1`
and the daily push goes back to the fixed relay text. Do not widen the
line: every field added here is something a relay compromise can read.

Configuration is one env var, `OIKONOME_PUSH_RELAY_URL` — unset means
the channel is off (the self-host default: pushing is opt-in because it
tells an external relay your instance exists) — plus
`OIKONOME_PUSH_RELAY_KEY`, the instance key a keyed relay demands (sent
as the X-Relay-Key header). Same doctrine as SMS and
Web Push: unconfigured degrades to a no-op with a reason, failures log
and never raise, dead tokens are marked instead of retried forever.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("oikonome.notify.push_native")

# What a push is allowed to say. The relay enforces the same list; keep
# the two in sync (relay/app.py EVENTS).
EVENTS = frozenset({"daily", "weekly", "monthly", "yearly", "alert", "test"})

_TIMEOUT = 10.0


def relay_url() -> str:
    return (os.environ.get("OIKONOME_PUSH_RELAY_URL") or "").strip().rstrip("/")


def _relay_headers() -> dict[str, str]:
    """The instance key, when one is configured. The relay fronts the
    publisher's APNs/FCM credentials and refuses unkeyed pushes once it
    has keys of its own; a self-run relay with none stays open, so the
    header is simply absent when the instance has no key to send."""
    key = (os.environ.get("OIKONOME_PUSH_RELAY_KEY") or "").strip()
    return {"X-Relay-Key": key} if key else {}


def available() -> bool:
    return bool(relay_url())


def unavailable_reason() -> str | None:
    """Why the channel is off, for the settings UI (same contract as
    sms.unavailable_reason)."""
    if available():
        return None
    return "no push relay configured (OIKONOME_PUSH_RELAY_URL)"


MAX_TEXT_LEN = 160          # mirror of relay/app.py MAX_TEXT_LEN


def content_free() -> bool:
    """True when the operator asked for routing-only pushes."""
    from ..envnum import env_flag
    return env_flag("OIKONOME_PUSH_CONTENT_FREE")


def send_tenant(conn, tenant_id: str, event: str,
                badge: int | None = None,
                detail: dict | None = None,
                text: str | None = None) -> int:
    """Tickle every registered device of the tenant. Returns the count
    the relay accepted; failures log and never raise. `conn` is a
    control-plane connection. Pass `detail` to learn WHY nothing went
    out: it gains "unavailable" (relay legs not configured),
    "unconfigured" (no relay URL at all — the self-host default) and "targets" (registrations
    found). `text` is the one display line the push may carry (the
    daily verdict); it is dropped under OIKONOME_PUSH_CONTENT_FREE and
    cut to one line of at most MAX_TEXT_LEN characters."""
    if text and event == "daily" and not content_free():
        text = " ".join(text.split())[:MAX_TEXT_LEN] or None
    else:
        text = None
    url = relay_url()
    if not url:
        # say WHY before bailing: without this, a phone with a registered
        # token on a relay-less install gets "delivered 0" with an empty
        # detail, and the test-push door would blame the user's sign-in for
        # what is a server-config problem
        if detail is not None:
            from ..auth import device_tokens
            detail["targets"] = len(
                device_tokens.push_targets(conn, tenant_id))
            detail["unconfigured"] = True
        return 0
    if event not in EVENTS:
        log.warning("push_native: refusing unknown event %r", event)
        return 0
    from ..auth import device_tokens
    targets = device_tokens.push_targets(conn, tenant_id)
    if detail is not None:
        detail["targets"] = len(targets)
    if not targets:
        return 0
    body = {"notifications": [
        {"token": t["push_token"], "platform": t["push_platform"],
         "event": event,
         **({"badge": badge} if badge is not None else {}),
         **({"text": text} if text else {})}
        for t in targets]}
    try:
        import httpx
        r = httpx.post(url + "/v1/push", json=body, timeout=_TIMEOUT,
                       headers=_relay_headers())
        if r.status_code != 200:
            log.warning("push_native: relay returned %s: %s",
                        r.status_code, r.text[:200])
            return 0
        out = r.json()
    except Exception as e:                         # noqa: BLE001
        log.warning("push_native: relay unreachable: %s", e)
        return 0
    # The relay is an external service: its answer is input, not gospel.
    # A non-object body (or garbage field types) must degrade like any
    # other relay failure — this function's contract is "never raise",
    # and an AttributeError here would 500 whatever send triggered it.
    if not isinstance(out, dict):
        log.warning("push_native: relay returned non-object JSON: %.200r",
                    out)
        return 0
    dead_field = out.get("dead")
    dead = [t for t in dead_field if isinstance(t, str)] \
        if isinstance(dead_field, list) else []
    if dead:
        device_tokens.mark_push_dead(conn, dead)
    if detail is not None:
        unav = out.get("unavailable")
        detail["unavailable"] = [u for u in unav if isinstance(u, str)] \
            if isinstance(unav, list) else []
    try:
        return int(out.get("sent") or 0)
    except (TypeError, ValueError):
        log.warning("push_native: relay 'sent' is not a number: %.100r",
                    out.get("sent"))
        return 0
