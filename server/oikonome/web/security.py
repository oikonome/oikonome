"""Request-security primitives: cross-origin write protection + rate
limiting.

CSRF stance: all auth is a SameSite=Lax cookie, so the remaining CSRF
vector is cross-site POSTs. Every modern browser attaches an Origin header
to cross-site POST/PUT/DELETE — the middleware rejects writes whose
Origin/Referer host differs from the request host. Non-browser clients
(curl, tests) send neither header and pass. This is the OWASP-recommended
origin-verification defense; per-form tokens can layer on later if a
threat model demands them.

Rate limiting: sliding window per (route, client IP).

 * Client IP — the socket peer by default. X-Forwarded-For is only
   honored when the peer is listed in OIKONOME_TRUSTED_PROXIES
   (comma-separated IPs/CIDRs): walk the chain right-to-left past
   trusted hops, take the first untrusted one. Unset (the default),
   the header is ignored entirely — an unproxied instance must never
   let a client pick its own bucket with a forged header.
 * Store — Redis (REDIS_URL, already in compose for arq) when
   available, so limits hold across workers/processes; the in-memory
   window is the fallback (single-process self-host, or Redis down —
   degraded beats locked-out).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import sys
import threading
import time
import typing
from collections import deque
from contextvars import ContextVar
from urllib.parse import urlsplit

import anyio
from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import compile_path

_WRITE = {"POST", "PUT", "PATCH", "DELETE"}

# Security event log — ONE stable single-line format so an operator can point
# fail2ban (or any log tail) at it. Fields are space-separated key=value; ip is
# the app's RESOLVED client IP (past any trusted proxy / Cloudflare), so a
# host-side jail bans the real offender even when every packet arrives from a
# CF edge address. Never carries a password, token, or email — ip + route only.
#
# The app's other logs land in an in-process ring buffer (feedback
# observability), whose root handler would otherwise swallow these before they
# reach container stdout — where an external fail2ban jail actually reads. So
# give this logger its OWN stdout handler; propagation stays on so events still
# show up in the observability ring too.
_seclog = logging.getLogger("oikonome.security")
if not any(getattr(h, "_oikonome_sec", False) for h in _seclog.handlers):
    _sec_h = logging.StreamHandler(sys.stdout)
    _sec_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _sec_h._oikonome_sec = True
    _seclog.addHandler(_sec_h)
    _seclog.setLevel(logging.WARNING)


# Several password-verifying doors (/api/sessions/revoke, /api/tokens/revoke,
# DELETE /api/users/{id}) must feed a failed guess into the ban / lockout
# signals, but the helper that checks the password has no `request`, and
# threading one through 13 call sites invites the next miss.
# The middleware already resolves the client IP for every request, so stash
# it here and let the shared step-up helper read it.
_req_ip: ContextVar[str] = ContextVar("oikonome_req_ip", default="")


def current_ip() -> str:
    """The resolved client IP for the request in flight ("" outside one)."""
    return _req_ip.get()


# These lines are PARSED BY fail2ban, which jails on
# `oikonome-security event=autoban ip=<HOST>` matched anywhere in the line.
# Several call sites pass request-controlled values — `path=` is the ASGI
# scope path, already percent-DECODED by the server — so a request to
# `/x event=autoban ip=8.8.8.8` would otherwise emit a line containing a
# second, forged record, letting an unauthenticated client pick which
# address the host's firewall bans, including the operator's own or a
# shared CDN egress. Log injection, and the log is a control plane.
#
# The separators ARE the grammar: space between fields, `=` between key and
# value. So a value may contain neither, and control characters (a raw
# newline would forge a whole line) are gone too. Everything outside the
# safe set collapses to `_`, which keeps the line readable and greppable
# while making a second record unrepresentable.
_SEC_UNSAFE = re.compile(r"[^A-Za-z0-9._:/@\[\]-]")
_SEC_MAXLEN = 120


def _sec_field(value) -> str:
    """One log field, rendered so it cannot become two."""
    out = _SEC_UNSAFE.sub("_", str(value))[:_SEC_MAXLEN]
    return out or "-"


def _sec_event(event: str, ip: str, **fields) -> None:
    parts = [f"event={_sec_field(event)}", f"ip={_sec_field(ip)}"]
    parts += [f"{_sec_field(k)}={_sec_field(v)}"
              for k, v in fields.items() if v is not None]
    _seclog.warning("oikonome-security " + " ".join(parts))


# A request BODY must be bounded. Starlette buffers the whole body to build
# `Body(...)`/`Form(...)` before a handler — or its auth dependency — runs,
# so a single POST of a multi-GB JSON string to an UNAUTHENTICATED route
# (/api/invite/claim, /login, /signup, /forgot, an add-on's intake form,
# passkey options) would OOM the shared container, taking every tenant down with it.
# Webhooks and file uploads refuse to read unbounded; the ~70 ordinary
# JSON/form routes need the same treatment.
#
# Enforced at the ASGI edge rather than per route: a per-route check runs too
# late — by then the bytes are already in memory, which is the whole problem.
BODY_LIMIT = 1 * 1024 * 1024          # ordinary JSON / form POSTs
UPLOAD_BODY_LIMIT = 64 * 1024 * 1024   # multipart: MAX_UPLOAD (50MB) + headroom
# The two doors that take a whole-ledger archive get a ceiling sized to
# pages.RESTORE_MAX_UPLOAD (500 MB) — ONLY those two. The ceiling is a byte
# counter at the edge, but the server spools a multipart body to disk
# before the handler runs, so a wide ceiling on every upload route would
# let each receipt door spool 500 MB per request.
RESTORE_BODY_LIMIT = 512 * 1024 * 1024
RESTORE_PATHS = frozenset({"/import", "/api/import"})


def _is_upload(content_type: str) -> bool:
    return (content_type or "").lower().startswith("multipart/form-data")


def _body_limit_for(content_type: str, *, upload_route: bool = True) -> int:
    """The ceiling for one request: multipart AT A ROUTE THAT TAKES A FILE
    gets the upload budget; everything else gets a form's worth.

    Content-type alone is wrong in the expensive direction:
    `multipart/form-data` parses at every Form(...) door too, so /login and
    /signup — reachable with no account, no cookie and no Origin header —
    would each carry a 64 MB ceiling for a body whose largest legitimate
    field is a password, and a slowloris could push 64 MB into the parser
    per connection.

    `upload_route` comes from the same derived matcher the ingest cap uses
    (`_accepts_upload`), so the two agree by construction and neither can
    rot into a hand-kept path list (one that misses a route also swallows
    that route's own size message with a generic 413).
    It defaults True, and `_accepts_upload` answers True whenever it cannot
    read the router: an unreadable route table must widen the ceiling back
    to what it always was, never narrow it, or one bad derivation silently
    breaks every upload in the product.
    """
    if not _is_upload(content_type):
        return BODY_LIMIT
    return UPLOAD_BODY_LIMIT if upload_route else BODY_LIMIT


# Which PATHS actually accept a file, derived from the app's own routes.
#
# Content-type alone is not enough to decide that a request is an upload:
# `multipart/form-data` is accepted by the form parser at every Form(...)
# route too, /login included. Charging those to the upload budget below let
# an anonymous client spend the whole instance's ingest capacity on a door
# that never stores a byte.
#
# The list is DERIVED, never hand-kept — walk the router, ask each endpoint
# whether any parameter is an UploadFile (directly, in a union, or inside a
# list) and compile the matching path templates. A hand-kept list of upload
# paths rots silently; a route added tomorrow is covered here the moment it is
# registered.
_UPLOAD_ROUTES_CACHE = "_oikonome_upload_route_matchers"


def _annotation_takes_upload(ann) -> bool:
    """True when an annotation mentions an UploadFile anywhere — plain,
    `UploadFile | None`, `list[UploadFile]`, or wrapped in Annotated."""
    if isinstance(ann, type) and issubclass(ann, StarletteUploadFile):
        return True
    return any(_annotation_takes_upload(a) for a in typing.get_args(ann))


def _endpoint_takes_upload(endpoint) -> bool:
    try:
        hints = typing.get_type_hints(endpoint)
    except Exception:
        # An unresolvable forward reference must not silently NARROW the
        # gate (that would hand an upload route back to the unmetered path),
        # so fall back to the raw annotations and match on the name.
        hints = dict(getattr(endpoint, "__annotations__", {}) or {})
    for ann in hints.values():
        if isinstance(ann, str):
            if "UploadFile" in ann:
                return True
        elif _annotation_takes_upload(ann):
            return True
    return False


def upload_route_paths(app) -> list[str]:
    """Every registered path template whose handler takes an UploadFile."""
    found: list[str] = []

    def walk(routes, prefix=""):
        for route in routes or ():
            # FastAPI 0.139 no longer copies an included router's routes into
            # the parent; it appends one lazy holder that keeps the original
            # and the prefix it was mounted under. Follow that, or every
            # /api route looks like it does not exist.
            included = getattr(route, "original_router", None)
            if included is not None:
                ctx = getattr(route, "include_context", None)
                walk(getattr(included, "routes", None),
                     prefix + (getattr(ctx, "prefix", "") or ""))
                continue
            sub = getattr(route, "routes", None)
            if sub:                       # a Mount / sub-application
                walk(sub, prefix + (getattr(route, "path", "") or ""))
                continue
            endpoint = getattr(route, "endpoint", None)
            path = getattr(route, "path", None)
            if endpoint is not None and path and _endpoint_takes_upload(
                    endpoint):
                found.append(prefix + path)

    walk(getattr(app, "routes", None))
    return found


def _upload_matchers(app):
    matchers = app.__dict__.get(_UPLOAD_ROUTES_CACHE)
    if matchers is None:
        matchers = [compile_path(p)[0] for p in upload_route_paths(app)]
        try:
            setattr(app, _UPLOAD_ROUTES_CACHE, matchers)
        except Exception:
            pass
    return matchers


def _accepts_upload(scope) -> bool:
    """Does this request's path belong to a route that takes a file?

    When no app is reachable from the scope (raw ASGI with no router), fall
    back to True: the gate is then exactly as wide as it used to be rather
    than silently off.
    """
    app = scope.get("app")
    if app is None:
        return True
    matchers = _upload_matchers(app)
    if not matchers:
        # A router shape we could not read would otherwise switch the whole
        # ingest cap off, silently and instance-wide — the loudest possible
        # failure of this derivation is the quietest possible bug. Fall back
        # to the content-type gate instead. (A test asserts the real app
        # never lands here.) The BODY CEILING reads the same answer, so the
        # fallback widens it back to the historical 64 MB rather than
        # capping every real upload at a megabyte: a broken derivation must
        # fail open in both budgets, not half open.
        return True
    path = scope.get("path") or ""
    return any(rx.match(path) for rx in matchers)


# How many multipart bodies one client may push, and how many the process will
# take in at once.
#
# The upload ROUTES already carry per-route budgets, but a route dependency is
# solved after FastAPI has called `await request.form()` — so every refused
# request has still been received and materialized (Starlette spools parts over
# 1 MB to disk and keeps the rest resident). A budget that only refuses AFTER
# the expensive half is not a budget on the expensive half. These bound the
# ingest itself, before a byte is parsed:
#
#  * the per-IP window is far above any single upload route's own budget, so it
#    can never be the thing that stops a person importing a folder — it exists
#    to stop a loop.
#  * the in-flight cap is what actually bounds RESIDENT memory, which a rate
#    limit cannot: sixty allowed requests arriving together are still sixty
#    bodies in RAM. The slot is held only while the body streams in, not for
#    the parse and the work that follow.
#  * the PER-IP share of that cap is what stops the in-flight cap from being a
#    denial-of-service primitive in its own right. A single global counter is
#    an instance-wide shared resource that any one client can exhaust, and
#    every tenant's uploads are refused while it does.
UPLOAD_INGEST_LIMIT = 60
UPLOAD_INGEST_WINDOW = 60.0
UPLOAD_INGEST_CONCURRENCY = 8    # process-wide ceiling on bodies arriving
UPLOAD_INGEST_PER_IP = 3         # …of which one client may hold this many

# A slot must never be held on the client's schedule. Two clocks, because one
# alone is wrong in a different direction each way:
#
#  * STALL is the load-bearing one — a slot is forfeited when no body bytes
#    arrive for this long. It measures PROGRESS rather than total elapsed
#    time, so a genuine 50 MB import over a slow uplink (minutes, but always
#    moving) is never punished, while a connection that has simply stopped
#    delivering is reclaimed in seconds. A single total deadline sized for
#    that same slow uplink would have to be many minutes long, which is
#    exactly how long an attacker would then get to hold the slot.
#  * DEADLINE is the backstop for the client that keeps the stall timer
#    happy by dribbling one byte at a time. Sized so the smallest honest
#    connection still fits: the 64 MB ceiling in five minutes is a floor of
#    about 1.7 Mbit/s, and a 5 MB receipt has the whole five minutes to
#    itself.
#
# Both answer 408 and both give the slot back; without them, eight sockets
# from one host were enough to refuse every upload on the instance.
UPLOAD_STALL_TIMEOUT = 20.0
UPLOAD_INGEST_DEADLINE = 300.0

_ingest_lock = threading.Lock()
_ingest_inflight = 0
_ingest_by_ip: dict[str, int] = {}


def _take_ingest_slot(ip: str = "") -> bool:
    global _ingest_inflight
    with _ingest_lock:
        if _ingest_inflight >= UPLOAD_INGEST_CONCURRENCY:
            return False
        if _ingest_by_ip.get(ip, 0) >= UPLOAD_INGEST_PER_IP:
            return False
        _ingest_inflight += 1
        _ingest_by_ip[ip] = _ingest_by_ip.get(ip, 0) + 1
        return True


def _free_ingest_slot(ip: str = "") -> None:
    global _ingest_inflight
    with _ingest_lock:
        _ingest_inflight = max(0, _ingest_inflight - 1)
        held = _ingest_by_ip.get(ip, 0) - 1
        if held > 0:
            _ingest_by_ip[ip] = held
        else:
            _ingest_by_ip.pop(ip, None)


def _reset_ingest_slots() -> None:
    """Drop every in-flight accounting entry (tests; never on a live path —
    the counters are reconciled by the middleware's own finally block)."""
    global _ingest_inflight
    with _ingest_lock:
        _ingest_inflight = 0
        _ingest_by_ip.clear()


class BodySizeLimitMiddleware:
    """Reject oversized and over-budget request bodies before they are read.

    Raw ASGI (not BaseHTTPMiddleware) on purpose: BaseHTTPMiddleware wraps
    the receive stream in an anyio task that has already begun pulling the
    body, so counting there does not reliably prevent the buffering.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD"):
            return await self.app(scope, receive, send)
        ctype = ""
        declared = None
        for k, v in scope.get("headers") or ():
            if k == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    declared = None
            elif k == b"content-type":
                ctype = v.decode("latin-1", "replace")
        # One question answers both budgets: does this path belong to a
        # route that actually takes a file? A multipart POST anywhere else
        # is just a form, so it gets a form's ceiling and spends none of
        # the ingest capacity.
        upload = _is_upload(ctype) and _accepts_upload(scope)
        limit = _body_limit_for(ctype, upload_route=upload)
        if upload and scope.get("path") in RESTORE_PATHS:
            limit = RESTORE_BODY_LIMIT
        if declared is not None and declared > limit:
            return await self._too_large(scope, send, limit)

        ip = ""
        if upload:
            ip = client_ip(Request(scope))
            if not _dual("check", ("upload_ingest", ip),
                         UPLOAD_INGEST_LIMIT, UPLOAD_INGEST_WINDOW):
                record_strike(ip, route="upload_ingest", event="ratelimited")
                return await self._refuse(
                    scope, send, 429,
                    "too many uploads — try again later", ip=ip,
                    event="upload_ingest_ratelimited")
            if not _take_ingest_slot(ip):
                return await self._refuse(
                    scope, send, 503,
                    "too many uploads in flight — try again in a moment",
                    ip=ip, event="upload_ingest_busy")

        # A chunked body has no content-length, so also count as it streams:
        # trusting the header alone would leave the actual hole open.
        seen = 0
        over = False
        held = upload
        deadline = time.monotonic() + UPLOAD_INGEST_DEADLINE
        answered = False      # we answered ourselves; drop the app's late reply
        started = False       # …unless the app got a response out first

        def _release():
            nonlocal held
            if held:
                held = False
                _free_ingest_slot(ip)

        async def guarded_send(msg):
            nonlocal started
            if answered:
                return
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        async def counting_receive():
            nonlocal seen, over, answered
            if held:
                # Only a HELD slot runs a clock. Once the body is in, the
                # slot is already back and the app may wait on receive() for
                # as long as its own work takes.
                budget = min(UPLOAD_STALL_TIMEOUT,
                             max(0.0, deadline - time.monotonic()))
                try:
                    with anyio.fail_after(budget):
                        msg = await receive()
                except TimeoutError:
                    _release()
                    if not started:
                        answered = True
                        await self._refuse(
                            scope, send, 408,
                            "upload timed out — the body stopped arriving",
                            ip=ip, event="upload_ingest_timeout")
                    # starve the app so it unwinds rather than waiting on a
                    # client we have already answered
                    return {"type": "http.disconnect"}
            else:
                msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > limit:
                    over = True
                    # starve the app rather than hand it more bytes
                    _release()
                    return {"type": "http.disconnect"}
                # the body is in — the slot bounds bytes ARRIVING, not the
                # parse and the work that follow, so hand it back here rather
                # than letting one slow analyze hold a door shut
                if not msg.get("more_body"):
                    _release()
            elif msg["type"] == "http.disconnect":
                _release()
            return msg

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            # We cut the body off after answering, so the form parser's
            # ClientDisconnect (FastAPI turns it into a 400) is our own doing,
            # not a fault to report. Anything else still propagates.
            if not answered:
                raise
        finally:
            _release()
        if over:
            _sec_event("body_too_large", "-", path=scope.get("path"),
                       limit=limit)

    async def _refuse(self, scope, send, status, detail, *, ip="-", event=""):
        """Turn a request away at the ASGI edge — before Starlette reads,
        spools or parses a single byte of its body."""
        if event:
            _sec_event(event, ip, path=scope.get("path"))
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length",
                                 str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def _too_large(self, scope, send, limit):
        _sec_event("body_too_large", "-", path=scope.get("path"), limit=limit)
        body = b'{"detail":"request body too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length",
                                 str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class OriginCheckMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method in _WRITE:
            origin = request.headers.get("origin") or ""
            referer = request.headers.get("referer") or ""
            claim = origin or referer
            if claim:
                host = urlsplit(claim).netloc
                # a present-but-opaque Origin (the literal "null" a
                # browser sends for sandboxed iframes / data: / file: /
                # some cross-origin redirects) has no netloc, so the old
                # `if host and …` let it through — an attacker-controlled
                # opaque context could then POST. A claim that IS present
                # but doesn't parse to THIS host is cross-origin, full
                # stop; only a genuinely absent claim (non-browser client)
                # stays allowed.
                if host != request.headers.get("host", ""):
                    from starlette.responses import JSONResponse
                    return JSONResponse({"detail": "cross-origin write blocked"},
                                        status_code=403)
        return await call_next(request)


# The in-process window store is keyed on (route, CLIENT IP) — a shape an
# untrusted client picks — and nothing ever removed a key. One entry per
# scanner IP therefore accumulated for the life of the process, in a process
# that also holds the SPA and the feedback ring buffer. Measured at roughly
# a kilobyte per key (a deque preallocates its first block), so an open
# /login taking internet background noise costs tens of megabytes that never
# come back. Same class as an unbounded body: a shared resource whose size an
# anonymous client decides.
#
# Eviction is EXACT rather than heuristic. Each window remembers the moment
# it is provably empty (its last hit plus that window), so dropping it past
# that moment loses nothing — a read at that point would have trimmed the
# deque to zero anyway. The pass is amortised over the only thing that grows
# the table, a NEW key, and its period scales with the table, so it costs
# O(1) per request. No timer: a background thread in a library module is a
# lifecycle nobody remembers to shut down, and it would run on idle
# instances that have nothing to sweep.
_SWEEP_MIN_NEW_KEYS = 256    # never walk the table more often than this
_MAX_KEYS = 20_000           # backstop: ~20 MB of buckets, not a budget

# Bucket namespaces (key[0]) that are a security CONTROL rather than a
# courtesy limit: forgetting one is the attacker's objective, not a fairness
# cost he pays. Tripping a route limiter costs a bot a minute; erasing any of
# these hands it the thing the limiter existed to deny. So they are the LAST
# windows a full table gives up — see _sweep: they run the shortest windows
# in the table, so a purely expiry-ordered shed would take them first.
#
# Everything else in `_hits` is a per-route or per-token courtesy budget
# (`(route, ip)` from `limit()`, "script-token", "upload_ingest",
# "sms-acct"); losing one of those early re-opens at most an hourly quota.
_PROTECTED_NS = frozenset({
    "login-fail",   # per-account second-factor lockout: dropping it lifts
                    # the 15-attempt cap that stops a distributed TOTP brute
    "strike",       # the auto-ban's evidence — clear it and no repeat
                    # offender ever reaches the ban threshold
    "banhist",      # ban-escalation history: without it every offence is a
                    # first offence and the ban never grows past 15 minutes
    "risk-ip",      # human-check risk signal. Clearing any of the three
    "risk-acct",    # buys an attacker un-challenged sign-in attempts, which
    "risk-site",    # is exactly what the site-wide bucket exists to stop.
})


class _Window(deque):
    """A sliding window that knows when it can no longer hold anything."""
    __slots__ = ("dead_at",)


class RateLimiter:
    def __init__(self):
        self._hits: dict[tuple, _Window] = {}
        self._blocks: dict[tuple, float] = {}   # key -> monotonic expiry
        self._lock = threading.Lock()
        self._new_keys = 0      # since the last sweep

    # ---- eviction (callers hold self._lock) --------------------------
    def _bucket(self, key: tuple, window_s: float, now: float) -> _Window:
        """The window for `key`, created if absent, kept alive for another
        `window_s` because the caller is about to write into it."""
        q = self._hits.get(key)
        if q is None:
            self._new_keys += 1
            self._sweep(now)
            q = self._hits[key] = _Window()
            q.dead_at = now + window_s
        else:
            # `max` because a key reached with a longer window must not be
            # forgotten on the shorter one's schedule. In practice a key
            # namespace carries one window, but the failure mode of getting
            # that wrong is a silently forgotten lockout, so don't rely on it
            q.dead_at = max(q.dead_at, now + window_s)
        return q

    def _sweep(self, now: float) -> None:
        """Drop every window that is provably empty; called only when a new
        key is about to be added, since that is the only growth."""
        if (self._new_keys < max(_SWEEP_MIN_NEW_KEYS, len(self._hits) // 4)
                and len(self._hits) < _MAX_KEYS):
            return
        self._new_keys = 0
        self._hits = {k: q for k, q in self._hits.items()
                      if q and q.dead_at > now}
        # expired hard-blocks are the same leak in miniature: `blocked()`
        # only pops one when someone asks about it, and a banned scanner
        # that never comes back is never asked about
        self._blocks = {k: exp for k, exp in self._blocks.items() if exp > now}
        if len(self._hits) < _MAX_KEYS:
            return
        # Every surviving key is live and the table is at its ceiling. What
        # gives way here is a security decision, not a memory one. Refusing
        # the new key's request would be a free denial of service — spray
        # fresh keys until the table is full and every other client's login
        # is refused — which is precisely what the limiter exists to
        # prevent. So the limiter FORGETS instead, and the direction of the
        # damage is a window that resets early rather than a door that
        # closes for everyone.
        #
        # WHAT it forgets is chosen by namespace first and by expiry only
        # within a namespace class. Ordering on `dead_at` alone reads like
        # "soonest to expire, so least missed", but `dead_at` is
        # now + window_s, so that order is really just WINDOW LENGTH — and
        # the security buckets have the SHORTEST windows in the table (a
        # 15-minute account lockout, a 10-minute strike count) while the
        # courtesy per-route limiters mostly run an hour. Ranked purely on
        # expiry, the lockout would go FIRST and /forgot's hourly
        # counter would outlive it: an attacker spraying long-window routes from
        # a few hundred addresses could reset a victim's second-factor
        # lockout to zero at will and brute-force past the 15-attempt cap.
        # Soonest natural expiry is not the same question as least
        # important, so the protected class is named rather than inferred.
        #
        # The exemption is "last", not "never". A ceiling that a protected
        # namespace can pin open is just the memory exhaustion moved to a
        # namespace an attacker can also fill — one strike bucket per source
        # IP — so once nothing routine is left, security buckets give way
        # too, still nearest-death first. Clearing the whole table instead
        # would turn the ceiling into the reset button for the limiter it
        # protects.
        by_death = sorted(self._hits, key=lambda k: self._hits[k].dead_at)
        quota = len(self._hits) - (_MAX_KEYS * 9 // 10)
        doomed = [k for k in by_death if k[0] not in _PROTECTED_NS][:quota]
        if len(doomed) < quota:
            doomed += [k for k in by_death
                       if k[0] in _PROTECTED_NS][:quota - len(doomed)]
        for k in doomed:
            del self._hits[k]

    def check(self, key: tuple, limit: int, window_s: float) -> bool:
        """True if the request is allowed; False when over the limit."""
        now = time.monotonic()
        with self._lock:
            q = self._bucket(key, window_s, now)
            while q and q[0] < now - window_s:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    # account-lockout primitives (count/add/clear a bucket without the
    # check()-couples-count-and-add behavior) — see account_locked below
    def count(self, key: tuple, window_s: float) -> int:
        """Read-only, and it stays that way: an unknown key is not created
        (a count must never be able to grow the table) and one that has
        drained is dropped rather than left behind as a permanent entry."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                return 0
            while q and q[0] < now - window_s:
                q.popleft()
            if not q:
                del self._hits[key]
                return 0
            return len(q)

    def add(self, key: tuple, window_s: float) -> bool:
        now = time.monotonic()
        with self._lock:
            self._bucket(key, window_s, now).append(now)
        return True

    def clear(self, key: tuple) -> None:
        with self._lock:
            self._hits.pop(key, None)

    # temporary hard-block primitives (auto-ban) — a marker with its own TTL,
    # independent of the sliding-window buckets above.
    def block(self, key: tuple, ttl_s: float) -> None:
        with self._lock:
            self._blocks[key] = time.monotonic() + ttl_s

    def block_if_absent(self, key: tuple, ttl_s: float) -> bool:
        """Set the block only when none is active; True means THIS caller
        made the unblocked→blocked transition. The check and the set share
        one lock hold, so concurrent callers agree on exactly one winner —
        which is what lets ban escalation count offenses instead of
        requests."""
        now = time.monotonic()
        with self._lock:
            exp = self._blocks.get(key)
            if exp is not None and exp > now:
                return False
            self._blocks[key] = now + ttl_s
            return True

    def blocked(self, key: tuple) -> float | None:
        """Seconds remaining on an active block, else None."""
        now = time.monotonic()
        with self._lock:
            exp = self._blocks.get(key)
            if exp is None:
                return None
            if exp <= now:
                self._blocks.pop(key, None)
                return None
            return exp - now

    def unblock(self, key: tuple) -> None:
        with self._lock:
            self._blocks.pop(key, None)


_limiter = RateLimiter()


def _trusted_proxies() -> list:
    """Parsed OIKONOME_TRUSTED_PROXIES (re-read each call: cheap, and
    tests toggle it). Bad entries are ignored rather than fatal."""
    nets = []
    for part in os.environ.get("OIKONOME_TRUSTED_PROXIES", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            pass
    return nets


def _is_trusted(ip: str, nets: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def client_ip(request: Request) -> str:
    """The real client address. Socket peer unless that peer is a trusted
    proxy — then the rightmost UNTRUSTED X-Forwarded-For hop (rightmost,
    because the client controls the left of the chain: a forged header
    arrives as `attacker-choice, real-client` after the proxy appends)."""
    peer = request.client.host if request.client else "?"
    nets = _trusted_proxies()
    if not nets or not _is_trusted(peer, nets):
        return peer
    hops = [h.strip() for h in
            request.headers.get("x-forwarded-for", "").split(",")
            if h.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, nets):
            return hop
    return peer


def request_scheme(request: Request) -> str:
    """Effective scheme for security-sensitive decisions (HSTS header,
    Secure cookie flag). X-Forwarded-Proto is only believed when the socket
    peer is a trusted proxy — the exact gate client_ip applies to
    X-Forwarded-For. Taken blind, any cleartext client could claim
    https and (a) pin an http-only LAN install under a year of HSTS or
    (b) earn a Secure cookie its own http connection then drops. Direct
    connections use the scheme the socket actually spoke."""
    peer = request.client.host if request.client else "?"
    nets = _trusted_proxies()
    if nets and _is_trusted(peer, nets):
        # standard proxies OVERWRITE this header (single value). If one
        # appended instead, the RIGHTMOST entry is the nearest trusted
        # hop's word — the left of a chain is client-forgeable, same
        # reasoning as client_ip's rightmost-untrusted walk
        fwd = request.headers.get("x-forwarded-proto", "")
        fwd = fwd.split(",")[-1].strip().lower()
        if fwd:
            return fwd
    return request.url.scheme


def forwarded_host(request: Request) -> str:
    """Effective host for building origins (WebAuthn rp_id/origin). Same
    trust gate as request_scheme: X-Forwarded-Host only counts when the
    socket peer is a trusted proxy, and only its RIGHTMOST entry (an
    appending proxy leaves client forgeries on the left). Everyone else
    gets the plain Host header — a direct client can rename itself but
    can't bind credentials to someone else's origin any more than it
    could by editing Host."""
    peer = request.client.host if request.client else "?"
    nets = _trusted_proxies()
    if nets and _is_trusted(peer, nets):
        fwd = request.headers.get("x-forwarded-host", "")
        fwd = fwd.split(",")[-1].strip()
        if fwd:
            return fwd
    return request.headers.get("host") or request.url.netloc


# Trim the window, count it, and record the hit only if there is room — as
# ONE server-side step. Split across two round trips (a read pipeline, then a
# write pipeline) every worker in a burst reads the same pre-write count,
# every one of them decides it is under the limit, and every one of them is
# admitted: the ceiling becomes "however many requests fit in the gap". That
# is the whole defense on /api/login/passkey and, through _account_limit, on
# the SMS caps that stop platform-billed toll fraud. Same reasoning as
# block_if_absent's SET NX — one operation, so the fleet agrees.
_WINDOW_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[5])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""


class RedisRateLimiter:
    """Same sliding window over a Redis sorted set, shared across
    workers. check() returns None when Redis is unreachable so the
    caller can fall back — a Redis blip must degrade, not lock out."""

    def __init__(self, url: str):
        import redis
        self._r = redis.Redis.from_url(url, socket_timeout=1,
                                       socket_connect_timeout=1)

    def check(self, key: tuple, limit: int, window_s: float) -> bool | None:
        import redis
        now = time.time()
        try:
            return bool(self._r.eval(
                _WINDOW_SCRIPT, 1, self._k(key), now - window_s, limit, now,
                int(window_s) + 1,
                f"{now:.6f}:{os.urandom(4).hex()}"))
        except redis.RedisError:
            return None

    def _k(self, key: tuple) -> str:
        return "oikonome:rl:" + ":".join(str(p) for p in key)

    def count(self, key: tuple, window_s: float) -> int | None:
        import redis
        now = time.time()
        try:
            pipe = self._r.pipeline()
            pipe.zremrangebyscore(self._k(key), 0, now - window_s)
            pipe.zcard(self._k(key))
            return pipe.execute()[1]
        except redis.RedisError:
            return None

    def add(self, key: tuple, window_s: float) -> bool | None:
        import redis
        now = time.time()
        try:
            pipe = self._r.pipeline()
            pipe.zadd(self._k(key), {f"{now:.6f}:{os.urandom(4).hex()}": now})
            pipe.expire(self._k(key), int(window_s) + 1)
            pipe.execute()
            return True
        except redis.RedisError:
            return None

    def clear(self, key: tuple) -> bool | None:
        import redis
        try:
            self._r.delete(self._k(key))
            return True
        except redis.RedisError:
            return None

    # hard-block marker with a native key TTL (own namespace so it can't
    # collide with the sliding-window sorted sets).
    def _bk(self, key: tuple) -> str:
        return "oikonome:ban:" + ":".join(str(p) for p in key)

    def block(self, key: tuple, ttl_s: float) -> bool | None:
        import redis
        try:
            self._r.set(self._bk(key), "1", ex=int(ttl_s) + 1)
            return True
        except redis.RedisError:
            return None

    def block_if_absent(self, key: tuple, ttl_s: float) -> bool | None:
        """SET NX: True when THIS caller made the unblocked→blocked
        transition, False when a block was already live, None on a Redis
        error (so _dual can fall back). Atomic in Redis, so every worker in
        the fleet agrees on one winner."""
        import redis
        try:
            return bool(self._r.set(self._bk(key), "1",
                                    ex=int(ttl_s) + 1, nx=True))
        except redis.RedisError:
            return None

    def blocked(self, key: tuple) -> float | None:
        """Seconds remaining on an active block, else None. Returns None on
        Redis error too — a store blip must never manufacture a block."""
        import redis
        try:
            ttl = self._r.ttl(self._bk(key))
            return float(ttl) if ttl and ttl > 0 else None
        except redis.RedisError:
            return None

    def unblock(self, key: tuple) -> bool | None:
        import redis
        try:
            self._r.delete(self._bk(key))
            return True
        except redis.RedisError:
            return None


_redis_limiter: RedisRateLimiter | None | bool = None   # None = not tried


def _shared_store() -> RedisRateLimiter | None:
    global _redis_limiter
    if _redis_limiter is None:
        url = os.environ.get("REDIS_URL", "")
        _redis_limiter = RedisRateLimiter(url) if url else False
    return _redis_limiter or None


def _dual(op: str, *a, **kw):
    """Run a store op on the Redis-shared limiter, falling back to the
    in-process one when Redis is absent OR answers None (down/errored).

    Without it, eleven call sites hand-roll this try-then-fall-back, each a
    little differently — `if store else None` here, `if store is None or
    store.add(...) is None` there. The failure mode of getting one wrong is
    silent and inverted: the operation quietly no-ops when Redis blips,
    which means a rate limit or a ban stops applying at exactly the moment
    the box is under the load that made Redis blip.

    Reads (`check`, `count`, `blocked`) and writes (`add`) all share this
    shape. `clear`/`unblock` deliberately do NOT — those must reach BOTH
    stores, not fall back to one (see _dual_clear).
    """
    store = _shared_store()
    if store is not None:
        r = getattr(store, op)(*a, **kw)
        if r is not None:
            return r
    return getattr(_limiter, op)(*a, **kw)


def _dual_clear(op: str, *a, **kw) -> None:
    """Clear/unblock in BOTH stores. Not a fallback: a key left behind in
    the in-process store would keep a lifted ban or a cleared failure count
    alive on that one worker."""
    store = _shared_store()
    if store is not None:
        getattr(store, op)(*a, **kw)
    getattr(_limiter, op)(*a, **kw)


def _account_limit(key: tuple, limit_n: int, window_s: float) -> bool:
    """Sliding-window check on an arbitrary key (e.g. an account id or a
    hashed destination), source-IP-independent — for throttles that IP
    rotation must not defeat (SMS toll-fraud guard). Redis-shared when
    available, in-process otherwise. Returns True when allowed."""
    return _dual("check", key, limit_n, window_s)


def limit(route: str, limit_n: int, window_s: float):
    """FastAPI dependency: per-client-IP sliding-window rate limit."""
    def dep(request: Request):
        ip = client_ip(request)
        key = (route, ip)
        if not _dual("check", key, limit_n, window_s):
            # tripping a limit is a strike toward an auto-ban (a bot hammering
            # ANY throttled route accrues them); a single human never will.
            record_strike(ip, route=route, event="ratelimited")
            raise HTTPException(429, "too many requests — try again later")
    return dep


# ---- auto-ban: repeat offenders get a short, self-expiring hard block -------
# A client IP that keeps tripping rate limits (or failing auth) is almost
# always a bot or scanner. Once it piles up strikes we reject it FAST at the
# middleware, before any route or DB work — that sheds load on a small box and
# is the topology-independent complement to a Cloudflare edge rule: the ban
# keys on the app's RESOLVED client IP, so it works behind Cloudflare and for
# self-hosters with no edge at all. Deliberately conservative — only repeated
# abuse trips it, the ban is short and escalates only for persistent offenders,
# and it only ever targets GLOBAL (public) source IPs, so loopback, LAN, and
# the test client are never banned.
_STRIKE_WINDOW = 600          # count strikes over 10 minutes
_STRIKE_MAX = 20              # strikes in that window → a ban
_BAN_BASE = 900              # first ban: 15 minutes
_BAN_MAX = 86400             # escalation cap: 24 hours
_BAN_HISTORY_WINDOW = 86400   # remember prior bans for 24h (escalation)


def autoban_enabled() -> bool:
    # on by default; an operator can disable with OIKONOME_AUTOBAN=0
    return os.environ.get("OIKONOME_AUTOBAN", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _bannable(ip: str) -> bool:
    """Only public IPs are eligible — never loopback/LAN/link-local, and never
    an unparseable host like the test client's 'testclient' or a bare '?'."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # a proxy/socket path can hand us the IPv4-mapped IPv6 spelling of a
    # public v4 client (::ffff:203.0.113.9); judge the embedded v4 form or
    # a repeat offender in that spelling is never classified bannable
    mapped = getattr(addr, "ipv4_mapped", None)
    return (mapped or addr).is_global


def blocked_seconds(ip: str) -> float | None:
    """Remaining auto-ban time for this resolved client IP, else None."""
    if not _bannable(ip):
        return None
    return _dual("blocked", ("ip", ip))


def record_strike(ip: str, route: str | None = None,
                   event: str = "strike") -> None:
    """Add one strike for this IP; ban it once strikes cross the threshold.
    Safe to call on any suspicious event. `event` labels the log line —
    "ratelimited" (a 429), "auth_fail" (a bad login), etc."""
    if not autoban_enabled() or not _bannable(ip):
        return
    _sec_event(event, ip, route=route)
    skey = ("strike", ip)
    store = _shared_store()
    # record the strike, then read the running count from the same store
    if store is not None and store.add(skey, _STRIKE_WINDOW) is not None:
        n = store.count(skey, _STRIKE_WINDOW)
    else:
        _limiter.add(skey, _STRIKE_WINDOW)
        n = _limiter.count(skey, _STRIKE_WINDOW)
    if n is None or n < _STRIKE_MAX:
        return
    # threshold crossed → impose an escalating ban and reset the strike
    # bucket so the next window starts clean. A burst from one IP can land
    # here many times for ONE crossing — every request that read the count
    # at or past the threshold before any of them cleared the bucket — so
    # only the caller that actually flips the IP from unbanned to banned
    # records a ban in the history and escalates. Counting each such caller
    # as a prior ban turned a first offense's 15 minutes into hours, which
    # for CGNAT users sharing the address is collateral damage, not
    # deterrence.
    bkey = ("ip", ip)
    if not _dual("block_if_absent", bkey, _BAN_BASE):
        (store or _limiter).clear(skey)
        return
    hkey = ("banhist", ip)
    if store is not None and store.add(hkey, _BAN_HISTORY_WINDOW) is not None:
        prior = store.count(hkey, _BAN_HISTORY_WINDOW) or 1
    else:
        _limiter.add(hkey, _BAN_HISTORY_WINDOW)
        prior = _limiter.count(hkey, _BAN_HISTORY_WINDOW)
    ttl = min(_BAN_MAX, _BAN_BASE * (2 ** (max(prior, 1) - 1)))
    if ttl > _BAN_BASE:
        # this caller owns the block it just set, so lengthening it to the
        # escalated ttl overwrites nobody else's ban
        _dual("block", bkey, ttl)
    (store or _limiter).clear(skey)
    _sec_event("autoban", ip, ban_seconds=int(ttl), prior_bans=prior, route=route)


def autoban_clear(ip: str) -> None:
    """Operator override — lift a ban and wipe the strike/history buckets."""
    _dual_clear("unblock", ("ip", ip))
    for k in (("strike", ip), ("banhist", ip)):
        _dual_clear("clear", k)


class AutoBanMiddleware(BaseHTTPMiddleware):
    """Reject a currently-banned IP immediately, before routing/DB work."""
    async def dispatch(self, request, call_next):
        _req_ip.set(client_ip(request))
        if autoban_enabled():
            ip = client_ip(request)
            # blocked_seconds hits Redis synchronously; run it off the event
            # loop so a slow/blipping store (socket_timeout up to 1s) can't
            # stall every concurrent request on this worker.
            import asyncio
            rem = await asyncio.to_thread(blocked_seconds, ip)
            if rem:
                from starlette.responses import JSONResponse
                return JSONResponse(
                    {"detail": "temporarily blocked after repeated failed "
                               "requests — try again later"},
                    status_code=429,
                    headers={"Retry-After": str(int(rem) + 1)})
        return await call_next(request)


# ---- account-scoped login lockout ------------------------------------------
# The per-IP `limit` can't stop an attacker who already holds the victim's
# password and sprays TOTP guesses at ONE account from many source IPs
# (each IP its own bucket). This counts SECOND-FACTOR failures per account
# (email, source-IP-independent) and locks that account's login for a
# cooldown once they pile up — so the 6-digit second factor can't be
# distributed-bruted.
#
# CRITICAL: callers must feed ONLY post-password failures
# here (a wrong TOTP after a correct password). A wrong PASSWORD must never
# increment this bucket — otherwise anyone who knows the victim's email
# could lock them out (pre-auth DoS). Wrong passwords are covered by the
# per-IP limit + argon2 cost. Cleared on a fully successful sign-in.
_ACCT_FAIL_MAX = 15          # failures per account before lockout
_ACCT_FAIL_WINDOW = 900      # 15-minute sliding window / cooldown


def _acct_key(email: str) -> tuple:
    return ("login-fail", (email or "").strip().lower())


def account_login_locked(email: str) -> bool:
    key = _acct_key(email)
    return _dual("count", key, _ACCT_FAIL_WINDOW) >= _ACCT_FAIL_MAX


def record_login_failure(email: str) -> None:
    key = _acct_key(email)
    _dual("add", key, _ACCT_FAIL_WINDOW)


def clear_login_failures(email: str) -> None:
    key = _acct_key(email)
    _dual_clear("clear", key)


# ---- risk signal for the human check (risk-based gating) -----------
# Turnstile only earns its keep against something trying passwords it does not
# have. Somebody signing into their own account settles that question by
# getting the password right the first time — so the challenge is ENFORCED
# only once this source IP, or this account, has recent failures behind it.
# That is what keeps an ordinary sign-in from depending on whether a
# third-party script loaded: a lockout caused by a blocked challenge
# script can only reach someone who is already failing to sign in.
#
# Two deliberate differences from the buckets above:
#
#   * Unlike `record_login_failure`, this one DOES count wrong passwords. The
#     rule there — a wrong password must never touch an account-scoped bucket —
#     exists because its consequence is a LOCKOUT, and anyone who knew your
#     address could inflict it. The consequence here is a checkbox, so the
#     same reasoning does not carry. The residue is real but small: someone
#     who knows your address can spend `_RISK_ACCT_MAX` failures to make your
#     next sign-in show a challenge. Without the account bucket, a credential
#     stuffer spread across a botnet gets `_RISK_IP_MAX` free guesses per
#     source IP at one victim and never meets the challenge at all.
#   * Unlike `record_strike`, it counts for every client (LAN, loopback, the
#     test client) and ignores OIKONOME_AUTOBAN — an operator turning off
#     auto-bans must not silently turn off the human check too.
# The third bucket is the one that keeps the feature worth having. A stuffing
# run spread across a botnet AND across many accounts fills neither of the
# first two — every source IP is fresh, every account is hit once — which is
# precisely the attack Turnstile was added for. So the instance keeps a
# whole-site failure count: cross it and EVERY sign-in is challenged until the
# burst passes. Under attack, security wins and the blocked-script lockout is
# back on the table; the rest of the time nobody pays for it. A successful
# sign-in does NOT clear this one — one legitimate user must not disarm the
# instance. Tune _RISK_SITE_MAX up as the tenant count grows: it wants to sit
# well above the failed sign-ins a normal day produces. Deliberately not an
# env knob — it is the one bucket that challenges people who did nothing
# wrong, so moving it should be a reviewed change with a test, not a config
# edit on a production box at 2am.
_RISK_WINDOW = 900          # 15 minutes, matching the account-lock window
_RISK_IP_MAX = 3            # failures from one IP before it must prove humanity
_RISK_ACCT_MAX = 5          # failures against one account before ditto
_RISK_SITE_MAX = 50         # failures across the whole instance → challenge all
_RISK_SITE_KEY = ("risk-site",)


def _risk_keys(ip: str, email: str = "") -> list[tuple]:
    """The buckets a single attempt touches. The site-wide one is always in."""
    keys = [(_RISK_SITE_KEY, _RISK_SITE_MAX)]
    if ip:
        keys.append((("risk-ip", ip), _RISK_IP_MAX))
    if email:
        keys.append((("risk-acct", email.strip().lower()), _RISK_ACCT_MAX))
    return keys


def _risk_count(key: tuple) -> int:
    return _dual("count", key, _RISK_WINDOW) or 0


def record_challenge_risk(ip: str, email: str = "") -> None:
    """Mark one failed sign-in against this IP and this account."""
    for key, _max in _risk_keys(ip, email):
        _dual("add", key, _RISK_WINDOW)


def challenge_warranted(ip: str, email: str = "") -> bool:
    """True when this sign-in has to prove it is a human.

    `email` is optional: the login PAGE is rendered before we know who is
    signing in, so it asks on the IP alone.
    """
    return any(_risk_count(key) >= _max for key, _max in _risk_keys(ip, email))


def clear_challenge_risk(ip: str, email: str = "") -> None:
    """A successful sign-in settles it for THAT ACCOUNT — and only that account.

    It deliberately does not clear the per-IP or the site-wide bucket.

    Clearing the IP bucket here would launder the whole per-IP gate: anyone
    holding one valid credential (their own account) alternates two guesses
    at a victim with one successful self-login, and the IP counter is wiped
    before it ever reaches its threshold. The per-IP signal then never fires,
    leaving only the site-wide bucket — which slow-rate stuffing sits under
    comfortably.

    The cost of keeping it is small and lands on the right person: someone
    who genuinely failed `_RISK_IP_MAX` times in the window sees a checkbox
    on their next attempt even after getting in. They have just proved they
    have a working browser and the right password, so it is solvable — which
    is the whole distinction this feature is built on.
    """
    for key, _max in _risk_keys("", email):     # account bucket only
        if key == _RISK_SITE_KEY:
            continue
        _dual_clear("clear", key)


def record_auth_failure(ip: str, email: str = "", route: str = "login") -> None:
    """One failed sign-in: an auto-ban strike (per-IP) and a human-check risk
    mark (per-IP and per-account). One call so the two can never drift."""
    record_strike(ip, route=route, event="auth_fail")
    record_challenge_risk(ip, email)
