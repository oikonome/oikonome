"""Cloudflare Turnstile — a human check on the public auth doors.

Turnstile is a CAPTCHA replacement: the browser runs a silent challenge and
only shows a checkbox when something looks off. It answers a question the
rate limiters cannot — *is this a human?* — where `security.limit` and the
auto-ban only answer *how fast is this one address knocking?*. A credential-
stuffing run spread thin across a botnet stays under every rate limit; the
challenge is what it actually has to solve.

**Off unless configured.** Both keys must be present, so a self-hosted
instance never sees it and never needs a Cloudflare account. Setting both
turns it on; nothing else changes.

**Fail open on silence, closed on a verdict.** A transport error or timeout
talking to Cloudflare ALLOWS the request: `/login` is the door to someone's
money, and a third-party outage must not lock every user out of their own
finances. An actual `success: false` — a forged, replayed, or absent token —
is enforced. The other four defenses (per-IP limit, account lockout, auto-ban,
breached-password check) all still stand during an outage, so failing open
here degrades one layer rather than the whole door.

**Tokens are single-use.** Two-step sign-in posts to `/api/login` twice
(password, then code), so the page must reset the widget between them and
hand up a fresh token; `pages.js` does that on every response. The no-JS
server-rendered form submits once and needs no such care.

**Sign-in is RISK-GATED; signup is not.** `verify_login` enforces the token
only once the source IP or the target account has recent failed attempts
(`security.challenge_warranted`). A first, correct sign-in never needs a
token — which is the whole point: a user whose browser blocks the Cloudflare
script gets no widget to click, so making every login depend on it turns an
extension or a DNS filter into a lockout with no in-app way out. Signup has
no such history to read — every request is from a stranger, and the cost of a
bot getting through is a whole fake tenant — so it is challenged every time.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("oikonome.turnstile")

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# The form field Cloudflare's widget injects. Hyphenated, so every FastAPI
# handler reads it through Form(alias=FIELD).
FIELD = "cf-turnstile-response"

_TIMEOUT = 5.0


def site_key() -> str:
    """Public key, safe to render. Empty when unconfigured."""
    return (os.environ.get("OIKONOME_TURNSTILE_SITE_KEY") or "").strip()


def _secret() -> str:
    return (os.environ.get("OIKONOME_TURNSTILE_SECRET") or "").strip()


def enabled() -> bool:
    """Both halves configured. Empty == unset: compose materializes every
    declared key, so an unconfigured widget arrives as "" (see the same
    note in spamdefense._flag)."""
    return bool(site_key() and _secret())


def render_key() -> str:
    """The site key to hand a template — empty unless BOTH halves are set.

    Gate the widget on `site_key()` while the CSP opens on `enabled()` and a
    half-configured instance (site key in, secret not yet) renders a widget
    whose script and iframe the CSP then blocks. A widget with no secret
    verifies nothing anyway, so there is nothing to render."""
    return site_key() if enabled() else ""


def verify(token: str, ip: str = "") -> bool:
    """True when the request may proceed.

    Returns True when Turnstile is not configured (the feature is off), and
    True when Cloudflare cannot be reached (fail open — see module docstring).
    Returns False only for a token Cloudflare actively rejects, or no token
    at all while the feature is on.
    """
    if not enabled():
        return True
    if not token:
        # The widget is on the page but the field came back empty: a script
        # posting straight at the endpoint, or a human who submitted before
        # the challenge finished. Both get the same answer.
        return False
    data = {"secret": _secret(), "response": token}
    if ip:
        data["remoteip"] = ip
    try:
        r = httpx.post(VERIFY_URL, data=data, timeout=_TIMEOUT)
        body = r.json()
    except Exception:                            # noqa: BLE001 — fail open
        log.warning("turnstile siteverify unreachable; allowing the request",
                    exc_info=True)
        return True
    if body.get("success"):
        return True
    log.info("turnstile rejected a submission: %s",
             ",".join(body.get("error-codes") or []) or "no error code")
    return False


def required_for_login(ip: str, email: str = "") -> bool:
    """Would a sign-in from here have to pass the human check right now?

    False whenever the feature is off, so an unconfigured instance never
    asks for a token. Rendering `/login` calls this to tell the page whether
    it must wait for a token before posting.
    """
    if not enabled():
        return False
    from . import security
    return security.challenge_warranted(ip, email)


def verify_login(token: str, ip: str = "", email: str = "") -> bool:
    """The sign-in doors' check: verify only when the attempt looks risky.

    A clean IP signing into a clean account is let through without a token —
    it has nothing to prove, and demanding proof it may be unable to produce
    is the lockout this gate exists to remove. Once failures accumulate on
    either the IP or the account, the token is required exactly as before.

    a Turnstile token is not the only way to pass. An account that
    has proved control of its mailbox holds a one-shot exemption, which is
    what lets a user the Cloudflare script cannot reach (JS off, blocked,
    proxied) answer the check at all. It is tried LAST so that a user who
    can produce a token never spends one.
"""
    if not required_for_login(ip, email):
        return True
    if verify(token, ip):
        return True
    return _spend_unlock(email)


def refund_unlock(email: str) -> None:
    """The sign-in doors call this when the password step
    succeeds but the request cannot finish (totp_required /
    passkey_required with no code supplied) — a just-spent emailed
    exemption is re-armed so the SECOND post of the two-step flow can
    pass the same check. No-op when the pass came from a token or from
    no-challenge (there is no recently-consumed row to refund)."""
    if not email:
        return
    try:
        from ..auth import login_unlock
        from ..db import tenancy
        with tenancy.control_connect() as conn:
            login_unlock.refund(conn, email)
    except Exception:                    # noqa: BLE001 — best-effort
        pass


def spend_unlock(email: str) -> bool:
    """Public spend-only door for /forgot — a held emailed
    exemption answers the human check there too, WITHOUT the per-account
    risk bucket entering the gate (that door must stay un-armable against
    a stranger's address)."""
    return _spend_unlock(email)


def _spend_unlock(email: str) -> bool:
    """True if this account held a live emailed exemption, which is now
    spent. Never raises: a database wobble here must fail CLOSED (the
    challenge simply stands), not hand out a free pass."""
    if not email:
        return False
    try:
        from ..auth import login_unlock
        from ..db import tenancy
        with tenancy.control_connect() as conn:
            return login_unlock.consume(conn, email)
    except Exception:
        return False
