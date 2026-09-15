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
