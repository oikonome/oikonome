"""Web Push delivery (no native app — the SPA's service worker shows the
notification). VAPID keypair is per-instance, generated lazily on first
use and stored in the control-plane push_vapid row with the private key
encrypted under OIKONOME_MASTER_KEY. Subscriptions live in
push_subscriptions (one per browser); delivery failures with 404/410
mark the subscription dead instead of retrying forever."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re

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


# One push attempt's bound (connect, then each read). Without it a push
# service that accepts the connection and then goes quiet holds the send,
# and with it the household's email or sync lock, until the worker restarts.
PUSH_TIMEOUT = 10.0

# The last resort when the operator has named no address at all. Apple's
# push service refuses it; the others accept it.
_FALLBACK_SUBJECT = "mailto:no-reply@localhost"
_MAILBOX = re.compile(r"^[^@\s<>]+@([^@\s<>]+\.[^@\s<>]+)$")


def _mailbox(value: str | None) -> str | None:
    """A bare address a push service will take as a contact: not a
    localhost or reserved-for-testing domain, which Apple refuses."""
    v = (value or "").strip()
    if v.lower().startswith("mailto:"):
        v = v[len("mailto:"):]
    m = _MAILBOX.match(v)
    if not m:
        return None
    domain = m.group(1).lower()
    if domain in ("localhost", "localdomain") or domain.endswith(
            (".localhost", ".local", ".invalid", ".test", ".example")):
        return None
    return v


def vapid_subject() -> str:
    """Who a push service contacts about this instance's pushes, as the
    VAPID `sub` claim: a mailto: or https: URL naming the operator. The
    push services check it, and Apple's refuses every delivery signed with
    a localhost subject. OIKONOME_PUSH_CONTACT when set; otherwise the
    support address, then the mail sender, then the public base URL."""
    explicit = (os.environ.get("OIKONOME_PUSH_CONTACT") or "").strip()
    if explicit.lower().startswith("https://"):
        return explicit
    for value in (explicit, os.environ.get("OIKONOME_SUPPORT_EMAIL"),
                  os.environ.get("OIKONOME_SMTP_FROM")):
        box = _mailbox(value)
        if box:
            return "mailto:" + box
    base = (os.environ.get("OIKONOME_BASE_URL") or "").strip().rstrip("/")
    if base.lower().startswith("https://"):
        from urllib.parse import urlsplit
        host = (urlsplit(base).hostname or "").lower()
        if host and host != "localhost" and "." in host:
            return base
    return _FALLBACK_SUBJECT


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


# A subscription is live while its browser's session is: one bound to a
# session that has expired but not yet been swept would otherwise keep
# receiving the household's verdict lines and alert text after the cookie
# that made it stopped working. Rows from before subscriptions named their
# session have none to check and stay live until their browser subscribes
# again (which binds them). A subscription also speaks for the household
# it was made in only while its person still belongs to it: someone moved
# to another household must not keep receiving the old one's notices on a
# browser subscribed before the move (a legacy row has no session whose
# end would retire it). Every read that counts or sends uses this.
LIVE = ("dead_at IS NULL AND (session_id IS NULL OR EXISTS ("
        "SELECT 1 FROM sessions ps WHERE ps.id = push_subscriptions.session_id"
        " AND ps.expires_at > now())) AND EXISTS ("
        "SELECT 1 FROM users pu WHERE pu.id = push_subscriptions.user_id"
        " AND pu.tenant_id = push_subscriptions.tenant_id)")


@contextlib.contextmanager
def db_handle(conn, connect):
    """The connection a send's database steps use: `conn` as given, or a
    fresh one from `connect` closed again at the end of the step. Shared
    with push_native so the two channels hold connections the same way."""
    if connect is None:
        yield conn
        return
    c = connect()
    try:
        yield c
    finally:
        c.close()


def send_tenant(conn, tenant_id: str, title: str, body: str,
                url: str = "/app/", *, user_id=None, connect=None) -> int:
    """Push to every live subscription of the tenant's users — or, with
    `user_id`, only that one user's browsers (a notice about one person's
    own account is theirs to read, not the household's). Returns the
    delivered count; failures log and never raise (same doctrine as SMS).
    `conn` is a control-plane connection.

    Pass `connect` (a callable that opens one) instead of `conn` and the
    send holds a connection only for its reads and writes, and NONE while a
    push service answers. Each POST may take PUSH_TIMEOUT; a request-path
    caller holding one of the small control pool's connections across them
    takes it from every signed-in request's session check."""
    if conn is None and connect is None:
        raise TypeError("send_tenant needs a connection or `connect`")
    if not available():
        return 0
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush
    with db_handle(conn, connect) as c:
        if user_id is None:
            subs = c.execute(
                "SELECT id, endpoint, p256dh, auth FROM push_subscriptions "
                f"WHERE tenant_id=%s AND {LIVE}", (tenant_id,)).fetchall()
        else:
            subs = c.execute(
                "SELECT id, endpoint, p256dh, auth FROM push_subscriptions "
                f"WHERE tenant_id=%s AND user_id=%s AND {LIVE}",
                (tenant_id, user_id)).fetchall()
        if not subs:
            return 0
        sendable = []
        for s in subs:
            if not endpoint_allowed(s["endpoint"]):
                # stored before the endpoint check read it the way the
                # sender does — never POST to it; retire the row instead
                c.execute("UPDATE push_subscriptions SET dead_at=now() "
                          "WHERE id=%s", (s["id"],))
                log.warning("push subscription %s has a disallowed "
                            "endpoint; retired without sending", s["id"])
                continue
            sendable.append(s)
        if not sendable:
            return 0
        try:
            priv, _pub = vapid_keys(c)
            # webpush() treats a STRING vapid_private_key as a FILE PATH (it
            # calls Vapid.from_file), so passing the PEM text directly makes
            # every send fail — push never actually delivers that way. Hand
            # it a built Vapid instead, once, before the loop.
            vapid = Vapid.from_pem(priv.encode())
        except Exception as e:                       # noqa: BLE001
            # The stored signing key cannot be read — typically it was
            # encrypted under a master key this process does not have (the
            # key was removed or changed after the startup sweep encrypted
            # it). Every existing browser subscription is bound to that
            # key, so minting a new one would orphan them all; keep the
            # row, send nothing, and say how to recover. The caller's other
            # channels still go out.
            log.error("web push skipped: the stored push signing key cannot "
                      "be read (%s: %s). Restore the OIKONOME_MASTER_KEY "
                      "it was encrypted under; `oikonome keycheck` checks "
                      "which secrets the current key can read",
                      type(e).__name__, e)
            return 0
    sent = 0
    gone = []
    claims_sub = vapid_subject()
    for s in sendable:
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=vapid,
                # a fresh dict per send: pywebpush writes the audience and
                # expiry of THIS endpoint into the claims it is handed
                vapid_claims={"sub": claims_sub},
                timeout=PUSH_TIMEOUT,
                ttl=6 * 3600)
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):     # endpoint gone — browser unsubscribed
                gone.append(s["id"])
            else:
                log.warning("push delivery failed (%s): %s", code, e)
        except Exception as e:                   # noqa: BLE001
            log.warning("push delivery failed: %s", e)
    if gone:
        try:
            with db_handle(conn, connect) as c:
                c.execute("UPDATE push_subscriptions SET dead_at=now() "
                          "WHERE id = ANY(%s)", (gone,))
        except Exception as e:                   # noqa: BLE001
            log.warning("could not retire gone push subscriptions: %s", e)
    return sent
