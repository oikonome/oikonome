"""The relay's HTTP surface: one endpoint, strict shape, no state.

Two layers of defense. The structural one holds regardless of
configuration: the payload admits one free-form string and only on the
daily push (a single display line, flattened and length-capped; every
other field is an enum, a small integer, or the platform token
itself), batches are capped,
bodies are capped, senders are rate-limited per source address and per
instance key (and, with no keys configured, the relay as a whole is
capped, since there is then no sender identity a spray cannot mint a
fresh one of), and tokens are never logged. On top of that sits an
instance key: the relay speaks for the publisher's APNs/FCM
credentials, so without one, anyone who can spray device tokens can
wake every phone and burn the publisher's quota through it. A relay
with `OIKONOME_RELAY_KEYS` set refuses every push that does not carry
one of those keys; a relay with none configured stays open (a
self-hoster running their own relay for their own instance loses
nothing) but says so once at startup.

Everything decidable from the request HEADERS is decided at the ASGI
edge, in `_EdgeGate`, before a byte of the body is buffered. FastAPI
reads and JSON-parses the whole body to build the handler's argument,
so a key check written inside the handler runs only after that memory
has already been spent — an unauthenticated caller could hand the
shared relay a multi-gigabyte body and be refused far too late. The
instance app registers a body-size middleware for the same reason
(`web/security.BodySizeLimitMiddleware`); shared infrastructure needs
it more, not less, so here the auth and rate checks move out to the
edge alongside it.

A key on its own says only that the caller is *an* opted-in instance —
not that the device tokens in its batch belong to its own users. So the
relay also BINDS each token to the key that first pushed it, and refuses
a later push for that token from any other key (403). Without the
binding, any accepted key could tickle any token it learned:
content-free notification bombing of someone else's household, and burn
of the publisher's shared push quota. Registration is the first push
carrying the token, because that is the only moment the relay hears of
one — the instance registers devices with itself, never here.

The registry is in memory and bounded, exactly like the rate-limit
windows and for the same reasons: one small container, and state that a
restart may forget without harm. What it costs is that a restart, or
eviction under a very large token population, re-opens first-use
claiming for a token. What it buys is that stealing someone's device
token is no longer enough — you need their instance key too, or a
window in which nobody is pushing that token. A binding expires after
TOKEN_BINDING_TTL of silence so a household that genuinely MOVES
instances (hosted to self-hosted, say) is not locked out for ever;
anything actively pushed refreshes on every push and never expires. A
key that leaves OIKONOME_RELAY_KEYS takes its claims with it, which is
how a key rotation completes — see `_claim_tokens`.
"""

from __future__ import annotations

import collections
import concurrent.futures
import contextlib
import hashlib
import hmac
import logging
import os
import re
import threading
import time

from fastapi import Body, FastAPI, HTTPException, Request

from .. import logsafe
# The trusted-proxy walk is shared with the instance on purpose: the relay
# runs from the same image, and the rule (believe X-Forwarded-For only when
# the socket peer is a trusted proxy, then take the rightmost untrusted hop)
# is exactly what keeps the per-address limiter from being sidestepped by a
# forged header on a direct-to-container hit.
from ..web.security import client_ip
from . import apns, fcm

log = logging.getLogger("oikonome.relay")

# The relay is a process of its own (`uvicorn oikonome.relay.app:app`), so
# it has to flatten its own log records: the instance installs the factory
# at web startup and the worker at its entrypoint, and neither of those
# runs here. It matters more here than there — the gate below logs the
# request path on refusals that carry no credential at all, and the ASGI
# server percent-decodes the target, so a request for `/v1/%0A...` would
# otherwise write a second, invented line into the operator's log. At
# import rather than in the lifespan because import is the one moment that
# precedes every record this process can emit.
logsafe.install()

# Instance keys the relay accepts, comma-separated so one can be rotated
# in while the other is still in use. The instance sends its key in the
# X-Relay-Key header (`OIKONOME_PUSH_RELAY_KEY` on that side).
KEY_HEADER = "x-relay-key"


def _keys() -> tuple[str, ...]:
    raw = os.environ.get("OIKONOME_RELAY_KEYS") or ""
    return tuple(k.strip() for k in raw.split(",") if k.strip())


