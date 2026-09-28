"""Verify Cloudflare Access AT THE ORIGIN.

Putting a Cloudflare Access policy in front of `/admin*` means an
unauthenticated probe never reaches the app at all — but an edge policy is
worth nothing if the origin is reachable directly (a leaked origin IP, a
private-network address, a misconfigured firewall). So when the operator says
Access is in front, the app checks the assertion Cloudflare mints and
refuses anything else.

Enabled only when BOTH OIKONOME_CF_ACCESS_TEAM and OIKONOME_CF_ACCESS_AUD
are set; otherwise this module is inert and self-host behaviour is
unchanged.

Deliberately no new dependency: an Access token is a plain RS256 JWT and
`cryptography` is already a hard dependency (webauthn needs it), so the
verify is ~40 lines rather than a supply-chain decision.

Fails CLOSED — including when the JWKS cannot be fetched. A Cloudflare
outage therefore locks the console until the operator unsets the env vars
and restarts; that is the correct trade for a door onto the root
host-agent, and the keys are cached for an hour so a blip does not bite.
"""

from __future__ import annotations

import base64
import json
import os
import time

JWKS_TTL = 3600.0          # Cloudflare rotates keys ~every 6 weeks
LEEWAY = 60.0              # clock skew allowance, seconds

# domain -> (fetched_at, keys)
_JWKS_CACHE: dict[str, tuple[float, list[dict]]] = {}


def team_domain() -> str:
    """`OIKONOME_CF_ACCESS_TEAM` as a full hostname. Accepts "acme",
    "acme.cloudflareaccess.com" or the full URL — operators paste all
    three."""
    raw = (os.environ.get("OIKONOME_CF_ACCESS_TEAM") or "").strip()
    if not raw:
        return ""
    if "//" in raw:
        from urllib.parse import urlsplit
        raw = urlsplit(raw).hostname or ""
    raw = raw.strip("/").lower()
    if not raw:
        return ""
    return raw if "." in raw else f"{raw}.cloudflareaccess.com"


def audience() -> str:
    return (os.environ.get("OIKONOME_CF_ACCESS_AUD") or "").strip()


def enabled() -> bool:
    return bool(team_domain() and audience())


def _b64u_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _jwks(domain: str) -> list[dict]:
    hit = _JWKS_CACHE.get(domain)
    if hit and time.time() - hit[0] < JWKS_TTL:
        return hit[1]
    import httpx
    r = httpx.get(f"https://{domain}/cdn-cgi/access/certs", timeout=5.0)
    if r.status_code != 200:
        raise ValueError(f"JWKS fetch failed ({r.status_code})")
    keys = r.json().get("keys") or []
    if not keys:
        raise ValueError("JWKS carried no keys")
    _JWKS_CACHE[domain] = (time.time(), keys)
    return keys


def _rsa_key(jwk: dict):
    from cryptography.hazmat.primitives.asymmetric import rsa
    n = int.from_bytes(_b64u_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64u_decode(jwk["e"]), "big")
    return rsa.RSAPublicNumbers(e, n).public_key()


def verify(token: str) -> dict:
    """Return the token's claims, or raise ValueError.

    Checks, in order: shape, RS256, a JWKS key whose signature covers the
    signing input, issuer, audience, and expiry/not-before.
    """
    if not token:
        raise ValueError("no Access assertion")
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed Access assertion")
    try:
        header = json.loads(_b64u_decode(parts[0]))
        claims = json.loads(_b64u_decode(parts[1]))
        signature = _b64u_decode(parts[2])
    except Exception:                                        # noqa: BLE001
        raise ValueError("undecodable Access assertion")
    if header.get("alg") != "RS256":
        # never trust the token's own algorithm choice beyond the one we
        # accept — "alg": "none" and HS256-with-the-public-key are the
        # classic JWT forgeries
        raise ValueError("unexpected Access assertion algorithm")

    domain = team_domain()
    signed = f"{parts[0]}.{parts[1]}".encode()
    kid = header.get("kid")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ok = False
    for jwk in _jwks(domain):
        if kid and jwk.get("kid") and jwk["kid"] != kid:
            continue
        try:
            _rsa_key(jwk).verify(signature, signed, padding.PKCS1v15(),
                                 hashes.SHA256())
            ok = True
            break
        except (InvalidSignature, KeyError, ValueError):
            continue
    if not ok:
        raise ValueError("Access assertion signature did not verify")

    if claims.get("iss") != f"https://{domain}":
        raise ValueError("Access assertion issuer mismatch")
    aud = claims.get("aud")
    aud = aud if isinstance(aud, list) else [aud]
    if audience() not in [a for a in aud if isinstance(a, str)]:
        raise ValueError("Access assertion audience mismatch")
    now = time.time()
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or now > exp + LEEWAY:
        raise ValueError("Access assertion expired")
    nbf = claims.get("nbf") or claims.get("iat")
    if isinstance(nbf, (int, float)) and now + LEEWAY < nbf:
        raise ValueError("Access assertion not yet valid")
    return claims


def identity(request) -> str:
    """The Access identity for the audit trail — email when Cloudflare
    supplies one (service tokens carry `common_name` instead). Empty when
    Access is not in front."""
    if not enabled():
        return ""
    try:
        c = verify(_token(request))
    except ValueError:
        return ""
    return str(c.get("email") or c.get("common_name") or c.get("sub") or "")


def _token(request) -> str:
    """Cloudflare sends the assertion as a header AND a cookie; the header
    is canonical, the cookie is what a browser navigation carries."""
    return (request.headers.get("cf-access-jwt-assertion")
            or request.cookies.get("CF_Authorization") or "")


def check(request) -> None:
    """Raise ValueError unless this request carries a good assertion.
    No-op when Access is not configured."""
    if not enabled():
        return
    verify(_token(request))
