"""FCM (Android) delivery for the relay.

Credentials are a Firebase service-account JSON file, path in
``OIKONOME_RELAY_FCM_CREDS``. The OAuth dance is done by hand — a
signed RS256 JWT exchanged for an access token — because the two
libraries this needs (cryptography, httpx) are already dependencies and
google-auth would be a third for one request shape.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time

log = logging.getLogger("oikonome.relay.fcm")

_OAUTH_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

_creds: dict | None = None
_access: tuple[str, float] | None = None    # (token, expiry epoch)
# The cache above is read from every relay request thread and from the
# send-workers each request fans out across, so the refresh below is a
# check-then-act on shared state. Unlocked, every thread that met the
# cache in the instant it went stale exchanged its own JWT for its own
# token: a thundering herd against the publisher's shared OAuth
# credentials at every expiry boundary, on traffic from every instance
# the relay serves. The lock makes the refresh single-flight.
_access_lock = threading.Lock()
_access_tried: float = 0.0     # when the last refresh attempt FINISHED


def _load_creds() -> dict | None:
    global _creds
    if _creds is not None:
        return _creds
    path = (os.environ.get("OIKONOME_RELAY_FCM_CREDS") or "").strip()
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        if not all(data.get(k) for k in
                   ("project_id", "client_email", "private_key")):
            log.error("fcm: credentials file is missing required fields")
            return None
        _creds = data
        return _creds
    except Exception:                              # noqa: BLE001
        log.exception("fcm: could not read credentials")
        return None


def available() -> bool:
    return _load_creds() is not None


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _signed_jwt(creds: dict) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(time.time())
    header = _b64u(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64u(json.dumps({
        "iss": creds["client_email"], "scope": _SCOPE,
        "aud": _OAUTH_URL, "iat": now, "exp": now + 3600}).encode())
    signing_input = f"{header}.{claims}".encode()
    key = serialization.load_pem_private_key(
        creds["private_key"].encode(), password=None)
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64u(sig)}"


def _cached_access(now: float | None = None) -> str | None:
    cached = _access
    if cached and cached[1] > (time.time() if now is None else now) + 60:
        return cached[0]
    return None


def _access_token() -> str | None:
    """OAuth access token, cached until shortly before expiry.

    Concurrent callers that find the cache stale produce exactly ONE
    exchange with Google between them: the first through the lock refreshes,
    the rest take whatever verdict it reached. Taking the FAILURE too is
    deliberate — a refresh serialized behind a ten-second timeout, retried
    once per waiting thread, would turn one unreachable token endpoint into
    a request that hangs for a multiple of that.
    """
    global _access, _access_tried
    now = time.time()
    token = _cached_access(now)
    if token is not None:
        return token
    creds = _load_creds()
    if creds is None:
        return None
    with _access_lock:
        # A refresh that finished after this thread found the cache stale
        # has already asked the question this thread was about to ask.
        if _access_tried >= now:
            return _cached_access()
        try:
            import httpx
            r = httpx.post(_OAUTH_URL, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": _signed_jwt(creds)}, timeout=10.0)
            r.raise_for_status()
            out = r.json()
            _access = (out["access_token"],
                       time.time() + int(out.get("expires_in") or 3600))
            return _access[0]
        except Exception:                          # noqa: BLE001
            log.exception("fcm: token exchange failed")
            return None
        finally:
            _access_tried = time.time()


# What the phone SHOWS: fixed text keyed on the event, baked into the
# relay, unless the instance sent a one-line `text` — the daily verdict
# ("OVER budget by $120 · Spend at most $23/day food …"), which the
# instance owner chose to have on the lock screen rather than a bare
# "your verdict is ready". The relay bounds and flattens that line
# (app.py) and never stores it. The app still fetches the real content
# from its own server when opened.
_DISPLAY = {
    "daily":   ("Oikonome", "Today's verdict is ready"),
    "weekly":  ("Oikonome", "Your weekly summary is ready"),
    "monthly": ("Oikonome", "Your monthly summary is ready"),
    "yearly":  ("Oikonome", "Your yearly summary is ready"),
    "alert":   ("Oikonome", "Something on your budget needs a look"),
    "test":    ("Oikonome", "Test notification from your server"),
}


def send(token: str, event: str, badge: int | None = None,
         text: str | None = None) -> str:
    """One tickle to one device. Returns "ok", "dead" (token unroutable
    — the caller reports it back to the instance), or "error". `text`,
    when the instance sent one, replaces the fixed body line."""
    creds = _load_creds()
    access = _access_token()
    if creds is None or access is None:
        return "error"
    data = {"event": event}
    if badge is not None:
        data["badge"] = str(badge)
    title, body = _DISPLAY.get(event, ("Oikonome", "Open the app for details"))
    if text:
        body = text
    msg = {"message": {
        "token": token,
        "data": data,
        "notification": {"title": title, "body": body},
        "android": {"priority": "high"},
    }}
    try:
        import httpx
        r = httpx.post(
            "https://fcm.googleapis.com/v1/projects/"
            f"{creds['project_id']}/messages:send",
            json=msg, headers={"Authorization": f"Bearer {access}"},
            timeout=10.0)
    except Exception as e:                         # noqa: BLE001
        log.warning("fcm: send failed: %s", e)
        return "error"
    if r.status_code == 200:
        return "ok"
    if r.status_code in (404, 410):
        return "dead"
    try:
        detail = r.json().get("error", {})
    except Exception:                              # noqa: BLE001
        detail = {}
    # UNREGISTERED arrives as a 404; INVALID_ARGUMENT on a garbage token
    # means the same thing from the caller's perspective — stop sending
    if detail.get("status") in ("UNREGISTERED", "INVALID_ARGUMENT"):
        return "dead"
    log.warning("fcm: send returned %s (%s)", r.status_code,
                detail.get("status") or "no detail")
    return "error"
