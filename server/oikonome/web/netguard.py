"""SSRF guard for tenant-supplied URLs.

A tenant owner can point `llm_url` (smart categorization) and, via a base64
setup token, the SimpleFIN claim URL at an arbitrary server. On the HOSTED
multi-tenant platform that is an SSRF primitive: reach the cloud metadata
endpoint (169.254.169.254), internal Redis/Postgres, other tenants' pods.

Policy:
  * HOSTED (OIKONOME_HOSTED set): reject any URL whose host resolves to a
    private / loopback / link-local / reserved / multicast address, and any
    non-http(s) scheme. All resolved addresses must be public.
  * SELF-HOST: a local LLM on 127.0.0.1 / 192.168.x is a first-class use
    case (see quickstart), so private addresses are allowed — EXCEPT the
    cloud metadata IP, which no self-host install has any reason to hit.

Two layers:
  * check_url / check_host — validation-time (config save / token claim /
    use-time re-check). Strong first layer, but it validates a NAME.
  * pinned_transport — fetch-time DNS pinning. A rebinding
    resolver can answer a public address at check time and 169.254.169.254
    at connect time; on hosted, tenant-URL fetches therefore go through a
    transport that resolves once, applies the same address policy to what
    actually resolved, and connects to that exact IP (Host header and TLS
    SNI/verification keep the original hostname).
"""

from __future__ import annotations

import ipaddress
from ..envnum import env_flag
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTimeout
from urllib.parse import urlsplit

METADATA_IPS = {"169.254.169.254", "fd00:ec2::254"}

# DNS resolution guards two very different callers now: the fetch-time SSRF
# check (rare, one host, an outbound request is about to happen anyway) and
# the locality classifiers used by GET /api/settings (hot path, up to ~6
# tenant-configured hosts per request, on the shared sync threadpool). A
# bare getaddrinfo has no timeout, so a host whose nameserver simply never
# answers would pin a worker thread for the OS resolver's full retry window
# and, multiplied across tenants, starve every sync endpoint. So: a hard
# per-lookup deadline, and a short TTL cache (positive AND negative) so a
# burst for the same host pays the cost once. A per-host lock collapses a
# concurrent stampede onto a single in-flight lookup.
_DNS_TIMEOUT = 2.0
_DNS_TTL_OK = 300.0
_DNS_TTL_FAIL = 60.0
_DNS_CACHE: dict[str, tuple[float, list[str]]] = {}
_DNS_LOCKS: dict[str, threading.Lock] = {}
_DNS_LOCKS_GUARD = threading.Lock()
_DNS_POOL = ThreadPoolExecutor(max_workers=4,
                               thread_name_prefix="netguard-dns")


class BlockedURL(ValueError):
    """Raised when a URL is disallowed by policy."""


def _getaddrinfo(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})


def _resolved_ips(host: str) -> list[str]:
    """Resolve `host` to its IPs, bounded and cached. An empty list means
    "could not resolve" — every caller treats that as fail-closed (hosted
    refuses; the locality classifiers judge non-local, so the off-box SSN
    consent gate still fires). A timeout is the same as unresolvable: a
    non-answering nameserver must not be able to hold a request thread."""
    now = time.monotonic()
    hit = _DNS_CACHE.get(host)
    if hit is not None and hit[0] > now:
        return hit[1]
    with _DNS_LOCKS_GUARD:
        lock = _DNS_LOCKS.setdefault(host, threading.Lock())
    with lock:
        # A stampeded peer may have filled the cache while we waited.
        hit = _DNS_CACHE.get(host)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        try:
            ips = _DNS_POOL.submit(_getaddrinfo, host).result(
                timeout=_DNS_TIMEOUT)
            ttl = _DNS_TTL_OK
        except (socket.gaierror, _FTimeout, OSError):
            # unresolvable or too slow: on hosted, refuse (can't prove it's
            # public); on self-host, let the real request surface the error
            ips, ttl = [], _DNS_TTL_FAIL
        _DNS_CACHE[host] = (time.monotonic() + ttl, ips)
        return ips