def _announce_key_posture() -> None:
    """Said once, when the process comes up, so an open relay is a line in
    the startup log rather than a surprise."""
    if _keys():
        log.info("relay: instance key required on /v1/push")
    else:
        log.warning("relay: OIKONOME_RELAY_KEYS is not set — /v1/push is "
                    "OPEN to any sender. Fine for a relay serving only your "
                    "own instance; a relay reachable by others must set it.")


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    _announce_key_posture()
    yield


app = FastAPI(title="oikonome-push-relay", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)

# The identity of a caller on a relay with no keys configured. It is not
# a key fingerprint and never gets a rate-limit bucket of its own: an
# open relay has no sender to attribute anything to, and one shared
# bucket would make every unkeyed sender compete for one window.
_OPEN = "open"


def _fingerprint(key: str) -> str:
    """A short digest of an instance key — enough to tell two senders
    apart in the log and to give each its own rate-limit bucket, never
    enough to replay the key itself."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _key_identity(request: Request) -> str | None:
    """Who is calling: a key fingerprint, `_OPEN` when the relay has no
    keys configured, or None when the caller is refused.

    Every configured key is compared even after a match, in constant
    time, so neither timing nor the returned identity reveals where in
    the list a key sits — the fingerprint is of the key that matched,
    derived after the loop.
    """
    keys = _keys()
    if not keys:
        return _OPEN
    given = (request.headers.get(KEY_HEADER) or "").strip()
    matched = None
    for k in keys:
        if hmac.compare_digest(given.encode(), k.encode()):
            matched = k
    return None if matched is None else _fingerprint(matched)


# mirror of notify/push_native.py EVENTS — the instance-side allowlist
EVENTS = frozenset({"daily", "weekly", "monthly", "yearly", "alert", "test"})

MAX_BATCH = 500
MAX_TOKEN_LEN = 4096
# The one content field a push may carry: a single display line (the
# daily verdict), bounded so the relay can never be used to ship a
# paragraph — and flattened to one printable line, since it goes
# straight into the platform's notification body.
MAX_TEXT_LEN = 160
# ...and carried by the daily push only. The instance drops it for every
# other event, but the promise is about what the relay will FORWARD, so
# the relay is where it has to hold: any keyed sender could otherwise
# push arbitrary display lines under an event nobody expects to carry
# content. A batch that tries is refused whole, so a misbehaving
# instance hears about it on the first attempt instead of shipping
# unreviewed text to phones until someone reads a log.
TEXT_EVENTS = frozenset({"daily"})

# A real APNs device token is hexadecimal (64 bytes today, but Apple says
# to treat the length as variable). The check matters because the token
# ends up in the request path of the relay's credentialed APNs connection.
# A token that cannot possibly be one is reported "dead" rather than
# 400ing the request: the relay's contract is per-token outcomes, and
# "dead" is the verdict that makes the instance drop the token instead of
# resending it on every sweep.
_APNS_TOKEN = re.compile(r"[0-9a-fA-F]{16,200}\Z")

# Every send — APNs or FCM — is a blocking HTTPS round-trip, and a full
# batch of them in sequence would hold the request for minutes while
# every other instance sharing this relay waits. Both legs fan out
# across one pool: APNs multiplexes over a pooled HTTP/2 connection and
# FCM is one POST per token, but the request-thread cost is the same
# either way, so a single mechanism covers both.
_SEND_WORKERS = 8

# Ceiling on the request body, enforced at the edge before FastAPI
# buffers or parses it. Derived from the limits the handler already
# publishes so it can never sit below a legal batch: every item at the
# maximum token length, plus room for its JSON framing.
MAX_BODY_BYTES = MAX_BATCH * (MAX_TOKEN_LEN + 256) + 4096

# Sliding minute window, one per bucket: the source address, and the
# instance key when the relay has keys configured. In-memory on purpose:
# the relay is a single small container and a restart forgetting counters
# is harmless. The source address comes from the shared trusted-proxy
# walk, so behind the TLS proxy the relay must list that proxy in
# OIKONOME_TRUSTED_PROXIES exactly as an instance would.
RATE_LIMIT_PER_MIN = 120
MAX_TRACKED_IPS = 10_000

# One more window on an OPEN relay (no OIKONOME_RELAY_KEYS), counting every
# push whatever address it came from.
#
# Per-address buckets cannot bound an open relay on their own: an attacker
# spraying from N addresses simply gets N clean windows, and once the map is
# full the least-recently-seen eviction hands them a fresh bucket for an
# address they already spent — the limiter under-counts exactly the sender it
# was there to bound. Changing the eviction policy does not close that,
# because a distributed sprayer never needs an eviction; the only bound that
# holds without per-sender identity is a global one.
#
# A KEYED relay needs none of this: the key bucket already bounds each
# instance however many addresses it pushes from, and keys are not
# attacker-controlled. So this ceiling applies only to the deployment the
# module docstring already treats as lesser-trust, and it sits well above
# what the documented open case is — one self-hoster's own instance, whose
# pushes are a handful a day in batches of up to MAX_BATCH tokens each.
OPEN_RELAY_PER_MIN = 2 * RATE_LIMIT_PER_MIN
# Not an address, so it can never collide with one, and touched by every
# open-relay push, so LRU eviction can never drop it.
_OPEN_GLOBAL = "open:all"
_hits: collections.OrderedDict[str, collections.deque] = \
    collections.OrderedDict()
# Concurrent pushes really do run at the same time — the endpoint is a
# sync def, so Starlette dispatches each one to its own worker thread —
# and the window below is a check-then-act on shared state. Unlocked,
# two threads arriving for the same fresh bucket each build their own
# deque and the second assignment discards the first, so one thread's
# timestamps land in an orphan nobody counts: the limiter undercounts
# exactly when it is under the load it exists to bound.
_hits_lock = threading.Lock()


def _rate_limited(bucket: str, limit: int | None = None) -> bool:
    """One sliding minute window for one bucket — a source address,
    `key:<fingerprint>` for the instance key that signed the push, or the
    whole open relay at once. The ceiling is read here rather than bound as
    a default so the module constant stays the single knob."""
    limit = RATE_LIMIT_PER_MIN if limit is None else limit
    now = time.monotonic()
    with _hits_lock:
        q = _hits.get(bucket)
        if q is None:
            q = _hits[bucket] = collections.deque()
        else:
            # most recently seen goes to the end, so the front of the map is
            # always the bucket that has been quiet the longest
            _hits.move_to_end(bucket)
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        # Bound the map against an address spray by evicting the LEAST
        # RECENTLY SEEN entries. Clearing everything here would let a spray
        # of fresh addresses hand every busy sender a clean window — the cap
        # would become the reset button for the limiter it was protecting.
        while len(_hits) > MAX_TRACKED_IPS:
            _hits.popitem(last=False)
    return False


# Which instance key owns which device token. A key proves the caller is
# an opted-in instance; it does not prove the tokens in the batch are that
# instance's own users, and without this map any accepted key could push to
# any token it learned.
#
# Keyed by a DIGEST of the token, never the token: the relay's promise is
# that tokens are not logged and not kept, and a registry of raw tokens
# would be a copy of every phone it has ever pushed, sitting in the process
# that also holds the publisher's APNs and FCM credentials. A digest
# answers the only question asked of it — "is this the same token?" — and
# answers nothing else.
#
# Bounded and LRU-evicted like _hits, and for the same reason: unbounded
# growth driven by whatever senders choose to send. Eviction re-opens
# first-use claiming for the evicted token, so the ceiling is set far above
# any real population — a relay serving a large fleet of instances is
# nowhere near it, and a sender trying to reach it has to get past two rate
# limiters per minute first.
TOKEN_BINDING_TTL = 30 * 24 * 3600
MAX_TRACKED_TOKENS = 100_000
_owners: collections.OrderedDict[str, tuple[str, float]] = \
    collections.OrderedDict()
# The endpoint is a sync def, so Starlette runs concurrent pushes on
# separate worker threads: check-then-claim has to be one atomic move, or
# two instances racing for one unbound token both see it free and both
# claim it — which is precisely the theft this prevents.
_owners_lock = threading.Lock()


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _claim_tokens(tokens: list[str], identity: str) -> str | None:
    """Bind every token in a batch to `identity`, or refuse the batch.

    Returns None when the whole batch is this sender's to push — every
    token free, expired, or already bound to it — having claimed and
    refreshed them all. Otherwise returns the fingerprint that owns the
    first token that is NOT this sender's, and claims nothing: a batch is
    all-or-nothing here for the same reason the field validation above is,
    so a refusal can never leave half a batch re-registered.
    """
    now = time.monotonic()
    keys = [_token_key(t) for t in tokens]
    # A claim by a key the operator has since REMOVED from
    # OIKONOME_RELAY_KEYS binds nothing: that is how a key rotation
    # finishes. Rotate by adding the new key, switching the instance over,
    # then dropping the old one — the tokens are free again the moment it
    # leaves the list, and the instance re-registers them on its next
    # push. (While BOTH keys are configured the relay cannot tell a
    # rotation from a second instance, and the second instance is the
    # thing this exists to refuse, so the overlap window is the price.)
    live = {_fingerprint(k) for k in _keys()}
    with _owners_lock:
        for k in keys:
            held = _owners.get(k)
            if held is None or now - held[1] > TOKEN_BINDING_TTL:
                continue                       # unclaimed, or gone quiet
            if held[0] != identity and held[0] in live:
                return held[0]
        for k in keys:
            _owners[k] = (identity, now)
            _owners.move_to_end(k)
        # A full registry evicts the CLAIMANT's own oldest bindings, never
        # another key's: a key pushing garbage tokens at its batch and rate
        # limits could otherwise flush every other instance's bindings in
        # minutes and reopen first-use claiming for their tokens.
        if len(_owners) > MAX_TRACKED_TOKENS:
            mine = [k for k, v in _owners.items() if v[0] == identity]
            for k in mine[:len(_owners) - MAX_TRACKED_TOKENS]:
                _owners.pop(k, None)
        while len(_owners) > MAX_TRACKED_TOKENS:
            _owners.popitem(last=False)
    return None


# Where the edge gate leaves the caller's identity for the handler. A
# private scope key rather than request.state: nothing else writes it,
# and it survives whatever Starlette does with the scope's own state.
_SCOPE_KEY = "oikonome_relay_key"


class _EdgeGate:
    """Refuse what can be refused from the headers alone, before the body
    is read.

    Raw ASGI rather than BaseHTTPMiddleware for the reason the instance's
    body-size middleware gives: BaseHTTPMiddleware wraps the receive
    stream in a task that has already begun pulling the body, so counting
    there does not actually prevent the buffering.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        is_push = scope.get("path") == "/v1/push"
        request = Request(scope)
        identity = None
        if is_push:
            # Key first, before the size check and before the bucket is
            # spent: an unkeyed spray must not cost a legitimate instance
            # behind the same proxy address its window.
            identity = _key_identity(request)
            if identity is None:
                return await self._refuse(scope, send, 401,
                                          "instance key required")
            # left where the handler and the refusal log can both name the
            # sender without re-deriving it
            scope[_SCOPE_KEY] = identity
        limit = MAX_BODY_BYTES
        declared = None
        for k, v in scope.get("headers") or ():
            if k == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    declared = None
        if declared is not None and declared > limit:
            return await self._refuse(scope, send, 413,
                                      "request body too large")
        if is_push:
            # Two buckets, because they bound different things: the address
            # bucket bounds one sender, and then either the key bucket —
            # one instance however many addresses it pushes from — or, on
            # an open relay where there is no sender to attribute anything
            # to, the relay as a whole, which is the only bound a spray
            # from fresh addresses cannot walk around.
            second = ((_OPEN_GLOBAL, OPEN_RELAY_PER_MIN)
                      if identity == _OPEN else (f"key:{identity}", None))
            if (_rate_limited(client_ip(request))
                    or _rate_limited(*second)):
                return await self._refuse(scope, send, 429, "slow down")

        # A chunked body carries no content-length, so also count as it
        # streams: trusting the header alone would leave the hole open.
        seen = 0
        over = False

        async def counting_receive():
            nonlocal seen, over
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > limit:
                    over = True
                    # starve the app rather than hand it more bytes
                    return {"type": "http.disconnect"}
            return msg

        await self.app(scope, counting_receive, send)
        if over:
            log.warning("relay: body over %d bytes on %s — cut off",
                        limit, scope.get("path"))

    async def _refuse(self, scope, send, status: int, detail: str):
        log.warning("relay: %s on %s (%s)", status, scope.get("path"),
                    scope.get(_SCOPE_KEY) or "unidentified")
        body = ('{"detail":"%s"}' % detail).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length",
                                 str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(_EdgeGate)


