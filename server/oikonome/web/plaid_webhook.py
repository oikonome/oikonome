"""Plaid webhook receiver — webhook-driven sync on top of the hourly poll.

POST /api/plaid/webhook has NO session auth (Plaid calls it); authenticity
is the Plaid-Verification header: a JWT signed ES256 by Plaid, validated
against the JWK fetched from /webhook_verification_key/get (Plaid's
documented webhook-verification scheme). The JWT's request_body_sha256
claim binds the signature to the exact raw body, and iat is capped at
5 minutes to stop replays. Anything unverified is a 401.

Verification runs under the PLATFORM env credentials
(OIKONOME_PLAID_CLIENT_ID/SECRET/ENV) — the webhook URL is only ever
registered on link tokens when the env-level OIKONOME_PLAID_WEBHOOK_URL
is set, i.e. the hosted/platform deployment shape. Without env creds the
endpoint rejects everything.

Handled events:
  * TRANSACTIONS / SYNC_UPDATES_AVAILABLE → resolve item_id → tenant
    (admin connection: items are per-tenant RLS rows) and enqueue a
    scoped arq `sync_item` job. Never sync inline in the request.
  * ITEM / ERROR, PENDING_EXPIRATION, PENDING_DISCONNECT,
    USER_PERMISSION_REVOKED, NEW_ACCOUNTS_AVAILABLE → record
    webhook_status on the item row (migration 041) for the re-auth UX
    and the future zombie-Item reaper. A later clean sync clears it,
    as does LOGIN_REPAIRED, which is Plaid saying the Item healed
    without the person doing anything.

Like the other webhooks: per-IP rate limited and body-size capped before
buffering (unauthenticated endpoint, memory-DoS hygiene)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os

from ..envnum import env_num
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from ..db import tenancy
from ..sync.plaid import ENV_URLS
from .security import _sec_event, current_ip, limit

log = logging.getLogger(__name__)

router = APIRouter()

# Plaid webhook bodies are a few KB; cap before buffering — an unauthed
# endpoint must not read request.body() unbounded.
_MAX_BODY = 256 * 1024

# verified JWKs by kid. Plaid rotates keys rarely; a cached key is reused
# until Plaid marks it expired (expired_at set), then refetched once.
_KEY_CACHE: dict[str, dict] = {}
# Both caches are keyed by a kid the REQUEST names, before any signature is
# checked — so an unauthenticated sender could grow them without bound.
# Plaid rotates keys rarely: a handful of live kids is the whole world, and
# clearing the table when it passes a small ceiling costs one extra key
# fetch, never correctness.
_CACHE_MAX = 64
_TRANSPORT = None            # tests inject an httpx.MockTransport here

# This endpoint is UNAUTHENTICATED and the
# key fetch happens BEFORE the signature check (the key is what checks the
# signature). Three defenses so an attacker spraying novel `kid`s can't turn
# each request into a blocking, authenticated round-trip to Plaid on the
# sole event loop:
#   1. the fetch is async (httpx.AsyncClient) and awaited — it never blocks
#      the loop while other requests wait;
#   2. a short NEGATIVE cache: a kid we just failed to resolve short-circuits
#      to 401 for _NEG_TTL seconds, so novel-kid spray can't re-fetch;
#   3. a global ceiling on CONCURRENT outbound key fetches — a burst can't
#      pile unbounded authenticated calls onto Plaid under the platform credentials.
_NEG_CACHE: dict[str, float] = {}       # kid -> monotonic expiry
_NEG_TTL = float(env_num("OIKONOME_PLAID_WEBHOOK_NEG_TTL", "60"))
_FETCH_CEILING = int(env_num(
    "OIKONOME_PLAID_WEBHOOK_FETCH_CEILING", "4"))
# asyncio.Semaphore construction needs no running loop on 3.10+.
_FETCH_SEM = asyncio.Semaphore(_FETCH_CEILING)


def _platform_creds() -> tuple[str, str, str] | None:
    cid = os.environ.get("OIKONOME_PLAID_CLIENT_ID")
    secret = os.environ.get("OIKONOME_PLAID_SECRET")
    env = os.environ.get("OIKONOME_PLAID_ENV", "production")
    if not cid or not secret:
        return None
    return cid, secret, ENV_URLS.get(env, ENV_URLS["production"])


async def _fetch_key(kid: str) -> dict | None:
    """POST /webhook_verification_key/get — async, never raises, never
    blocks the event loop."""
    creds = _platform_creds()
    if not creds:
        return None
    cid, secret, base_url = creds
    try:
        async with httpx.AsyncClient(
                base_url=base_url, transport=_TRANSPORT,
                timeout=httpx.Timeout(10, connect=5)) as http:
            r = await http.post("/webhook_verification_key/get",
                                json={"client_id": cid, "secret": secret,
                                      "key_id": kid})
            if r.status_code != 200:
                return None
            return r.json().get("key")
    except Exception:                                      # noqa: BLE001
        return None


async def _key_for(kid: str) -> dict | None:
    jwk = _KEY_CACHE.get(kid)
    if jwk is not None and not jwk.get("expired_at"):
        return jwk
    # negative cache: a kid we recently failed to resolve costs no fetch
    exp = _NEG_CACHE.get(kid)
    if exp is not None:
        if exp > time.monotonic():
            return None
        _NEG_CACHE.pop(kid, None)
    # global ceiling: a concurrency burst must not pile authenticated fetches
    # onto Plaid. Exhaustion is transient (don't poison a legit kid) — fail
    # this one verification (401); the hourly poll remains the reconcile floor.
    if _FETCH_SEM.locked():
        return None
    async with _FETCH_SEM:
        jwk = await _fetch_key(kid)
    if jwk:
        if len(_KEY_CACHE) >= _CACHE_MAX:
            _KEY_CACHE.clear()
        _KEY_CACHE[kid] = jwk
        return jwk
    if len(_NEG_CACHE) >= _CACHE_MAX:
        _NEG_CACHE.clear()
    _NEG_CACHE[kid] = time.monotonic() + _NEG_TTL
    return None


def _b64url(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _refuse(why: str) -> bool:
    """Every 401 says why in the log — which stage refused, never the
    secret material. A webhook Plaid confirms it fired but the app turns
    away with a bare 401 is undiagnosable from the outside; the reason
    costs nothing and is what an operator needs.

    `why` is always one of a FIXED set of stage tokens, never a value from
    the JWT: this stream is parsed by fail2ban, and the unauthenticated
    header's kid/alg would otherwise let anyone write a forged
    `event=autoban ip=…` record into it. _sec_event also sanitises every
    field, so a second record is unrepresentable either way."""
    _sec_event("plaid_webhook_refused", current_ip(), reason=why)
    return False


async def verify(header_jwt: str, body: bytes) -> bool:
    """True iff `header_jwt` (the Plaid-Verification header) is a valid,
    fresh Plaid ES256 JWT whose request_body_sha256 matches `body`. Async
    so the key fetch never blocks the sole event loop."""
    if not header_jwt:
        return _refuse("no_header")
    try:
        h_b64, p_b64, s_b64 = header_jwt.split(".")
        hdr = json.loads(_b64url(h_b64))
    except (ValueError, AttributeError):
        return _refuse("malformed")
    # pin the algorithm — accepting attacker-chosen algs (none/HS256) is
    # the classic JWT verification bypass
    if hdr.get("alg") != "ES256" or not hdr.get("kid"):
        return _refuse("bad_alg")
    jwk = await _key_for(hdr["kid"])
    if not jwk:
        # fetch failed, negative-cached, or at the fetch ceiling
        return _refuse("no_key")
    if jwk.get("expired_at") or jwk.get("kty") != "EC" \
            or jwk.get("crv") != "P-256":
        return _refuse("bad_key")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import \
        encode_dss_signature
    try:
        pub = ec.EllipticCurvePublicNumbers(
            int.from_bytes(_b64url(jwk["x"]), "big"),
            int.from_bytes(_b64url(jwk["y"]), "big"),
            ec.SECP256R1()).public_key()
        sig = _b64url(s_b64)
        if len(sig) != 64:                       # JOSE raw r||s, not DER
            return _refuse("bad_sig")
        pub.verify(
            encode_dss_signature(int.from_bytes(sig[:32], "big"),
                                 int.from_bytes(sig[32:], "big")),
            f"{h_b64}.{p_b64}".encode(), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return _refuse("bad_sig")
    try:
        claims = json.loads(_b64url(p_b64))
    except ValueError:
        return _refuse("bad_claims")
    iat = claims.get("iat")
    if not isinstance(iat, (int, float)) or isinstance(iat, bool) \
            or abs(time.time() - iat) > 300:     # 5-min freshness (replay)
        return _refuse("stale")
    want = claims.get("request_body_sha256")
    if not isinstance(want, str):
        return _refuse("no_body_hash")
    if not hmac.compare_digest(want, hashlib.sha256(body).hexdigest()):
        return _refuse("body_hash_mismatch")
    return True


# ---- event handling --------------------------------------------------------


def _tenant_for_item(item_id: str) -> str | None:
    """items rows are per-tenant (RLS); the webhook is tenantless, so the
    lookup runs on the admin connection."""
    admin = tenancy.admin_connect()
    try:
        # suspended tenants don't sync (suspension is total — the hourly
        # sweep also skips them); a webhook must not sneak one in. Archived
        # items don't either: after disconnect (/item/remove + archive) a
        # late webhook must not enqueue a sync that would stamp
        # error:ITEM_NOT_FOUND over 'archived'.
        row = admin.execute(
            """SELECT i.tenant_id FROM items i
               JOIN tenants t ON t.id = i.tenant_id AND t.status = 'active'
               WHERE i.id=%s AND i.aggregator='plaid'
                 AND COALESCE(i.status,'') != 'archived'""",
            (item_id,)).fetchone()
        return str(row["tenant_id"]) if row else None
    finally:
        admin.close()


async def _enqueue_sync(tenant_id: str, item_id: str) -> bool:
    """Queue the scoped sync through arq — the webhook request must never
    run a sync inline. False (logged) when Redis is unreachable; the
    hourly poll remains the reconcile floor."""
    try:
        from arq import create_pool

        from ..jobs.worker import _redis_settings
        pool = await create_pool(_redis_settings())
        try:
            await pool.enqueue_job("sync_item", tenant_id, item_id)
        finally:
            await pool.close()
        return True
    except Exception as e:                                 # noqa: BLE001
        log.warning("plaid webhook: enqueue failed item=%s: %s", item_id, e)
        return False


# ITEM webhook codes we persist; everything else is acknowledged and
# dropped (the hourly poll discovers whatever state matters).
_ITEM_STATUS = {
    "ERROR": lambda evt: "error:" + (
        (evt.get("error") or {}).get("error_code") or "UNKNOWN"),
    "PENDING_EXPIRATION": lambda evt: "pending_expiration",
    # the sibling of PENDING_EXPIRATION: consent is about to lapse and
    # update mode is what prevents the disconnect. It was unhandled, so
    # the only warning arrived after the connection had already broken.
    "PENDING_DISCONNECT": lambda evt: "pending_disconnect",
    "USER_PERMISSION_REVOKED": lambda evt: "revoked",
    # a bank the person opened a new account at. Plaid's guidance is to
    # prompt update mode so they can share it; ignoring this is why a
    # newly-opened account would simply never appear, with nothing on
    # screen suggesting the app knew.
    "NEW_ACCOUNTS_AVAILABLE": lambda evt: "new_accounts",
}

# The one that CLEARS rather than sets. Plaid fires LOGIN_REPAIRED when
# an Item healed without the person going through update mode (the bank
# resolved it, or they fixed credentials elsewhere), and its documented
# instruction is to stop showing the re-auth prompt. Without it the app
# keeps demanding a sign-in that is no longer needed until the next
# successful sync happens to clear the flag.
_ITEM_CLEARED = ("LOGIN_REPAIRED",)


def _clear_item_status(item_id: str) -> bool:
    """The Item healed on its own — drop the webhook flag AND an error
    status left by a failing sync, so the "needs attention" door closes
    at the moment Plaid says it should rather than an hour later.
    'archived' still wins: a disconnect must not be undone by a late
    webhook about the connection it released."""
    admin = tenancy.admin_connect()
    try:
        cur = admin.execute(
            "UPDATE items SET webhook_status=NULL, webhook_status_at=NULL, "
            "status='ok' WHERE id=%s AND aggregator='plaid' "
            "AND COALESCE(status,'') != 'archived'", (item_id,))
        return cur.rowcount > 0
    finally:
        admin.close()


def _record_item_status(item_id: str, status: str) -> bool:
    admin = tenancy.admin_connect()
    try:
        cur = admin.execute(
            "UPDATE items SET webhook_status=%s, webhook_status_at=now() "
            "WHERE id=%s AND aggregator='plaid' "
            "AND COALESCE(status,'') != 'archived'", (status, item_id))
        return cur.rowcount > 0
    finally:
        admin.close()


@router.post("/api/plaid/webhook",
             dependencies=[Depends(limit("plaid_webhook", 120, 60))])
async def plaid_webhook(request: Request):
    """Plaid → us. Auth is the Plaid-Verification JWT, never a session."""
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > _MAX_BODY:
        raise HTTPException(413, "payload too large")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY:
            raise HTTPException(413, "payload too large")
        chunks.append(chunk)
    payload = b"".join(chunks)
    if not await verify(request.headers.get("plaid-verification", ""),
                        payload):
        raise HTTPException(401, "webhook verification failed")
    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(400, "invalid payload")
    wtype = event.get("webhook_type")
    wcode = event.get("webhook_code")
    item_id = event.get("item_id") or ""
    disposition = "ignored"
    # INITIAL_UPDATE / HISTORICAL_UPDATE matter for the same reason as
    # SYNC_UPDATES_AVAILABLE: each says "more transactions are ready".
    # HISTORICAL_UPDATE especially — it fires when the full backfill
    # window has landed, and the sync it triggers records
    # HISTORICAL_UPDATE_COMPLETE on the item, which is what the setup
    # wizard's step-2 gate is waiting to see.
    if wtype == "TRANSACTIONS" and wcode in (
            "SYNC_UPDATES_AVAILABLE", "INITIAL_UPDATE",
            "HISTORICAL_UPDATE") and item_id:
        tenant_id = _tenant_for_item(item_id)
        if tenant_id:
            queued = await _enqueue_sync(tenant_id, item_id)
            disposition = "sync-enqueued" if queued else "enqueue-failed"
        else:
            disposition = "unknown-item"
    elif wtype == "ITEM" and wcode in _ITEM_STATUS and item_id:
        status = _ITEM_STATUS[wcode](event)
        disposition = ("status-recorded" if _record_item_status(
            item_id, status) else "unknown-item")
    elif wtype == "ITEM" and wcode in _ITEM_CLEARED and item_id:
        disposition = ("status-cleared" if _clear_item_status(item_id)
                       else "unknown-item")
    return {"received": True, "disposition": disposition}
