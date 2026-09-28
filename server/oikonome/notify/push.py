"""Web Push delivery (no native app — the SPA's service worker shows the
notification). VAPID keypair is per-instance, generated lazily on first
use and stored in the control-plane push_vapid row with the private key
encrypted under OIKONOME_MASTER_KEY. Subscriptions live in
push_subscriptions (one per browser); delivery failures with 404/410
mark the subscription dead instead of retrying forever."""

from __future__ import annotations

import base64
import json
import logging

log = logging.getLogger("oikonome.notify.push")


def available() -> bool:
    try:
        import pywebpush  # noqa: F401
        return True
    except ImportError:
        return False


# The hosts a subscription endpoint may name: the real browser push
# services. An internal address (10.0.0.5, 169.254.169.254) or an arbitrary
# host can't match these suffixes, so it can never be stored or sent to —
# and the delivery POST verifies TLS to the real service name, so a DNS
# rebind of a genuine push host doesn't help either. (A resolve-time IP
# check would flap whenever a known-good host's DNS is briefly unavailable;
# the allowlist is the robust gate.)
PUSH_HOST_SUFFIXES = (
    ".googleapis.com",          # FCM (fcm.googleapis.com)
    ".push.apple.com",          # Safari / iOS
    ".notify.windows.com",      # WNS (Edge)
    ".push.services.mozilla.com",  # Firefox autopush
)


def endpoint_allowed(endpoint) -> bool:
    """Is this a push endpoint on an allowlisted host — read the same way
    by every parser that will touch it?

    The check and the send use different URL parsers: the check here, the
    POST in pywebpush through requests/urllib3. They disagree about some
    strings — `https://169.254.169.254\\@fcm.googleapis.com/x` is a Google
    host to urlsplit (backslash in the userinfo) and the metadata address
    to urllib3 (backslash ends the authority) — and an allowlist checked
    by one parser and obeyed by another is no allowlist. So the endpoint
    must be plain: printable ASCII with no backslash, an authority that is
    the host alone (no userinfo, no port), and the host urllib3 reads must
    be the host checked here."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
        return False
    if "\\" in endpoint or any(not (0x21 <= ord(c) <= 0x7e) for c in endpoint):
        return False
    from urllib.parse import urlsplit
    try:
        p = urlsplit(endpoint)
        host = (p.hostname or "").lower()
    except ValueError:
        return False
    if p.scheme != "https" or not host or p.netloc.lower() != host:
        return False
    try:
        from urllib3.util import parse_url
        if (parse_url(endpoint).host or "").lower() != host:
            return False
    except Exception:                                    # noqa: BLE001
        return False
    return any(host.endswith(sfx) for sfx in PUSH_HOST_SUFFIXES)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _generate_keypair() -> tuple[str, str]:
    """(private_pem, public_b64url) — the raw uncompressed P-256 point is
    what PushManager.subscribe wants as applicationServerKey."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    return priv, _b64u(pub)


def vapid_keys(conn) -> tuple[str, str]:
    """(private_pem, public_b64url), creating + storing on first call.
    `conn` is a control-plane connection."""
    from ..db import crypto
    row = conn.execute("SELECT private_key, public_key FROM push_vapid "
                       "WHERE id=1").fetchone()
    if row:
        return crypto.decrypt_cp(row["private_key"]), row["public_key"]
    priv, pub = _generate_keypair()
    conn.execute(
        "INSERT INTO push_vapid (id, private_key, public_key) "
        "VALUES (1, %s, %s) ON CONFLICT (id) DO NOTHING",
        (crypto.encrypt_cp(priv), pub))
    # a concurrent first-call may have won the insert — read back
    row = conn.execute("SELECT private_key, public_key FROM push_vapid "
                       "WHERE id=1").fetchone()
    return crypto.decrypt_cp(row["private_key"]), row["public_key"]


def send_tenant(conn, tenant_id: str, title: str, body: str,
                url: str = "/app/") -> int:
    """Push to every live subscription of the tenant's users. Returns the
    delivered count; failures log and never raise (same doctrine as SMS).
    `conn` is a control-plane connection."""
    if not available():
        return 0
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush
    subs = conn.execute(
        "SELECT id, endpoint, p256dh, auth FROM push_subscriptions "
        "WHERE tenant_id=%s AND dead_at IS NULL", (tenant_id,)).fetchall()
    if not subs:
        return 0
    priv, _pub = vapid_keys(conn)
    # webpush() treats a STRING vapid_private_key as a FILE PATH (it calls
    # Vapid.from_file), so passing the PEM text directly makes every send
    # fail — push never actually delivers that way. Hand it a built Vapid
    # instead, once, before the loop.
    vapid = Vapid.from_pem(priv.encode())
    sent = 0
    for s in subs:
        if not endpoint_allowed(s["endpoint"]):
            # stored before the endpoint check read it the way the sender
            # does — never POST to it; retire the row instead
            conn.execute("UPDATE push_subscriptions SET dead_at=now() "
                         "WHERE id=%s", (s["id"],))
            log.warning("push subscription %s has a disallowed endpoint; "
                        "retired without sending", s["id"])
            continue
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=vapid,
                vapid_claims={"sub": "mailto:no-reply@localhost"},
                ttl=6 * 3600)
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):     # endpoint gone — browser unsubscribed
                conn.execute("UPDATE push_subscriptions SET dead_at=now() "
                             "WHERE id=%s", (s["id"],))
            else:
                log.warning("push delivery failed (%s): %s", code, e)
        except Exception as e:                   # noqa: BLE001
            log.warning("push delivery failed: %s", e)
    return sent