@app.get("/healthz")
def healthz():
    # liveness only — which legs are configured is the operator's business,
    # not something to publish to whoever can reach the health check
    return {"ok": True}


def _one_line(text: str) -> str:
    """Collapse a display line to printable characters on one line: no
    newlines (a body that wraps into a second notification line is not
    the line the instance meant), no control characters."""
    return " ".join("".join(
        ch if ch.isprintable() else " " for ch in text).split())


@app.post("/v1/push")
def push(request: Request, body: dict = Body(...)):
    # The instance key, the source-address and per-key rate limits, and
    # the body ceiling were all settled by _EdgeGate before FastAPI read
    # a byte of `body` — by the time this runs, the caller is known.
    identity = request.scope.get(_SCOPE_KEY, _OPEN)
    items = body.get("notifications")
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "notifications: non-empty list required")
    if len(items) > MAX_BATCH:
        raise HTTPException(400, f"batch too large (max {MAX_BATCH})")

    # Validate the whole batch before sending anything, so a 400 always
    # means zero notifications delivered — never a half-sent batch the
    # instance will retry in full.
    checked = []
    for it in items:
        if not isinstance(it, dict):
            raise HTTPException(400, "each notification must be an object")
        token = it.get("token")
        platform = it.get("platform")
        event = it.get("event")
        badge = it.get("badge")
        if (not isinstance(token, str)
                or not 10 <= len(token) <= MAX_TOKEN_LEN):
            raise HTTPException(400, "bad token")
        if event not in EVENTS:
            raise HTTPException(400, "unknown event")
        if badge is not None and (not isinstance(badge, int)
                                  or not 0 <= badge <= 999):
            raise HTTPException(400, "bad badge")
        if platform not in ("fcm", "apns"):
            raise HTTPException(400, "platform must be fcm or apns")
        text = it.get("text")
        if text is not None:
            if event not in TEXT_EVENTS:
                raise HTTPException(
                    400, "text is only carried on the daily push")
            if not isinstance(text, str):
                raise HTTPException(400, "bad text")
            text = _one_line(text)
            if len(text) > MAX_TEXT_LEN:
                raise HTTPException(400, "text too long")
            text = text or None
        checked.append((token, platform, event, badge, text))

    # Whose tokens are these? A key gets the caller past the edge gate; it
    # does not make every token in the batch theirs to wake. Bound AFTER
    # the field validation, so a malformed batch cannot register anything,
    # and BEFORE a single send, so a refusal means zero notifications
    # delivered — the same all-or-nothing contract the validation keeps.
    #
    # Skipped on an open relay (no OIKONOME_RELAY_KEYS): there is no sender
    # identity there, so binding every token to the one pseudo-identity
    # would record a fact about nobody and refuse nothing.
    if identity != _OPEN:
        owner = _claim_tokens([c[0] for c in checked], identity)
        if owner is not None:
            # fingerprints only — the token is the thing being protected
            # and saying it here would put it in the operator's logs
            log.warning("relay: key=%s pushed a token registered to key=%s "
                        "— refused", identity, owner)
            raise HTTPException(
                403, "a token in this batch is registered to another "
                     "instance")

    sent, dead, unavailable = 0, [], set()
    batch = []
    for token, platform, event, badge, text in checked:
        if platform == "fcm":
            if not fcm.available():
                unavailable.add("fcm")
                continue
            batch.append((token, fcm.send, event, badge, text))
        else:
            if not _APNS_TOKEN.fullmatch(token):
                # cannot be a device token (see _APNS_TOKEN) — dead, and
                # it never gets near the wire
                dead.append(token)
                continue
            if not apns.available():
                unavailable.add("apns")
                continue
            batch.append((token, apns.send, event, badge, text))

    if batch:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(_SEND_WORKERS, len(batch))) as pool:
            futures = [pool.submit(leg, token, event, badge, text)
                       for token, leg, event, badge, text in batch]
            # tallies are updated here on the request thread only, so the
            # accounting needs no lock however the sends interleave
            for (token, _leg, _event, _badge, _text), fut in zip(batch, futures):
                try:
                    outcome = fut.result()
                except Exception:                  # noqa: BLE001
                    log.exception("push: send raised in the worker pool")
                    outcome = "error"
                if outcome == "ok":
                    sent += 1
                elif outcome == "dead":
                    dead.append(token)

    # Which KEY pushed, never which token. The fingerprint is what makes a
    # pattern attributable — a refusal above logs it too, so an instance
    # reaching for tokens that are not its own is visible by name.
    log.info("relay: push key=%s items=%d sent=%d dead=%d", identity,
             len(checked), sent, len(dead))
    return {"sent": sent, "dead": dead, "unavailable": sorted(unavailable)}
