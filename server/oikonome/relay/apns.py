"""APNs (iOS) delivery for the relay.

Token-based auth: an ES256 JWT signed with the publisher's APNs key
(a ``.p8`` file), refreshed well inside Apple's 20–60 minute window.
APNs speaks HTTP/2 only, so requests go through a pooled ``httpx``
client with ``http2=True`` (the ``h2`` package provides the protocol).

A device token is minted against one environment — sandbox for local
dev builds, production for TestFlight and the App Store — and the relay
cannot tell which from the token itself. Delivery tries production
first and retries the sandbox host when APNs answers ``BadDeviceToken``;
only a token both environments refuse is reported dead.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.parse

log = logging.getLogger("oikonome.relay.apns")

_PROD = "https://api.push.apple.com"
_SANDBOX = "https://api.sandbox.push.apple.com"

# Apple rejects provider tokens older than an hour and throttles refresh
# under 20 minutes; 50 minutes sits comfortably between the two.
_JWT_LIFETIME = 50 * 60

_key: dict | None = None                 # {"pem": bytes, "key_id", "team_id", "topic"}
_jwt: tuple[str, float] | None = None    # (token, minted epoch)
_clients: dict = {}                      # base URL -> pooled httpx.Client
# sends fan out on a thread pool, so client creation needs a guard —
# without it two threads can each build a client and one leaks
_clients_lock = threading.Lock()
# The provider token is the same shape of shared state, read from every
# request thread and every send-worker they fan out across. Unguarded, the
# expiry boundary — and the ExpiredProviderToken remint, which every
# in-flight send hits at once — had each thread signing its own JWT and
# presenting it to Apple, which throttles providers that refresh too often.
# The guard makes the mint single-flight.
_jwt_lock = threading.Lock()


def _load_key() -> dict | None:
    global _key
    if _key is not None:
        return _key
    path = (os.environ.get("OIKONOME_RELAY_APNS_KEY") or "").strip()
    key_id = (os.environ.get("OIKONOME_RELAY_APNS_KEY_ID") or "").strip()
    team_id = (os.environ.get("OIKONOME_RELAY_APNS_TEAM_ID") or "").strip()
    topic = (os.environ.get("OIKONOME_RELAY_APNS_TOPIC") or "").strip()
    if not (path and key_id and team_id and topic):
        return None
    if not os.path.exists(path):
        log.error("apns: key file %s does not exist", path)
        return None
    try:
        with open(path, "rb") as f:
            pem = f.read()
        # fail here, loudly, rather than on the first send
        from cryptography.hazmat.primitives import serialization
        serialization.load_pem_private_key(pem, password=None)
    except Exception:                              # noqa: BLE001
        log.exception("apns: could not read the signing key")
        return None
    _key = {"pem": pem, "key_id": key_id, "team_id": team_id,
            "topic": topic}
    return _key


def available() -> bool:
    return _load_key() is not None


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _signed_jwt(key: dict) -> str:
    """ES256 provider token. JOSE wants the raw r||s signature, not the
    DER blob ``cryptography`` produces, hence the decode/re-pack."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature)
    now = int(time.time())
    header = _b64u(json.dumps(
        {"alg": "ES256", "kid": key["key_id"]}).encode())
    claims = _b64u(json.dumps(
        {"iss": key["team_id"], "iat": now}).encode())
    signing_input = f"{header}.{claims}".encode()
    pk = serialization.load_pem_private_key(key["pem"], password=None)
    der = pk.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{claims}.{_b64u(sig)}"


def _provider_token(fresh: bool = False) -> str | None:
    """The cached provider token, minting one when it is stale (or when
    `fresh` says Apple has just rejected the one we hold).

    Concurrent callers mint ONE token between them: a token minted after
    this caller decided it needed one is by definition newer than the one
    it found stale — or than the one Apple just refused — so taking the
    other thread's is both correct and one signature instead of N.
    """
    global _jwt
    now = time.time()
    if not fresh and _jwt and now - _jwt[1] < _JWT_LIFETIME:
        return _jwt[0]
    with _jwt_lock:
        minted = _jwt
        if minted and minted[1] >= now:
            return minted[0]
        key = _load_key()
        if key is None:
            return None
        try:
            _jwt = (_signed_jwt(key), time.time())
            return _jwt[0]
        except Exception:                          # noqa: BLE001
            log.exception("apns: could not sign the provider token")
            return None


def _request(base: str, token: str, headers: dict, payload: dict):
    """One POST to one APNs host, on a pooled HTTP/2 connection."""
    import httpx
    with _clients_lock:
        cli = _clients.get(base)
        if cli is None:
            cli = httpx.Client(base_url=base, http2=True, timeout=10.0)
            _clients[base] = cli
    # The endpoint only admits hex tokens, but this module must not lean
    # on its caller: percent-encode so no token text can ever extend or
    # redirect the request path on this credentialed connection.
    return cli.post(f"/3/device/{urllib.parse.quote(token, safe='')}",
                    json=payload, headers=headers)


def _reason(response) -> str:
    try:
        return response.json().get("reason") or ""
    except Exception:                              # noqa: BLE001
        return ""


# Same display rule as the FCM leg: fixed text keyed on the event unless
# the instance sent a bounded one-line `text`. One table serves both.
from .fcm import _DISPLAY                                    # noqa: E402


def send(token: str, event: str, badge: int | None = None,
         text: str | None = None) -> str:
    """One tickle to one device. Returns "ok", "dead" (both APNs
    environments refuse the token — the caller reports it back to the
    instance), or "error". `text`, when the instance sent one, replaces
    the fixed body line."""
    key = _load_key()
    provider = _provider_token()
    if key is None or provider is None:
        return "error"
    title, body = _DISPLAY.get(event, ("Oikonome", "Open the app for details"))
    if text:
        body = text
    aps: dict = {"alert": {"title": title, "body": body}, "sound": "default"}
    if badge is not None:
        aps["badge"] = badge
    payload = {"aps": aps, "event": event}
    headers = {"authorization": f"bearer {provider}",
               "apns-topic": key["topic"],
               "apns-push-type": "alert",
               "apns-priority": "10"}
    refused = 0
    for base in (_PROD, _SANDBOX):
        try:
            r = _request(base, token, headers, payload)
            if r.status_code == 403 and _reason(r) == "ExpiredProviderToken":
                reminted = _provider_token(fresh=True)
                if reminted is None:
                    # retrying would send the literal "bearer None" and
                    # dress a signing failure up as a delivery failure
                    log.error("apns: could not re-sign after "
                              "ExpiredProviderToken")
                    return "error"
                headers["authorization"] = f"bearer {reminted}"
                r = _request(base, token, headers, payload)
        except Exception as e:                     # noqa: BLE001
            log.warning("apns: send failed: %s", e)
            return "error"
        if r.status_code == 200:
            return "ok"
        reason = _reason(r)
        if r.status_code == 410 or reason == "Unregistered":
            return "dead"
        if reason == "BadDeviceToken":
            refused += 1
            continue                 # likely minted for the other environment
        log.warning("apns: send returned %s (%s)", r.status_code,
                    reason or "no reason")
        return "error"
    return "dead" if refused == 2 else "error"
