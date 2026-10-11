"""/api/logo — merchant logos served from an instance-wide cache.

A merchant's logo is Plaid's public asset (plaid-merchant-logos /
plaid-counterparty-logos CDNs). Serving it through the instance means the
browser and the phone never fetch from plaid.com: nothing about which
merchants a household pays leaves a self-hosted box, and the CSP img-src
stays 'self'. The first request for a URL fetches and caches the bytes
(logo_cache, control plane — a logo is not tenant data); later requests
are a table read. Only the two Plaid logo hosts are proxied — this is a
logo cache, not an open fetch door.

A SESSION is required. That the URL names nothing but a merchant Plaid
publishes is true of the URL and beside the point for the endpoint: an
anonymous caller could otherwise make the instance fetch from Plaid's
CDN and grow a table nobody is watching, 600 times a minute per IP,
forever. The web `<img>` is same-origin so the
session cookie flows on its own; the mobile client sends its bearer
header. The rate limit stays on top of the session.
"""
from __future__ import annotations

import logging
import threading
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ..db import tenancy
from .security import limit

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _user():
    from .app import current_user
    return current_user

LOGO_HOSTS = {"plaid-merchant-logos.plaid.com", "plaid-counterparty-logos.plaid.com"}
_MAX_BYTES = 512 * 1024
_TIMEOUT = 6.0
# Cache bounds. The table is global and nothing else prunes it, so the
# writer prunes: newest-first, keep at most _CAP_ROWS rows and _CAP_BYTES
# of image data. BOTH bounds are needed and they bound different things —
# bytes bound the disk a real fill costs, rows bound a flood of MISS rows,
# which carry no bytes at all and so are invisible to a byte cap. A cache
# miss is what triggers pruning, and a miss already costs a network fetch,
# so the pruning query runs at most as often as we talk to Plaid.
_CAP_ROWS = 5000
_CAP_BYTES = 64 * 1024 * 1024
# a fetch that failed is remembered briefly (empty bytes) so a dead URL
# does not cost a round trip per row per page
_MISS_TYPE = "application/x-miss"
# Miss rows get their own, separate row cap. The cache is instance-wide,
# and misses are the cheap half: any signed-in caller can invent allowed
# .png URLs that will never resolve, and each one writes a fresh miss row.
# Ranking misses and real logos together in one prune would let _CAP_ROWS
# fresh misses push every real logo past the cap — one user emptying the
# whole instance's cache for the price of some URLs. Real rows cost a
# genuine PNG on Plaid's CDN each, so the two populations are pruned
# against their own caps and misses can never evict a real logo.
_CAP_MISS_ROWS = 2000
# The only content type we serve. The allowlist already requires a .png
# path, and accepting whatever Plaid's CDN labels the body means
# image/svg+xml — a scriptable document — could be stored and later
# served from our own origin. Anything else is treated as a miss.
_PNG = "image/png"


def allowed(url: str) -> bool:
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    return u.scheme == "https" and u.netloc in LOGO_HOSTS and u.path.endswith(".png") \
        and not u.query and len(url) < 400


def _fetch(url: str) -> tuple[str, bytes] | None:
    """The bytes behind an allowed logo URL, or None (treated as a miss).

    Streamed and measured as it arrives: reading the whole body and THEN
    checking its length means a hostile or broken upstream decides how
    much memory we spend."""
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as c:
            with c.stream("GET", url, headers={"Accept": "image/png"}) as r:
                ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                if r.status_code != 200 or ct != _PNG:
                    return None
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > _MAX_BYTES:
                        log.info("logo too large, skipped: %s", url)
                        return None
                return _PNG, bytes(buf)
    except httpx.HTTPError as e:
        log.info("logo fetch failed %s: %s", url, e)
        return None


def _prune(conn) -> None:
    """Trim the cache to its caps, oldest fetch first — misses and real
    logos each against their own cap, so a flood of miss rows can never
    evict real entries (see _CAP_MISS_ROWS)."""
    # expired misses first: the read path already treats a miss older than
    # a day as absent, so these rows are dead weight either way
    conn.execute(
        "DELETE FROM logo_cache WHERE content_type = %s "
        "AND fetched_at <= now() - interval '1 day'", (_MISS_TYPE,))
    conn.execute(
        """DELETE FROM logo_cache WHERE url IN (
               SELECT url FROM (
                   SELECT url, row_number() OVER w AS rn
                     FROM logo_cache
                    WHERE content_type = %s
                     WINDOW w AS (ORDER BY fetched_at DESC, url)) s
                WHERE s.rn > %s)""",
        (_MISS_TYPE, _CAP_MISS_ROWS))
    conn.execute(
        """DELETE FROM logo_cache WHERE url IN (
               SELECT url FROM (
                   SELECT url,
                          row_number() OVER w AS rn,
                          sum(length(bytes)) OVER w AS running
                     FROM logo_cache
                    WHERE content_type <> %s
                     WINDOW w AS (ORDER BY fetched_at DESC, url)) s
                WHERE s.rn > %s OR s.running > %s)""",
        (_MISS_TYPE, _CAP_ROWS, _CAP_BYTES))