def check_url(url: str, *, what: str = "URL") -> None:
    """Raise BlockedURL when `url` violates the SSRF policy. No-op on pass."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL(f"{what} must be an http(s) URL")
    check_host(parts.hostname, what=what)


def check_host(host: str, *, what: str = "host") -> None:
    """The same address policy for a bare hostname or IP literal — SMTP
    servers aren't URLs. Self-host keeps a LAN relay first-class (only the
    metadata endpoint is blocked); hosted requires public."""
    if not host:
        raise BlockedURL(f"{what} is required")
    hosted = env_flag("OIKONOME_HOSTED")

    # literal-IP hosts: check directly; hostnames: resolve first
    ips = []
    try:
        ips = [str(ipaddress.ip_address(host))]
    except ValueError:
        ips = _resolved_ips(host)
        if hosted and not ips:
            raise BlockedURL(f"{what} host does not resolve")

    for ip in ips:
        _check_ip(ip, what=what, hosted=hosted)


def _check_ip(ip: str, *, what: str, hosted: bool) -> None:
    """The single-address policy shared by check_host and the pinned
    transport."""
    addr = ipaddress.ip_address(ip)
    # An IPv4-mapped IPv6 address (::ffff:169.254.169.254) must be judged by
    # its embedded v4 form BEFORE any policy check, on self-host as much as
    # hosted: the kernel routes a v6 socket aimed at a mapped address to the
    # embedded v4 destination, so the mapped spelling of the metadata IP is
    # the metadata IP.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if str(addr) in METADATA_IPS:
        raise BlockedURL(
            f"{what} may not point at the cloud metadata endpoint")
    if not hosted:
        return                            # self-host: private LLM is allowed
    # Hosted invariant: the address MUST be globally routable. `not is_global`
    # subsumes the private/loopback/link-local/reserved/multicast/unspecified
    # set AND closes the ranges an enumerated allowlist silently misses —
    # notably RFC 6598 CGNAT 100.64.0.0/10, which is where a management
    # overlay network typically lives, plus 192.0.0.0/24,
    # 192.88.99.0/24, 198.18.0.0/15. Enumerating flags left all of these open
    # to tenant-set URLs (llm_url, SimpleFIN, SMTP) → SSRF into internal infra
    if not addr.is_global:
        raise BlockedURL(
            f"{what} must be a public address on the hosted platform "
            f"(got {ip})")


def pinned_smtp_host(host: str) -> str:
    """Connect-time DNS pinning for SMTP — the smtplib sibling of
    pinned_transport. check_host validates a NAME; smtplib then re-resolves
    it at connect, which is the rebind window the pinned transport closes
    for the httpx fetches. Hosted: resolve once, run every address through
    the hosted policy, return the vetted IP to connect to (the caller keeps
    TLS verification on the original hostname). Self-host: returns the host
    unchanged — LAN relays are first-class there and the rebind threat
    model is the hosted multi-tenant one."""
    if not env_flag("OIKONOME_HOSTED"):
        return host
    try:
        literal = str(ipaddress.ip_address(host))
    except ValueError:
        ips = _resolved_ips(host)
        if not ips:
            raise BlockedURL(f"{host} does not resolve")
        for ip in ips:
            _check_ip(ip, what=host, hosted=True)
        # prefer IPv4: small cloud hosts routinely lack a v6 route
        return next((i for i in ips if ":" not in i), ips[0])
    _check_ip(literal, what=host, hosted=True)
    return literal


def pinned_transport():
    """Fetch-time DNS pinning for tenant-supplied URLs.

    Hosted: returns an httpx transport that, per request, resolves the
    hostname ONCE, runs every resolved address through the hosted policy,
    and rewrites the URL to the vetted IP before connecting — so the
    address that passed the check is the address we talk to. The Host
    header was already built from the original URL, and TLS handshakes
    carry it via the sni_hostname extension (httpcore uses it as
    server_hostname, so certificate verification still checks the real
    name). Redirect hops, if a caller ever follows them, re-enter
    handle_request and get the same treatment.

    Self-host: returns None (httpx default transport) — private LLMs and
    SimpleFIN bridges are first-class there, and the rebind threat model
    is the hosted multi-tenant one.

    Callers pass it as `transport or pinned_transport()` so test
    MockTransports keep priority.
    """
    if not env_flag("OIKONOME_HOSTED"):
        return None
    import httpx

    class _Pinned(httpx.HTTPTransport):
        def handle_request(self, request):
            host = request.url.host
            try:
                literal = str(ipaddress.ip_address(host))
            except ValueError:
                ips = _resolved_ips(host)
                if not ips:
                    raise BlockedURL(f"{host} does not resolve")
                for ip in ips:
                    _check_ip(ip, what=host, hosted=True)
                # prefer IPv4: small cloud hosts routinely lack a v6 route
                pin = next((i for i in ips if ":" not in i), ips[0])
                if request.url.scheme == "https":
                    request.extensions["sni_hostname"] = host
                request.url = request.url.copy_with(host=pin)
            else:
                _check_ip(literal, what=host, hosted=True)
            return super().handle_request(request)

    return _Pinned()