# One in-flight fetch per URL. A ledger page asks for the same logo once
# per row, dozens at once; the first miss fetches and the rest wait for its
# answer instead of each going to the CDN. Entries are dropped when the
# fetch finishes, so the dict only ever holds what is in flight.
_inflight: dict[str, threading.Event] = {}
_inflight_lock = threading.Lock()
# How long a follower waits on the leader's fetch before trying itself;
# just past _fetch's own timeout, so a wedged leader cannot hold it forever.
_FOLLOW_WAIT = 8.0


def _read(url: str):
    conn = tenancy.control_connect()
    try:
        return conn.execute(
            "SELECT content_type, bytes FROM logo_cache WHERE url=%s "
            "AND (content_type <> %s OR fetched_at > now() - interval '1 day')",
            (url, _MISS_TYPE)).fetchone()
    finally:
        conn.close()


def _write(url: str, got: tuple[str, bytes] | None) -> None:
    conn = tenancy.control_connect()
    try:
        conn.execute(
            """INSERT INTO logo_cache (url, content_type, bytes, fetched_at)
               VALUES (%s,%s,%s,now())
               ON CONFLICT (url) DO UPDATE SET content_type=EXCLUDED.content_type,
                   bytes=EXCLUDED.bytes, fetched_at=now()""",
            (url, got[0] if got else _MISS_TYPE, got[1] if got else b""))
        _prune(conn)
    finally:
        conn.close()


def _answer(row) -> tuple[str, bytes] | None:
    return None if row["content_type"] == _MISS_TYPE else (row["content_type"], bytes(row["bytes"]))


def cached_logo(url: str) -> tuple[str, bytes] | None:
    """Bytes for a Plaid logo URL, from the cache or Plaid (then cached).
    None when the host is not allowed or the fetch failed."""
    if not allowed(url):
        return None
    # Pooled app-role connections: this runs once per ledger row on a page
    # load, and admin_connect opens a fresh unpooled superuser connection
    # per call. The app role's write grant on logo_cache is deliberate
    # (see migrate.py) — the table holds no tenant data and is capped.
    # The CDN fetch runs with NO connection held: the control pool is small
    # and every signed-in request draws its session check from it, so a few
    # slow fetches holding connections would stall the whole instance.
    row = _read(url)
    if row:
        return _answer(row)
    with _inflight_lock:
        waiting = _inflight.get(url)
        if waiting is None:
            _inflight[url] = done = threading.Event()
    if waiting is not None:
        waiting.wait(_FOLLOW_WAIT)
        row = _read(url)
        if row:
            return _answer(row)
        # the leader gave up or its write failed: fetch for ourselves,
        # without registering, so a stuck leader is not waited on twice
        got = _fetch(url)
        _write(url, got)
        return got
    try:
        got = _fetch(url)
        _write(url, got)
        return got
    finally:
        with _inflight_lock:
            _inflight.pop(url, None)
        done.set()


def _current_user(request: Request) -> dict:
    # late import: web/app.py imports this module at the bottom, so the
    # import has to happen per request rather than at decoration time
    from .app import current_user
    return current_user(request)


@router.get("/logo", dependencies=[Depends(limit("logo", 600, 60.0))])
def logo(u: str = Query(..., max_length=400),
         user: dict = Depends(_current_user)):
    if not allowed(u):
        raise HTTPException(400, "not a merchant logo URL")
    got = cached_logo(u)
    if not got:
        raise HTTPException(404, "no logo")
    ct, data = got
    # nosniff is also set globally by SecurityHeadersMiddleware; it is
    # stated here too because this route is the one that serves bytes
    # fetched from somewhere else.
    return Response(content=data, media_type=ct,
                    headers={"Cache-Control": "private, max-age=604800, immutable",
                             "X-Content-Type-Options": "nosniff"})


@router.get("/institutions/{item_id}/logo")
def institution_logo(item_id: str, user: dict = Depends(_user())):
    """The bank's own logo for a connected item, as bytes. Plaid hands the
    institution logo over as base64 once at link time; carrying it inside
    every /api/accounts payload would cost a few KB per institution on
    every load, so the payload carries a URL instead and the browser / the
    phone caches the image itself. Tenant-scoped through the RLS connection, so
    an item id from another household is simply not found."""
    import base64
    import hashlib
    from ..db import tenancy
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        row = conn.execute("SELECT logo FROM items WHERE id = %s",
                           (item_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["logo"]:
        raise HTTPException(404, "no logo for that institution")
    try:
        data = base64.b64decode(row["logo"], validate=False)
    except Exception:                                       # noqa: BLE001
        raise HTTPException(404, "no logo for that institution")
    etag = '"' + hashlib.sha1(data).hexdigest()[:20] + '"'
    return Response(data, media_type=_PNG,
                    headers={"Cache-Control": "private, max-age=86400",
                             "ETag": etag})

