"""FastAPI skeleton — auth flow + health checks + one real engine endpoint.

Connection discipline:
  * control-plane (users/sessions/tenants) → plain app-role connection
  * everything tenant-scoped → tenancy.tenant_connect(tenant_id) so RLS +
    ambient tenant_id apply. NEVER run engine code on an unscoped conn
    (it sees zero rows — by design).

Session middleware, signup/login/logout and the account-security doors
live here; the React SPA (webapp/) consumes /api/*. Both connection kinds
are pooled (tenancy.py owns the pools and the connection budget).
"""

import datetime as dt
import html as _htmlmod
import json
import logging
import os
import re
import uuid

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware


def _html_escape(s) -> str:
    """Escape a value going into a hand-built HTML mail body. Addresses and
    notes are user-supplied; an unescaped one lets a crafted address inject
    markup into a mail the recipient trusts."""
    return _htmlmod.escape(str(s or ""))

from ..auth import api_tokens, device_tokens, email_verify, login_unlock, \
    passwords, reset as reset_mod, sessions, signup_invites
from ..db import crypto, tenancy
from ..engine import budget
# `FLAG=0` has to mean off: compose materializes every declared key, and a
# bare os.environ.get() reads the string "0" as true — which on these gates
# is the difference between a closed instance and an open one
from ..envnum import env_flag
from .. import ext
from . import demoguard
from . import permissions
from . import security
from . import turnstile
# the widget's form field is hyphenated, so every handler that reads it needs
# the name as a Form alias — evaluated at def time, hence a module import
_TS_FIELD = turnstile.FIELD
from .security import AutoBanMiddleware, BodySizeLimitMiddleware, \
    OriginCheckMiddleware, forwarded_host, limit, request_scheme

# Precomputed once so an unknown-email login runs exactly ONE argon2
# verify — same work as a known email. Hashing "dummy" per request would be
# two argon2 ops (hash + verify) and a timing/cost oracle for user enumeration.
_DUMMY_HASH = passwords.hash_password("dummy")

log = logging.getLogger("oikonome.auth")

# ---- security headers ------------------------------------------------------
# ONE strict policy everywhere — script-src 'self', no inline
# script on either surface (the Jinja pages' handlers live in
# static/pages.js, data-attribute driven). style-src keeps 'unsafe-inline'
# by design: the email-shared templates and React both use inline style
# attributes, and nonce-ing styles buys little against XSS once scripts
# are locked down. No external hosts anywhere.
# Merchant logos come through the instance's own /api/logo cache (web/
# logos.py) — the browser never fetches plaid.com, so no image host needs
# opening here.
_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self'; script-src 'self'")

# Turnstile is the ONE exception to "no external hosts", and only
# while it is configured — an unconfigured instance serves _CSP verbatim.
# Three directives have to open, and all three are load-bearing: the widget
# loads api.js (script-src), renders the challenge inside an IFRAME
# (frame-src — which otherwise falls back to default-src 'self'), and posts
# the result back to Cloudflare (connect-src). Missing any one of them and
# the widget silently never produces a token, so every login 403s while the
# server-side check itself looks perfectly healthy — curl sees a correct
# page and a correct 403, because only a real browser enforces CSP.
_TS_HOST = "https://challenges.cloudflare.com"
_CSP_TURNSTILE = (
    f"default-src 'self'; style-src 'self' 'unsafe-inline'; "
    f"img-src 'self' data:; connect-src 'self' {_TS_HOST}; font-src 'self'; "
    f"object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
    f"form-action 'self'; script-src 'self' {_TS_HOST}; "
    f"frame-src {_TS_HOST}")


def _csp() -> str:
    return _CSP_TURNSTILE if turnstile.enabled() else _CSP


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # HSTS only over HTTPS (a plain-http LAN install must stay
        # reachable — sending it there would pin browsers to https for a
        # year with no cert). X-Forwarded-Proto only counts from a
        # trusted proxy — taken blind, one forged header on a cleartext
        # install would brick plain-http access for a year.
        if request_scheme(request) == "https":
            h.setdefault("Strict-Transport-Security",
                         "max-age=31536000; includeSubDomains")
        h.setdefault("Content-Security-Policy", _csp())
        return resp


DEV_MODE = os.environ.get("OIKONOME_DEV") == "1"

# The OpenAPI schema maps every route
# incl. every sensitive path — free recon for an attacker. Docs UIs are
# off, and so is the schema on anything but dev. Gated on DEV_MODE
# rather than a bare truthiness test on the variable: OIKONOME_DEV=0 reads
# as a non-empty (true) string, which would publish the schema on every box
# whose operator wrote the obvious spelling of "not a dev box".
app = FastAPI(title="Oikonome", docs_url=None, redoc_url=None,
              openapi_url="/openapi.json" if DEV_MODE else None)
# Compress responses. A reverse proxy may compress on its own, but
# a self-hoster who points a browser straight at uvicorn, or
# fronts it with a proxy that does not compress, would otherwise download
# the SPA's main chunk raw — and every JSON page payload too. Innermost, so
# the security headers and the timing ring see the real response.
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
# /api request timings feed the infra-stress sampler (p95 via a
# small in-process ring, summarized to Redis — see reqmetrics.py)
from .reqmetrics import RequestTimingMiddleware  # noqa: E402
app.add_middleware(RequestTimingMiddleware)
# Body-size ceiling, added just under the ban check — an oversized
# body must be refused before any middleware buffers it, but a banned IP
# should still be shed first (cheaper).
app.add_middleware(BodySizeLimitMiddleware)
# added LAST → outermost: a banned IP is rejected here before any other
# middleware, routing, or DB work runs (sheds bot load on a small host).
app.add_middleware(AutoBanMiddleware)


def _control_conn():
    # pooled — a per-request unpooled TLS connect here would be the
    # concurrency knee under load (see tenancy.control_connect and
    # the connection budget above it).
    return tenancy.control_connect()


# Who may change what lives in `permissions` — one module, because the
# rule is enforced both here (every mutating request goes through this
# dependency) and inside the settings save. These names are re-exported so
# the gate below, and the tests that pin its prefix boundaries, keep
# reading as one piece of policy.
VIEWER_WRITE_OK = permissions.SELF_WRITE_OK
path_within = permissions.path_within

# writes a hosted account may make WHILE it still has no strong
# factor — exactly the enrollment surface (TOTP/passkey), plus logout.
# Everything else is blocked until a factor exists (an SPA-only
# gate would be bypassable by a non-SPA client).
NEEDS_2FA_OK = ("/api/logout", "/api/totp", "/api/passkeys", "/api/recovery",
                # email verification is part of getting set up — a hosted
                # user resends/clicks it before (or while) enrolling a factor
                "/api/verify-email", "/verify-email",
                # account-security doors that RE-AUTH on their own (password
                # +/- TOTP): self-guarded, so the enrollment gate is
                # redundant — and these are legitimate onboarding actions
                "/api/password/change", "/api/email/change",
                # signing out must never require enrolling first — that
                # holds for the mobile door exactly as for the web one
                "/api/sessions/revoke", "/api/devices/revoke",
                # a member leaving the household re-auths the same way; a
                # factorless leftover session must be able to leave rather
                # than be forced to enroll on an account it was invited
                # to by mistake
                "/api/account/leave",
                # the step-up assertion IS presenting the strong factor —
                # gating it behind "have a strong factor" is circular, and
                # the self-guarded doors above accept its ticket
                "/api/stepup",
                # the "sudo" window itself: it IS the proof the doors
                # above read, so an account still enrolling its first
                # factor has to be able to open it
                "/api/auth",
                # Resolving an account's standing is not a posture change,
                # and the enrollment doors have to stay reachable under
                # every lockout. Otherwise the two allowlists are disjoint
                # on the escape road: a standing that freezes everything
                # but /api/billing (so no factor can be enrolled) meets a
                # gate that blocks /api/billing (so the standing cannot be
                # resolved), and a session that has enrolled nothing has no
                # reachable door at all. Every billing write is
                # owner-gated, demo-gated, rate-limited and talks only to
                # the installed gate, so it changes nothing an unenrolled
                # session could turn against the account.
                "/api/billing")

# a PUBLIC demo instance (OIKONOME_DEMO=1) closes every pre-auth
# door that could create accounts or rewrite the shared credential — the
# tenant-level demoguard can't cover pre-auth paths. 404 (not 403) so the
# demo box doesn't advertise which doors exist.
def _not_demo_instance():
    if env_flag("OIKONOME_DEMO"):
        raise HTTPException(404, "not found")


# paths a tenant under a read-only standing may still write — resolving
# the standing and leaving. Reads stay open: read-only is the whole
# penalty, and it never destroys records.
# /api/export: reads and the archive stay open under a read-only
# standing, and the door that mints the export ticket is a POST — so
# without this it would answer 402 to exactly the person whose records
# those are.
# /api/auth + /api/stepup: opening the archive and deleting the account both
# require a fresh elevation, and elevating is a POST too — without these
# two the elevation sheet would hit 402 with no way through, in both
# clients. Neither door moves money nor touches household data, which is
# the same argument /api/billing already makes.
BILLING_WRITE_OK = ("/api/billing", "/api/logout", "/api/export",
                    "/api/auth", "/api/stepup")

# The tenant statuses that freeze a household out of the app COMPLETELY —
# reads included, unlike the billing read-only state. Sign-out always stays
# open so a browser is never stuck holding a dead session, and each status
# names whatever else it must leave open to be escapable at all.
#
# One table because more than one door has to agree on the set: current_user
# below enforces it, and GET /login must not shortcut a frozen session into
# an app that will refuse it every page. A second hand-written list would
# drift from this one.
#
#   status -> (HTTP status, message, paths that stay open beyond sign-out)
LOCKOUT_STATUSES: dict[str, tuple[int, str, tuple[str, ...]]] = {
    # operator suspension is the abuse/support lever: nothing but sign-out,
    # and only support can lift it.
    "suspended": (
        403, "this account is suspended — contact support", ()),
    # a tenant scheduled for deletion (grace window open) is frozen the
    # same way, with a message that says it is recoverable until the window
    # lapses.
    "pending_delete": (
        403, "this account is scheduled for deletion — contact support "
             "to cancel before the grace period ends", ()),
    # An installed add-on may freeze a household under statuses of its own
    # and name the doors that stay open so the state can be escaped from
    # inside the app.
    **ext.gate.lockout_statuses(),
}
LOCKED_OUT_STATUSES = tuple(LOCKOUT_STATUSES)


def _has_passkey(user_id) -> bool:
    with _control_conn() as conn:
        return conn.execute("SELECT 1 FROM passkeys WHERE user_id=%s LIMIT 1",
                            (user_id,)).fetchone() is not None


def _burn_totp_counter(conn, user_id, counter: int) -> bool:
    """Claim a TOTP counter for this user. True = it was OURS to claim.

    The accept/reject decision has to ride the UPDATE's rowcount, not the
    SELECT that preceded it. Two requests arriving with the SAME code inside
    its ~90 s window both read the old counter, both get a match out of
    `verify_used`, and both reach here. Only the WHERE clause makes exactly
    one of the writes stick, so reading the rowcount is what makes the
    single-use guard hold under precisely the concurrency an attacker
    replaying an observed code would create: 0 rows affected means "already
    spent", i.e. reject.

    Same shape as `recovery.redeem`."""
    return conn.execute(
        "UPDATE users SET totp_last_counter=%s WHERE id=%s "
        "AND (totp_last_counter IS NULL OR totp_last_counter < %s)",
        (counter, user_id, counter)).rowcount == 1


def _totp_consume(user_id, secret_enc, code: str) -> bool:
    """Verify a TOTP code for a step-up / posture change AND burn it.

    Login uses `verify_used` + `users.totp_last_counter`, so a code cannot
    be replayed inside its ~90s window. Every OTHER door — password change,
    TOTP re-enroll/disable, recovery regenerate, account erasure, email
    change — must do the same. The bare `verify()` checks the code and
    consumes nothing, which leaves a ~90-second window in which one observed
    code opens the same door repeatedly, and lets a code already spent at
    login still open all of them.
    These are precisely the endpoints that change credentials, so they are
    the ones where a replayed factor matters most.

    Returns True and advances the counter; False on no-match or replay.
    """
    if not secret_enc:
        return False
    from ..auth import totp as totp_mod
    with _control_conn() as conn:
        row = conn.execute(
            "SELECT totp_last_counter FROM users WHERE id=%s",
            (user_id,)).fetchone()
        matched = totp_mod.verify_used(crypto.decrypt_cp(secret_enc), code,
                                       row["totp_last_counter"] if row else None)
        if matched is None:
            return False
        return _burn_totp_counter(conn, user_id, matched)


def _require_passkey_stepup(user_id, totp_secret,
                            recovery_code: str) -> str | None:
    """For a passkey-only hosted account the password is NOT a
    sufficient factor for a destructive posture change (login already
    refuses password-only for it). The passkey can't be asserted inline in
    these form/JSON endpoints, so require a RECOVERY CODE — which a
    session-thief holding only the password does not have. No-op for TOTP
    accounts (their caller verifies the code) and self-host.

    Returns the id of the passkey that proved it, when a passkey did — the
    one key a credential rotation may keep. None on every other road,
    including the recovery-code fallback, where no key has been proved and
    a rotation must therefore keep none."""
    if (env_flag("OIKONOME_HOSTED") and not totp_secret
            and _has_passkey(user_id)):
        from ..auth import passkeys, recovery
        with _control_conn() as rconn:
            # the passkey itself is the preferred proof — an inline
            # WebAuthn assertion (/api/stepup/passkey) mints a short-lived
            # ticket that rides this same field. Recovery code stays as the
            # lost-key fallback.
            ok, pk_id = passkeys.redeem_stepup_ticket(rconn, user_id,
                                                      recovery_code or "")
            if ok:
                return pk_id
            if not recovery.redeem(rconn, user_id, recovery_code or ""):
                raise HTTPException(
                    401, "recovery_required: this account signs in with a "
                         "passkey — confirm with your passkey (or a "
                         "recovery code) to make this change")
    return None


def _passkey_only_login_blocked(u) -> bool:
    """A hosted user who satisfies forced-2FA with ONLY a passkey (no
    TOTP) would be protected by PASSWORD ALONE on the email+password login
    path — that path only ever challenges TOTP, so the passkey never
    participates. Block password-only login for such accounts;
    they must sign in via the passkey (WebAuthn) flow, where the factor is
    actually presented. Hosted-only (forced-2FA is hosted-only, and a
    self-host user losing a passkey must not be locked out of password login
    in their own LAN trust domain)."""
    return (env_flag("OIKONOME_HOSTED")
            and not u["totp_secret"] and _has_passkey(u["id"]))


def _billing_write_blocked(sess: dict) -> bool:
    """True when the installed gate says this tenant may read but not
    write. Always False on an instance with no gate."""
    return ext.gate.write_blocked(sess["tenant_id"])


# The exact doors a script token may open — push-only, as
# docs/community-scripts.md promises ("push through the import endpoints
# and update a manual account balance — nothing else"). A
# startswith("/api/import") prefix would also match rollback, bulk
# analyze/run and the batch list — so a leaked collector token could
# UNDO imports and read import history, not just push rows. Exact
# (method, path) pairs; extending it is a deliberate one-line act.
#
# The hub `/api/import` route stays reachable — a community script can
# push CSVs through it with a token — but
# a `.zip` upload there is a FULL-LEDGER RESTORE (restore.restore_zip), so
# the restore branch itself refuses script-token callers (see
# pages.dispatch_import). The allowlist blocks rollback/history PATHS;
# that refusal closes the restore CONTENT hole it alone couldn't see.
SCRIPT_TOKEN_ALLOW = frozenset({
    ("POST", "/api/import"),            # import hub (single file; no ZIP)
    ("POST", "/api/import/mapped"),     # hub fallback: explicit mapping
    ("POST", "/api/import/plan-activity"),  # endpoint door (plan CSV collector)
    ("POST", "/api/import/coinbase"),   # endpoint door
    ("POST", "/api/import/amazon"),     # endpoint door
    ("POST", "/api/import/costco"),     # endpoint door
    ("POST", "/api/accounts/balance"),  # manual-account balance push
})

# The push/import doors are rate-limited like everything else. A
# script token is an hourly cron credential — a generous per-IP budget
# stops a leaked token (or a runaway collector) from hammering the doors
# while never tripping legitimate nightly pushes. Enforced centrally here
# so every allowlisted door is covered without decorating each route.
SCRIPT_TOKEN_RATE = (60, 3600.0)     # (requests, window seconds)


def _script_token_ratelimit(request: Request) -> None:
    n, window = SCRIPT_TOKEN_RATE
    key = ("script-token", security.client_ip(request))
    store = security._shared_store()
    ok = store.check(key, n, window) if store else None
    if ok is None:                       # no Redis, or Redis down
        ok = security._limiter.check(key, n, window)
    if not ok:
        raise HTTPException(429, "too many requests — try again later")


def current_user(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer " + api_tokens.PREFIX):
        if (request.method, request.url.path) not in SCRIPT_TOKEN_ALLOW:
            raise HTTPException(
                403, "script tokens are limited to the import push endpoints")
        _script_token_ratelimit(request)
        with _control_conn() as conn:
            row = api_tokens.lookup(conn, auth[len("Bearer "):])
            # The script-token branch enforces every lockout the cookie
            # path enforces below. Returning before them would leave a
            # suspended, pending-delete or read-only
            # tenant a fully working WRITE path through the collector doors
            # forever, unable even to revoke the token once its session was
            # locked out. A credential minted while in good standing must not
            # outlive the standing that justified it.
            status = None
            if row is not None:
                status = (conn.execute(
                    "SELECT coalesce(status, 'active') AS s FROM tenants "
                    "WHERE id = %s", (row["tenant_id"],)).fetchone()
                    or {}).get("s")
        if row is None:
            raise HTTPException(401, "invalid or revoked token")
        frozen = LOCKOUT_STATUSES.get(status)
        if frozen is not None:
            raise HTTPException(frozen[0], frozen[1])
        if _billing_write_blocked({"tenant_id": row["tenant_id"]}):
            raise HTTPException(402, "this account is read-only until "
                                     "billing is resolved")
        return {"user_id": row["created_by"], "tenant_id": row["tenant_id"],
                "email": f"script:{row['name']}", "role": "owner",
                "script_token": True}
    if auth.startswith("Bearer " + device_tokens.PREFIX):
        # a mobile device token is a full session equivalent (it was
        # minted BY an authenticated session, so every login defense
        # already ran) — resolve it to the same row shape and fall
        # through the identical lockout/role/billing/2FA checks below
        with _control_conn() as conn:
            sess = device_tokens.lookup(conn, auth[len("Bearer "):])
        if sess is None:
            raise HTTPException(401, "invalid or revoked device token")
    else:
        token = request.cookies.get(sessions.COOKIE_NAME, "")
        with _control_conn() as conn:
            sess = sessions.lookup_session(conn, token)
        if sess is None:
            raise HTTPException(401, "not signed in")
    # Total lockout (reads too — unlike the billing read-only state) for
    # every status in LOCKOUT_STATUSES, which is where the set, the code and
    # the wording live so /login can consult the same one.
    frozen = LOCKOUT_STATUSES.get(sess.get("tenant_status"))
    if frozen is not None:
        code, message, still_open = frozen
        if (request.url.path not in ("/api/logout", "/logout")
                and not path_within(request.url.path, still_open)):
            raise HTTPException(code, message)
    # Three gates below ask "is this a write?", and they do NOT all mean
    # the same thing by it.
    #
    # `mutating` is the method: anything that is not a safe verb. That is
    # what the billing read-only state and the forced-2FA enrollment gate
    # are about — both are about the ACCOUNT's standing, not about who is
    # asking, and a body-carrying read still costs the operator money and
    # still comes from an account that has enrolled no factor.
    #
    # The ROLE gate is the one that needs the exemption. Its policy is
    # "everybody in a household reads everything", so refusing a viewer a
    # door that carries a body and only SELECTs (permissions.READ_POST_OK)
    # contradicts it — a viewer would be told to ask the owner for
    # permission to ask a question.
    #
    # One predicate for all three would mean adding /api/assistant to
    # READ_POST_OK for the role gate silently opens the other two: a
    # billing-lapsed tenant would keep unmetered access to the one endpoint
    # that costs per call, and a hosted account with zero factors reach it.
    mutating = request.method not in ("GET", "HEAD", "OPTIONS")
    role_write = mutating and not permissions.read_only_route(
        request.url.path)
    if role_write and not permissions.can_write(sess["role"],
                                                request.url.path):
        raise HTTPException(
            403, permissions.denial(sess["role"], request.url.path))
    if (mutating
            and not path_within(request.url.path, BILLING_WRITE_OK)
            and _billing_write_blocked(sess)):
        raise HTTPException(402, "this account is read-only right now")
    # Server enforcement of the forced-2FA-enrollment gate. Enforced in
    # the SPA alone it would be no gate at all — a non-SPA client with a
    # valid cookie could WRITE without ever enrolling a factor. So here:
    # a hosted account with no strong factor — viewers included —
    # may not perform writes until it enrolls TOTP or a
    # passkey. Reads (their own data) are allowed; the enrollment/logout
    # endpoints stay open (NEEDS_2FA_OK) so they can actually enroll.
    # has_totp rides the session row; only TOTP-less users pay the passkey
    # lookup.
    if (env_flag("OIKONOME_HOSTED")
            and mutating
            and not path_within(request.url.path, NEEDS_2FA_OK)
            and not sess.get("has_totp")
            and not sess.get("second_factor_waived")):
        with _control_conn() as conn:
            has_passkey = conn.execute(
                "SELECT 1 FROM passkeys WHERE user_id=%s LIMIT 1",
                (sess["user_id"],)).fetchone() is not None
        if not has_passkey:
            raise HTTPException(
                403, "enroll a second factor (authenticator or passkey) "
                     "before making changes")
    return sess


@app.get("/")
def index():
    from fastapi.responses import RedirectResponse
    from . import setup as setup_mod
    # a HOSTED instance is never claimed through the self-host
    # /setup token wizard — accounts arrive by invite (/signup). So an
    # unclaimed hosted box sends visitors to /login (invite recipients
    # use their direct link), NOT to a setup form that 403s them.
    if env_flag("OIKONOME_HOSTED"):
        return RedirectResponse("/login", status_code=303)
    with _control_conn() as conn:
        fresh = not setup_mod.instance_has_users(conn)
    return RedirectResponse("/setup" if fresh else "/app/", status_code=303)


def _setup_token_check(request: Request, form_token: str = "") -> str:
    """First-boot gate: when the installer generated OIKONOME_SETUP_TOKEN,
    the wizard only answers to the link that carries it — anyone else who
    can reach the port before the owner claims the instance gets a 403,
    not a signup form. An empty env var keeps the wizard open to anyone
    who can reach it. Returns the token for re-embedding in the form."""
    import secrets as secrets_mod
    tok = os.environ.get("OIKONOME_SETUP_TOKEN", "")
    if not tok:
        return ""
    supplied = form_token or request.query_params.get("token", "")
    # A pre-auth trap: compare_digest raises TypeError on non-ASCII str,
    # so GET /setup?token=café would 500 instead of 403. Hashing both
    # sides makes the compare fixed-length and total.
    import hashlib as _hl
    _d = lambda s: _hl.sha256(s.encode()).digest()   # noqa: E731
    if not secrets_mod.compare_digest(_d(supplied), _d(tok)):
        raise HTTPException(
            403, "setup needs its one-time link — run ./oikonome.sh status "
                 "on the server to print it")
    return tok


@app.get("/setup")
def setup_page(request: Request):
    from fastapi.responses import HTMLResponse, RedirectResponse
    from . import setup as setup_mod, todayview
    # /setup is a self-host-only flow — a hosted instance bounces
    # to /login so nobody hits the token 403 there either
    if env_flag("OIKONOME_HOSTED"):
        return RedirectResponse("/login", status_code=303)
    with _control_conn() as conn:
        if setup_mod.instance_has_users(conn):
            # claimed instance: send humans to the app (login handles the
            # rest) instead of a raw JSON 403
            return RedirectResponse("/app/", status_code=303)
    token = _setup_token_check(request)
    return HTMLResponse(todayview._env.get_template("setup.html").render(
        title="Setup", error=None, token=token, centered=False))


@app.post("/setup", dependencies=[Depends(limit("setup", 5, 3600))])
def setup_submit(request: Request, email: str = Form(...),
                 password: str = Form(...),
                 simplefin_token: str = Form(""),
                 plaid_client_id: str = Form(""),
                 plaid_secret: str = Form(""),
                 plaid_env: str = Form("production"),
                 llm_url: str = Form(""),
                 llm_model: str = Form(""),
                 llm_api_key: str = Form(""),
                 token: str = Form("")):
    from urllib.parse import quote

    from fastapi.responses import HTMLResponse
    from . import setup as setup_mod, todayview
    # /setup is a self-host-only flow — hosted uses invite-gated signup. Mirror
    # GET /setup's guard so the POST can't bootstrap a first user on hosted
    # (defense in depth alongside the setup-token check).
    if env_flag("OIKONOME_HOSTED"):
        raise HTTPException(404)
    with _control_conn() as conn:
        if setup_mod.instance_has_users(conn):
            raise HTTPException(403, "instance is already set up")
    _setup_token_check(request, token)
    try:
        r = setup_mod.bootstrap(email, password, simplefin_token,
                                plaid_client_id=plaid_client_id,
                                plaid_secret=plaid_secret,
                                plaid_env=plaid_env,
                                llm_url=llm_url, llm_model=llm_model,
                                llm_api_key=llm_api_key)
    except ValueError as e:
        # re-render with what the user typed — except the Plaid secret and
        # the LLM API key, which are never echoed back into HTML
        return HTMLResponse(todayview._env.get_template("setup.html").render(
            title="Setup", error=str(e), email=email,
            simplefin_token=simplefin_token,
            plaid_client_id=plaid_client_id, plaid_env=plaid_env,
            llm_url=llm_url, llm_model=llm_model, token=token,
            centered=False),
            status_code=400)
    resp = _login_response(r["user_id"], r["tenant_id"],
                           request.headers.get("user-agent", ""),
                           request=request)
    resp.status_code = 303
    if r.get("sync_error"):
        # SimpleFIN failed: land on the retry form (keys, if any, are saved)
        resp.headers["location"] = "/accounts?msg=" + quote(r["sync_error"])
    elif r.get("plaid_saved"):
        # the wizard's first step is Accounts, where the Plaid button lives
        resp.headers["location"] = "/app/welcome?msg=" + quote(
            "Plaid keys saved — connect your first bank with the "
            "Plaid button.")
    else:
        # land in the guided setup wizard, not on a bare page
        resp.headers["location"] = "/app/welcome"
    return resp


@app.get("/login")
def login_page(request: Request):
    from fastapi.responses import HTMLResponse, RedirectResponse
    from . import security, todayview, turnstile
    # a valid session skips the form. Browsers learn /login from repeated
    # sign-ins and then autocomplete every later visit straight to it,
    # where an already-signed-in person would be asked to sign in again —
    # which reads as "my session didn't stick" while the abandoned sessions
    # sit valid in the DB, and every extra sign-in reinforces the
    # autocomplete.
    # Two exceptions keep the form: an email-flow arrival (?reset/?verified/
    # ?unlocked — the link may belong to a DIFFERENT account than the
    # machine's cookie, and on a shared machine bouncing into the stale
    # account would hide that), and a frozen tenant (the app would only
    # bounce them back out). Frozen means every status current_user locks
    # out — suspended, scheduled for deletion, and whatever an installed
    # gate adds — read from
    # that one table rather than named again here, because a second list
    # is a list that drifts.
    email_flow = any(request.query_params.get(k)
                     for k in ("reset", "verified", "unlocked"))
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    if token and not email_flow:
        with _control_conn() as conn:
            sess = sessions.lookup_session(conn, token)
        # a frozen tenant keeps the form — with one exception. A lockout
        # that leaves doors open (LOCKOUT_STATUSES' third element) is meant
        # to be escaped from inside the app, so bouncing that session to a
        # sign-in form would hide the way out instead of showing it.
        frozen = {st for st, (_, _, doors) in LOCKOUT_STATUSES.items()
                  if not doors}
        if sess is not None and sess.get("tenant_status") not in frozen:
            return RedirectResponse("/app/", status_code=303)
    notice = ("Password updated — sign in with your new password."
              if request.query_params.get("reset") else None)
    if request.query_params.get("unlocked"):
        notice = ("Security check cleared for this sign-in — "
                  "enter your password as usual.")
    if request.query_params.get("verified"):
        notice = "Email confirmed — sign in to continue."
    challenged = turnstile.required_for_login(security.client_ip(request))
    return HTMLResponse(todayview._env.get_template("login.html").render(
        centered=True, title="Sign in", error=None, email="", notice=notice,
        # only offer the emailed way past the check when a check is
        # actually being asked for. Advertising it to everyone would train
        # users to reach for an email round-trip they do not need.
        show_unlock=challenged and turnstile.enabled(),
        turnstile_key=turnstile.render_key(),
        # The widget still renders for everyone (a token waiting in the form
        # costs nothing and makes the challenge seamless if risk rises
        # mid-session). This only tells pages.js whether it must WAIT for
        # one: a clean client posts immediately, so a blocked script adds no
        # delay to a sign-in that does not need it.
        turnstile_wait=challenged))


@app.post("/login", dependencies=[Depends(limit("login", 10, 300))])
def login_form(request: Request, email: str = Form(...),
               password: str = Form(...), totp_code: str = Form(""),
               cf_token: str = Form("", alias=_TS_FIELD)):
    from fastapi.responses import HTMLResponse
    from ..auth import totp as totp_mod
    from . import security, todayview
    # Human check before the password is touched — but only for an attempt
    # with recent failures behind it (see turnstile.verify_login). No-JS
    # door: one submit, one token. A rejected token re-renders the form
    # rather than 4xx-ing, so the widget gets a fresh challenge.
    if not turnstile.verify_login(cf_token, security.client_ip(request), email):
        # show_unlock is load-bearing HERE above all: this is the
        # response a user gets when they cannot complete the widget — script
        # blocked, JS off, corporate proxy — so omitting it leaves exactly
        # that user with no visible route to /unlock. Jinja's default
        # environment renders an undefined as false silently, so the escape
        # hatch would simply never draw on the one page built to offer it.
        return HTMLResponse(todayview._env.get_template("login.html").render(
            centered=True, title="Sign in", email=email,
            turnstile_key=turnstile.render_key(), turnstile_wait=True,
            show_unlock=turnstile.enabled(),
            error="Please complete the human check and try again."),
            status_code=403)
    # account-scoped lockout: stops a distributed TOTP/password brute at
    # one account regardless of source IP
    if security.account_login_locked(email):
        return HTMLResponse(todayview._env.get_template("login.html").render(
            centered=True, title="Sign in", email=email,
            turnstile_key=turnstile.render_key(),
            turnstile_wait=turnstile.required_for_login(
                security.client_ip(request), email),
            show_unlock=turnstile.enabled() and turnstile.required_for_login(
                security.client_ip(request), email),
            error="Too many failed attempts on this account — try again "
                  "in a few minutes."), status_code=429)
    with _control_conn() as conn:
        u = conn.execute(
            "SELECT id, tenant_id, password_hash, totp_secret, "
            "totp_last_counter FROM users WHERE email=%s",
            (email.strip().lower(),)).fetchone()
    ok = passwords.verify_password(
        u["password_hash"] if u else _DUMMY_HASH, password)
    err = None
    # Only reveal that TOTP is needed AFTER the password verifies —
    # showing the code field on a wrong password (for a known email) is an
    # enrollment oracle.
    need_totp = False
    _totp_counter = None
    if not u or not ok:
        err = "Wrong email or password."
    elif _passkey_only_login_blocked(u):
        # A passkey-only account can't complete password-only login.
        # But a lost-passkey owner must still have an escape — accept
        # a one-time recovery code (entered in the code field), the same
        # lockout net TOTP accounts get. Otherwise steer them to the passkey.
        from ..auth import recovery
        with _control_conn() as _c:
            redeemed = totp_code and recovery.redeem(_c, u["id"], totp_code)
        if not redeemed:
            if totp_code:
                security.record_login_failure(email)
            else:
                # The password step of this two-phase form spent the
                # emailed unlock the recovery-code step still needs —
                # re-arm it, as /api/login does for the same case.
                turnstile.refund_unlock(email)
            security.record_auth_failure(security.client_ip(request), email)
            return HTMLResponse(
                todayview._env.get_template("login.html").render(
                    centered=True, title="Sign in", email=email,
                    passkey_only=True, turnstile_key=turnstile.render_key(),
                    turnstile_wait=turnstile.required_for_login(
                        security.client_ip(request), email),
                    show_unlock=turnstile.enabled()
                    and turnstile.required_for_login(
                        security.client_ip(request), email),
                    error="This account signs in with a passkey — use “Sign "
                          "in with a passkey”, or enter a recovery code."),
                status_code=401)
    elif u["totp_secret"] and (_totp_counter := totp_mod.verify_used(
            crypto.decrypt_cp(u["totp_secret"]), totp_code,
            u["totp_last_counter"])) is None:
        # accept a one-time recovery code here too (lost-phone
        # escape), only after the TOTP check fails
        from ..auth import recovery
        with _control_conn() as _c:
            redeemed = totp_code and recovery.redeem(_c, u["id"], totp_code)
        if not redeemed:
            err = ("Enter the 6-digit code from your authenticator (or a "
                   "recovery code)." if not totp_code
                   else "That code didn't match.")
            need_totp = True
    if not err and _totp_counter is not None:
        # burn the accepted TOTP step BEFORE minting a session — if another
        # request already spent this code, this login does not happen
        # (see _burn_totp_counter).
        with _control_conn() as _c:
            if not _burn_totp_counter(_c, u["id"], _totp_counter):
                err = "That code has already been used — wait for the next one."
                need_totp = True
    if err:
        # Count ONLY a SUPPLIED-but-wrong second
        # factor toward the account lock. A wrong PASSWORD (email-only DoS)
        # OR an EMPTY code (the normal first step of this two-phase
        # form) must never lock a valid account. Mirrors
        # /api/login, which treats an absent code as "totp_required".
        if need_totp and totp_code:
            security.record_login_failure(email)
        if need_totp and not totp_code:
            # The Jinja twin of /api/login's totp_required: the
            # password step of the two-phase form spent the emailed unlock
            # this same sign-in still needs for its code step — re-arm it
            turnstile.refund_unlock(email)
        # any wrong-password / wrong-code attempt is a strike toward auto-ban
        # (catches credential-stuffing that stays under the per-IP limit by
        # spraying many emails); a human fat-fingering won't reach 20/10min.
        # It also arms the human check for the next attempt from this IP or
        # against this account.
        security.record_auth_failure(security.client_ip(request), email)
        return HTMLResponse(todayview._env.get_template("login.html").render(
            centered=True, title="Sign in", error=err, email=email,
            need_totp=need_totp, turnstile_key=turnstile.render_key(),
            turnstile_wait=turnstile.required_for_login(
                security.client_ip(request), email),
            # recomputed AFTER record_auth_failure above, so a user who just
            # crossed the challenge threshold is offered the way past it on
            # the very render that starts demanding one
            show_unlock=turnstile.enabled() and turnstile.required_for_login(
                security.client_ip(request), email)), status_code=401)
    security.clear_login_failures(email)
    security.clear_challenge_risk(security.client_ip(request), email)
    resp = _login_response(u["id"], u["tenant_id"],
                           request.headers.get("user-agent", ""),
                           request=request)
    resp.status_code = 303
    resp.headers["location"] = "/app/"
    return resp


# ---- password reset --------------------------------------------------------

FORGOT_MSG = "If that address exists, a reset link was sent."


def _log_link(kind: str, email: str, path: str) -> None:
    """The emailed-link log fallback IS the delivery mechanism on a
    self-host box with no SMTP — keep the full link there. On HOSTED the
    operator always has email and shared ops logs shouldn't carry a live
    token, so redact to a short hash. Always no_capture (never in the
    feedback bundle).

    Every caller reaches this line only when the mail did NOT go out (no
    SMTP, send raised, or the recipient is blocked) — so the hosted line
    must never read as a delivery receipt; it records that a link went
    unsent and that the console is the way to resend it."""
    # keep `kind` in the format-string literal (log filters/greps key on
    # it); escape any % so a note with a percent can't break rendering
    safe = kind.replace("%", "%%")
    if env_flag("OIKONOME_HOSTED"):
        import hashlib
        d = hashlib.sha256(path.encode()).hexdigest()[:12]
        log.warning(safe + " for %s: NOT emailed [link redacted %s] — "
                    "resend from the admin console once mail works",
                    email, d, extra={"no_capture": True})
    else:
        log.warning(safe + " for %s: %s", email, path,
                    extra={"no_capture": True})


def _smtp_for(email: str):
    """The tenant's resolved SMTP settings for an address, or None. Never
    raises — every caller is an out-of-band delivery whose response must not
    vary with what happened in here."""
    if DEV_MODE:
        return None
    from . import report as _report
    from ..db import tenancy as _tn
    try:
        with _control_conn() as _c:
            row = _c.execute("SELECT tenant_id FROM users WHERE email=%s",
                             (email,)).fetchone()
        if not row:
            return None
        tc = _tn.tenant_connect(row["tenant_id"])
        try:
            return _report.resolve_smtp(tc)
        finally:
            tc.close()
    except Exception:                            # noqa: BLE001
        return None


def _deliver_unlock(email: str, token: str) -> None:
    """Email the one-time link that lets this account answer the human
    check without JavaScript. Same out-of-band discipline and same
    pinned-base rule as the reset mail — see _deliver_reset for why the
    base URL is never derived from request headers."""
    path = f"/unlock?token={token}"
    from . import report as _report
    smtp = _smtp_for(email)
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    if smtp and smtp["configured"] and not base:
        log.warning("SMTP is configured but OIKONOME_BASE_URL is not set — "
                    "refusing to email a Host-derived unlock link")
    if smtp and smtp["configured"] and base:
        url = f"{base}{path}"
        plain = (f"Open this link, then sign in as usual (link valid 30 "
                 f"minutes, one use):\n\n{url}\n\nIt does NOT sign you in "
                 f"by itself — you will still need your password.")
        html = (f"<p><a href=\"{url}\">Open this link</a>, then sign in as "
                f"usual (link valid 30 minutes, one use).</p>"
                f"<p>It does <b>not</b> sign you in by itself — you will "
                f"still need your password.</p>")
        try:
            _report.send("Sign-in help — Oikonome", plain, html, [email],
                         smtp=smtp, bcc=False)
            return
        except Exception:                # noqa: BLE001 — fall through to log
            log.exception("unlock email to %s failed; logging the link "
                          "instead", email)
    _log_link("sign-in unlock link", email, path)


def _deliver_reset(email: str, token: str) -> None:
    """Email the link when SMTP is configured AND OIKONOME_BASE_URL is
    pinned; otherwise (self-hosted default, DEV, or unpinned base) log it
    server-side — operators read logs. NEVER raises: the /forgot response
    must be identical no matter what happened."""
    path = f"/reset?token={token}"
    from . import report as _report
    smtp = _smtp_for(email)
    # OIKONOME_BASE_URL pins the emailed link's origin. Deriving it
    # from the Host / X-Forwarded-Proto headers would let a forged /forgot
    # mail a REAL one-time token on an attacker-domain link (takeover).
    # Same policy as invite emails: no pinned base → no email, ever — the
    # link still lands in the server log below so the operator can recover.
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    if smtp and smtp["configured"] and not base:
        log.warning("SMTP is configured but OIKONOME_BASE_URL is not set — "
                    "refusing to email a Host-derived reset link; set "
                    "OIKONOME_BASE_URL to enable reset emails")
    if smtp and smtp["configured"] and base:
        url = f"{base}{path}"
        plain = (f"Reset your password (link valid 1 hour, one use):\n\n{url}\n")
        html = (f"<p><a href=\"{url}\">Reset your password</a> "
                f"(link valid 1 hour, one use).</p>")
        try:
            _report.send("Reset your password — Oikonome", plain, html, [email],
                         smtp=smtp, bcc=False)
            return
        except Exception:                # noqa: BLE001 — fall through to log
            log.exception("password-reset email to %s failed; "
                          "logging the link instead", email)
    # WARNING, not INFO: with default logging config INFO never reaches the
    # console, and on a no-SMTP install this line IS the reset delivery
    _log_link("password reset link", email, path)


def _deliver_welcome(email: str, token: str) -> None:
    """Operator-provisioned account (admin console "Create an
    instance"). Same one-time set-password mechanics as a reset, but the
    recipient never asked for anything — "someone requested a password
    reset" reads like phishing for an account they didn't know they had.
    Welcome copy instead; link TTL is the 7-day WELCOME_TTL."""
    path = f"/reset?token={token}"
    from . import report as _report
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    smtp = _report._env_smtp()
    if smtp["configured"] and base:
        url = f"{base}{path}"
        # Three sentences, nothing else: whose account,
        # what to do, how long the link lives.
        plain = (f"Your Oikonome account is ready.\n\n"
                 f"Choose your password (link valid 7 days, one use):\n\n"
                 f"{url}\n")
        html = (f"<p>Your Oikonome account is ready.</p>"
                f"<p><a href=\"{url}\">Choose your password</a> "
                f"(link valid 7 days, one use).</p>")
        try:
            _report.send("Welcome to Oikonome — set your password",
                         plain, html, [email], smtp=smtp, bcc=False)
            return
        except Exception:                # noqa: BLE001 — fall through to log
            log.exception("welcome email to %s failed; logging the link",
                          email)
    _log_link("welcome set-password link", email, path)


@app.get("/forgot", dependencies=[Depends(_not_demo_instance)])
def forgot_page(request: Request):
    from fastapi.responses import HTMLResponse
    from . import security, todayview
    return HTMLResponse(todayview._env.get_template("forgot.html").render(
        centered=True, title="Reset password", sent=False,
        turnstile_key=turnstile.render_key(),
        turnstile_wait=turnstile.required_for_login(
            security.client_ip(request))))


# How many reset mails one ADDRESS may receive per hour, whatever IP asks.
# The route's other throttle, `limit("forgot", 5, 3600)`, is per-IP — so on
# its own a distributed caller (or anyone with a phone and a laptop) could
# mint unbounded reset mail into a chosen victim's inbox. Nothing about
# the flow needs more than a couple of tries an hour, and the sibling
# login-unlock door is already capped per address for the same reason.
#
# Counted off password_resets, which already exists and is already keyed on
# the user: no new table, and it counts what was actually MINTED rather than
# what was requested.
_RESET_PER_HOUR = 3


def _reset_send_allowed(conn, user_id) -> bool:
    """False when this account has already been mailed its hourly quota.

    The caller must behave IDENTICALLY either way — the response is the same
    "if that address exists we've sent a link" page, and the send is
    off-thread — or this becomes an oracle for which addresses have accounts,
    which is the exact property the out-of-band delivery below protects.
    """
    try:
        n = conn.execute(
            "SELECT count(*) AS n FROM password_resets "
            "WHERE user_id = %s AND created_at > now() - interval '1 hour'",
            (user_id,)).fetchone()["n"]
    except Exception:                                # noqa: BLE001
        return True          # never let a counter failure block a real reset
    if n >= _RESET_PER_HOUR:
        log.warning("password reset suppressed — %s already minted this hour",
                    _RESET_PER_HOUR)
        return False
    return True


@app.post("/forgot", dependencies=[Depends(limit("forgot", 5, 3600)),
                              Depends(_not_demo_instance)])
def forgot_submit(request: Request, email: str = Form(...),
                  cf_token: str = Form("", alias=_TS_FIELD)):
    from fastapi.responses import HTMLResponse
    from . import security, todayview
    # /forgot sits inside the human-check perimeter. Outside it, with only
    # a 5/hr per-IP limit, a botnet could drive unlimited outbound reset
    # mail — sender-reputation damage for the instance, mail-bombing for the victim.
    #
    # Gated on the IP and INSTANCE buckets only, never the per-account one:
    # this is the password-recovery door, and letting anyone arm a challenge
    # against a specific address by guessing at their login would put the
    # blocked-script dead end in front of the exact user who needs the door.
    # The residual is a purely distributed flood with no login failures behind
    # it, which stays bounded by the per-IP limit.
    ip = security.client_ip(request)
    email_in = email.strip().lower()
    if not turnstile.verify_login(cf_token, ip):
        # The emailed unlock must answer /forgot too. Calling verify_login
        # with no email means _spend_unlock can never fire, and with no way
        # to mint one from forgot.html a no-JS user is locked out of
        # password recovery entirely the moment the site-wide bucket arms.
        # Spend-only, for the SUBMITTED address — the
        # per-account RISK bucket deliberately stays out of this gate (see
        # the comment above), so nobody can arm a challenge against a
        # stranger by guessing at their login here.
        if not turnstile.spend_unlock(email_in):
            return HTMLResponse(
                todayview._env.get_template("forgot.html").render(
                    centered=True, title="Reset password", sent=False,
                    turnstile_key=turnstile.render_key(), turnstile_wait=True,
                    show_unlock=turnstile.enabled(),
                    error="Please complete the human check and try again."),
                status_code=403)
    email = email.strip().lower()
    with _control_conn() as conn:
        u = conn.execute("SELECT id FROM users WHERE email=%s",
                         (email,)).fetchone()
        token = None
        if u:
            # atomic check-and-mint: the per-account hourly cap is the only
            # thing bounding reset mail to one inbox, and a bare
            # count-then-insert would let concurrent requests each read under
            # the cap before any committed (the same race login_unlock guards).
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext("
                             "'oikonome:pwreset:' || %s))", (str(u["id"]),))
                if _reset_send_allowed(conn, u["id"]):
                    token = reset_mod.create_reset(conn, u["id"])
        if token:
            # deliver OUT-OF-BAND: a synchronous SMTP send is a timing
            # oracle for account existence
            import threading
            threading.Thread(target=_deliver_reset,
                             args=(email, token),
                             daemon=True).start()
    # identical response whether or not the account exists — no enumeration
    return HTMLResponse(todayview._env.get_template("forgot.html").render(
        centered=True, title="Reset password", sent=True, message=FORGOT_MSG))


UNLOCK_MSG = ("If that address exists, a sign-in link was sent. Open it, "
              "then sign in as usual.")


@app.get("/unlock", dependencies=[Depends(_not_demo_instance)])
def unlock_page(request: Request, token: str = ""):
    """The way past a human check the browser cannot answer.

    Without a token: the request form, for a user staring at a challenge
    they cannot answer. With one: follow it, which opens a short exemption
    for THAT ACCOUNT and sends them back to sign in normally.

    Deliberately carries no human check of its own — gating the escape
    hatch behind the very thing being escaped is a third locked door. The
    abuse it must not enable is mail-bombing, and that is capped per
    address in login_unlock, where a botnet's many IPs buy it nothing.
    """
    from fastapi.responses import HTMLResponse
    from . import todayview
    if not token:
        return HTMLResponse(todayview._env.get_template("unlock.html").render(
            centered=True, title="Sign-in help", sent=False))
    # PEEK only. Following here would burn the one-time token whenever
    # a mail scanner or prefetcher fetched the link — and the users this
    # hatch exists for (corporate proxies, locked-down browsers) are exactly
    # the ones whose mail is prescanned. The real use is the POST below.
    with _control_conn() as conn:
        row = login_unlock.peek(conn, token)
    if not row:
        return HTMLResponse(todayview._env.get_template("unlock.html").render(
            centered=True, title="Sign-in help", sent=False,
            error="That link has expired or was already used. "
                  "Request a new one below."), status_code=400)
    return HTMLResponse(todayview._env.get_template("unlock.html").render(
        centered=True, title="Sign-in help", confirm_token=token,
        email=row["email"]))


@app.post("/unlock/confirm", dependencies=[Depends(limit("unlock", 10, 3600)),
                                           Depends(_not_demo_instance)])
def unlock_confirm(token: str = Form("")):
    """The explicit human action that actually spends the link."""
    from fastapi.responses import HTMLResponse, RedirectResponse
    from . import todayview
    with _control_conn() as conn:
        row = login_unlock.follow(conn, token)
    if not row:
        return HTMLResponse(todayview._env.get_template("unlock.html").render(
            centered=True, title="Sign-in help", sent=False,
            error="That link has expired or was already used. "
                  "Request a new one below."), status_code=400)
    # NOT a sign-in: no session is created here. The password (and any
    # second factor) is still required — that is the whole difference
    # between this and a magic link.
    return RedirectResponse("/login?unlocked=1", status_code=303)


@app.post("/unlock", dependencies=[Depends(limit("unlock", 5, 3600)),
                                   Depends(_not_demo_instance)])
def unlock_submit(request: Request, email: str = Form(...)):
    from fastapi.responses import HTMLResponse
    from . import todayview
    email = email.strip().lower()
    with _control_conn() as conn:
        u = conn.execute("SELECT id FROM users WHERE email=%s",
                         (email,)).fetchone()
        token = login_unlock.create(conn, u["id"]) if u else None
    if token:
        # out-of-band, like the reset mail: a synchronous send is a timing
        # oracle for account existence
        import threading
        threading.Thread(target=_deliver_unlock, args=(email, token),
                         daemon=True).start()
    # identical response whether the account exists, or existed and was over
    # its hourly cap — neither the prober nor the mail-bomber learns anything
    return HTMLResponse(todayview._env.get_template("unlock.html").render(
        centered=True, title="Sign-in help", sent=True, message=UNLOCK_MSG))


def _reset_needs_recovery(conn, row) -> bool:
    """A password reset must not be the way around the second factor.
    reset.consume strips EVERY factor — passkeys and the TOTP secret alike
    (a hijacker may have planted either) — so without a second proof the
    reset would turn "I control the inbox" into "I own the account". For an
    account with any strong factor enrolled, the reset also takes one of
    the recovery codes that factor minted — the same step-up every other
    posture change demands. This holds for TOTP accounts as much as
    passkey-only ones: the secret does not survive the reset either.

    There are NO exemptions, and there is no second flavour of reset link.
    A waiver for links minted by the signup-reclaim door would have to rest
    on reading `verified_at` as "this household never proved a mailbox",
    and it does not mean that: a hosted email change clears `verified_at`
    on a fully verified household, so such a waiver reads a real,
    long-lived account as an unproven squat and lets a reset strip every
    factor with no recovery code — whoever receives mail at the new address
    could then take it. The rule is unconditional, which is the only form
    of it that cannot be misread at a call site.
    """
    user_id = row["user_id"]
    u = conn.execute(
        "SELECT totp_secret, (SELECT count(*) FROM passkeys "
        "WHERE user_id = %s) AS n_pk FROM users WHERE id = %s",
        (user_id, user_id)).fetchone()
    if not u:
        return False
    if not (u["totp_secret"] or u["n_pk"]):
        return False
    # The one thing that stands in for the code: a cooling-off request
    # that has LANDED. It was asked for from a reset page (inbox proven),
    # the account was told on every channel it has, a week passed, and
    # nobody cancelled it or signed in. That is not inbox control alone —
    # see auth/factor_reset.py — so the rule above still holds.
    from ..auth import factor_reset
    return factor_reset.matured(conn, user_id) is None


@app.get("/reset", dependencies=[Depends(_not_demo_instance)])
def reset_page(token: str = ""):
    from fastapi.responses import HTMLResponse
    from . import todayview
    with _control_conn() as conn:
        row = reset_mod.lookup_reset(conn, token)
        needs = bool(row) and _reset_needs_recovery(conn, row)
    if row is None:
        return HTMLResponse(todayview._env.get_template("reset.html").render(
            centered=True, title="Reset password", token=None,
            error="That reset link is invalid, expired, or already used."),
            status_code=400)
    return HTMLResponse(todayview._env.get_template("reset.html").render(
        centered=True, title="Reset password", token=token, error=None,
        needs_recovery=needs, support_email=support_email()))


@app.post("/reset", dependencies=[Depends(limit("reset", 10, 3600)),
                             Depends(_not_demo_instance)])
def reset_submit(token: str = Form(...), password: str = Form(...),
                 recovery_code: str = Form("")):
    from fastapi.responses import HTMLResponse, RedirectResponse
    from . import todayview
    with _control_conn() as conn:
        row = reset_mod.lookup_reset(conn, token)
        if row is None:
            return HTMLResponse(
                todayview._env.get_template("reset.html").render(
                    centered=True, title="Reset password", token=None,
                    error="That reset link is invalid, expired, or already "
                          "used."), status_code=400)
        needs = _reset_needs_recovery(conn, row)
        err = passwords.password_error(password)
        if err:
            return HTMLResponse(
                todayview._env.get_template("reset.html").render(
                    centered=True, title="Reset password", token=token,
                    error=err.capitalize() + ".", needs_recovery=needs,
                    support_email=support_email()),
                status_code=400)
        # the code is redeemed BEFORE the reset consumes anything: a wrong
        # code leaves the link live (the person can try another code), and
        # a right one is burned exactly like it would be at sign-in
        from ..auth import recovery
        if needs and not recovery.redeem(conn, row["user_id"],
                                         recovery_code or ""):
            return HTMLResponse(
                todayview._env.get_template("reset.html").render(
                    centered=True, title="Reset password", token=token,
                    needs_recovery=True, support_email=support_email(),
                    error="This account has a second factor, so the reset "
                          "also needs one of its recovery codes — that one "
                          "did not match."), status_code=401)
        from ..auth import factor_reset
        landed = factor_reset.matured(conn, row["user_id"])
        if not reset_mod.consume(conn, row["id"], row["user_id"],
                                 passwords.hash_password(password)):
            return HTMLResponse(
                todayview._env.get_template("reset.html").render(
                    centered=True, title="Reset password", token=None,
                    error="That reset link is invalid, expired, or already "
                          "used."), status_code=400)
        # the request that let this reset through is spent by it; a
        # coded reset on an account with a request still cooling cancels
        # the clock too — whoever holds a code does not need the door
        if landed is not None:
            factor_reset.consume(conn, landed["id"])
        else:
            factor_reset.cancel_for_user(conn, row["user_id"], reason="reset")
    return RedirectResponse("/login?reset=1", status_code=303)


# ---- lost everything: the cooling-off reset ----------------------------


def _recover_page(**ctx):
    from fastapi.responses import HTMLResponse
    from . import todayview
    status = ctx.pop("status", 200)
    return HTMLResponse(todayview._env.get_template("recover.html").render(
        centered=True, title="Account recovery", support_email=support_email(),
        **ctx), status_code=status)


@app.post("/recover", dependencies=[Depends(limit("recover", 5, 3600)),
                                Depends(_not_demo_instance)])
def recover_request(request: Request, token: str = Form(...)):
    """Start the cooling-off clock. The proof of the inbox is the live
    reset token the person is holding — this door opens only from the
    reset page that just demanded a recovery code they do not have. The
    reset link itself is left live and unused: it expires on its own hour,
    and consuming it here would take the password hostage for a week."""
    from ..auth import factor_reset
    from .security import client_ip
    with _control_conn() as conn:
        row = reset_mod.lookup_reset(conn, token)
        if row is None:
            return _recover_page(
                state="bad_link", status=400,
                error="That reset link is invalid, expired, or already used. "
                      "Request a new one and start again.")
        if not _reset_needs_recovery(conn, row):
            # nothing to cool off — the reset works as it is, either because
            # the account has no factor or because an earlier request has
            # already landed (asking again must not restart THAT clock)
            landed = factor_reset.matured(conn, row["user_id"]) is not None
            return _recover_page(state="landed" if landed else "not_needed",
                                 token=token)
        req, cancel = factor_reset.request(conn, row["user_id"],
                                           ip=client_ip(request))
        u = conn.execute("SELECT tenant_id FROM users WHERE id=%s",
                         (row["user_id"],)).fetchone()
    import threading
    threading.Thread(target=_deliver_factor_reset_notice,
                     args=(row["email"], str(u["tenant_id"]), req["lands_at"],
                           cancel), daemon=True).start()
    return _recover_page(state="requested", lands_at=req["lands_at"])


@app.get("/recover/cancel", dependencies=[Depends(_not_demo_instance)])
def recover_cancel_page(token: str = ""):
    """The mailed link lands here side-effect-free (mail scanners follow
    links); the cancel itself is the explicit POST below."""
    from ..auth import factor_reset
    with _control_conn() as conn:
        row = factor_reset.lookup_cancel(conn, token)
    if row is None:
        return _recover_page(
            state="cancel_gone", status=400,
            # a third case: the link is sound but the wait it announced
            # has passed. Saying only "already done its work" leaves the
            # person holding it with a refusal and no route forward, which
            # matters because this is the door back into a locked account.
            error="This link has already done its work, or the waiting "
                  "period it was sent for has passed, or the request it "
                  "belonged to is no longer pending. If a recovery is "
                  "still running, use the link in the most recent email "
                  "we sent you — or just sign in, which cancels it.")
    return _recover_page(state="cancel_confirm", token=token,
                         lands_at=row["lands_at"], email=row["email"])


@app.post("/recover/cancel", dependencies=[Depends(limit("recover", 30, 3600)),
                                       Depends(_not_demo_instance)])
def recover_cancel(token: str = Form("")):
    from ..auth import factor_reset
    with _control_conn() as conn:
        row = factor_reset.cancel_by_token(conn, token)
    if row is None:
        return _recover_page(
            state="cancel_gone", status=400,
            # a third case: the link is sound but the wait it announced
            # has passed. Saying only "already done its work" leaves the
            # person holding it with a refusal and no route forward, which
            # matters because this is the door back into a locked account.
            error="This link has already done its work, or the waiting "
                  "period it was sent for has passed, or the request it "
                  "belonged to is no longer pending. If a recovery is "
                  "still running, use the link in the most recent email "
                  "we sent you — or just sign in, which cancels it.")
    return _recover_page(state="cancelled")


def _deliver_factor_cleared_notice(email: str) -> None:
    """After the operator clears an account's second factor: the account is
    told, so a clear the person did not ask for is not silent. NEVER
    raises; mail-less installs log the fact."""
    from . import report as _report
    smtp = _smtp_for(email)
    body = ("The second factor on your Oikonome account — authenticator, "
            "passkeys and recovery codes — was removed by the instance "
            "operator, and every signed-in session and device was signed "
            "out. If you asked for this, use Reset password to set a new "
            "password (no code is needed now), then enrol a new "
            "authenticator. If you did NOT ask for this, ")
    body += (f"write to {support_email()} straight away." if support_email()
             else "tell whoever runs this instance straight away.")
    if smtp and smtp["configured"]:
        try:
            _report.send("Your second factor was removed — Oikonome", body,
                         f"<p>{body}</p>", [email], smtp=smtp, bcc=False)
            return
        except Exception:                                # noqa: BLE001
            log.exception("second-factor-cleared notice to %s failed", email)
    log.warning("second factor cleared for %s (no mail sent)", email)


def _deliver_factor_reset_notice(email: str, tenant_id: str, lands_at,
                                 cancel_token: str) -> None:
    """Tell the account, on every channel it has, that a factor-clearing
    reset is coming and how to stop it. Email carries the cancel link;
    push and SMS carry the fact and point at the mail. NEVER raises —
    out-of-band, like every other notice; a channel that is not configured
    is simply skipped, and the mail-less fallback is the server log."""
    when = lands_at.strftime("%b %d, %Y at %H:%M UTC")
    path = f"/recover/cancel?token={cancel_token}"
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    from . import report as _report
    smtp = _smtp_for(email)
    mailed = False
    if smtp and smtp["configured"] and base:
        url = f"{base}{path}"
        plain = (f"Someone holding a password-reset link for this Oikonome "
                 f"account said they have lost the authenticator and the "
                 f"recovery codes too, and asked for a reset that removes "
                 f"them.\n\nNothing has changed yet. That reset becomes "
                 f"possible on {when}. If this was you, nothing to do — "
                 f"request a fresh reset link after that date and it will "
                 f"work without a code.\n\nIf this was NOT you, cancel it "
                 f"with one click (or simply sign in, which also cancels "
                 f"it):\n\n{url}\n"
                 + (f"\nQuestions: {support_email()}\n" if support_email() else ""))
        html = (f"<p>Someone holding a password-reset link for this Oikonome "
                f"account said they have lost the authenticator and the "
                f"recovery codes too, and asked for a reset that removes "
                f"them.</p><p><b>Nothing has changed yet.</b> That reset "
                f"becomes possible on <b>{when}</b>. If this was you, "
                f"nothing to do — request a fresh reset link after that "
                f"date and it will work without a code.</p><p>If this was "
                f"NOT you, <a href=\"{url}\">cancel it</a> with one click "
                f"(or simply sign in, which also cancels it).</p>"
                + (f"<p>Questions: {support_email()}</p>" if support_email() else ""))
        try:
            _report.send("Account recovery requested — Oikonome", plain,
                         html, [email], smtp=smtp, bcc=False)
            mailed = True
        except Exception:                # noqa: BLE001 — fall through to log
            log.exception("factor-reset notice to %s failed; logging the "
                          "cancel link instead", email)
    if not mailed:
        _log_link("factor-reset cancel link", email, path)
    short = (f"Oikonome: a reset that removes your authenticator and "
             f"recovery codes was requested and lands {when}. Not you? "
             f"Sign in, or use the cancel link in the email we sent.")
    try:
        with _control_conn() as cc:
            from ..notify import push as _push
            from ..notify import push_native as _native
            _push.send_tenant(cc, tenant_id, "Account recovery requested",
                              short, url="/app/settings")
            _native.send_tenant(cc, tenant_id, "alert")
    except Exception:                                    # noqa: BLE001
        log.warning("factor-reset push notice failed", exc_info=True)
    try:
        from ..engine import budget
        from ..notify import sms as _sms
        tconn = tenancy.tenant_connect(tenant_id)
        try:
            phone = budget.load_config(tconn).get("notify_phone")
        finally:
            tconn.close()
        # An account-security notice to a number the person verified
        # themselves; not one of the opt-in summary programs. This is the
        # channel most likely to reach someone whose inbox is the thing
        # in question, which is the whole point of telling every channel.
        if isinstance(phone, dict) and phone.get("verified") \
                and phone.get("number"):
            _sms.send(phone["number"], short)
    except Exception:                                    # noqa: BLE001
        log.warning("factor-reset SMS notice failed", exc_info=True)


# ---- signup page + email verification -----------------------------


def _verification_body(url: str) -> tuple[str, str]:
    """The confirmation mail. Super simple, the invite mail's shape: one
    line, one link, one sentence of reassurance. It deliberately does NOT
    explain what the daily verdict will be — that belongs in the app's
    setup wizard, which the person is about to walk through, not between
    them and the link. Clicking the link IS the confirmation (no button on
    the other side)."""
    plain = (f"Confirm your email address for Oikonome (link valid 7 days, "
             f"one use):\n\n{url}\n")
    html = (f"<p><a href=\"{url}\">Confirm your email address</a> for "
            f"Oikonome (link valid 7 days, one use).</p>")
    return plain, html


# How many reclaim mails one ADDRESS may receive per hour, whatever IP asks.
# The route's only throttle is `limit("signup", 5, 3600)`, which security.limit
# keys on (route, client IP) — so a distributed caller could mint an unbounded
# number of these into a chosen victim's inbox: the instance's sender
# reputation and mail allowance burnt, and a live one-hour link kept permanently fresh in an
# inbox the caller is hoping someone clicks. Every sibling mail door already
# carries this cap for the same reason (/forgot's _RESET_PER_HOUR,
# login_unlock's SEND_MAX_PER_HOUR). Counted off signup_reclaims, which
# already exists and is already keyed on the user, so it counts what was
# actually MINTED rather than what was asked for.
_RECLAIM_PER_HOUR = 3


def _reclaim_send_allowed(admin, user_id) -> bool:
    """False when this address has already been mailed its hourly quota.

    The caller must behave IDENTICALLY either way — the taken branch's 409
    is the same word for word — or the cap becomes an oracle for which
    addresses are sitting unconfirmed, the exact residual the shared 409
    exists to hide. Suppressing the mail also suppresses the MINT, so the
    link already in the inbox stays the live one instead of being burnt by
    a flood of new ones.

    The count runs in its own SAVEPOINT because the caller is inside
    signup's writing transaction and the very next statement mints the
    reclaim. A statement failure aborts the whole transaction in
    PostgreSQL, and catching it in Python does not un-abort it — so
    returning True from a bare except would have handed the caller a dead
    transaction, turning "the counter is broken, send the mail anyway" into
    a 500 on a door whose two answers must be indistinguishable. The
    savepoint is what makes fail-open actually mean fail-open.
    """
    try:
        with admin.transaction():
            n = admin.execute(
                "SELECT count(*) AS n FROM signup_reclaims "
                "WHERE user_id = %s "
                "AND created_at > now() - interval '1 hour'",
                (user_id,)).fetchone()["n"]
    except Exception:                                # noqa: BLE001
        log.warning("reclaim rate count failed; sending", exc_info=True)
        return True          # a counter failure must never block a real one
    if n >= _RECLAIM_PER_HOUR:
        log.warning("signup reclaim mail suppressed — %s already minted "
                    "for this address this hour", _RECLAIM_PER_HOUR)
        return False
    return True


def _deliver_reclaim(email: str, token: str) -> None:
    """Mail the link that finishes a signup for an address that exists
    but was never proven. The INSTANCE's mail settings only, never the
    existing row's tenant: that tenant may be the squatter's, and its
    SMTP relay would receive the very link that hands the address back.
    Same base-URL pin and log-line fallback as the reset mail. NEVER
    raises."""
    from . import spamdefense
    if spamdefense.recipient_blocked(email):
        return
    path = f"/signup/reclaim?token={token}"
    from . import report as _report
    smtp = None if DEV_MODE else _report._env_smtp()
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    if smtp and smtp["configured"] and base:
        url = f"{base}{path}"
        # The mail promises only what the door can always deliver: a page
        # that states its own act. Which act depends on what the existing
        # household holds, and that can change in the hour this link
        # lives — so the outcomes are listed, none of them is promised,
        # and the page (which re-derives the answer at the moment of the
        # click) is where the commitment is made. Every outcome named
        # here must be one the code actually performs — copy that promises
        # a road the door does not take is worse than no copy at all.
        plain = (f"Someone — probably you — started creating an Oikonome "
                 f"account for this address, which already has an "
                 f"unconfirmed one. Open this link to finish; you'll "
                 f"choose your password as part of it, and the page says "
                 f"exactly what it will do before you press anything "
                 f"(link valid 1 hour, one use):\n\n{url}\n\nWhat "
                 f"finishing does depends on what that unconfirmed "
                 f"account holds. If it holds nothing, finishing REPLACES "
                 f"it. If this address was only added to someone else's "
                 f"household, it is moved out into a new, empty household "
                 f"of your own, and that household's records stay where "
                 f"they are. And if that unconfirmed account holds "
                 f"anything at all — records, a subscription, or other "
                 f"people — the page will not act: nothing is deleted and "
                 f"nothing is handed over, and it tells you how to reach "
                 f"us instead.\n\n"
                 f"If this wasn't you, ignore this mail; nothing changes "
                 f"without the link.\n")
        html = (f"<p>Someone — probably you — started creating an Oikonome "
                f"account for this address, which already has an "
                f"unconfirmed one. <a href=\"{url}\">Finish creating your "
                f"account</a> — you'll choose your password as part of "
                f"it, and the page says exactly what it will do before "
                f"you press anything (link valid 1 hour, one use).</p>"
                f"<p>What finishing does depends on what that unconfirmed "
                f"account holds. If it holds nothing, finishing "
                f"<b>replaces</b> it. If this address was only added "
                f"to someone else's household, it is <b>moved out</b> "
                f"into a new, empty household of your own, and that "
                f"household's records stay where they are. If that "
                f"unconfirmed account holds <b>anything at all</b> — "
                f"records, a subscription, or other people — the page "
                f"will not act: nothing is deleted and nothing is handed "
                f"over, and it tells you how to reach us instead.</p><p>If this "
                f"wasn't you, ignore this mail; nothing changes without "
                f"the link.</p>")
        try:
            _report.send("Finish creating your account — Oikonome",
                         plain, html, [email], smtp=smtp, bcc=False)
            return
        except Exception:                # noqa: BLE001 — fall through to log
            log.exception("reclaim email to %s failed; logging the link",
                          email)
    _log_link("signup reclaim link", email, path)


def _deliver_referral(inviter_tenant_id) -> None:
    """Hand a redeemed referral to the installed gate to settle. NEVER
    raises: this runs on a thread on behalf of a DIFFERENT tenant than
    the one signing up, and the redemption is already recorded, so a
    failure here is a delayed thank-you the nightly sweep retries — not
    anything the new account should ever hear about."""
    try:
        ext.gate.deliver_referral(inviter_tenant_id)
        log.info("referral delivery for %s done", inviter_tenant_id)
    except Exception:                                  # noqa: BLE001
        log.exception("referral delivery thread failed for %s",
                      inviter_tenant_id)


def _deliver_verification(email: str, token: str, tenant_id) -> None:
    """Email the verification link — the _deliver_reset doctrine: tenant
    SMTP (env fallback) via resolve_smtp, links only under a PINNED
    OIKONOME_BASE_URL (never a Host-derived origin), and the
    server-log line IS the delivery on a mail-less install. NEVER raises."""
    from . import spamdefense
    if spamdefense.recipient_blocked(email):
        log.info("skipping verification email to blocked recipient %s", email)
        return
    path = f"/verify-email?token={token}"
    from . import report as _report
    smtp = None
    if not DEV_MODE:
        try:
            tc = tenancy.tenant_connect(tenant_id)
            try:
                smtp = _report.resolve_smtp(tc)
            finally:
                tc.close()
        except Exception:                        # noqa: BLE001
            smtp = None
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    if smtp and smtp["configured"] and not base:
        log.warning("SMTP is configured but OIKONOME_BASE_URL is not set — "
                    "refusing to email a Host-derived verification link")
    if smtp and smtp["configured"] and base:
        url = f"{base}{path}"
        plain, html = _verification_body(url)
        try:
            _report.send("Confirm your email — Oikonome",
                         plain, html, [email], smtp=smtp, bcc=False)
            return
        except Exception:                # noqa: BLE001 — fall through to log
            log.exception("verification email to %s failed; "
                          "logging the link instead", email)
    _log_link("email verification link", email, path)


def _recipient_invite_body(url: str, inviter: str | None) -> tuple[str, str]:
    """The invite mail — written for a person who did not ask for it.

    This is the only mail Oikonome sends to someone who has no account and
    may want nothing to do with one, so the shape is: say who is asking and
    what it is, in the first sentence; make declining as easy as accepting;
    claim nothing about them. It deliberately does NOT pitch the product —
    a person deciding whether to receive their partner's balances is not a
    lead, and treating them like one is how this becomes the kind of mail
    people report.

    The address is not enrolled by this message arriving. Nothing else is
    ever sent to it unless the link is opened."""
    who = f"{inviter} " if inviter else ""
    plain = (
        f"{who}invited you to receive their daily Oikonome summary.\n\n"
        f"It's one short email each morning: are they on budget "
        f"today, and the one number to hit if they're not. It contains "
        f"their real balances and spending, so it only starts if you say "
        f"yes.\n\n"
        f"Say yes or no here (link valid 14 days, one use):\n\n{url}\n\n"
        f"Until you do, nothing else is sent to this address. There is no "
        f"account to create, we don't track opens, and we never send "
        f"marketing.\n\n"
        f"If you don't know who this is, ignore this email — one message "
        f"is all that arrives.\n")
    inviter_html = (f"<b>{_html_escape(inviter)}</b> " if inviter else "")
    html = (
        f"<p>{inviter_html}invited you to receive their daily Oikonome "
        f"summary.</p>"
        f"<p>It's one short email each morning: "
        f"are they on budget today, and the one number to hit if they're "
        f"not. It contains their real balances and spending, so it only "
        f"starts if you say yes.</p>"
        f"<p><a href=\"{url}\">Say yes or no</a> (link valid 14 days, "
        f"one use).</p>"
        f"<p>Until you do, nothing else is sent to this address. There is "
        f"no account to create, we don't track opens, and we never send "
        f"marketing.</p>"
        f"<p style=\"color:#666\">If you don't know who this is, ignore "
        f"this email — one message is all that arrives.</p>")
    return plain, html


def _tenant_smtp(tenant_id):
    """Resolve a tenant's SMTP settings once, on one pooled connection.

    Every invite in a single save goes to the SAME tenant, so resolving
    per address buys nothing and costs one pooled checkout each."""
    if DEV_MODE:
        return None
    try:
        tc = tenancy.tenant_connect(tenant_id)
        try:
            return _report_mod().resolve_smtp(tc)
        finally:
            tc.close()
    except Exception:                            # noqa: BLE001
        return None


def _report_mod():
    from . import report as _report
    return _report


def _deliver_recipient_invite(email: str, token: str, tenant_id,
                              inviter: str | None = None,
                              smtp: dict | None = None,
                              resolved: bool = False) -> None:
    """Send one invite. Same out-of-band doctrine as _deliver_verification:
    tenant SMTP, links only under a PINNED OIKONOME_BASE_URL, the
    server-log line IS the delivery on a mail-less self-host, and it NEVER
    raises — a household must not fail to save its settings because a
    relay is down.

    A refused send is recorded in the delivery state, which is what puts
    "not arriving" on the row in Settings instead of leaving the owner
    watching an invite that never had a chance.

    `smtp`/`resolved` let a caller sending several invites for one tenant
    resolve the settings once instead of once per address — `resolved=True`
    means "this IS the answer", including when it is None."""
    from . import spamdefense
    if spamdefense.recipient_blocked(email):
        log.info("skipping recipient invite to blocked recipient %s", email)
        return
    path = f"/recipient-invite?token={token}"
    _report = _report_mod()
    if not resolved:
        smtp = _tenant_smtp(tenant_id)
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    if smtp and smtp["configured"] and not base:
        # The refusal is right — a Host-derived link in outbound mail is a
        # forgery vector — but it must not be SILENT. Silently, the address
        # keeps the "invited" badge and the owner is told the invite was
        # sent, while the only copy of the link goes to the server log; on
        # an SMTP-configured install nobody reads that log, so the person
        # could never accept and never learn why. Record it the same way a
        # refused send is recorded, so the row says "not arriving" and
        # offers Resend — which is the fix once BASE_URL is set.
        log.warning("SMTP is configured but OIKONOME_BASE_URL is not set — "
                    "refusing to email a Host-derived invite link")
        from ..notify import delivery as _delivery
        _delivery.record_failure(
            email, state="bouncing", bounce_type="Unconfigured",
            reason="OIKONOME_BASE_URL is not set, so the invitation link "
                   "could not be built — set it and use Resend.")
    if smtp and smtp["configured"] and base:
        url = f"{base}{path}"
        plain, html = _recipient_invite_body(url, inviter)
        subject = (f"{inviter} invited you to their daily Oikonome summary"
                   if inviter else
                   "You've been invited to a daily Oikonome summary")
        try:
            _report.send(subject, plain, html, [email], smtp=smtp, bcc=False)
            return
        except Exception as exc:                 # noqa: BLE001
            log.exception("recipient invite to %s failed; "
                          "logging the link instead", email)
            from ..notify import delivery as _delivery
            _delivery.record_send_failure(email, str(exc))
    _log_link("recipient invite link", email, path)


def _invite_answered_or_unknown(token: str):
    """The page for a link that did not resolve to a live invitation.

    A spent link keeps its hash (see `recipient_invites.answered`), so the
    common case — someone re-opening their own invite mail — can be told what
    they already chose. Only a genuinely unknown or expired token falls
    through to the error page. A single message for the whole branch will
    not do: it would tell a recipient who had DECLINED to ask the household
    to add them again, which `invite()` will never act on.
    """
    from fastapi.responses import HTMLResponse
    from . import todayview
    from ..notify import recipient_invites
    tpl = todayview._env.get_template("recipient_invite.html")
    prev = recipient_invites.answered(token)
    if prev is not None:
        return HTMLResponse(tpl.render(
            title="Daily email", state=prev["state"], email=prev["email"],
            again=True))
    return HTMLResponse(
        tpl.render(title="Daily email", state="unknown",
                   error="That invitation link is invalid or expired."),
        status_code=400)


def _unsubscribe_page(state: str, email: str = "", token: str = "",
                      status: int = 200):
    from fastapi.responses import HTMLResponse
    from . import todayview
    return HTMLResponse(
        todayview._env.get_template("unsubscribe.html").render(
            title="Unsubscribe", state=state, email=email, token=token),
        status_code=status)


@app.get("/unsubscribe",
         dependencies=[Depends(limit("unsubscribe", 30, 3600))])
def unsubscribe_page(token: str = ""):
    """The footer link of every household email. PEEKS ONLY: a mail scanner
    that prefetches links must not silence a person who did nothing. The
    mute is the POST below — one button on this page."""
    from ..notify import unsubscribe as _unsub
    got = _unsub.parse(token)
    if got is None:
        return _unsubscribe_page("unknown", status=400)
    return _unsubscribe_page("confirm", email=got[1], token=token)


@app.post("/unsubscribe",
          dependencies=[Depends(limit("unsubscribe", 30, 3600))])
async def unsubscribe_act(request: Request):
    """Mute the address the token names, for every household email. Two
    callers: the button on the page above (form field `token`) and a mail
    client's own Unsubscribe button — RFC 8058 one-click, which POSTs
    `List-Unsubscribe=One-Click` to the header URL, token in the query.
    Both are a person pressing a button, so both act."""
    from ..notify import unsubscribe as _unsub
    token = request.query_params.get("token", "")
    form_token = ""
    try:
        form = await request.form()
        form_token = str(form.get("token") or "")
    except Exception:                                      # noqa: BLE001
        pass
    got = _unsub.parse(form_token or token)
    if got is None:
        return _unsubscribe_page("unknown", status=400)
    tid, email = got
    if not _unsub.apply(tid, email):
        return _unsubscribe_page("failed", email=email, status=503)
    return _unsubscribe_page("done", email=email)


@app.get("/recipient-invite",
         dependencies=[Depends(limit("recipient_invite", 30, 3600))])
def recipient_invite_page(token: str = ""):
    """The invited person's landing page. PEEKS ONLY — see `lookup`: a mail
    scanner that prefetches this link must not be able to consent to
    someone's financial mail on their behalf, so both answers are the POST
    below (the same peek/act split as /verify-email)."""
    from fastapi.responses import HTMLResponse
    from . import todayview
    from ..notify import recipient_invites
    row = recipient_invites.lookup(token)
    if row is None:
        return _invite_answered_or_unknown(token)
    return HTMLResponse(
        todayview._env.get_template("recipient_invite.html").render(
            title="Daily email", state="invited", token=token,
            email=row["email"], invited_by=row["invited_by_email"]))


@app.post("/recipient-invite",
          dependencies=[Depends(limit("recipient_invite", 30, 3600))])
def recipient_invite_answer(token: str = Form(""), answer: str = Form("")):
    """Accept or decline. The two are one door because they are one
    decision, and because a decline that is harder to reach than an accept
    is not really an offer."""
    from fastapi.responses import HTMLResponse
    from . import todayview
    from ..notify import recipient_invites
    # An unrecognised answer is NOT treated as yes. Defaulting to accept
    # would mean any POST that reaches here without the button's value —
    # a replay, a rewriting proxy, a hand-rolled form — enrols somebody by
    # accident, which is the failure this whole feature exists to remove.
    # Declining is FINAL for both sides — `invite()` refuses to re-ask an
    # address that said no, deliberately, so that re-adding someone cannot
    # nag them. That makes a misclick unrecoverable by anyone: the person
    # cannot undo it and the household cannot fix it either. Accepting is
    # reversible (ask to be removed), so only this one gets a confirm step.
    if answer == "decline":
        from fastapi.responses import HTMLResponse as _HTML
        from ..notify import recipient_invites as _inv
        row = _inv.lookup(token)
        if row is None:
            return _invite_answered_or_unknown(token)
        return _HTML(todayview._env.get_template(
            "recipient_invite.html").render(
                title="Daily email", state="confirm_decline", token=token,
                email=row["email"], invited_by=row["invited_by_email"]))
    declining = answer == "decline_confirmed"
    row = None
    if answer in ("accept", "decline_confirmed"):
        row = (recipient_invites.decline(token) if declining
               else recipient_invites.accept(token))
    if row is None:
        return _invite_answered_or_unknown(token)
    if not declining:
        # someone just proved this mailbox receives mail and opened
        # a link from it — any bounce recorded against it is stale. Same
        # reasoning as /verify-email, and it matters more here: the invite
        # is the FIRST mail this address ever got from this instance, so a stale row
        # would pause the very cadence they just agreed to.
        from ..notify import delivery as _delivery
        _delivery.clear(row["email"])
    return HTMLResponse(
        todayview._env.get_template("recipient_invite.html").render(
            title="Daily email",
            state="declined" if declining else "accepted",
            email=row["email"]))


def _signup_needs_invite() -> bool:
    """Whether the signup FORM should ask for an invite code.

    The same condition /api/signup enforces, kept out of that handler so
    both read it. Left inside, opening signup (OIKONOME_OPEN_SIGNUP=1)
    lets anyone through the door while the page in front of it still says
    signup is invite-only and demands a code — and only a browser catches
    that, because the API says signup is open and only the rendered page
    disagrees."""
    return env_flag("OIKONOME_HOSTED") and not env_flag("OIKONOME_OPEN_SIGNUP")


@app.get("/signup", dependencies=[Depends(_not_demo_instance)])
def signup_page(invite: str = "", email: str = "", ref: str = ""):
    """Invite-landing page (the link `oikonome invite` prints). Hosted
    only — a self-hosted instance bootstraps through its setup link and
    stays single-household (the same doctrine /api/signup enforces). Optional
    `ref` is a referral token an installed add-on interprets."""
    from fastapi.responses import HTMLResponse, RedirectResponse
    from . import todayview
    if not env_flag("OIKONOME_HOSTED") and not DEV_MODE:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(todayview._env.get_template("signup.html").render(
        centered=True, title="Create your account", invite=invite, email=email,
        ref=ref, turnstile_key=turnstile.render_key(),
        invite_required=_signup_needs_invite()))


@app.post("/signup", dependencies=[Depends(limit("signup", 5, 3600)),
                              Depends(_not_demo_instance)])
def signup_submit(request: Request, email: str = Form(...),
                  password: str = Form(...), invite: str = Form(""),
                  ref: str = Form(""),
                  cf_token: str = Form("", alias=_TS_FIELD)):
    """Form flavor of /api/signup: same gate, same limiter bucket; errors
    re-render the form instead of raw JSON."""
    from fastapi.responses import HTMLResponse
    from . import todayview
    try:
        resp = signup(request, email=email, password=password, invite=invite,
                      ref=ref, cf_token=cf_token)
    except HTTPException as e:
        # 503 is the capacity wall and nothing else on this door: the
        # template then renders the sentence with its mail link live and
        # points at the intake link when an add-on registers one, instead
        # of a bare red line that reads as an outage.
        return HTMLResponse(
            todayview._env.get_template("signup.html").render(
                centered=True, title="Create your account", invite=invite,
                email=email, ref=ref, error=e.detail,
                at_capacity=(e.status_code == 503),
                support_email=support_email(),
                intake_open=bool(ext.gate.intake_path()),
                turnstile_key=turnstile.render_key(),
                invite_required=_signup_needs_invite()),
            status_code=e.status_code)
    resp.status_code = 303
    # every signup is a brand-new tenant — send them to the setup wizard
    # rather than straight to the dashboard, which is how an invited user
    # skips the wizard entirely.
    resp.headers["location"] = "/app/welcome"
    return resp


def _reclaim_page(token: str, error: str | None = None, email: str = "",
                  status: int = 200, road: str = "replace"):
    from fastapi.responses import HTMLResponse
    from . import todayview
    return HTMLResponse(
        todayview._env.get_template("signup_reclaim.html").render(
            centered=True, title="Finish creating your account",
            token=token, email=email, error=error, road=road,
            support_email=support_email()),
        status_code=status)


_RECLAIM_GONE = ("That link is invalid, expired, or already used — start "
                 "the signup again to get a fresh one.")

# Said only after the erasure committed and the replacement did not. The
# address is genuinely free at this point, so signing up again is a real
# road out rather than a suggestion to try the dead link once more.
_RECLAIM_HALF_DONE = (
    "The empty, unconfirmed account under this address was removed, but "
    "creating the new one failed. The address is free now — start the "
    "signup again.")


# Tables that carry tenant_id but whose rows are NOT evidence that anybody
# is using the household. Everything else counts, because the erasure this
# guards is itself discovered generically (tenancy.delete_tenant_rows walks
# every tenant_id column): a hand-written list of what to PROTECT drifts
# away from the list of what gets DESTROYED the moment a migration adds a
# table, and it drifts in the direction that loses somebody's records.
#
# Excluding rather than enumerating also fails safe. A table added later is
# counted by default, so at worst a household reads "in use" when it is not
# — which costs a wipe that nobody needed, not a household that somebody
# did. The reverse mistake is unrecoverable.
_RECLAIM_NOT_OCCUPANCY = frozenset({
    # provisioning writes this for every household ever created; it is
    # inspected on its own terms below instead
    "tenant_settings",
    # identity, not records: other PEOPLE are counted separately, and a
    # squatter's own sign-ins must never count (that is the squat)
    "users", "sessions", "device_tokens",
    # machinery the server writes on a household's behalf. A shell account
    # accumulates worker runs, sync attempts, snapshots and its per-tenant
    # key while nobody touches it, so none of it says the account is used.
    "job_runs", "job_progress", "sync_log", "script_heartbeats",
    "alerts_log", "tenant_keys",
    "budget_snapshots", "networth_snapshot", "categorizer_model",
}) | ext.gate.machinery_tables()

# The settings keys provisioning itself writes. Anything else in the config
# is the budget somebody built — manual assets, savings goals, envelopes,
# category overrides — and lives nowhere else.
_RECLAIM_PROVISIONED_SETTINGS = frozenset({"today_view"})


def _reclaim_occupied_table(admin, tenant_id) -> str | None:
    """The name of the first tenant-scoped table holding a row for this
    household, or None when every one of them is empty. Discovered the same
    way the erasure discovers what it deletes."""
    from psycopg import sql
    rows = admin.execute(
        """SELECT DISTINCT table_name FROM information_schema.columns
           WHERE table_schema='public' AND column_name='tenant_id'"""
    ).fetchall()
    names = sorted({r["table_name"] for r in rows} - _RECLAIM_NOT_OCCUPANCY)
    if not names:
        return None
    probe = sql.SQL(" UNION ALL ").join(
        sql.SQL("SELECT {name} AS t WHERE EXISTS "
                "(SELECT 1 FROM {tbl} WHERE tenant_id = %s)").format(
                    name=sql.Literal(n), tbl=sql.Identifier(n))
        for n in names)
    row = admin.execute(
        sql.SQL(" ").join([probe, sql.SQL("LIMIT 1")]),
        [tenant_id] * len(names)).fetchone()
    return row["t"] if row else None


def _reclaim_holdings(admin, tenant_id, user_id) -> dict:
    """What the address's existing, never-confirmed household HOLDS.

    "Unverified" is not evidence of emptiness. Hosted signup returns a
    session immediately, so an account whose owner never opened the
    verification mail — spam folder, deferred, read on a phone that lost
    the tab — is otherwise a fully working household: banks linked, years
    of transactions, receipts, a hand-built budget, an invited partner.
    The reclaim below erases a tenant and releases what it holds at
    external services, which no backup restores, so the door has to be
    able to tell that household apart from the empty shell a squatter
    parks on an address.

    Returns {table, settings, billing, people}: which table holds a row,
    whether the budget config has been touched, whether money is attached,
    and how many OTHER people are in the household (an invited spouse or
    viewer, with their own password, authenticator and passkeys, whom the
    erasure's cascade would delete along with everything else).
    """
    # whether money is attached to the household is the installed gate's
    # to know about
    attached = ext.gate.money_attached(admin, tenant_id)
    cfg = admin.execute(
        "SELECT config FROM tenant_settings WHERE tenant_id = %s",
        (tenant_id,)).fetchone()
    config = (cfg or {}).get("config")
    if isinstance(config, (str, bytes)):
        import json as _json
        try:
            config = _json.loads(config)
        except ValueError:
            config = {}
    keys = set(config) if isinstance(config, dict) else set()
    people = admin.execute(
        "SELECT count(*) AS n FROM users WHERE tenant_id = %s AND id <> %s",
        (tenant_id, user_id)).fetchone()["n"]
    return {"table": _reclaim_occupied_table(admin, tenant_id),
            "settings": bool(keys - _RECLAIM_PROVISIONED_SETTINGS),
            "billing": attached,
            "people": people}


def _reclaim_household_in_use(h: dict) -> bool:
    """True when that household is somebody's real records, not a shell.

    Rows, money and other people are the test. A second factor deliberately
    is NOT: enrolling one on an address you do not own is precisely how a
    squat is held — it is the reason this whole door exists — so counting it
    as occupancy would hand every squatter a permanent claim on an address.
    A brand-new household has none of these (provisioning writes the tenant,
    one settings row with one key, and whatever standing row the installed
    gate opens), so an account that has genuinely never been used still
    reads as empty.

    This question is asked ONLY about a household the click would erase —
    i.e. of an owner. It says nothing about whether a MEMBER row may move
    out of somebody else's household, and must never be used to decide
    that: see `_reclaim_road`.
    """
    return bool(h.get("table") or h.get("settings") or h.get("billing")
                or h.get("people"))


def _reclaim_road(admin, r) -> str:
    """Which of the three answers this token leads to. The confirm page
    renders it and the POST performs it off this one function, so the page
    can never promise a different act than the button takes.

    "replace"  — the address OWNS a household that holds nothing. It is
                 erased and a fresh, already-verified account takes its
                 place.

    "move_out" — the address is a MEMBER row inside somebody else's
                 household. It is moved out into a household of its own,
                 with its planted credentials burnt. Occupancy has no say
                 here and must not be consulted: the move-out erases
                 nothing — the other household keeps every row it has, and
                 the person moved out is exactly the person who just proved
                 the mailbox. Gating this on occupancy would be a takeover
                 in its own right: anyone can mint a family invite and claim
                 it as victim@example.com (`/api/invite/claim` asks for no
                 mailbox proof), and then the attacker's own transactions
                 make the household "in use", so the door would hand the
                 victim a password reset INSIDE the attacker's tenant —
                 the victim's future bank links and transactions landing
                 where the attacker still has an owner session.

    "blocked"  — the address owns a household that holds ANYTHING, or has
                 any other user in it. Refused: nothing is erased, nothing
                 is handed over, and the person is told to write to the operator.

    There is deliberately no fourth answer between the last two. Handing
    an owner alone in a household that holds records a password reset
    against that same account — on the reasoning that the clicker proved
    the mailbox and the account never did — hides a takeover in every
    corner, not least because "never proved a mailbox" would be read off
    `verified_at`, which a hosted email change clears on a fully verified
    household. An in-use household gets one answer, and it is no.
    """
    if (r["role"] or "owner") != "owner":
        return "move_out"
    h = _reclaim_holdings(admin, r["tenant_id"], r["user_id"])
    return "replace" if not _reclaim_household_in_use(h) else "blocked"


@app.get("/signup/reclaim", dependencies=[Depends(limit("reclaim", 30, 3600)),
                                          Depends(_not_demo_instance)])
def signup_reclaim_page(token: str = ""):
    """The emailed link lands on a confirm page; the POST behind its one
    button is what spends the token. A GET must not perform the reclaim:
    a mail scanner prefetching the link would wipe the earlier account
    and open a session in the scanner's hands (the /reset doctrine).

    The page also says WHICH road the button takes — replace an empty
    shell, move a member row out of a stranger's household, or refuse
    outright — so nobody presses a button expecting a different one."""
    from ..auth import signup_reclaim
    admin = tenancy.admin_connect()
    try:
        r = signup_reclaim.lookup(admin, token)
        road = _reclaim_road(admin, r) if r is not None else "replace"
    finally:
        admin.close()
    if r is None or r["verified_at"] is not None:
        return _reclaim_page(token, error=_RECLAIM_GONE, status=400)
    return _reclaim_page(token, email=r["email"], road=road)


@app.post("/signup/reclaim", dependencies=[Depends(limit("reclaim", 30, 3600)),
                                           Depends(_not_demo_instance)])
def signup_reclaim_finish(request: Request, token: str = Form(""),
                          password: str = Form("")):
    """Spend the token and perform the reclaim the mailbox just
    authorised. Under the per-email signup lock and the per-tenant
    member lock (the order invites.claim uses), so a concurrent signup or
    claim cannot interleave. `_reclaim_road` names the act; the three are
    described there and each one is performed below.

    An unverified OWNER of an EMPTY household has its tenant wiped whole,
    on the same contract as the other erasure doors — invites, tokens,
    push subscriptions, external holdings released and recorded. An
    unverified MEMBER of someone else's household is moved out into a
    fresh tenant with its persistence burnt, leaving that household
    untouched. In both the new account is created already verified: the
    click is that proof, and the password is the CLICKER's — never the one
    parked with the form, which may have been typed by someone who does
    not own the mailbox.

    The WIPE is reserved for a household that holds NOTHING. An address can
    be unverified and still be someone's real records (the mail went to
    spam and the app kept working), and the wipe releases what the
    household holds at external services before it drops the rows, so no backup
    brings such a household back. For a household that holds ANYTHING this
    door does nothing at all and says so. The clever alternative — handing
    the clicker a password reset against that account — is an account
    takeover in several different disguises. There is no automated answer
    for an in-use household; there is a human one."""
    from ..auth import signup_reclaim
    admin = tenancy.admin_connect()
    # SESSION-level locks, not transaction-scoped: this door commits more
    # than once (the wipe, then the spend-and-provision) and the exclusion
    # has to span all of them. Same keys and the same email-then-tenant
    # order every other door uses. Released in the finally below.
    locked: list[str] = []

    def _lock(key: str) -> None:
        admin.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
        locked.append(key)

    try:
        with admin.transaction():
            r = signup_reclaim.lookup(admin, token)
            if r is None:
                return _reclaim_page(token, error=_RECLAIM_GONE, status=400)
            email = r["email"]
        _lock("oikonome:signup:" + email)
        with admin.transaction():
            # re-read under the lock: the row may have been verified by
            # its owner since, in which case a reclaim would be the
            # takeover this whole door exists to prevent
            r = signup_reclaim.lookup(admin, token)
            if r is None or r["verified_at"] is not None:
                return _reclaim_page(token, error=_RECLAIM_GONE, status=400)
        uid, old_tid = r["user_id"], str(r["tenant_id"])
        _lock("oikonome:tenant-members:" + old_tid)
        is_owner = (r["role"] or "owner") == "owner"
        # Decided FIRST, under both locks and before the token is spent, so
        # nothing irreversible has happened yet when the answer is "not
        # this door". Re-derived here rather than trusted from the confirm
        # page: the household can have gained a row, attached money or a
        # second person since the GET rendered.
        road = _reclaim_road(admin, r)
        if road == "blocked":
            # Refused, and the token is deliberately NOT spent: nothing
            # happened, so the link stays as valid (and as inert) as it was.
            log.info("signup reclaim: the household on this address is in "
                     "use — refusing")
            return _reclaim_page(token, email=email, road="blocked",
                                 status=409)
        # Both remaining roads create an account with this password, so it
        # is validated for both.
        err = passwords.password_error(password)
        if err:
            return _reclaim_page(token, email=email, error=err, status=400)
        wiped = False
        if is_owner:
            # Spending here, before the wipe, is forced: signup_reclaims
            # cascades off users, so the wipe below deletes this very row
            # and a spend afterwards would find nothing to burn. Committed
            # on its own so a twin request cannot re-enter the erasure.
            with admin.transaction():
                if not signup_reclaim.spend(admin, r["id"]):
                    return _reclaim_page(token, error=_RECLAIM_GONE,
                                         status=400)
            # the lock the other erasure doors take: the domain tables
            # have no FK to tenants, so a sync in flight would happily
            # upsert rows for a tenant whose erasure already committed
            _lock("oikonome:sync:" + old_tid)
            # The wipe is the point of no return and it stands ALONE, on
            # the autocommit connection — never inside the provisioning
            # transaction. Releasing what the household holds at external
            # services cannot be rolled back, so a later failure in
            # provisioning must not revert the DB half of the wipe (nor,
            # worse, the pending-release marker that is the retry handle
            # for a release that did not land). The self-serve delete
            # door commits in the same order for the same reason.
            from .. import erasure as _erasure
            try:
                released = _erasure.release_external(old_tid)
                retry_ids = _erasure.collect_retry_ids(old_tid)
                _erasure.record_pending_release(admin, old_tid, released,
                                                retry_ids)
            except Exception:                            # noqa: BLE001
                log.warning("signup reclaim: external release of tenant %s "
                            "failed", old_tid, exc_info=True)
            tenancy.delete_tenant_rows(admin, old_tid)
            wiped = True
        try:
            with admin.transaction():
                inv = (signup_invites.lookup(admin, r["invite"], email)
                       if r["invite"] else None)

                def before_user():
                    if is_owner:
                        return                # already wiped, above
                    for sql in (
                            "DELETE FROM sessions WHERE user_id=%s",
                            "DELETE FROM passkeys WHERE user_id=%s",
                            "UPDATE device_tokens SET revoked_at=now() "
                            "WHERE user_id=%s AND revoked_at IS NULL",
                            "DELETE FROM webauthn_challenges "
                            "WHERE user_id=%s",
                            "DELETE FROM recovery_codes WHERE user_id=%s",
                            "DELETE FROM push_subscriptions WHERE user_id=%s",
                            "UPDATE api_tokens SET revoked_at=now() "
                            "WHERE created_by=%s AND revoked_at IS NULL",
                            "DELETE FROM invites WHERE created_by=%s "
                            "AND used_at IS NULL",
                            "UPDATE password_resets SET used_at=now() "
                            "WHERE user_id=%s AND used_at IS NULL",
                            "UPDATE login_unlocks SET consumed_at=now() "
                            "WHERE user_id=%s AND consumed_at IS NULL",
                            "UPDATE email_verifications SET used_at=now() "
                            "WHERE user_id=%s AND used_at IS NULL"):
                        admin.execute(sql, (uid,))

                if not is_owner and not signup_reclaim.spend(admin, r["id"]):
                    # Nothing irreversible happens on the member road — the
                    # stranger's household is untouched and the move-out
                    # rolls back with this transaction — so the burn rides
                    # WITH the provisioning instead of ahead of it. A crash
                    # in between then leaves the link live rather than
                    # leaving the person with neither household nor link.
                    return _reclaim_page(token, error=_RECLAIM_GONE,
                                         status=400)
                new_uid, tid, owed_to, _ = _provision_household(
                    admin, email=email,
                    password_hash=passwords.hash_password(password),
                    hosted=True, inv=inv, ref=None,
                    move_uid=None if is_owner else uid, verified=True,
                    before_user=before_user)
        except Exception:                                # noqa: BLE001
            if not wiped:
                raise
            # The erasure committed and the replacement did not. The
            # address is free now, so the way out is a fresh signup — say
            # that, rather than letting a bare 500 leave someone believing
            # their account is in limbo.
            log.exception("signup reclaim: provisioning failed after the "
                          "wipe of tenant %s", old_tid)
            return _reclaim_page("", error=_RECLAIM_HALF_DONE, status=500)
    finally:
        for key in locked:
            try:
                admin.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                              (key,))
            except Exception:                            # noqa: BLE001
                log.warning("signup reclaim: could not release %s", key)
        admin.close()
    _audit_control("signup_reclaim", email)
    # the other door a household can be born at: the signup that parked
    # this token answered 409 and notified nobody
    _operator_signup_notice(email, str(tid), reclaimed=True)
    resp = _login_response(new_uid, tid, request=request)
    resp.status_code = 303
    resp.headers["location"] = "/app/welcome"
    return resp


def _audit_control(action: str, target: str) -> None:
    """A control-plane audit line, best-effort."""
    try:
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO admin_audit (action, target, detail, ip) "
                "VALUES (%s, %s, NULL, NULL)", (action, target))
        finally:
            admin.close()
    except Exception:                                    # noqa: BLE001
        log.warning("audit line %s failed", action, exc_info=True)


def _notify_operator_signup(email: str, tenant_id: str,
                            reclaimed: bool = False) -> None:
    """Email the operator (OIKONOME_OPERATOR_EMAIL) that a household just
    signed up, with the address. Same delivery doctrine as every other
    operator notice: env SMTP, NEVER raises, runs out-of-band. There is no action
    link, so this one does not need OIKONOME_BASE_URL — the operator acts
    from the admin console, which is not reachable at the public base URL
    anyway.

    `reclaimed` marks the second door: an address parked as unverified,
    then claimed by whoever proved the mailbox. The operator sees two
    notices for that address on purpose — they are two different people,
    and the second one wiped or moved out of the first one's household."""
    operator = os.environ.get("OIKONOME_OPERATOR_EMAIL", "").strip()
    from . import report as _report
    smtp = _report._env_smtp()
    what = "reclaimed signup" if reclaimed else "signup"
    if operator and smtp["configured"]:
        # the address is UNAUTH attacker input landing in the operator's
        # mailbox — escape it before it touches HTML, exactly as every
        # other operator notice does (the plain part is inert text)
        import html as _html
        e_esc = _html.escape(email)
        plain = (f"New {what}:\n\n  {email}\n\nHousehold: {tenant_id}\n\n"
                 f"Manage it from the admin console.")
        html = (f"<p>New {what}:</p><p><b>{e_esc}</b></p>"
                f"<p>Household: <code>{tenant_id}</code></p>"
                f"<p>Manage it from the admin console.</p>")
        try:
            _report.send(f"New {what} — Oikonome", plain, html, [operator],
                         smtp=smtp, bcc=False)
            return
        except Exception:                # noqa: BLE001 — fall through to log
            log.exception("operator signup notice failed; logging instead")
    # the mail-less fallback, like every other operator notice: record that
    # the signup happened where the operator can still find it. Carries an
    # address, so it stays out of the feedback bundle.
    log.info("new %s: %s (household %s) — operator NOT emailed", what, email,
             tenant_id, extra={"no_capture": True})


def operator_notice_muted(email: str) -> bool:
    """Whether this signup address is the operator's own automation.

    An instance that is walked by a test harness mints a household on every
    run, and each one mails the operator about a person who does not exist.
    The notice is worth having, so the answer is not to drop it but to let
    the operator name the addresses that are theirs:
    OIKONOME_OPERATOR_NOTICE_SKIP holds comma-separated shell globs matched
    against the whole address (`bot+*@example.com`, `*@test.invalid`).
    Empty — the default, and every self-host — mutes nothing."""
    pats = [p.strip().lower()
            for p in os.environ.get("OIKONOME_OPERATOR_NOTICE_SKIP", "").split(",")
            if p.strip()]
    if not pats:
        return False
    import fnmatch
    addr = (email or "").strip().lower()
    return any(fnmatch.fnmatch(addr, p) for p in pats)


def _operator_signup_notice(email: str, tenant_id: str,
                            reclaimed: bool = False) -> None:
    """Fire the notice off the request — one SMTP round trip must not sit
    in the path of a new household's first page load, and a relay that is
    down must not fail the signup. No operator address configured means no
    notice and no thread (self-host, and every tenant the suite mints)."""
    if not os.environ.get("OIKONOME_OPERATOR_EMAIL", "").strip():
        return
    if operator_notice_muted(email):
        return
    import threading
    threading.Thread(target=_notify_operator_signup,
                     args=(email, tenant_id, reclaimed), daemon=True).start()


def _deliver_invite(email: str, signup_path: str) -> None:
    """Email a freshly minted invite to the requester. Same doctrine as
    every other mail; log line is the fallback delivery."""
    from . import spamdefense
    if spamdefense.recipient_blocked(email):
        log.info("skipping invite email to blocked recipient %s", email)
        return
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    from . import report as _report
    smtp = _report._env_smtp()
    if smtp["configured"] and base:
        url = f"{base}{signup_path}"
        plain = (f"You're in! Create your Oikonome account here (link valid "
                 f"14 days, one use):\n\n{url}")
        html = (f"<p>You're in! <a href=\"{url}\">Create your Oikonome "
                f"account</a> (link valid 14 days, one use).</p>")
        try:
            _report.send("Your Oikonome invite", plain, html, [email],
                         smtp=smtp, bcc=False)
            return
        except Exception:                # noqa: BLE001
            log.exception("invite email to %s failed; logging", email)
    _log_link("signup invite", email, signup_path)


def _verify_email_act(token: str):
    """Spend the verification link and say so. Shared by the emailed GET
    and the POST (older mails and no-JS forms).

    Verification is the one emailed action that is SAFE to spend on a
    GET, unlike /reset and /unlock: all it proves is that the mailbox
    receives this instance's mail, and a mail scanner prefetching the link proves the
    same thing. What must never happen is the human being told the
    link was "already used" — so a spent-but-unexpired link for a
    verified user renders the same confirmed page as a fresh one. The
    page then forwards itself to sign-in (Refresh header: the strict CSP
    allows no inline script) so a click in the mail is the whole task."""
    from fastapi.responses import HTMLResponse
    from . import todayview
    with _control_conn() as conn:
        row = email_verify.lookup(conn, token)
        spent = None if row else email_verify.lookup_spent(conn, token)
    ok = False
    if row is not None:
        admin = tenancy.admin_connect()
        try:
            ok = email_verify.consume(admin, row["id"], row["user_id"])
        finally:
            admin.close()
        if not ok:                                # lost a race to a twin GET
            with _control_conn() as conn:
                spent = email_verify.lookup_spent(conn, token)
    if not ok and not (spent and spent["verified"]):
        return HTMLResponse(
            todayview._env.get_template("login.html").render(
                centered=True, title="Verify email",
                error="That verification link is invalid, expired, or "
                      "already used — sign in and use “Resend” to get a "
                      "fresh one."),
            status_code=400)
    email = (row or spent)["email"]
    if ok:
        # someone just proved this mailbox receives mail, so any bounce
        # recorded against it is stale — clear it, and lift the
        # provider's own suppression too. Without the second half a
        # corrected address stays dead upstream however clean the local table is.
        from ..notify import delivery as _delivery
        _delivery.clear(email)
    return HTMLResponse(
        todayview._env.get_template("unlock.html").render(
            centered=True, title="Email confirmed", confirmed=True,
            email=email),
        headers={"Refresh": "4; url=/login?verified=1"})


@app.get("/verify-email", dependencies=[Depends(limit("verify_email", 30, 3600))])
def verify_email(token: str = ""):
    """The emailed link: verifies on the click — no confirm button.
    Burns the token and stamps users.verified_at on the ADMIN
    connection — the app role deliberately cannot write verified_at
    (column scope). See _verify_email_act for why a GET may spend this
    particular token."""
    return _verify_email_act(token)


@app.post("/verify-email",
          dependencies=[Depends(limit("verify_email", 30, 3600))])
def verify_email_confirm(token: str = Form("")):
    return _verify_email_act(token)


@app.post("/api/verify-email/resend",
          dependencies=[Depends(limit("verify_resend", 3, 3600))])
def verify_email_resend(user: dict = Depends(current_user)):
    with _control_conn() as conn:
        u = conn.execute("SELECT verified_at FROM users WHERE id=%s",
                         (user["user_id"],)).fetchone()
        if u and u["verified_at"]:
            return {"ok": True, "verified": True}
        token = email_verify.create(conn, user["user_id"])
    import threading
    threading.Thread(target=_deliver_verification,
                     args=(user["email"], token, user["tenant_id"]),
                     daemon=True).start()
    return {"ok": True, "verified": False}


# A classical garnish under the functional message —
# οἰκονόμος is a Greek word, so the error pages wink in Greek. Every quote
# is genuinely classical with a checkable source (nothing apocryphal: the
# famous one-liner "I know that I know nothing" is NOT ancient in that
# form, so the Apology's actual wording is used instead). Greek first,
# translation beneath; picked at render time.
_ERROR_QUOTES = {
    404: [  # pages that moved on, hid, wandered, or could not be reached
        ("πάντα χωρεῖ καὶ οὐδὲν μένει",
         "Everything flows and nothing stays.",
         "Heraclitus (Plato, Cratylus 402a)"),
        ("φύσις κρύπτεσθαι φιλεῖ",
         "Nature loves to hide.",
         "Heraclitus, DK B123"),
        ("ἐὰν μὴ ἔλπηται ἀνέλπιστον, οὐκ ἐξευρήσει",
         "If you do not expect the unexpected, you will not find it.",
         "Heraclitus, DK B18"),
        ("ψυχῆς πείρατα ἰὼν οὐκ ἂν ἐξεύροιο, πᾶσαν ἐπιπορευόμενος ὁδόν",
         "You could travel every road and never find the limits of the "
         "soul.",
         "Heraclitus, DK B45"),
        ("ἄνδρα μοι ἔννεπε, Μοῦσα, πολύτροπον, ὃς μάλα πολλὰ πλάγχθη",
         "Sing to me, Muse, of the man of many turns, who wandered far "
         "and wide.",
         "Homer, Odyssey 1.1"),
        ("Οὖτις ἐμοί γ᾽ ὄνομα",
         "Nobody — that is my name.",
         "Odysseus to the Cyclops — Homer, Odyssey 9.366"),
        ("οὐ μὰν ἐκλελάθοντ᾽, ἀλλ᾽ οὐκ ἐδύναντ᾽ ἐπίκεσθαι",
         "Like the sweet apple high on the topmost bough: not forgotten — "
         "the pickers could not reach it.",
         "Sappho, fr. 105a"),
        ("ἄνθρωπον ζητῶ",
         "I am looking for a human being.",
         "Diogenes, lantern in hand (Diogenes Laertius 6.41)"),
    ],
    429: [  # asked to slow down, not turned away
        ("μηδὲν ἄγαν",
         "Nothing in excess.",
         "Delphic maxim (Plato, Protagoras 343b)"),
        ("σπεῦδε βραδέως",
         "Make haste slowly.",
         "Augustus, in Suetonius, Divus Augustus 25"),
    ],
    500: [  # Stoic composure while the operator reads the logs
        ("ταράσσει τοὺς ἀνθρώπους οὐ τὰ πράγματα, ἀλλὰ τὰ περὶ τῶν "
         "πραγμάτων δόγματα",
         "It is not events that disturb people, but their judgments "
         "about them.",
         "Epictetus, Enchiridion 5"),
        ("μὴ ζήτει τὰ γινόμενα γίνεσθαι ὡς θέλεις, ἀλλὰ θέλε τὰ γινόμενα "
         "ὡς γίνεται",
         "Do not demand that things happen as you wish; wish them to "
         "happen as they do.",
         "Epictetus, Enchiridion 8"),
        ("τὰ δέ μοι παθήματα ἐόντα ἀχάριτα μαθήματα γέγονε",
         "My sufferings, unwelcome as they were, have become my lessons.",
         "Croesus, in Herodotus, Histories 1.207"),
        ("ἆρον τὴν ὑπόληψιν, ἦρται τὸ βέβλαμμαι",
         "Take away the judgment “I am harmed,” and the harm "
         "is taken away.",
         "Marcus Aurelius, Meditations 4.7"),
    ],
}


def _error_page(status: int, heading: str, message: str,
                headers: dict | None = None):
    """Branded dark-theme error page (templates/error.html on the same
    Jinja shell as login/signup — the base brand header included)."""
    import random

    from fastapi.responses import HTMLResponse

    from . import todayview
    return HTMLResponse(
        todayview._env.get_template("error.html").render(
            title=heading, heading=heading, message=message,
            quote=random.choice(_ERROR_QUOTES[status])
                  if status in _ERROR_QUOTES else None),
        status_code=status, headers=headers)


# Registered for STARLETTE's HTTPException on purpose: the router's own
# 404 for an unknown path raises the starlette class directly, which a
# fastapi-subclass registration does not match — so a bad browser URL
# would reach FastAPI's raw {"detail": "Not Found"} JSON.
@app.exception_handler(StarletteHTTPException)
async def _auth_redirect(request: Request, exc: StarletteHTTPException):
    """Page routes bounce anonymous users to /login instead of raw JSON
    and render branded 404s; API routes keep JSON errors — the SPA and
    integrations parse them."""
    from fastapi.responses import JSONResponse, RedirectResponse
    page = not request.url.path.startswith("/api")
    if (exc.status_code == 401 and page
            and not request.url.path.startswith("/login")):
        return RedirectResponse("/login", status_code=303)
    if exc.status_code == 404 and page:
        return _error_page(404, "404",
                           "There's nothing at this address — the link may "
                           "be stale or mistyped.")
    if exc.status_code == 429 and page:
        # the limiter is a dependency, so it fires BEFORE a form route's own
        # error re-render — without this a person who signs up too often
        # from one connection reads raw JSON
        return _error_page(429, "Slow down",
                           "Too many attempts from your connection in a short "
                           "time — wait a few minutes and try again.",
                           headers={"Retry-After": "300"})
    body = {"detail": exc.detail}
    if isinstance(exc.detail, dict):
        # A structured refusal (elevation_required) is keyed on by three
        # clients written against "the JSON has `error`"; FastAPI nests
        # detail, so mirror the fields at the top level and nobody has to
        # guess which spelling the server chose.
        body = {**exc.detail, "detail": exc.detail}
    return JSONResponse(body, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


# load shed: a starved pool (control or tenant) means the DB
# connection budget is exhausted — every further request would only queue
# and then 500. Answer 503 + Retry-After instead: an honest brown-out
# signal that clients can back off on and that an edge proxy's maintenance
# page can key on (those trigger on 502/503/504, never on "slow", so a
# brown-out serving raw 500s would sail past one).
from psycopg_pool import PoolTimeout  # noqa: E402
from psycopg.errors import InvalidTextRepresentation  # noqa: E402


@app.exception_handler(PoolTimeout)
async def _pool_starved(request: Request, exc: PoolTimeout):
    from fastapi.responses import JSONResponse
    if request.url.path.startswith("/api"):
        return JSONResponse(
            {"detail": "server busy — try again shortly"},
            status_code=503, headers={"Retry-After": "15"})
    return _error_page(503, "Catching our breath",
                       "The server is briefly over capacity. Wait a few "
                       "seconds and reload.",
                       headers={"Retry-After": "15"})


@app.exception_handler(InvalidTextRepresentation)
async def _bad_literal(request: Request, exc: InvalidTextRepresentation):
    """A path or query id Postgres cannot even cast (sqlstate 22P02 —
    almost always a uuid column fed a non-uuid string) identifies nothing:
    that is the caller's 4xx, not a server 500. Only a handful of routes coerce
    ids by hand (_require_record_id); without this every other uuid-taking
    route surfaces a bare 500 on '/api/thing/not-a-uuid'. One handler covers
    the class; a uuid miss reads as 404 (the id names no record), any
    other bad literal as 400."""
    if request.url.path.startswith("/api"):
        from fastapi.responses import JSONResponse
        if "uuid" in str(exc):
            return JSONResponse({"detail": "no such record"}, status_code=404)
        return JSONResponse({"detail": "invalid value"}, status_code=400)
    return _error_page(404, "Not found",
                       "That link doesn't point at anything here.")


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    """Browser routes render a branded 500 instead of Starlette's bare
    text. ServerErrorMiddleware re-raises after this response is sent, so
    the traceback still reaches the logs — the page's "it's been logged"
    stays true. API routes keep the plain-text body clients already treat
    as opaque."""
    if request.url.path.startswith("/api"):
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("Internal Server Error", status_code=500)
    return _error_page(500, "Something went wrong",
                       "The error has been logged. Try again in a moment — "
                       "if it keeps happening, tell the operator.")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/.well-known/assetlinks.json")
def assetlinks():
    """Digital Asset Links for the Android app. Android fetches this
    anonymously from whatever domain the app signs in against, so it is
    unauthenticated, tenant-free, and served on EVERY install — the app
    is the same package whether the server is hosted or self-hosted.
    The statement only names public cert fingerprints; it grants nothing
    by itself (passkey login still runs the full WebAuthn verify)."""
    from ..auth import passkeys
    return passkeys.android_assetlinks()


@app.get("/.well-known/apple-app-site-association")
def apple_app_site_association():
    """The iOS half of what ``assetlinks`` is for Android: Apple fetches
    this anonymously (through its own CDN) from every domain the app
    lists in its associated-domains entitlement, and only then offers
    that domain's passkeys inside the app. No extension, JSON body,
    unauthenticated, tenant-free — Apple's rules."""
    from ..auth import passkeys
    return passkeys.apple_app_site_association()


@app.get("/api/access")
def access_info():
    """How a person WITHOUT an account gets one on this instance — read by
    the native app's sign-in screen (and anyone) before any login, so it
    carries no secrets and no per-instance detail beyond the public doors:
    self-service signup (OIKONOME_OPEN_SIGNUP, the open-signup switch), an
    intake form an installed add-on registers, or neither — in which case
    the operator's site is the door. Self-host has no signup door at all
    (accounts are made by the operator's setup link / invites)."""
    hosted = env_flag("OIKONOME_HOSTED")
    # the demo box closes every account-creating door with a 404
    # (_not_demo_instance); advertising one here would send the native
    # sign-in screen's "Create one" at a door that does not exist
    demo = env_flag("OIKONOME_DEMO")
    signup = hosted and not demo and env_flag("OIKONOME_OPEN_SIGNUP")
    intake = ext.gate.intake_path() if hosted and not demo else None
    return {"hosted": hosted,
            "signup_open": signup,
            "request_access_open": bool(intake),
            "signup_path": "/signup" if signup else None,
            "request_access_path": intake,
            # the operator's own site, when they publish one
            "site_url": site_url() if hosted else None,
            # where a locked-out person can write; the sign-in screens'
            # recovery prompts name it next to the self-serve door
            "support_email": (support_email() or None) if hosted else None}


@app.get("/readyz")
def readyz():
    try:
        with _control_conn() as conn:
            conn.execute("SELECT 1")
            n = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()
        return {"ok": True, "migrations": n["n"]}
    except Exception as e:               # noqa: BLE001 — readiness must not 500
        return Response(f'{{"ok": false, "error": "{type(e).__name__}"}}',
                        status_code=503, media_type="application/json")


# one generic message for EVERY invite failure mode (absent,
# bogus, expired, burned, bound to a different email) — a probe learns
# nothing about which check failed, or whether the address has an account
INVITE_MSG = ("signup is by invitation — check your invite link, or ask "
              "for a new one")

# One answer for every taken address, verified or not. It has to read
# correctly in both worlds: for the owner of a live account it says sign
# in; for someone whose earlier signup was never confirmed — or whose
# address someone else registered and abandoned — it points at the mail
# that has just been sent. Saying which case it is would tell a prober
# whether the address is sitting unconfirmed, which is exactly the
# pretext a phishing copy of that mail would need.
TAKEN_MSG = ("this address already has an account — sign in, or if you "
             "never confirmed it, check your email for a link to finish "
             "creating it")

# Where a person who is locked out, turned away or confused can write. No
# default: an operator names their inbox in OIKONOME_SUPPORT_EMAIL, and an
# instance with none configured simply names no address anywhere.
SUPPORT_EMAIL_DEFAULT = ""


def support_email() -> str:
    return (os.environ.get("OIKONOME_SUPPORT_EMAIL") or "").strip() \
        or SUPPORT_EMAIL_DEFAULT


def site_url() -> str | None:
    """The operator's public site, if they publish one — the sign-in page's
    "No account?" door when neither signup nor an intake form is open."""
    return (os.environ.get("OIKONOME_SITE_URL") or "").strip() or None


def privacy_url() -> str | None:
    """The operator's privacy policy, linked from the sign-in page when set."""
    return (os.environ.get("OIKONOME_PRIVACY_URL") or "").strip() or None


def capacity_msg() -> str:
    """What a signup hears when the instance's account ceiling is reached.

    "At capacity" on its own reads as a broken product, so the installed
    gate writes the sentence: that the ceiling is temporary and where to
    write. Asked on every call, never cached and never a number written
    here, because the operator raises the cap by changing the
    environment, not by shipping code."""
    return ext.gate.capacity_message()

# How intake is counted: registered accounts PLUS the live unclaimed
# invites, which are seats already promised. Both mint doors count the same
# way, so a signup and a mint can never both pass on one remaining seat.
_INTAKE_LOCK = "oikonome:intake-mint"


def _intake_seats_taken(admin) -> int:
    promised = admin.execute(
        "SELECT count(*) AS n FROM signup_invites "
        "WHERE used_at IS NULL AND expires_at > now()").fetchone()["n"]
    return ext.gate.accounts_used(admin) + promised


def _intake_full_advisory(admin, email: str) -> bool:
    """A cheap, LOCK-FREE read of the intake ceiling, for refusing before
    the expensive part of a signup.

    Deliberately approximate: it is here so an instance at its ceiling
    answers without paying for an argon2id hash (~100 ms, 64 MiB), not to
    decide anything. The binding decision is `_intake_check_exact` below,
    which runs under the instance-wide lock inside the writing transaction.

    An address that already has an account is never refused here — that
    signup mints no tenant, it parks a reclaim, and the reclaim door is
    exempt from the cap for the same reason invited signups are: it hands
    an address back to whoever already had it rather than taking a seat.
    """
    if admin.execute("SELECT 1 FROM users WHERE email=%s",
                     (email,)).fetchone():
        return False
    try:
        return ext.gate.at_cap(_intake_seats_taken(admin))
    except Exception:                                    # noqa: BLE001
        # Safe to swallow HERE and nowhere else on this path: the
        # connection is autocommit and no transaction is open, so a failed
        # statement takes nothing else with it. A counting failure must not
        # turn a real person away — the binding check below still runs.
        log.warning("intake pre-check failed; admitting", exc_info=True)
        return False


def _intake_check_exact(admin) -> None:
    """The binding capacity decision, taken as LATE as possible inside the
    writing transaction and raising 503 to roll the whole signup back.

    The lock has to be instance-wide — different addresses do contend for
    one ceiling, which the per-email lock cannot express — and an xact lock
    is held until COMMIT. That is why nothing expensive may sit behind it:
    with the hash and the tenant/settings/billing/user writes in front of
    it, every hosted signup queues behind one argon2id plus a full write,
    each waiting request holding an unpooled connection open, so arrivals
    pile up raw backends until Postgres refuses new ones — which takes out
    every other admin_connect() door, sign-in unlock and email verification
    included, not just signup.

    Exactness survives the move: the count runs after this transaction's
    own INSERT and while the lock is held, so a concurrent signup either
    has not committed and is not counted yet by anyone, or has committed
    and released the lock and IS counted here. What is traded is
    concurrency of the refusal, not its accuracy — hashes and inserts now
    overlap, and a request that passed the advisory check above can still
    be turned away here.
    """
    admin.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                  (_INTAKE_LOCK,))
    # The count runs inside a SAVEPOINT because this transaction has
    # already written the whole household and still has to COMMIT it.
    # PostgreSQL aborts a transaction at the failed statement; catching the
    # exception in Python does not un-abort it, so a bare try/except around
    # a counting query here would convert any statement-level failure — a
    # query cancelled mid-deploy, `out of shared memory` from
    # max_locks_per_transaction on a busy box, a container rolled ahead of
    # its migrations — into a 500 on the COMMIT, for a signup the guard
    # meant to wave through. With the savepoint, the failure is contained
    # and the transaction is still committable.
    #
    # this transaction's own tenant is already inserted and visible to it,
    # so the count includes the seat about to be taken
    try:
        with admin.transaction():
            taken = _intake_seats_taken(admin)
    except Exception:                                    # noqa: BLE001
        # Fail OPEN, like the pre-check: the ceiling is a soft operator
        # limit, and a counting failure is not a reason to refuse someone
        # an account. The refusal below is the only thing skipped.
        log.warning("intake count failed; admitting this signup",
                    exc_info=True)
        return
    if ext.gate.at_cap(taken - 1):
        raise HTTPException(503, capacity_msg())


def _provision_household(admin, *, email: str, password_hash: str,
                         hosted: bool, inv: dict | None, ref: str | None,
                         move_uid=None, verified: bool = False,
                         before_user=None):
    """Everything a new household is made of, inside the caller's
    transaction (under the per-email signup lock): the tenant, its
    settings, the invite's one use, the owner row (inserted — or, on a
    reclaim, the existing unverified row moved in), whatever standing the
    installed gate opens for a new account, and the
    verification token when the mailbox is not yet proven. Shared by
    signup and by the reclaim click so the two can never drift.
    Returns (user_id, tenant_id, owed_to_inviter, verification_token)."""
    owed_to = None
    tid = tenancy.create_tenant(admin, email)
    # a new account opens on the DETAIL face of Today; the toggle can
    # still put it back. Set explicitly
    # here rather than by changing the absent-means default, so
    # existing accounts keep the face they have.
    admin.execute(
        """INSERT INTO tenant_settings (tenant_id, config)
           VALUES (%s, '{"today_view": "detail"}'::jsonb)
           ON CONFLICT (tenant_id) DO UPDATE SET config =
             tenant_settings.config || '{"today_view": "detail"}'::jsonb""",
        (tid,))
    if inv is not None and not signup_invites.consume(
            admin, inv["id"], tid):
        # lost a race on the invite's one use — the transaction
        # rolls the tenant back with it
        raise HTTPException(403, INVITE_MSG)
    if before_user is not None:
        # the reclaim's wipe / move-out, once the invite is consumed and
        # nothing can refuse any more — the user row below needs the
        # address free (wipe) or the row itself (move)
        before_user()
    now = dt.datetime.now(dt.timezone.utc)
    if move_uid is not None:
        # the reclaimed row moves into the fresh tenant as its owner,
        # credentials replaced, verified by the click that got us here
        moved = admin.execute(
            """UPDATE users SET tenant_id=%s, role='owner',
                      password_hash=%s, totp_secret=NULL,
                      totp_last_counter=NULL, verified_at=%s
                WHERE id=%s""",
            (tid, password_hash, now if verified else None,
             move_uid)).rowcount
        if not moved:
            # the row went while we were working — the household that
            # held it erased itself (that door takes a different lock).
            # Refuse rather than return a user id nothing points at: the
            # session insert would 500 and leave an orphaned tenant.
            raise HTTPException(409, "that account was removed while this "
                                     "was in flight — start again")
        row = {"id": move_uid}
    else:
        # hosted accounts start unverified — verified_at is stamped by
        # the emailed link (or by the reclaim click, which IS that
        # proof). Self-host/DEV signups (test suite, wizard-adjacent)
        # are operator-typed addresses and stay verified so no banner
        # nags them.
        row = admin.execute(
            """INSERT INTO users (tenant_id, email, password_hash,
                                  verified_at)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (tid, email, password_hash,
             now if (verified or not hosted) else None)).fetchone()
    if hosted:
        # the installed gate writes whatever standing it dictates for a
        # new account inside this same transaction; it records only —
        # anything that talks to an outside service runs off the request
        # afterwards
        owed_to = ext.gate.provision(admin, tid, invite=inv, ref=ref,
                                     verified=verified,
                                     reclaim=move_uid is not None)
    vtoken = (email_verify.create(admin, row["id"])
              if hosted and not verified else None)
    return row["id"], tid, owed_to, vtoken


@app.post("/api/signup", dependencies=[Depends(limit("signup", 5, 3600)),
                                  Depends(_not_demo_instance)])
def signup(request: Request, email: str = Form(...),
           password: str = Form(...), invite: str = Form(""),
           ref: str = Form(""),
           cf_token: str = Form("", alias=_TS_FIELD)):
    # Human check first: a fake-signup bot costs a whole tenant, and this
    # runs before the password rules, the invite lookup, and the spam-defense
    # network call — nothing expensive happens for a request that fails it.
    if not turnstile.verify(cf_token, security.client_ip(request)):
        raise HTTPException(403, "Please complete the human check and try "
                                 "again.")
    email = email.strip().lower()
    err = passwords.password_error(password)
    if err:
        raise HTTPException(400, err)
    # On a self-hosted box, /api/signup would otherwise let ANY
    # network-reachable visitor create a tenant + owner — and an owner can
    # stream the RLS-bypassing full-DB dump of EVERY household. A gate that
    # fires only once the instance is CLAIMED leaves the install window
    # open: before the operator opens the printed setup link, signup would
    # still answer and hand ownership to whoever asked first, so
    # OIKONOME_SETUP_TOKEN would buy nothing. Self-host bootstraps through the
    # /setup wizard (token-gated) and stays single-household after, so signup
    # is 403 off-hosted whether or not the instance is claimed. Open signup is
    # a hosted-SaaS need only. DEV_MODE is exempt (the multi-tenant test suite
    # mints tenants via signup); production self-host never sets OIKONOME_DEV.
    hosted = env_flag("OIKONOME_HOSTED")
    if not hosted and not DEV_MODE:
        raise HTTPException(
            403, "this instance is claimed through its setup link; open "
                 "signup is a hosted-only feature")
    # the invite-only gate. OIKONOME_HOSTED alone would open
    # signup to the whole internet — hosted signup therefore requires an
    # operator-minted invite bound to this email, unless the operator
    # EXPLICITLY opens it (OIKONOME_OPEN_SIGNUP=1). Invite-required is the
    # safe default.
    invite_gate = hosted and _signup_needs_invite()
    # Defense-in-depth on top of the invite gate: a blocklisted/flagged domain
    # is rejected with the same generic response as a missing invite (no
    # enumeration). Matters most if OIKONOME_OPEN_SIGNUP is ever set.
    from . import spamdefense
    from .security import client_ip
    if spamdefense.check("signup", email, ip=client_ip(request))["blocked"]:
        raise HTTPException(403, INVITE_MSG)
    # set when this signup redeemed someone's referral; settled off the
    # request once the tenant is committed
    owed_to = None
    admin = tenancy.admin_connect()
    try:
        # Capacity first, cheaply and outside every lock, so an instance at
        # its ceiling turns people away without paying for the hash below.
        # Only a signup WITHOUT an invite can hit the ceiling at all — an
        # invited one already has its seat, and a gated instance answers
        # the invite gate first, as it always did.
        if hosted and not invite_gate and not invite \
                and _intake_full_advisory(admin, email):
            raise HTTPException(503, capacity_msg())
        # The password hash is the expensive part of a signup (argon2id,
        # ~100 ms and 64 MiB by design) and it is computed ONCE, here,
        # before any advisory lock is held: inside the instance-wide intake
        # lock it would serialize every hosted signup on the box. Every road
        # out of this handler pays exactly one hash at the same point, so
        # the taken-address answers stay indistinguishable from each other.
        password_hash = passwords.hash_password(password)
        # The check and the insert must not straddle a TOCTOU window — two
        # concurrent signups for one email would both pass the exists-check,
        # both mint a tenant, and the loser hit the users.email UNIQUE index
        # with a 500 and leave its tenant orphaned. Serialize the claim per
        # email (not one global lock: unrelated hosted signups shouldn't
        # queue) and do the whole thing — check, tenant, user — inside it.
        with admin.transaction():
            admin.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                          ("oikonome:signup:" + email,))
            # An invite is honoured whenever one is presented — the open
            # switch only decides whether signing up WITHOUT one is
            # allowed. Looking the invite up only behind the gate would
            # mean that the day OIKONOME_OPEN_SIGNUP flips, every invite
            # still in someone's inbox silently loses whatever standing it
            # carried.
            inv = None
            if hosted and invite:
                inv = signup_invites.lookup(admin, invite, email)
            if invite_gate and inv is None:
                raise HTTPException(403, INVITE_MSG)
            existing = admin.execute(
                "SELECT id, tenant_id, verified_at FROM users WHERE email=%s",
                (email,)).fetchone()
            if existing is not None and hosted and existing["verified_at"] is None:
                # Taken but never proven. NOTHING is created and no
                # session is opened: any immediate reclaim — resetting
                # the row's credentials, or wiping its tenant and starting
                # over — is a takeover of whatever that account holds by
                # anyone who merely knows the address, and "unverified" is
                # the normal state of a household that has not clicked
                # its link yet, 2FA enrolled and banks connected. The
                # signup is parked under a one-shot token and the address
                # is mailed; the click proves the mailbox, and only then
                # does /signup/reclaim perform the reclaim.
                #
                # The ANSWER is the same 409 a verified address gets, word
                # for word: the reply must not say which of the two a
                # prober hit. A distinct 200 would tell them the address
                # exists AND is sitting unconfirmed — the pretext a phishing
                # clone of the very mail just sent would want.
                from ..auth import signup_reclaim
                # the password typed here is NOT kept: the clicker sets
                # one on the confirm page, because the person who fills
                # this form and the person who reads that mailbox are
                # different parties in the case this exists for
                pending = None
                if _reclaim_send_allowed(admin, existing["id"]):
                    rtoken = signup_reclaim.create(
                        admin, existing["id"], email, invite, ref)
                    pending = (email, rtoken)
                taken = True
                row, tid, owed_to, vtoken = {"id": None}, None, None, None
            elif existing is not None:
                # a VERIFIED address: the same answer, no mail. The
                # residual (that the address has SOME account) is
                # accepted — the reset door discloses it anyway.
                taken, pending = True, None
            else:
                taken, pending = False, None
                # The instance's account ceiling. Both invite-MINT doors refuse
                # at it, but this is the only door that actually INSERTS a
                # tenant — and the one OIKONOME_OPEN_SIGNUP throws open —
                # so without it the cap bounds the invites and not the
                # accounts. The per-email lock above serializes nothing
                # here: two different addresses never contend, so N
                # concurrent visitors would all provision past the
                # ceiling. Take the same
                # instance-wide lock the mint doors take, and count the
                # same way they do (accounts plus the live unclaimed
                # invites, which are seats already promised), so a signup
                # and a mint can no longer both pass on one remaining seat.
                #
                # An INVITED signup is never refused: minting that invite
                # already claimed its seat, so turning the claim away would
                # spend the seat on nobody. The reclaim door is exempt for
                # the same reason — it hands an address back to whoever
                # already had it.
                uid, tid, owed_to, vtoken = _provision_household(
                    admin, email=email, password_hash=password_hash,
                    hosted=hosted, inv=inv, ref=ref)
                row = {"id": uid}
                if hosted and inv is None:
                    # LAST, and cheap: the instance-wide lock is held only
                    # from here to the commit a few statements away, and a
                    # refusal rolls the tenant above back with it.
                    _intake_check_exact(admin)
    finally:
        admin.close()
    if owed_to:
        # off the signup request, like the verification mail: a round trip
        # to an outside service and one email on someone ELSE's behalf
        # must not sit in the path of this person's first page load
        import threading
        threading.Thread(target=_deliver_referral, args=(owed_to,),
                         daemon=True).start()
    if taken:
        # Both taken branches answer HERE, after the same work. The pair
        # that has to be indistinguishable is verified-409 against
        # unverified-409: a prober already knows the address exists by
        # then, and what must not leak is whether a live reclaim mail is
        # sitting in that inbox — the pretext a phishing copy of it would
        # want. argon2 dominates the branch that parks (it also writes two
        # rows), and BOTH branches paid for it at the same point above,
        # before any lock, so neither answers sooner than the other.
        if pending is not None:
            import threading
            threading.Thread(target=_deliver_reclaim, args=pending,
                             daemon=True).start()
        raise HTTPException(409, TAKEN_MSG)
    if vtoken:
        # out-of-band like the reset mail: a synchronous SMTP send would
        # add a timing oracle and slow every signup on a wobbly relay
        import threading
        threading.Thread(target=_deliver_verification,
                         args=(email, vtoken, tid), daemon=True).start()
    # reached only on the branch that actually provisioned a household —
    # the taken branches raised above, so a prober cannot make the
    # operator's mailbox echo an address that already had an account
    _operator_signup_notice(email, str(tid))
    return _login_response(row["id"], tid, request=request)


@app.post("/api/login", dependencies=[Depends(limit("login", 10, 300))])
def login(request: Request, email: str = Form(...), password: str = Form(...),
          totp_code: str = Form(""),
          cf_token: str = Form("", alias=_TS_FIELD)):
    from ..auth import totp as totp_mod
    from . import security
    # EVERY call is checked, not just the first step. Both steps carry the
    # password, so verifying only the code-less step would let a brute force
    # skip the challenge by always sending a junk totp_code. Tokens are
    # single-use, so pages.js resets the widget after each response.
    # Risk-gated: an IP and account with no recent failures are not asked for
    # a token at all (turnstile.verify_login).
    if not turnstile.verify_login(cf_token, security.client_ip(request), email):
        raise HTTPException(403, "human_check_failed")
    # account-scoped lockout: source-IP-
    # independent brute defense — see security.account_login_locked
    if security.account_login_locked(email):
        raise HTTPException(429, "too many failed attempts on this account "
                                 "— try again in a few minutes")
    with _control_conn() as conn:
        u = conn.execute(
            "SELECT id, tenant_id, password_hash, totp_secret, "
            "totp_last_counter FROM users WHERE email=%s",
            (email.strip().lower(),)).fetchone()
    # verify against a dummy hash on unknown users — uniform timing
    ok = passwords.verify_password(
        u["password_hash"] if u else _DUMMY_HASH, password)
    if not u or not ok:
        # NB: a wrong PASSWORD does NOT feed the account lock (that would
        # let anyone with the email DoS the owner) — only second-factor
        # failures below do. Wrong passwords are held by the per-IP limit,
        # an auto-ban strike, and the human-check risk mark (a challenge on
        # the next attempt, which is recoverable in a way a lock is not).
        security.record_auth_failure(security.client_ip(request), email)
        raise HTTPException(401, "bad credentials")
    if _passkey_only_login_blocked(u):
        # Passkey-only account — the SPA routes this to the
        # WebAuthn sign-in, where the factor is actually presented.
        # A lost-passkey owner escapes with a one-time recovery
        # code (supplied in the code field), the same net TOTP users get.
        if totp_code:
            from ..auth import recovery
            with _control_conn() as conn2:
                if recovery.redeem(conn2, u["id"], totp_code):
                    security.clear_login_failures(email)
                    security.clear_challenge_risk(
                        security.client_ip(request), email)
                    return _login_response(
                        u["id"], u["tenant_id"],
                        request.headers.get("user-agent", ""), request=request)
            security.record_login_failure(email)
            security.record_auth_failure(security.client_ip(request), email)
        if not totp_code:
            # Same as totp_required below: the password step
            # spent the unlock this flow still needs — with a supplied-but-
            # wrong recovery code the spend stays spent
            turnstile.refund_unlock(email)
        raise HTTPException(401, "passkey_required")
    if u["totp_secret"]:
        if not totp_code:
            # This request PASSED the human check (possibly by
            # spending the emailed unlock) and the password verified — the
            # flow just isn't finished. Re-arm the exemption so the code
            # step of this same sign-in can pass the check too.
            turnstile.refund_unlock(email)
            raise HTTPException(401, "totp_required")
        matched = totp_mod.verify_used(crypto.decrypt_cp(u["totp_secret"]),
                                       totp_code, u["totp_last_counter"])
        if matched is None:
            # a lost-authenticator escape — the same field also
            # accepts a one-time recovery code (burned on use). Only tried
            # when the TOTP check failed, so normal 2FA is unaffected.
            from ..auth import recovery
            with _control_conn() as conn2:
                if not recovery.redeem(conn2, u["id"], totp_code):
                    # password already verified — this IS a second-factor
                    # brute attempt; count it toward the account lock
                    security.record_login_failure(email)
                    security.record_auth_failure(
                        security.client_ip(request), email)
                    raise HTTPException(401, "bad one-time code")
        else:                            # burn the accepted TOTP step
            with _control_conn() as conn3:
                if not _burn_totp_counter(conn3, u["id"], matched):
                    # a concurrent request already spent this code
                    security.record_auth_failure(
                        security.client_ip(request), email)
                    raise HTTPException(401, "bad one-time code")
    security.clear_login_failures(email)
    security.clear_challenge_risk(security.client_ip(request), email)
    return _login_response(u["id"], u["tenant_id"],
                           request.headers.get("user-agent", ""),
                           request=request)


def _demo_login_payload(nusers: int, cfg: dict | None) -> dict:
    """Decide what /api/demo-login may reveal. Credentials only when the
    whole instance is ONE user whose tenant is demo-locked — a demo-seeded
    box, where the seeder already printed them and every door is locked.
    Any other shape (multi-user, hosted, real install) gets a bare
    {"demo": false}, so nothing leaks even if a tenant plants a
    demo_login key in its own config."""
    login = (cfg or {}).get("demo_login") or {}
    if (nusers == 1 and (cfg or {}).get("demo_mode")
            and login.get("email") and login.get("password")):
        return {"demo": True, "email": login["email"],
                "password": login["password"]}
    return {"demo": False}


@app.get("/api/demo-login",
         dependencies=[Depends(limit("demo_login", 30, 3600))])
def demo_login():
    """Pre-auth: the demo instance's printed credentials, so the login
    page can pre-fill them."""
    with _control_conn() as conn:
        rows = conn.execute("SELECT tenant_id FROM users LIMIT 2").fetchall()
    cfg = None
    if len(rows) == 1:
        admin = tenancy.admin_connect()
        try:
            row = admin.execute(
                "SELECT config FROM tenant_settings WHERE tenant_id = %s",
                (rows[0]["tenant_id"],)).fetchone()
        finally:
            admin.close()
        cfg = row["config"] if row else None
    return _demo_login_payload(len(rows), cfg)


def _login_response(user_id, tenant_id, user_agent: str = "",
                    request: Request | None = None) -> Response:
    # record where the session was opened from, under the same
    # trusted-proxy policy as the rate limiter — X-Forwarded-For only
    # counts when the peer is in OIKONOME_TRUSTED_PROXIES; taking the
    # header blind would let any client write a forged address into its
    # own session row, and the sessions panel would show the lie as fact.
    ip = ""
    if request is not None:
        from .security import client_ip
        ip = client_ip(request)
        ip = "" if ip == "?" else ip
    # One checkout for both writes: login is the hottest auth path and a
    # small host's connection budget is tight, so the ticket rides the
    # session's connection rather than taking a second one.
    with _control_conn() as conn:
        token = sessions.create_session(conn, user_id, tenant_id, user_agent,
                                        ip=ip)
        ticket = device_tokens.issue_mint_ticket(conn, user_id)
        # Whoever can sign in does not need a factor-clearing reset, and if
        # it was not them who asked for one, it must not land: every live
        # sign-in cancels a pending cooling-off request (auth/factor_reset).
        from ..auth import factor_reset
        if factor_reset.cancel_for_user(conn, user_id, reason="sign-in"):
            log.info("pending factor reset cancelled by sign-in")
    # Secure only when the request actually arrived over https — a Secure
    # cookie on http://192.168.x.x silently vanishes and bricks LAN
    # installs. Reverse proxies must forward X-Forwarded-Proto
    # AND be listed in OIKONOME_TRUSTED_PROXIES (the same trust gate
    # as client_ip — a client's own forged proto must not shape its cookie)
    scheme = request_scheme(request) if request is not None else "http"
    # A native app cannot rely on the cookie above: iOS exposes no
    # Set-Cookie to the app's fetch and its jar does not carry the session
    # to the very next request, so a sign-in there authenticates and then
    # fails to mint its device token — a login that will not stick, with
    # nothing wrong on either side that a log could show. So the
    # response also carries an explicit one-shot ticket good for exactly
    # that exchange (issued above, on the session's own connection).
    # Browsers ignore it and keep using the cookie.
    resp = Response(json.dumps({"ok": True, "mint_ticket": ticket}),
                    media_type="application/json")
    resp.set_cookie(sessions.COOKIE_NAME, token, httponly=True,
                    secure=(scheme == "https") and not DEV_MODE,
                    samesite="lax",
                    max_age=int(sessions.SESSION_TTL.total_seconds()))
    return resp


def _clear_session_cookie(resp: Response, request: Request) -> None:
    # deletion must carry the SAME attributes the cookie was
    # set with — browsers key cookies on (name, domain, path) plus the
    # secure partition, so a mismatched delete can be ignored and leave a
    # dead session cookie behind.
    resp.delete_cookie(
        sessions.COOKIE_NAME, httponly=True,
        secure=(request_scheme(request) == "https") and not DEV_MODE,
        samesite="lax")


@app.post("/api/logout")
def logout(request: Request):
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    with _control_conn() as conn:
        # Signing out must also void the mint ticket the login response
        # carried: it is a bearer that turns into a 90-day device token,
        # and on a shared machine the next person can read it out of the
        # login response after the cookie is long gone.
        sess = sessions.lookup_session(conn, token)
        if sess is not None:
            device_tokens.revoke_mint_tickets(conn, sess["user_id"])
        sessions.revoke_session(conn, token)
    resp = Response('{"ok": true}', media_type="application/json")
    _clear_session_cookie(resp, request)
    return resp


def _started_by_a_person(request: Request) -> bool:
    """True when this GET is someone following a link or typing the URL,
    rather than a page pulling the route in as a sub-resource.

    The app's CSRF model (web/security.py) checks Origin/Referer on
    POST/PUT/PATCH/DELETE and treats GET as safe. Sign-out by GET is not
    safe: it revokes the session and burns every unspent mint ticket, and
    SameSite=Lax still attaches the cookie to cross-site GETs, so an
    `<img src="https://…/logout">` on any page would sign a visitor out
    and hand them a sign-in form a phishing page can imitate.

    Fetch metadata is the instrument that fits. Referer cannot carry this
    alone: a page can strip it (rel=noreferrer, a meta refresh) and an
    ABSENT claim has to stay allowed, or curl and old browsers lose the
    only door a frozen account has. Sec-Fetch-* are forbidden header
    names, so page script can neither forge nor remove them. A real
    sign-out is a top-level document navigation the person started —
    mode=navigate, dest=document, and a site of none (typed or
    bookmarked), same-origin, or same-site (a link from the marketing
    domain). An image, script, XHR, prefetch or cross-site redirect
    fails at least one of the three.

    A request carrying no fetch metadata at all falls back to the same
    Origin/Referer host check the write middleware uses, absent claim
    allowed included — that is the pre-Sec-Fetch browser and the typed
    URL, both of which must keep working."""
    dest = request.headers.get("sec-fetch-dest", "").lower()
    mode = request.headers.get("sec-fetch-mode", "").lower()
    site = request.headers.get("sec-fetch-site", "").lower()
    if dest or mode or site:
        return (mode == "navigate" and dest == "document"
                and site in ("", "none", "same-origin", "same-site"))
    claim = (request.headers.get("origin")
             or request.headers.get("referer") or "")
    if claim:
        from urllib.parse import urlsplit
        return urlsplit(claim).netloc == request.headers.get("host", "")
    return True


@app.get("/logout")
def logout_page(request: Request):
    """Sign out from a plain link, then land on the sign-in form.

    `current_user` carves this path out three times — an account under
    any lockout status is frozen out of everything
    EXCEPT logout, so the route it names has to exist or those doors
    answer 404 and the browser keeps its dead session. It is also the
    only way to reach the sign-in form once /login redirects a live
    session into the app: signing in as someone else is sign out, then
    sign in."""
    if not _started_by_a_person(request):
        from fastapi.responses import JSONResponse
        security._sec_event("cross_origin_logout",
                            security.current_ip() or "-", route="/logout")
        return JSONResponse({"detail": "cross-origin sign-out blocked"},
                            status_code=403)
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    if token:
        with _control_conn() as conn:
            sess = sessions.lookup_session(conn, token)
            if sess is not None:
                device_tokens.revoke_mint_tickets(conn, sess["user_id"])
            sessions.revoke_session(conn, token)
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse("/login", status_code=303)
    _clear_session_cookie(resp, request)
    return resp


def _proved_passkey_or_refuse(user_id, keep_passkey_id):
    """The passkey id a caller's proof names, but only while that passkey
    still exists. Returns it, or None when nothing was named; raises the
    ordinary elevation refusal when the name is dangling.

    The name can outlive the credential. `sessions.elevated_passkey_id` is
    deliberately NOT a foreign key (migration 120), and nothing clears it
    when a key is deleted, so: elevate with passkey A, delete A on the
    Security page (allowed while B remains), then change the password
    inside the same ten-minute window. The rotation would keep "A" and
    delete every other key — B included — leaving an account with no
    passkey and no TOTP, password-only login re-enabled, and a response
    reporting a key kept that does not exist. A step-up TICKET minted
    against A and redeemed after A was deleted names it the same way.

    So a name whose row is gone is not a proof of anything, and the
    rotation is refused rather than run without a factor to keep. Same
    answer the caller who never elevated gets, so the clients open the
    same sheet — and re-proving with a key they still have then works.

    Checked BEFORE the caller mutates anything: these connections are
    autocommit, so a refusal raised later would leave a half-rotated
    account behind.
    """
    if not keep_passkey_id:
        return None
    with _control_conn() as conn:
        alive = conn.execute(
            "SELECT 1 FROM passkeys WHERE id=%s AND user_id=%s",
            (keep_passkey_id, user_id)).fetchone()
    if alive:
        return keep_passkey_id
    raise HTTPException(403, detail={"error": "elevation_required",
                                     "fresh": False})


# THE CANONICAL LOCK ORDER for every account-security door, and the one
# thing to keep straight when adding another.
#
#     1. the per-user factor lock, "oikonome:passkeys:<user id>"
#     2. only then any write to that user's `users` row
#
# Both are real locks in one lock manager — an advisory lock and the
# row-exclusive lock an UPDATE takes are interchangeable as far as
# PostgreSQL's deadlock detector is concerned — so two doors that take them
# in opposite orders deadlock, and the loser is aborted with 40P01 (an
# uncaught 500). It is not hypothetical: the web Settings tab and the
# mobile Security screen are two clients on one account, and a double
# submit is one client on its own.
#
# The TOTP doors take the lock first because their whole question ("would
# this leave the account with no factor?") has to be asked under it. A
# password- or email-change door that updated `users` first and reached
# the lock later, inside `_evict_passkeys`, would invert that order — and
# since each rotation runs as ONE transaction, the row lock is held until
# COMMIT and the cycle is real. Every door here takes the factor lock as
# the first statement of its transaction, so the order is the same
# everywhere and no cycle exists.
#
# `pg_advisory_xact_lock` is re-entrant within a transaction, so a caller
# that takes it here and a callee that takes it again is fine.
def _lock_user_factors(conn, user_id) -> None:
    """Take the per-user factor lock. FIRST, before touching `users` —
    see the canonical lock order above."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 ("oikonome:passkeys:" + str(user_id),))


def _evict_passkeys(conn, user_id, keep_passkey_id=None) -> tuple[int, int]:
    """Burn the passkeys a credential rotation must not leave behind.
    Returns (removed, kept).

    A password or email change evicts passkeys because a hijacked session
    could have enrolled one, and WebAuthn login would otherwise outlive
    every session, token and invite the rotation revokes. But on a hosted
    account whose ONLY strong factor is a passkey, evicting all of them
    drops the account to zero factors: _passkey_only_login_blocked goes
    False and email+password alone opens the whole household — the exact
    downgrade passkey_delete and totp_disable each refuse — so the
    rotation doors have to honour the same invariant.

    So exactly ONE key may survive: the one that produced this caller's
    elevation proof, named by `keep_passkey_id`. Every other one goes.

    Recency is not that test. /api/stepup/passkey is
    reachable by any authenticated session with no elevation of its own, so
    an attacker who plants a key can re-assert it every few minutes and
    keep it exactly as freshly used as the owner's — the rotation would
    then spare the planted key alongside the real one, or, on the
    recovery-code road where the owner's key is the stale one, spare the
    stranger's key and delete the owner's while telling them it is "your
    only second factor".

    When no passkey proved this caller — a password, a live TOTP code, a
    recovery code — `keep_passkey_id` is None and they ALL go. That is the
    lost-authenticator road, and it lands exactly where the password-reset
    road lands on the same proof: no factor left, and the account's next
    write goes through the forced-enrollment gate.

    An account with TOTP keeps a factor either way, and a self-hosted or
    operator-waived account was never under forced 2FA (nor is a self-host
    password login blocked), so both evict unconditionally.
    """
    with conn.transaction():
        # the lock passkey_delete takes, for the same reason: the count and
        # the delete must not straddle another door's delete. A rotation
        # door has already taken it (canonical order) — re-taking it in
        # the same transaction is free.
        _lock_user_factors(conn, user_id)
        row = conn.execute(
            "SELECT totp_secret, second_factor_waived FROM users WHERE id=%s",
            (user_id,)).fetchone()
        if (env_flag("OIKONOME_HOSTED") and row and not row["totp_secret"]
                and not row["second_factor_waived"] and keep_passkey_id):
            # Truthy is not existence. The callers refuse a dangling name
            # up front (_proved_passkey_or_refuse), so this only fires when
            # the key was deleted between that check and this lock — but
            # the keep branch is precisely where a name that no longer
            # exists deletes the account's last factor and calls it
            # success, so it is re-asked here, where passkey_delete's own
            # lock makes the answer final.
            if not conn.execute(
                    "SELECT 1 FROM passkeys WHERE id=%s AND user_id=%s",
                    (keep_passkey_id, user_id)).fetchone():
                raise HTTPException(403,
                                    detail={"error": "elevation_required",
                                            "fresh": False})
            removed = conn.execute(
                "DELETE FROM passkeys WHERE user_id=%s AND id <> %s",
                (user_id, keep_passkey_id)).rowcount
            kept = conn.execute(
                "SELECT count(*) AS n FROM passkeys WHERE user_id=%s",
                (user_id,)).fetchone()["n"]
            if not kept:
                # Unreachable given the check above, and it stays checked:
                # this branch exists to keep ONE factor, so reporting
                # success with none left is the one outcome it must never
                # have. Raising inside the transaction rolls the delete
                # back, so nothing is destroyed by the refusal.
                raise HTTPException(500, "passkey rotation would have left "
                                         "this account with no second "
                                         "factor — nothing was removed")
            return removed, kept
        return conn.execute("DELETE FROM passkeys WHERE user_id=%s",
                            (user_id,)).rowcount, 0


@app.post("/api/password/change",
          dependencies=[Depends(limit("password_change", 5, 3600))])
def password_change(request: Request, user: dict = Depends(current_user),
                    body: dict = Body(...)):
    """Change the signed-in user's password. Re-verifies the current
    password (argon2 — constant-time by construction) and, when 2FA is
    enrolled, the current TOTP code; then swaps the hash and revokes every
    OTHER session (mirror of the TOTP-confirm pattern: a hijacked session
    must not survive a credential change). The calling session stays alive."""
    demoguard.deny(user)
    current = str(body.get("current_password") or "")
    new = str(body.get("new_password") or "")
    code = str(body.get("totp_code") or "")
    with _control_conn() as conn:
        u = conn.execute(
            "SELECT password_hash, totp_secret FROM users WHERE id=%s",
            (user["user_id"],)).fetchone()
    # verify against a dummy hash if the row vanished — uniform timing
    ok = passwords.verify_password(
        u["password_hash"] if u else _DUMMY_HASH, current)
    if not u or not ok:
        # A wrong password at this door is a password guess like any
        # other — strike, lock bucket, human-check risk — or a session
        # thief could grind the password here at rate-limit speed and
        # then walk through every password-only door.
        security.record_auth_failure(security.current_ip() or "-",
                                     user.get("email", ""),
                                     route="password_change")
        raise HTTPException(401, "wrong password")
    # Validate the NEW password BEFORE burning single-use factors.
    # password_error includes the HIBP breached-password lookup on hosted —
    # a result no client can predict — so consuming the TOTP counter or a
    # one-time recovery code first would make each rejected candidate
    # silently cost a passkey-only user one of their eight codes and walk
    # them toward lockout.
    err = passwords.password_error(new)
    if err:
        raise HTTPException(400, err)
    recovery_code = str(body.get("recovery_code") or "")
    # the ONE key the eviction below may keep — the credential that proved
    # this caller, not merely one that was used recently
    keep_passkey = None
    if "totp_code" in body or "recovery_code" in body:
        # LEGACY: the store apps send the code / recovery-code KEY with
        # the change (empty until their totp_required prompt fills it) —
        # run the same checks, with the same 401s, as before
        if u["totp_secret"]:
            if not code:
                raise HTTPException(401, "totp_required")
            if not _totp_consume(user["user_id"], u["totp_secret"], code):
                raise HTTPException(401, "bad one-time code")
        # passkey-only accounts present a recovery code instead of TOTP
        keep_passkey = _require_passkey_stepup(
            user["user_id"], u["totp_secret"], recovery_code)
    elif u["totp_secret"] or (env_flag("OIKONOME_HOSTED")
                              and _has_passkey(user["user_id"])):
        # The current password above is the change's own input, and for a
        # plain account it was the whole re-auth. An account with a second
        # factor owed that factor here too; the elevation window is where
        # it was collected. A session+password thief on such an account
        # therefore still cannot rotate the password.
        _require_elevation(user)
        keep_passkey = _elevation_passkey(user)
    # A proof that names a passkey which is no longer there is no proof —
    # refuse before a single row is written (see _proved_passkey_or_refuse).
    keep_passkey = _proved_passkey_or_refuse(user["user_id"], keep_passkey)
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    # argon2id is ~100 ms and 64 MiB by design, so it is paid BEFORE the
    # transaction below opens — the same rule the intake lock records:
    # nothing expensive runs while a lock is held.
    new_hash = passwords.hash_password(new)
    with _control_conn() as conn:
        # ONE transaction for the whole rotation. Every statement here is
        # part of a single act — the new password and the eviction of
        # everything the old one could still reach — and a rotation that
        # half happens is worse than one that does not. The eviction below
        # can still REFUSE (a passkey deleted between the pre-flight check
        # and its lock), and on an autocommit connection that refusal would
        # arrive after the password had already changed: the caller told
        # 403 while their password was in fact new, their other sessions
        # gone, and their tokens, devices, push subscriptions and pending
        # invites still live — a rotation reported as a failure that left
        # the hijacker's persistence in place. Inside the transaction the
        # refusal rolls the whole act back, so the answer is true either
        # way.
        with conn.transaction():
            # FIRST statement of the transaction — the canonical lock
            # order. `_evict_passkeys` below takes this same lock, and
            # updating `users` ahead of it would put this door's two locks
            # in the opposite order from the TOTP doors' — which deadlocks
            # two concurrent requests on one account and rolls the whole
            # rotation back after a full step-up.
            _lock_user_factors(conn, user["user_id"])
            conn.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                         (new_hash, user["user_id"]))
            sessions.revoke_all_others(conn, user["user_id"], token)
            # Mirror the session eviction for passkeys — a
            # hijacked session could have enrolled one; a password change must
            # not leave that backdoor. The user re-adds passkeys after. The key
            # that PROVED this caller — the one behind this session's step-up
            # ticket, never merely a recently-used one — is kept when it is the
            # account's last factor: see _evict_passkeys.
            passkeys_removed, passkeys_kept = _evict_passkeys(
                conn, user["user_id"], keep_passkey)
            # Same for script tokens —
            # a hijack-minted oik_ token must not survive the password change.
            tokens_revoked = conn.execute(
                "UPDATE api_tokens SET revoked_at=now() "
                "WHERE created_by=%s AND revoked_at IS NULL",
                (user["user_id"],)).rowcount
            # mobile device tokens are the same class of persistence — every
            # device re-authenticates after a credential rotation
            devices_revoked = device_tokens.revoke_all_for_user(
                conn, user["user_id"], keep_device_id=user.get("device_id"))
            # and the unspent mint ticket from this login — a bearer that
            # would plant a fresh device token right after this revocation
            device_tokens.revoke_mint_tickets(conn, user["user_id"])
            # and every browser's web-push subscription — household content
            # must not keep arriving where the session just died
            _drop_web_push(conn, user["user_id"],
                           str(body.get("keep_push_endpoint") or ""))
            # and any unspent reset link: one minted to the OLD mailbox by
            # whoever held it would otherwise outlive this rotation and strip
            # every factor an hour later
            conn.execute("UPDATE password_resets SET used_at=now() "
                         "WHERE user_id=%s AND used_at IS NULL",
                         (user["user_id"],))
            # and pending family invites — a hijacker who minted an
            # invite before the rotation would otherwise keep a claimable
            # member slot. The owner re-mints; a real pending invitee asks.
            invites_removed = conn.execute(
                "DELETE FROM invites WHERE created_by=%s "
                "AND used_at IS NULL", (user["user_id"],)).rowcount
    # The counts let the client say what the rotation actually cost —
    # a passkey that stops working with no explanation reads as a bug.
    return {"ok": True, "passkeys_removed": passkeys_removed,
            # the count that survived, so the client can say "your passkey
            # still works" instead of leaving someone to discover it
            "passkeys_kept": passkeys_kept,
            "tokens_revoked": tokens_revoked,
            "devices_revoked": devices_revoked,
            "invites_removed": invites_removed}


def _check_password(user_id: str, password: str,
                    route: str = "step_up") -> dict:
    """The password half of a step-up: verify it (constant-time, dummy hash
    for a vanished row) and return the user row; a miss is a 401 that also
    counts as an auth failure at the login door. Nothing is consumed, so
    it is safe to call once before a ceremony and again after it. `route`
    names the door in the strike log (elevate vs. a legacy per-request
    step-up), nothing else changes with it."""
    with _control_conn() as conn:
        u = conn.execute(
            "SELECT email, password_hash, totp_secret FROM users WHERE id=%s",
            (user_id,)).fetchone()
    ok = passwords.verify_password(
        u["password_hash"] if u else _DUMMY_HASH, password or "")
    if not u or not ok:
        # A wrong password HERE counts like a wrong password at the login
        # door — auto-ban strike, account-lock bucket, Turnstile risk.
        # Without that a session thief could grind the password against any
        # posture-change route with no throttle and no trace.
        security.record_auth_failure(security.current_ip() or "-",
                                     (u or {}).get("email", ""),
                                     route=route)
        raise HTTPException(401, "password_required: confirm your password "
                                 "to change two-factor settings")
    return u


def _step_up_precheck(user_id: str, password: str, totp_code: str) -> None:
    """The step-up, checked BEFORE a WebAuthn registration ceremony — and
    without consuming anything, so the real step-up on the route that
    persists the key (`_step_up` + `_require_live_totp`) still runs on the
    same password and the same code.

    Why both: the browser asks the authenticator to CREATE the credential
    between /api/passkeys/options and /api/passkeys. A password manager or
    platform authenticator saves that credential the moment the user
    approves the prompt — so were the password only found wrong at
    persist time, the server would store nothing but the person would be left with a
    passkey in their vault that never worked. Refusing to mint the challenge
    on a wrong password (or a wrong live code) means the ceremony never
    starts, and the only passkeys that exist are ones the server kept."""
    u = _check_password(user_id, password)
    if u["totp_secret"]:
        if not totp_code:
            raise HTTPException(401, "totp_required")
        from ..auth import totp as totp_mod
        if not totp_mod.verify(crypto.decrypt_cp(u["totp_secret"]),
                               totp_code):
            raise HTTPException(401, "bad one-time code")


def _step_up(user_id: str, password: str, recovery_code: str = "",
             route: str = "step_up") -> None:
    """Re-verify the account password for a security-posture change.

    Were adding a second factor to need only a LIVE SESSION, a stolen one
    could enroll its own TOTP or passkey and then revoke the real owner's
    sessions — theft escalated into lockout, using the 2FA machinery as
    the weapon. Re-auth breaks that: a session thief has the cookie, not
    the password.

    Guards the FIRST factor on enroll/confirm — once TOTP exists, those
    routes demand a code from the current authenticator instead, which is a
    stronger proof than the password and is checked by the callers. Turning
    2FA OFF is the exception: disable requires BOTH this password check and
    a live code, because it's a downgrade, not a rotation.

    Mirrors password_change: argon2 is constant-time by construction, and a
    vanished row verifies against a dummy hash so timing can't distinguish
    "no such user" from "wrong password".
    """
    u = _check_password(user_id, password, route=route)
    # A passkey-only account signs in with a possession factor, so the
    # password is NOT sufficient proof for ANY posture change — including
    # passkey add/remove. Exempting the passkey-management surface would
    # let a session+password thief strip the owner's only passkey (dropping
    # the account to zero factors → password-only login re-enabled →
    # durable takeover) or graft on their own key. Every posture change
    # demands a recovery code for such accounts. First-factor bootstrap
    # still works: with no passkey/TOTP yet, _require_passkey_stepup is a
    # no-op.
    _require_passkey_stepup(user_id, u["totp_secret"], recovery_code)


def _require_live_totp(user_id: str, totp_code: str) -> None:
    """The caller-side half of a posture-change step-up on a TOTP account:
    require + verify a LIVE code from the current authenticator (mirrors
    password_change / totp_disable). _step_up guards the password + the
    passkey-only recovery path; a TOTP account additionally owes a live code
    here, or a password+session thief could rotate recovery codes / enroll a
    passkey and bypass the second factor. No-op when TOTP isn't enrolled."""
    with _control_conn() as conn:
        u = conn.execute("SELECT totp_secret FROM users WHERE id=%s",
                         (user_id,)).fetchone()
    if u and u["totp_secret"]:
        if not totp_code:
            raise HTTPException(401, "totp_required")
        if not _totp_consume(user_id, u["totp_secret"], totp_code):
            raise HTTPException(401, "bad one-time code")


# ---- session elevation ("sudo mode") ---------------------------------------
# Demanding the password (+ live TOTP / passkey step-up) at every
# account-security door on EVERY request costs Settings a password field
# per card. Identity is re-proved ONCE instead — by exactly the proofs
# those doors demand — and stamped on THIS session row; inside the window
# the doors
# read the stamp. The threat model is unchanged: a SESSION THIEF (cookie,
# no password/passkey) still cannot elevate, and cannot borrow the owner's
# other session's stamp because it lives on the row, not the user.
ELEVATION_TTL = dt.timedelta(minutes=10)


def _elevation_row(user: dict) -> dict | None:
    """(elevated_at, db_now) for the principal's OWN session row — the
    cookie session or the mobile device token, which is a session by
    another name. None for a script token: it has no session, and a
    long-lived collector credential must never become a sudo window.
    The clock is the database's on both reads and the stamp, so the
    window cannot drift with the app host's."""
    if user.get("script_token"):
        return None
    # The id comes off the row current_user just resolved for THIS
    # request's credential; the user_id clause is belt and braces so no
    # future caller can hand this a foreign session id.
    if user.get("device_id"):
        sql = ("SELECT elevated_at, elevated_passkey_id, now() AS db_now "
               "FROM device_tokens "
               "WHERE id = %s AND user_id = %s AND revoked_at IS NULL")
        key = user["device_id"]
    elif user.get("session_id"):
        sql = ("SELECT elevated_at, elevated_passkey_id, now() AS db_now "
               "FROM sessions WHERE id = %s AND user_id = %s")
        key = user["session_id"]
    else:
        return None
    with _control_conn() as conn:
        return conn.execute(sql, (key, user["user_id"])).fetchone()


def _elevation_passkey(user: dict) -> str | None:
    """The passkey that opened THIS session's still-open window, if a
    passkey opened it. None when the window is shut or the proof was a
    password or a recovery code — in which case a credential rotation keeps
    no key at all."""
    row = _elevation_row(user)
    if not row or not row["elevated_at"] or not row["elevated_passkey_id"]:
        return None
    if row["db_now"] - row["elevated_at"] >= ELEVATION_TTL:
        return None
    return str(row["elevated_passkey_id"])


def _elevation_age(user: dict) -> dt.timedelta | None:
    """How old this session's proof is; None when it never proved. A
    principal with no session row (script token) is refused outright."""
    row = _elevation_row(user)
    if row is None:
        raise HTTPException(
            403, "elevation needs a signed-in session — a script token "
                 "cannot re-prove identity")
    if not row["elevated_at"]:
        return None
    return row["db_now"] - row["elevated_at"]


def _elevation_state(user: dict) -> dict:
    """The shape GET /api/auth/elevation and a successful elevate return:
      {"elevated": bool, "until": iso | null,
       "methods": ["passkey"] | ["passkey", "password"] | ["password"],
       "totp": bool}
    `methods` names the proofs the client may collect, preferred first:
    the passkey where one exists; the password too when the account ALSO
    has TOTP (a device without the key falls back to password + code
    instead of burning a recovery code) — but NOT for a passkey-only
    hosted account, where the password is not a factor (the rule
    _require_passkey_stepup enforces); `totp` says a live code goes with
    the password."""
    row = _elevation_row(user)
    if row is None:
        raise HTTPException(
            403, "elevation needs a signed-in session — a script token "
                 "cannot re-prove identity")
    at = row["elevated_at"]
    elevated = bool(at) and row["db_now"] - at < ELEVATION_TTL
    has_totp = bool(user.get("has_totp"))
    methods = ["password"]
    if _has_passkey(user["user_id"]):
        methods = ["passkey"]
        if has_totp or not env_flag("OIKONOME_HOSTED"):
            methods.append("password")
    return {"elevated": elevated,
            "until": (at + ELEVATION_TTL).isoformat() if elevated else None,
            "methods": methods, "totp": has_totp}


def _stamp_elevation(user: dict, passkey_id: str | None = None) -> None:
    """Open the window on THIS row only, recording WHICH passkey proved it.

    Always written, never merged: an elevation re-proved with a password or
    a recovery code must CLEAR the key an earlier one recorded, or the
    rotation that follows would spare a key nobody proved in this window."""
    with _control_conn() as conn:
        if user.get("device_id"):
            conn.execute("UPDATE device_tokens SET elevated_at = now(), "
                         "elevated_passkey_id = %s WHERE id = %s",
                         (passkey_id, user["device_id"]))
        else:
            conn.execute("UPDATE sessions SET elevated_at = now(), "
                         "elevated_passkey_id = %s WHERE id = %s",
                         (passkey_id, user["session_id"]))


def _require_elevation(user: dict, *, fresh_seconds: int | None = None,
                       password: str = "", totp_code: str = "",
                       recovery_code: str = "") -> None:
    """The re-auth gate every account-security door now calls.

    LEGACY first: when the request carries a password (or a recovery
    code / step-up ticket) it is an older store app build that still
    re-proves per request — run exactly the checks the door ran before (`_step_up`
    + a live code on a TOTP account), so those clients keep working and
    a wrong proof fails exactly as it did.

    Otherwise the session must be inside its window — and inside
    `fresh_seconds` when the door is irreversible enough that a
    minutes-old proof is too stale (account delete, the everything
    export). The refusal is a 403 with a structured detail the clients
    key on to run the elevate sheet and retry; it is NOT a 401, which the
    SPA reads as a lost session. A TOTP account's live code was collected
    at elevate time and is not asked for again inside the window.

    Nothing here weakens the door: an unelevated call with no proof in
    the body is refused, which is precisely what a session thief sends."""
    if password or recovery_code:
        _step_up(user["user_id"], password, recovery_code)
        _require_live_totp(user["user_id"], totp_code)
        return
    age = _elevation_age(user)
    limit_ = ELEVATION_TTL
    if fresh_seconds is not None:
        limit_ = min(limit_, dt.timedelta(seconds=fresh_seconds))
    if age is None or age >= limit_:
        raise HTTPException(403, detail={"error": "elevation_required",
                                         "fresh": bool(fresh_seconds)})


@app.get("/api/auth/elevation")
def elevation_status(user: dict = Depends(current_user)):
    """Is this session inside its sudo window, and what proof opens it."""
    return _elevation_state(user)


# 10 per hour: the doors this window opens throttle their per-request
# proofs at 5–10/h each (password_change, account_delete, totp_disable…),
# and one window opens all of them, so the guess budget is the tightest of
# the set, not login's 10 per 5 minutes.
@app.post("/api/auth/elevate",
          dependencies=[Depends(limit("elevate", 10, 3600))])
def elevate(user: dict = Depends(current_user), body: dict = Body(...)):
    """Open the window on this session. Returns the GET shape. Exactly the
    proofs the doors take per request:
      {"password", "totp_code"}     — password accounts (code when enrolled)
      {"password", "recovery_code"} — a TOTP account whose authenticator
                                      is lost: the code stands in for the
                                      second factor, as at login
      {"stepup_ticket"}             — the WebAuthn assertion's ticket
                                      (/api/stepup/passkey); the passkey IS
                                      the factor, no password needed
      {"recovery_code"}             — lost-KEY fallback, passkey-only
                                      accounts only: a recovery code is a
                                      stand-in for a factor, never for the
                                      whole proof, so alone it opens
                                      nothing on a password account
    This door takes a password guess, and a wrong one — password or code —
    counts as a login failure so the lock bucket and auto-ban see it. The
    stamp is written only after the proof — a session thief who reaches
    this door leaves with nothing."""
    if _elevation_row(user) is None:
        raise HTTPException(
            403, "elevation needs a signed-in session — a script token "
                 "cannot re-prove identity")
    ticket = str(body.get("stepup_ticket") or "")
    recovery_code = str(body.get("recovery_code") or "")
    password = str(body.get("password") or "")
    totp_code = str(body.get("totp_code") or "")
    # legacy clients send the ticket in the recovery_code field; they keep
    # working
    if not ticket and recovery_code.startswith("pkstep-"):
        ticket, recovery_code = recovery_code, ""
    proved_by_passkey = None
    if ticket:
        from ..auth import passkeys
        with _control_conn() as conn:
            ok, proved_by_passkey = passkeys.redeem_stepup_ticket(
                conn, user["user_id"], ticket)
        if not ok:
            security.record_auth_failure(security.current_ip() or "-",
                                         user.get("email", ""),
                                         route="elevate")
            raise HTTPException(
                401, "passkey check failed: that confirmation is expired "
                     "or already used — confirm with your passkey again")
    elif password:
        u = _check_password(user["user_id"], password, route="elevate")
        if u["totp_secret"]:
            if recovery_code:
                _redeem_recovery_or_fail(user, recovery_code)
            elif not totp_code:
                raise HTTPException(401, "totp_required")
            elif not _totp_consume(user["user_id"], u["totp_secret"],
                                   totp_code):
                security.record_auth_failure(security.current_ip() or "-",
                                             user.get("email", ""),
                                             route="elevate")
                raise HTTPException(401, "bad one-time code")
        # for a passkey-only hosted account this refuses (recovery_required)
        # exactly as every door did: the password is not the factor such an
        # account signs in with, and a bare recovery code is handled below
        _require_passkey_stepup(user["user_id"], u["totp_secret"], "")
    elif recovery_code:
        with _control_conn() as conn:
            u = conn.execute("SELECT totp_secret FROM users WHERE id=%s",
                             (user["user_id"],)).fetchone()
        if not (u and env_flag("OIKONOME_HOSTED") and not u["totp_secret"]
                and _has_passkey(user["user_id"])):
            # not a passkey-only account: the code is only ever a
            # stand-in for the second factor, and the first is missing
            raise HTTPException(
                401, "password_required: a recovery code stands in for "
                     "your authenticator — send it with your password")
        _redeem_recovery_or_fail(user, recovery_code)
    else:
        raise HTTPException(
            401, "password_required: confirm your password or passkey to "
                 "continue")
    _stamp_elevation(user, proved_by_passkey)
    return _elevation_state(user)


def _redeem_recovery_or_fail(user: dict, recovery_code: str) -> None:
    """Burn one recovery code for the elevate door; a miss is a strike."""
    from ..auth import recovery
    with _control_conn() as conn:
        ok = recovery.redeem(conn, user["user_id"], recovery_code)
    if not ok:
        security.record_auth_failure(security.current_ip() or "-",
                                     user.get("email", ""), route="elevate")
        raise HTTPException(
            401, "recovery_required: that recovery code is not valid — "
                 "each one works once")


def _revoke_bearer_persistence(conn, user: dict) -> None:
    """The non-session persistence a hijacked session could have planted:
    device tokens (every one but the device making this request), script
    tokens, and unspent device-mint tickets. Called by the posture changes
    that already evict other sessions — enrolling the first strong factor
    is precisely the moment an owner believes a hijack is closed."""
    device_tokens.revoke_all_for_user(conn, user["user_id"],
                                      keep_device_id=user.get("device_id"))
    conn.execute("UPDATE api_tokens SET revoked_at=now() "
                 "WHERE created_by=%s AND revoked_at IS NULL",
                 (user["user_id"],))
    device_tokens.revoke_mint_tickets(conn, user["user_id"])
    _drop_web_push(conn, user["user_id"])


def _drop_web_push(conn, user_id, keep_endpoint: str = "") -> int:
    """A browser's web-push subscription outlives its session: the
    verdict text and alert titles keep arriving in a browser whose cookie
    was just revoked, because nothing tied the two. Every posture change
    that evicts sessions drops the subscriptions too; the browser that
    made the request may keep its own (`keep_endpoint`). Native tickles
    are content-free and already follow the device token's revocation."""
    return conn.execute(
        "DELETE FROM push_subscriptions WHERE user_id=%s AND endpoint <> %s",
        (user_id, keep_endpoint or "")).rowcount


@app.post("/api/totp/enroll",
          dependencies=[Depends(limit("totp_enroll", 10, 3600))])
def totp_enroll(request: Request, user: dict = Depends(current_user),
                current_code: str = Form(""), password: str = Form(""),
                recovery_code: str = Form("")):
    """Start enrollment: returns the secret + otpauth URI (render as a QR
    client-side). The EXISTING secret stays active until /api/totp/confirm
    proves possession of the new one — enrollment never downgrades 2FA.
    When 2FA is already on, starting a
    re-enrollment requires the CURRENT code; when it is NOT, the account
    password (see _step_up)."""
    demoguard.deny(user)
    from ..auth import totp as totp_mod
    with _control_conn() as conn:
        u = conn.execute("SELECT totp_secret FROM users WHERE id=%s",
                         (user["user_id"],)).fetchone()
    if u and u["totp_secret"]:
        if current_code or password:
            # LEGACY: a code from the current authenticator, per request —
            # a password without one is a legacy client's first try, and it
            # prompts on this 401 (a 403 would read to it as an error)
            if not _totp_consume(user["user_id"], u["totp_secret"],
                                 current_code):
                raise HTTPException(401, "current one-time code required to "
                                         "re-enroll")
        else:
            _require_elevation(user)
    else:
        _require_elevation(user, password=password,
                           recovery_code=recovery_code)
    secret = totp_mod.new_secret()
    # Return a scannable QR (SVG data URI) + the base32 secret for manual
    # entry — NOT the raw otpauth:// string, which embeds the seed as
    # plain text.
    uri = totp_mod.provisioning_uri(secret, user["email"])
    return {"secret": secret, "qr": totp_mod.qr_svg_data_uri(uri)}


@app.post("/api/totp/confirm",
          dependencies=[Depends(limit("totp_enroll", 10, 3600))])
def totp_confirm(request: Request, user: dict = Depends(current_user),
                 secret: str = Form(...), code: str = Form(...),
                 current_code: str = Form(""), password: str = Form(""),
                 recovery_code: str = Form("")):
    """Prove possession → atomically bind the new secret and revoke every
    other session (a hijacked session must not survive a 2FA change).
    When 2FA is ALREADY active, rebinding also demands a code from the
    EXISTING authenticator — otherwise confirm bypasses enroll's guard
    and a hijacked session could silently swap the secret."""
    demoguard.deny(user)
    from ..auth import totp as totp_mod
    with _control_conn() as conn0:
        row0 = conn0.execute("SELECT totp_secret FROM users WHERE id=%s",
                             (user["user_id"],)).fetchone()
    if row0 and row0["totp_secret"]:
        if current_code or password:
            # LEGACY: a code from the current authenticator, per request
            # (same first-try-then-prompt shape as enroll)
            if not _totp_consume(user["user_id"], row0["totp_secret"],
                                 current_code):
                raise HTTPException(
                    401, "totp_required: enter a code from your CURRENT "
                         "authenticator to replace it")
        else:
            _require_elevation(user)
    else:
        # Confirm is the route that BINDS the secret and revokes other
        # sessions, so guarding only enroll would leave the real door open
        _require_elevation(user, password=password,
                           recovery_code=recovery_code)
    # possession of the NEW secret. verify_used against no prior counter is
    # just a verify, but it hands back the matched step so we can stamp it
    # below — otherwise the very code that confirmed enrollment stays valid
    # for a login a few seconds later. Every door that accepts a code
    # burns it.
    new_counter = totp_mod.verify_used(secret, code, None)
    if new_counter is None:
        raise HTTPException(400, "code doesn't match — check your app's clock")
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    import hashlib
    codes = None
    # The recovery batch is minted on an ADMIN connection (the app role
    # cannot insert codes), which is unpooled — a fresh connect per call.
    # Open it BEFORE the lock below, and only when the cheap read says a
    # batch will probably be needed, so the critical section holds a
    # pooled control connection and the per-user lock across a handful of
    # statements, never across a TLS handshake.
    maybe_first = not (row0 and row0["totp_secret"]) and not _has_passkey(
        user["user_id"])
    admin = tenancy.admin_connect() if maybe_first else None
    try:
      with _control_conn() as conn, conn.transaction():
        # "Is this the FIRST strong factor?" is decided, written and acted
        # on under the same per-user advisory lock passkey_register and
        # the last-factor removals serialise on. Decided outside it, two
        # concurrent enrolments (TOTP here, a passkey on the phone, or a
        # retried request) would each see "no factor yet" and each mint a
        # recovery batch — and recovery.issue replaces, so the loser's
        # codes, already shown to the user, would die silently.
        _lock_user_factors(conn, user["user_id"])
        cur = conn.execute(
            "SELECT totp_secret, (SELECT count(*) FROM passkeys "
            "WHERE user_id = %s) AS n_pk FROM users WHERE id = %s",
            (user["user_id"], user["user_id"])).fetchone()
        first_factor = not (cur and (cur["totp_secret"] or cur["n_pk"]))
        # the seed is encrypted at rest — a DB dump must not be a
        # 2FA-seed dump. Control-plane path (master key direct, no tenant).
        # totp_last_counter is per-SECRET: a counter stamped against the OLD
        # secret must not reject the new secret's codes in the same wall-clock
        # window. Stamp the counter the confirming code just
        # matched rather than clearing it outright — same effect for the old
        # secret, and it also burns the confirming code itself.
        conn.execute("UPDATE users SET totp_secret=%s, "
                     "totp_last_counter=%s WHERE id=%s",
                     (crypto.encrypt_cp(secret), new_counter,
                      user["user_id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=%s AND token_hash != %s",
                     (user["user_id"],
                      hashlib.sha256(token.encode()).hexdigest()))
        # Sessions are not the only thing a hijacker can be holding. A
        # device token is a full session equivalent that never prompts
        # TOTP, and a stolen cookie can mint one inside its fresh-auth
        # window with no password; a script token is durable too. Both
        # must die with the enrollment or the owner closes the hijack in
        # good faith while a 90-day bearer rides on. The device driving
        # this enrollment (if it is one) stays, like the calling session.
        _revoke_bearer_persistence(conn, user)
        # issuing TOTP as the user's FIRST strong factor mints a batch
        # of one-time recovery codes (shown once) so a lost authenticator
        # isn't a lockout under the forced-2FA policy. Re-binding an
        # existing TOTP doesn't reissue (they still hold their codes;
        # Settings can regenerate). Minted while the lock is held, so the
        # batch the response carries is the batch that stands.
        if first_factor:
            from ..auth import recovery
            if admin is None:          # the cheap read was stale; rare
                admin = tenancy.admin_connect()
            codes = recovery.issue(admin, user["user_id"])
    finally:
        if admin is not None:
            admin.close()
    return {"ok": True, "recovery_codes": codes}


@app.post("/api/totp/recovery-regenerate",
          dependencies=[Depends(limit("totp_enroll", 10, 3600))])
def recovery_regenerate(user: dict = Depends(current_user),
                        password: str = Form(""), totp_code: str = Form(""),
                        recovery_code: str = Form("")):
    """Mint a fresh batch of recovery codes (shown once), voiding the old
    set. Step-up gated — password AND, on a TOTP account, a live code: the
    fresh codes are TOTP-bypass primitives, so minting them must demand the
    second factor, exactly like password_change / totp_disable."""
    demoguard.deny(user)
    _require_elevation(user, password=password, totp_code=totp_code,
                       recovery_code=recovery_code)
    from ..auth import recovery
    admin = tenancy.admin_connect()
    try:
        codes = recovery.issue(admin, user["user_id"])
    finally:
        admin.close()
    return {"ok": True, "recovery_codes": codes}


# Same throttle reasoning as password_change/account_delete — this door
# takes a password guess, so leaving it unthrottled would hand a stolen
# session an offline-speed oracle against the password.
@app.post("/api/totp/disable",
          dependencies=[Depends(limit("totp_disable", 5, 3600))])
def totp_disable(request: Request, user: dict = Depends(current_user),
                 code: str = Form(...), password: str = Form("")):
    """Turning 2FA OFF demands the password AND a live code. A code
    alone would mean one shoulder-surfed 6-digit number plus a stolen
    session could strip the second factor and keep that session — weaker than
    enroll and passkey-delete, which both re-verify the password. The
    elevation window stands in for the password half only: the live code
    is owed every time, because a window opened by a passkey (on an
    account with both) would otherwise turn TOTP off with no code at
    all, and a downgrade must present the factor it removes."""
    demoguard.deny(user)
    import hashlib
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    with _control_conn() as conn:
        u = conn.execute(
            "SELECT totp_secret, second_factor_waived FROM users WHERE id=%s",
            (user["user_id"],)).fetchone()
        if not u or not u["totp_secret"]:
            return {"ok": True}
        # Same last-factor refusal as passkey_delete: while hosted
        # forced-2FA is in effect, an account whose ONLY strong factor is
        # TOTP must not be able to turn it off — otherwise a captured
        # password + session + one live code becomes a permanent downgrade
        # to password-only login. A passkey on file means a factor
        # remains; an operator waiver means forced-2FA never applied to
        # this account, so the guard steps aside. Checked BEFORE the
        # password and live-code verification so nothing is consumed by a
        # refused disable.
        if (env_flag("OIKONOME_HOSTED") and not u["second_factor_waived"]
                and not _has_passkey(user["user_id"])):
            raise HTTPException(
                400, "last_factor: this is your only two-factor method — add "
                     "another passkey or an authenticator app before removing "
                     "it")
        if password:
            # LEGACY: password + live code, per request
            _step_up(user["user_id"], password)
        else:
            # checked BEFORE the code is consumed, so an unelevated
            # request burns nothing
            _require_elevation(user)
        if not _totp_consume(user["user_id"], u["totp_secret"], code):
            raise HTTPException(401, "bad one-time code")
        # The check above ran before the argon2/TOTP work, on a stale
        # read: a concurrent passkey_delete can have removed the fallback
        # passkey in the meantime, and each request's own guard would have
        # passed. Re-check under the SAME per-user advisory lock
        # passkey_delete serializes on, in one transaction with the
        # write, so the pair cannot interleave down to zero factors.
        with conn.transaction():
            _lock_user_factors(conn, user["user_id"])
            row = conn.execute(
                "SELECT second_factor_waived, (SELECT count(*) FROM passkeys "
                "WHERE user_id = %s) AS n_pk FROM users WHERE id = %s",
                (user["user_id"], user["user_id"])).fetchone()
            if (env_flag("OIKONOME_HOSTED") and row
                    and not row["second_factor_waived"]
                    and not row["n_pk"]):
                raise HTTPException(
                    400, "last_factor: this is your only two-factor method "
                         "— add another passkey or an authenticator app "
                         "before removing it")
            conn.execute("UPDATE users SET totp_secret=NULL, "
                         "totp_last_counter=NULL WHERE id=%s",
                         (user["user_id"],))
        # Dropping 2FA is a security-posture change — revoke every OTHER
        # session (like enroll/confirm and password-change already do) so a
        # hijacked session can't quietly persist after the owner turns it off.
        conn.execute("DELETE FROM sessions WHERE user_id=%s AND token_hash != %s",
                     (user["user_id"],
                      hashlib.sha256(token.encode()).hexdigest()))
        _revoke_bearer_persistence(conn, user)
    return {"ok": True}


def _delivery_state_for(email: str) -> dict | None:
    """The SPA's view of "is our mail reaching you". Shaped for a human
    sentence, not for a dashboard: the state, the provider's own words for
    why, and — when the address looks like a typo of a big provider — the
    address they probably meant, so the fix is one click and not a puzzle.

    Best-effort: /api/me runs on every page load and must never fail
    because a delivery lookup did."""
    try:
        from ..notify import delivery, emailhint
        row = delivery.state_for(email)
        if not row:
            return None
        return {"state": row["state"],
                "bounce_type": row["bounce_type"],
                "reason": row["reason"],
                "suppressed": bool(row["suppressed"]),
                "since": (row["first_seen"].isoformat()
                          if row["first_seen"] else None),
                "did_you_mean": emailhint.suggest(email)}
    except Exception:                            # noqa: BLE001
        log.exception("delivery state lookup failed")
        return None


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    with _control_conn() as conn:
        u = conn.execute(
            "SELECT totp_secret, verified_at, created_at, "
            "second_factor_waived FROM users "
            "WHERE id=%s", (user["user_id"],)).fetchone()
        n_passkeys = conn.execute(
            "SELECT count(*) n FROM passkeys WHERE user_id=%s",
            (user["user_id"],)).fetchone()["n"]
        from ..auth import recovery
        recovery_left = recovery.remaining(conn, user["user_id"])
        # operator broadcast banner (set from the admin console)
        # a reboot broadcast whose deadline passed >5 min ago is OVER —
        # never serve it (the worker's sweep deletes it on its own cadence,
        # but the UX must not depend on that racing the reboot)
        bc = conn.execute(
            """SELECT message, severity, deadline FROM broadcast
               WHERE id = 1 AND (deadline IS NULL
                                 OR deadline > now() - interval '5 minutes')
            """).fetchone()
    # a "strong factor" is TOTP or at least one passkey
    # (or an operator-set per-user waiver)
    has_factor = bool((u and (u["totp_secret"] or u["second_factor_waived"]))
                      or n_passkeys)
    # entitlement tier + feature flags, as the installed gate reports
    # them, so the SPA can gate features. None (no gate installed) → the
    # SPA treats every feature as available. Best-effort; a lookup
    # failure must never break /api/me.
    plan = None
    features: dict = {}
    try:
        from ..db import tenancy as _ten
        tc = _ten.tenant_connect(user["tenant_id"])
        try:
            if ext.gate.billing_enabled():
                try:
                    plan = ext.gate.effective_tier(tc)
                    features = {f: ext.gate.tier_allows(tc, f) for f in
                                ("investments", "liabilities", "sms",
                                 "priority_sync", "business", "enrich")}
                except Exception:                # noqa: BLE001
                    plan, features = None, {}
            # The institution allowance is published whenever the server
            # WOULD ENFORCE it, which is not the same question as whether
            # the gate's payment rail is configured. Riding inside the
            # billing branch, an instance with no rail would show the
            # clients no cap while `sync.base.institution_cap` went on
            # refusing at the instance default. A household at the cap would then be
            # shown a live "Connect an account" button, pick their bank,
            # sign in at the bank, and have the token exchange refuse AFTER
            # Plaid had authorized it: the post-authorization refusal the
            # cap lines exist to prevent. Ask the same function the ADD
            # door asks, on
            # the same connection, so the figure on screen and the refusal
            # can never disagree. None means genuinely uncapped (self-host,
            # or an operator override of 0) — the key is then absent and
            # the clients render no allowance, which is the truth.
            from ..sync import base as _sync_base
            _cap = _sync_base.institution_cap(tc)
            if _cap is not None:
                features["institution_cap"] = _cap
                # …and how many of them are spent. The cap alone can only
                # be rendered as a promise ("up to N"); without the count,
                # the one number a household actually needs — "how many do
                # I have left" — is nowhere in the product.
                features["institutions_used"] = \
                    _sync_base.institution_count(tc)
        finally:
            tc.close()
    except Exception:                            # noqa: BLE001
        plan, features = None, {}
    # does this tenant actually run a business? The Business tab is
    # hidden until they set one up in the wizard — most
    # households never will, and a permanently empty top-level tab is clutter
    # that makes the app look like it is for someone else. Counted, not
    # listed: /api/me is on every page load and the payload should not grow
    # with the number of entities. Archived entities still count — someone who
    # closed their LLC keeps the tab, because their books and past Schedule C
    # data are still in there and hiding the way back to them would be worse
    # than one extra tab. Best-effort: a failure here hides the tab rather
    # than breaking /api/me, and the route stays reachable either way.
    has_business = False
    # the household's daily-email schedule and whether THIS login is muted
    # from it — a viewer's own delivery control; the
    # schedule itself stays the owner's to set
    daily_email = {"on": True, "hour": 7, "summary": False, "muted": False}
    merchant_logos = True
    from .. import localtime as _localtime
    _tz_name, _tz_source = _localtime.instance_tz_name(), "instance"
    try:
        from ..db import tenancy as _ten
        tc = _ten.tenant_connect(user["tenant_id"])
        try:
            has_business = bool(tc.execute(
                "SELECT 1 FROM business_entity LIMIT 1").fetchone())
            cfg = budget.load_config(tc)
            merchant_logos = cfg.get("merchant_logos", True) is not False
            if _localtime.valid_zone(cfg.get("timezone")):
                _tz_name, _tz_source = cfg["timezone"], "household"
            from ..notify.schedule import effective_email_schedule
            d = (effective_email_schedule(cfg).get("daily") or {})
            if not isinstance(d, dict):
                d = {}
            muted = cfg.get("email_muted") or []
            if isinstance(muted, str):
                muted = [x for x in muted.split(",") if x.strip()]
            daily_email = {
                "on": bool(d.get("on", True)),
                "hour": int(d.get("hour", 7) or 7),
                "summary": bool(d.get("summary", False)),
                "muted": (user.get("email") or "").strip().lower() in
                         {str(m).strip().lower() for m in muted}}
        finally:
            tc.close()
    except Exception:                            # noqa: BLE001
        pass
    # "Support development" link. Points at the project donate page.
    # Shown by default on self-host, hidden by default on hosted. Env
    # OIKONOME_DONATE_URL overrides (set a URL to force-show; "off"/"" hides).
    # NB: compose passes OIKONOME_DONATE_URL="" when unset (the `:-` default),
    # so an empty string means "not configured" → fall through to the default;
    # only the literal "off" hides, and any real URL force-shows it.
    _donate_env = (os.environ.get("OIKONOME_DONATE_URL") or "").strip()
    if _donate_env.lower() == "off":
        donate_url = None
    elif _donate_env:
        donate_url = _donate_env
    else:
        donate_url = (None if env_flag("OIKONOME_HOSTED")
                      else "https://oikonome.com/donate")
    return {"email": user["email"], "tenant_id": str(user["tenant_id"]),
            # entitlement tier, or null when no gate is installed
            "plan": plan, "features": features,
            # true once a business exists → the SPA shows the
            # Business tab. False hides the TAB only, never the route.
            "has_business": has_business,
            # merchant logos on/off (Settings; default on) — every ledger
            # surface reads it here so the row component needs no extra fetch
            "merchant_logos": merchant_logos,
            # donate/support link (null → hide the SPA footer link)
            "donate_url": donate_url,
            # additive: lets the SPA Settings card show enrollment state
            "totp_enabled": bool(u and u["totp_secret"]),
            # hosted accounts verify their email; the SPA nags
            # (banner + resend) while false and hosted
            "verified": bool(u and u["verified_at"]),
            # account age (ISO) so the SPA can HOLD the verify
            # banner until day 3 — a fresh signup shouldn't be nagged on
            # its first login; only surface it once ≥3 days still unverified
            "created_at": (u["created_at"].isoformat()
                           if u and u["created_at"] else None),
            # null while there is no reason to think this address
            # is broken. Non-null means the mail provider has reported it
            # could not deliver, the scheduled sends are HELD, and the SPA
            # escalates the calm verify nudge into a warning that names the
            # provider's own reason. This one overrides the day-3 hold: a
            # recorded bounce is not a guess about a new account, it is a
            # fact about an address, and waiting three days to mention it
            # is three more mornings of silence.
            "email_delivery": _delivery_state_for(user["email"]),
            "daily_email": daily_email,
            # hosted accounts must enroll a second factor (TOTP or
            # passkey) before using the app — the SPA renders a blocking
            # enrollment gate while this is true. Viewers INCLUDED: a
            # household member is prompted like the owner on first login —
            # they read the same money and hold a standing credential to
            # it. Hosted-only (self-host LAN-http
            # can't do passkeys; TOTP still works but is not forced).
            "needs_2fa": env_flag("OIKONOME_HOSTED") and not has_factor,
            # unused recovery codes remaining (Settings shows this
            # + a regenerate control; a low count is a nudge to regenerate)
            "recovery_codes_left": recovery_left,
            # viewers get read-only chrome; members get the editing chrome
            # without the owner's account controls (connections, exports,
            # the household roster, billing)
            "role": user["role"],
            # operator broadcast — SPA renders a dismissible
            # banner while set (maintenance heads-up etc.)
            "broadcast": ({"message": bc["message"],
                           "severity": bc["severity"],
                           "deadline": (bc["deadline"].isoformat()
                                        if bc["deadline"] else None)}
                          if bc else None),
            # git describe from install time (SPA footer)
            "version": os.environ.get("OIKONOME_VERSION") or "dev",
            # hosted product swaps the provider grid's copy
            "hosted": env_flag("OIKONOME_HOSTED"),
            # the zone every scheduled hour is kept in, and whether it is
            # the household's own or the instance's default — clients
            # name it beside the hour pickers, and offer to set it when a
            # household has none and the device's zone differs
            "timezone": _tz_name,
            "timezone_source": _tz_source,
            "instance_timezone": _localtime.instance_tz_name(),
            # hosted one-door bank linking — true when the
            # PLATFORM aggregator credentials are present (env-resolved;
            # tenants never see or choose an aggregator on hosted). The
            # SPA's "Connect your accounts" card keys off this, showing
            # an honest not-yet-live note while false.
            "bank_link": env_flag("OIKONOME_HOSTED")
            and bool(os.environ.get("OIKONOME_PLAID_CLIENT_ID")
                     and os.environ.get("OIKONOME_PLAID_SECRET")),
            # demo instances render data doors + account
            # security disabled (server 403s back them up)
            "demo": demoguard.is_demo(user["tenant_id"])}


# ---- family members: invites + member management -----------------

def _owner_only(user: dict) -> None:
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")


@app.post("/api/invites",
          dependencies=[Depends(limit("invite_create", 10, 3600))])
def invite_create(request: Request, user: dict = Depends(current_user),
                  body: dict = Body(...)):
    """Mint a one-time share-link. Also emails it when the label is an
    address and SMTP is configured — but the URL in the response is the
    primary delivery (share it any way you like)."""
    demoguard.deny(user)
    from ..auth import invites
    _owner_only(user)
    # Minting a family invite is a
    # durable grant — a stolen session could add an attacker as a member
    # who survives the owner's password change. Password step-up + a rate
    # limit make a hijacked cookie insufficient, matching the 2FA doors.
    # And on a TOTP account, a LIVE code — the passkey_register
    # rationale applies verbatim here: a password+session thief must not be
    # able to mint themselves a durable member principal (claimable at their
    # own address, verified_at=now) without ever presenting the second
    # factor. Both proofs ride the elevation window.
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    label = str(body.get("label") or "")
    hosted = env_flag("OIKONOME_HOSTED")
    with _control_conn() as conn:
        try:
            inv = invites.create(
                conn, user["tenant_id"], user["user_id"],
                role=str(body.get("role") or permissions.DEFAULT_ROLE),
                label=label)
        except ValueError as e:
            raise HTTPException(400, str(e))
        # The copy-paste URL in the response may be Host-derived (the owner
        # sees it in their own browser — no forgery leverage). Emailing a
        # link is different: a forged Host header would mail an
        # attacker-domain link to a real recipient. So only EMAIL when
        # OIKONOME_BASE_URL is pinned (mirrors the password-reset rule).
        pinned = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
        base = pinned \
            or f"{request.headers.get('x-forwarded-proto', request.url.scheme)}://{request.headers.get('host', request.url.netloc)}"
        # Mint the SPA claim URL directly. The /invite/{token} form
        # 303-bounces through the server, landing the live credential in TWO
        # access-log lines (path form, then query form) per click; the direct
        # form is one request. The path route stays for shared links.
        url = f"{base}/app/invite?token={inv['token']}"
        emailed = False
        to = invites.bound_address(inv["label"])
        if to and pinned:
            url = f"{pinned}/app/invite?token={inv['token']}"  # pinned origin
            from . import report
            tconn = tenancy.tenant_connect(user["tenant_id"])
            try:
                smtp = report.resolve_smtp(tconn)
            finally:
                tconn.close()
            if smtp["configured"]:
                try:
                    report.send(
                        "You're invited to Oikonome",
                        f"Create your account here (link valid 7 days, "
                        f"one use):\n\n{url}\n",
                        f"<p><a href=\"{url}\">Create your account</a> "
                        f"(link valid 7 days, one use).</p>",
                        # single-recipient transactional mail: To=the
                        # invitee. The bcc default would put To=sender +
                        # Bcc=invitee, sending the invite TWICE — once to
                        # the invitee and once to the sending mailbox
                        [to], smtp=smtp, bcc=False)
                    emailed = True
                except Exception:                     # noqa: BLE001
                    logging.getLogger("oikonome.web").exception(
                        "invite email failed")
        if hosted:
            # On hosted, DELIVERY is what proves the mailbox — the claim
            # door has no other proof, and `auth/invites` binds the account
            # it creates to this address on the strength of it. So the link
            # must reach that mailbox and nowhere else: it is never handed
            # back to the minter (who would otherwise be able to claim a
            # stranger's address themselves), and an invite that could not be
            # sent is destroyed rather than left lying in the table as a
            # token only the sending failure knows.
            if not emailed:
                invites.revoke(conn, user["tenant_id"], inv["token_hash"])
                raise HTTPException(
                    502, "couldn't email the invite — check the address and "
                         "try again in a minute")
            url = None
    return {"url": url, "role": inv["role"], "label": inv["label"],
            "expires_at": inv["expires_at"], "emailed": emailed}


@app.get("/api/invites")
def invite_list(user: dict = Depends(current_user)):
    from ..auth import invites
    _owner_only(user)
    with _control_conn() as conn:
        return {"invites": invites.pending(conn, user["tenant_id"])}


@app.delete("/api/invites/{token_hash}")
def invite_revoke(token_hash: str, user: dict = Depends(current_user)):
    demoguard.deny(user)
    from ..auth import invites
    _owner_only(user)
    with _control_conn() as conn:
        n = invites.revoke(conn, user["tenant_id"], token_hash)
    if not n:
        raise HTTPException(404, "no such pending invite")
    return {"ok": True}


@app.get("/api/users")
def users_list(user: dict = Depends(current_user)):
    """Household members — visible to everyone (shared truth), managed
    by the owner."""
    with _control_conn() as conn:
        rows = conn.execute(
            """SELECT id, email, role, created_at FROM users
               WHERE tenant_id = %s ORDER BY created_at""",
            (user["tenant_id"],)).fetchall()
    return {"users": [{**r, "id": str(r["id"]),
                       "me": str(r["id"]) == str(user["user_id"])}
                      for r in rows],
            # what the owner may pick in the roster, named by the server so
            # the two clients cannot drift from the policy or each other
            "assignable_roles": list(permissions.ASSIGNABLE_ROLES)}


@app.post("/api/users/{user_id}/role",
          dependencies=[Depends(limit("user_role", 20, 3600))])
def user_set_role(user_id: str, user: dict = Depends(current_user),
                  body: dict = Body(...)):
    """Move a household member between 'member' (can edit) and 'viewer'.

    Step-up, at the same bar as minting an invite or evicting somebody:
    handing edit rights to an account is a durable grant, and a stolen
    owner cookie must not be able to make one. The owner's own role is not
    editable here — there is one owner, and passing the account to
    somebody else is a different act than changing what a member may do.
    """
    demoguard.deny(user)
    _owner_only(user)
    import uuid as uuid_mod
    try:
        target = uuid_mod.UUID(str(user_id))
    except ValueError:
        raise HTTPException(404, "no such member")
    role = str(body.get("role") or "").strip().lower()
    if role not in permissions.ASSIGNABLE_ROLES:
        raise HTTPException(
            400, "role must be one of "
                 + ", ".join(permissions.ASSIGNABLE_ROLES))
    # Both refusals are decided BEFORE the step-up, so a request that could
    # never change anything cannot spend a single-use recovery code or burn
    # the TOTP counter (as on the member-removal door).
    if str(target) == str(user["user_id"]):
        raise HTTPException(400, "you can't change your own role")
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    # ADMIN connection, like every other write to a users column that
    # confers privilege: the app role's UPDATE grant is column-scoped
    # precisely so an injected statement cannot rewrite this one. Widening
    # that grant to add `role` would hand SQLi a promotion path; the
    # authorization above is what makes this write legitimate.
    admin = tenancy.admin_connect()
    try:
        n = admin.execute(
            "UPDATE users SET role = %s WHERE id = %s AND tenant_id = %s "
            # the owner row is excluded in SQL as well as above: the check
            # there is about the CALLER, this one is about the TARGET, and
            # a second owner (a restored one) must
            # not be demotable by whoever holds the other owner session
            "AND role <> 'owner'",
            (role, target, user["tenant_id"])).rowcount
    finally:
        admin.close()
    if not n:
        raise HTTPException(404, "no such member")
    return {"ok": True, "role": role}


@app.delete("/api/users/{user_id}",
            dependencies=[Depends(limit("user_remove", 10, 3600))])
def user_remove(user_id: str, user: dict = Depends(current_user),
                body: dict = Body(default=None)):
    """Remove a member (sessions cascade). Not yourself — an instance
    must never delete its last owner by accident. Step-up (password) like
    the other sensitive account changes: a stolen owner session shouldn't
    be able to evict the household's other members."""
    demoguard.deny(user)
    _owner_only(user)
    # A non-UUID id can never name a member — 404 before the step-up so a
    # single-use recovery code / passkey ticket is not consumed by a
    # request that could not remove anyone. Left to the DELETE's %s::uuid
    # cast, garbage would raise in Postgres as a 500 after the proof was spent.
    import uuid as uuid_mod
    try:
        target_uuid = uuid_mod.UUID(str(user_id))
    except ValueError:
        raise HTTPException(404, "no such member")
    # Refusing yourself is as doomed as a malformed id — decided before the
    # step-up for the same reason: a request that can never remove anyone
    # must not redeem a single-use recovery code or burn the TOTP counter.
    if str(target_uuid) == str(user["user_id"]):
        raise HTTPException(400, "you can't remove your own account")
    # Same bar as minting an invite: evicting a member is a durable change
    # to who can see the household, and a session-plus-password thief must
    # not be able to make it without the second factor.
    _require_elevation(
        user, password=str((body or {}).get("password") or ""),
        totp_code=str((body or {}).get("totp_code") or ""),
        recovery_code=str((body or {}).get("recovery_code") or ""))
    with _control_conn() as conn:
        n = conn.execute(
            "DELETE FROM users WHERE id = %s AND tenant_id = %s",
            (target_uuid, user["tenant_id"])).rowcount
    if not n:
        raise HTTPException(404, "no such member")
    return {"ok": True}


@app.get("/invite/{token}")
def invite_landing(token: str):
    """The share-link target: hand off to the SPA claim page."""
    from urllib.parse import quote
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/app/invite?token={quote(token, safe='')}",
                            status_code=303)


@app.get("/api/invite/peek")
def invite_peek(token: str = ""):
    """Claim-page greeting: is this link still good, and for which role?"""
    from ..auth import invites
    with _control_conn() as conn:
        inv = invites.peek(conn, token)
    if inv is None:
        raise HTTPException(404, "invalid, used, or expired invite")
    # `email` is the address this invite may be claimed by, "" when it names
    # none (self-host share links). The claim page fills and locks its field
    # from it, so the binding the server enforces is the one the person sees.
    return {"role": inv["role"], "label": inv["label"],
            "email": inv["email"]}


def _assert_tenant_accepts_members(conn, tenant_id) -> None:
    """The standing gates every write meets in current_user, applied to a
    claim — which is pre-auth and so never reaches them. A claim plants a
    verified viewer on the live ledger; an account that is suspended,
    scheduled for deletion, frozen or read-only must not grow
    a new member off a leftover share link, and a demo tenant never gets
    a real account at all. Device mint and script tokens already refuse
    this class for the same reason."""
    row = conn.execute("SELECT status FROM tenants WHERE id = %s",
                       (tenant_id,)).fetchone()
    status = row["status"] if row else None
    blocked = (status in LOCKED_OUT_STATUSES
               or _billing_write_blocked({"tenant_id": tenant_id})
               or demoguard.is_demo(tenant_id))
    if blocked:
        raise HTTPException(403, "this household is not accepting new "
                                 "members right now — ask the owner")


@app.post("/api/invite/claim",
          dependencies=[Depends(limit("invite_claim", 10, 3600))])
def invite_claim(request: Request, body: dict = Body(...)):
    from ..auth import invites
    with _control_conn() as conn:
        # Standing is checked BEFORE the burn, so a link a suspended
        # household hands out is refused rather than spent — it works
        # again the moment the household does.
        inv = invites.peek(conn, str(body.get("token") or ""))
        if inv is not None:
            _assert_tenant_accepts_members(conn, inv["tenant_id"])
        try:
            r = invites.claim(conn, str(body.get("token") or ""),
                              str(body.get("email") or ""),
                              str(body.get("password") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
    if r.get("verify_token"):
        # Hosted claims are created unverified (the claim form takes any
        # address, so verified_at must be earned by the mailbox owner) —
        # send the confirmation link out-of-band, like signup does.
        import threading
        threading.Thread(target=_deliver_verification,
                         args=(r["email"], r["verify_token"], r["tenant_id"]),
                         daemon=True).start()
    resp = _login_response(r["user_id"], r["tenant_id"],
                           request.headers.get("user-agent", ""),
                           request=request)
    return resp


# ---- passkeys (optional WebAuthn upgrade) --------------------------

def _require_hosted() -> None:
    """Passkey sign-in is offered when the instance runs in hosted mode —
    a real domain + cert + a public login, where phishing-resistance
    matters. A self-hosted LAN instance has no public login to protect and
    stays on password (+ optional TOTP); WebAuthn needs HTTPS anyway.
    These routes 404 off the hosted flag, so the feature is fully absent
    there."""
    if not env_flag("OIKONOME_HOSTED"):
        raise HTTPException(404, "not found")


def _rp(request: Request) -> tuple[str, str]:
    """(rp_id, origin) for WebAuthn.

    Hosted installs pin both to OIKONOME_BASE_URL when set — reverse-proxy
    header gaps (missing X-Forwarded-Proto) otherwise yield origin
    ``http://app…`` while the browser signs ``https://app…``, and every
    assertion fails verify. Self-host / unset base still uses the
    trust-gated Host/X-Forwarded-* path."""
    base = (os.environ.get("OIKONOME_BASE_URL") or "").rstrip("/")
    if base:
        from urllib.parse import urlsplit
        u = urlsplit(base)
        if u.hostname and u.scheme in ("http", "https"):
            return u.hostname, f"{u.scheme}://{u.hostname}" + (
                f":{u.port}" if u.port and u.port not in (80, 443) else "")
    host = forwarded_host(request)
    scheme = request_scheme(request)
    return host.split(":")[0], f"{scheme}://{host}"


@app.post("/api/passkeys/options",
          dependencies=[Depends(limit("passkey_options", 30, 3600))])
def passkey_register_options(request: Request,
                             user: dict = Depends(current_user),
                             body: dict = Body(default={})):
    """Mint the registration challenge — only once the password (and, on a
    TOTP account, a current code) has been checked. The authenticator
    creates and SAVES the credential between this call and /api/passkeys,
    so a wrong password caught only at persist time leaves the person with
    a passkey in their vault that the server never kept. Rate-limited in
    its own bucket since a challenge mint is a password check (the wrong
    password also counts at the login door, via _check_password)."""
    demoguard.deny(user)
    _require_hosted()
    if body.get("password"):
        # LEGACY: per-request password (+ code), checked without consuming
        _step_up_precheck(user["user_id"], str(body.get("password") or ""),
                          str(body.get("totp_code") or ""))
    else:
        # the window is the same non-consuming check: refusing to mint the
        # challenge here is what keeps a never-persisted key out of the
        # user's vault
        _require_elevation(user)
    from ..auth import passkeys
    rp_id, _ = _rp(request)
    with _control_conn() as conn:
        return passkeys.register_options(conn, user["user_id"],
                                         user["email"], rp_id)


@app.post("/api/passkeys",
          dependencies=[Depends(limit("passkey_manage", 10, 3600))])
def passkey_register(request: Request, user: dict = Depends(current_user),
                     body: dict = Body(...)):
    """Enrolling a passkey is a security-posture change — a stolen
    session could otherwise add its own key and keep access after the owner
    changes their password. Re-auth on the route that PERSISTS the key
    (options only mints a challenge, which is worthless without this)."""
    demoguard.deny(user)
    _require_hosted()
    # a TOTP account must ALSO present a live code to bind a NEW passkey —
    # otherwise a password+session thief enrolls their own key and bypasses
    # TOTP forever (passkey login stands in for the whole factor). Mirrors
    # totp_confirm's bind-time guard; the window collected both proofs.
    _require_elevation(user, password=str(body.get("password") or ""),
                       totp_code=str(body.get("totp_code") or ""),
                       recovery_code=str(body.get("recovery_code") or ""))
    from ..auth import passkeys
    rp_id, origin = _rp(request)
    # A passkey can be a user's FIRST strong factor (no TOTP). Like
    # totp_confirm, minting the first factor issues one-time recovery codes so
    # a lost passkey isn't a lockout AND so posture-change step-up has a factor
    # to redeem (_require_passkey_stepup). Decided under the same per-user
    # advisory lock totp_confirm and the last-factor removals hold, in the
    # transaction that inserts the key: decided on a plain read, a TOTP
    # enrolment on another session could land between the read and the
    # insert and both requests would mint a recovery batch — recovery.issue
    # replaces, so the codes one of them had already shown would die silently.
    # A SESSION-level advisory lock (not a transaction one) on purpose:
    # register_verify burns the single-use WebAuthn challenge before it
    # verifies, and that burn has to COMMIT even when verification fails —
    # inside one transaction a failed verify would roll the burn back and
    # the challenge stay replayable for its whole TTL. Autocommit statements
    # under a session lock keep the burn and still serialise the first-
    # factor decision against totp_confirm and the last-factor removals
    # (session and transaction advisory locks share one lock space).
    # pg_advisory_unlock_all in `finally` so a pooled connection can never
    # carry the lock back to the pool.
    with _control_conn() as c0:
        maybe_first = (c0.execute(
            "SELECT totp_secret FROM users WHERE id=%s",
            (user["user_id"],)).fetchone() or {}).get("totp_secret") is None \
            and not _has_passkey(user["user_id"])
    admin = tenancy.admin_connect() if maybe_first else None
    try:
      with _control_conn() as conn:
        conn.execute("SELECT pg_advisory_lock(hashtext(%s))",
                     ("oikonome:passkeys:" + str(user["user_id"]),))
        try:
            cur = conn.execute(
                "SELECT totp_secret, (SELECT count(*) FROM passkeys "
                "WHERE user_id = %s) AS n_pk FROM users WHERE id = %s",
                (user["user_id"], user["user_id"])).fetchone()
            first_factor = not (cur and (cur["totp_secret"] or cur["n_pk"]))
            try:
                result = passkeys.register_verify(
                    conn, user["user_id"],
                    str(body.get("challenge_id") or ""),
                    body.get("credential") or {}, rp_id, origin,
                    label=str(body.get("label") or ""))
            except ValueError as e:
                raise HTTPException(400, str(e))
            except Exception as e:                # noqa: BLE001 — lib errors
                raise HTTPException(
                    400, f"passkey rejected: {type(e).__name__}")
            if first_factor:
                # The first strong factor is the same posture change as
                # totp_confirm and gets the same eviction: every other
                # session, device token, script token and mint ticket a
                # hijacker could be holding dies here, or the passkey the
                # owner just added to close a hijack closes nothing.
                token = request.cookies.get(sessions.COOKIE_NAME, "")
                sessions.revoke_all_others(conn, user["user_id"], token)
                _revoke_bearer_persistence(conn, user)
                from ..auth import recovery
                if admin is None:          # the cheap read was stale; rare
                    admin = tenancy.admin_connect()
                # ALWAYS a fresh batch, never "keep whatever is left":
                # codes left over from before a reset (or a prior
                # enrolment) are live bypass primitives for passkey login
                # and passkey step-up, and the owner may believe they were
                # voided. Minted while the lock is held, so the batch the
                # response carries is the batch that stands.
                result = {**result, "recovery_codes":
                          recovery.issue(admin, user["user_id"])}
        finally:
            conn.execute("SELECT pg_advisory_unlock_all()")
    finally:
        if admin is not None:
            admin.close()
    return result


@app.get("/api/passkeys")
def passkey_list(user: dict = Depends(current_user)):
    _require_hosted()
    with _control_conn() as conn:
        rows = conn.execute(
            """SELECT id, label, transports, created_at, last_used
               FROM passkeys WHERE user_id = %s ORDER BY created_at""",
            (user["user_id"],)).fetchall()
    return {"passkeys": [{**r, "id": str(r["id"])} for r in rows]}


@app.delete("/api/passkeys/{pk_id}",
            dependencies=[Depends(limit("passkey_manage", 10, 3600))])
def passkey_delete(pk_id: str, user: dict = Depends(current_user),
                   body: dict = Body(default=None)):
    """Removing a factor is a posture change too — a stolen session must
    not be able to strip the owner's keys off the account.

    For a passkey-only account this is the takeover linchpin: deleting the
    last passkey drops the account to zero strong factors, which re-enables
    password-only login (_passkey_only_login_blocked goes False) and hands
    a session+password thief durable access. So step-up demands a recovery
    code for passkey-only accounts (no exemption), AND removing the LAST
    strong factor is refused while forced-2FA is in effect — the account
    must always keep at least one passkey or TOTP."""
    demoguard.deny(user)
    _require_hosted()
    # A non-UUID id can never name a passkey, so answer 404 up front —
    # before the guard and, crucially, before _step_up, which consumes a
    # single-use recovery code / passkey ticket. Left to the DELETE's
    # %s::uuid cast, garbage would raise in Postgres as a 500 after the proof
    # was already spent on an operation that could not happen.
    import uuid as uuid_mod
    try:
        pk_uuid = uuid_mod.UUID(str(pk_id))
    except ValueError:
        raise HTTPException(404, "no such passkey")
    # last-factor guard BEFORE step-up so a recovery code / passkey ticket
    # is not burned when the delete would be refused. An operator-waived
    # account (forced-2FA never applied to it) may drop its last passkey —
    # the same exemption the device-mint standing check already grants.
    with _control_conn() as conn:
        row = conn.execute(
            "SELECT totp_secret, second_factor_waived, "
            "(SELECT count(*) FROM passkeys WHERE user_id = %s) AS n_pk "
            "FROM users WHERE id = %s",
            (user["user_id"], user["user_id"])).fetchone()
        if (row and not row["totp_secret"]
                and not row["second_factor_waived"] and row["n_pk"] <= 1):
            raise HTTPException(
                400, "last_factor: this is your only two-factor method — add "
                     "another passkey or an authenticator app before removing "
                     "it")
    # Same extra factor as enroll: a session+password thief must not
    # strip the owner's passkeys (passkey-only accounts owe the ticket /
    # recovery code, at elevate time or in the legacy body).
    _require_elevation(
        user, password=str((body or {}).get("password") or ""),
        totp_code=str((body or {}).get("totp_code") or ""),
        recovery_code=str((body or {}).get("recovery_code") or ""))
    with _control_conn() as conn:
        # The pre-check above ran on another connection, before an argon2
        # verify, so two deletes of two different keys can each have seen
        # "two passkeys" and both proceed — leaving zero and re-enabling
        # password-only login. Re-check and delete under a per-user
        # advisory lock in ONE transaction, so the two serialize and the
        # second sees the first's delete.
        with conn.transaction():
            _lock_user_factors(conn, user["user_id"])
            row = conn.execute(
                "SELECT totp_secret, second_factor_waived, "
                "(SELECT count(*) FROM passkeys WHERE user_id = %s) AS n_pk "
                "FROM users WHERE id = %s",
                (user["user_id"], user["user_id"])).fetchone()
            if (row and not row["totp_secret"]
                    and not row["second_factor_waived"] and row["n_pk"] <= 1):
                raise HTTPException(
                    400, "last_factor: this is your only two-factor method "
                         "— add another passkey or an authenticator app "
                         "before removing it")
            n = conn.execute(
                "DELETE FROM passkeys WHERE id = %s AND user_id = %s",
                (pk_uuid, user["user_id"])).rowcount
            # A live session or device may be carrying an elevation stamp
            # that NAMES this key. The column has no foreign key
            # (migration 120), so nothing else clears it, and a credential
            # rotation inside the remaining window reads that name as
            # "keep this one" — deleting every OTHER key to spare one that
            # no longer exists, which on a passkey-only account is the
            # account's last factor.
            #
            # The whole stamp goes, `elevated_at` included, not just the
            # id: a sudo window is only as good as the credential that
            # opened it, and this one has just been destroyed by its own
            # owner. Clearing the id alone would leave the window open
            # with no key named, which is the lost-authenticator road —
            # and that road evicts EVERY key, so the same last factor
            # would still go. Closing it costs one re-confirmation with a
            # key they still have; leaving it open costs the factor.
            # Rows whose stamp names a key that still exists, or that was
            # opened by a password or a recovery code, are untouched.
            for tbl in ("sessions", "device_tokens"):
                conn.execute(
                    f"UPDATE {tbl} SET elevated_at = NULL, "             # noqa: S608 — table name is a literal from the tuple above
                    "elevated_passkey_id = NULL "
                    "WHERE user_id = %s AND elevated_passkey_id = %s",
                    (user["user_id"], pk_uuid))
    if not n:
        raise HTTPException(404, "no such passkey")
    return {"ok": True}


# Inline step-up assertion for destructive actions on a passkey
# account. options → navigator.credentials.get() → verify mints a 5-minute
# single-use ticket the SPA passes in the recovery_code field of whatever
# destructive call it was making. The ticket is worthless without the
# physical authenticator (UV enforced on the signed response).
@app.post("/api/stepup/passkey/options",
          dependencies=[Depends(limit("stepup_passkey", 20, 3600))])
def stepup_passkey_options(request: Request,
                           user: dict = Depends(current_user)):
    _require_hosted()
    from ..auth import passkeys
    rp_id, _ = _rp(request)
    with _control_conn() as conn:
        try:
            return passkeys.stepup_options(conn, user["user_id"], rp_id)
        except ValueError as e:
            raise HTTPException(400, str(e))


@app.post("/api/stepup/passkey",
          dependencies=[Depends(limit("stepup_passkey", 20, 3600))])
def stepup_passkey(request: Request, user: dict = Depends(current_user),
                   body: dict = Body(...)):
    _require_hosted()
    from ..auth import passkeys
    rp_id, origin = _rp(request)
    with _control_conn() as conn:
        try:
            ticket = passkeys.stepup_verify(
                conn, user["user_id"], str(body.get("challenge_id") or ""),
                body.get("credential") or {}, rp_id, origin)
        except ValueError as e:
            raise HTTPException(401, str(e))
        except Exception as e:                    # noqa: BLE001 — lib errors
            raise HTTPException(401, f"passkey check failed: "
                                     f"{type(e).__name__}")
    return {"stepup_ticket": ticket}


@app.post("/api/login/passkey/options",
          dependencies=[Depends(limit("passkey_login", 20, 3600))])
def passkey_login_options(request: Request, body: dict = Body(default=None)):
    """email is optional. Empty → discoverable (usernameless) passkey login;
    with email → allow-list that user's credentials (helps non-resident keys)."""
    _require_hosted()
    from ..auth import passkeys
    rp_id, _ = _rp(request)
    with _control_conn() as conn:
        return passkeys.login_options(conn, rp_id,
                                      email=str((body or {}).get("email") or ""))


@app.post("/api/login/passkey",
          dependencies=[Depends(limit("passkey_login", 20, 3600))])
def passkey_login(request: Request, body: dict = Body(...)):
    _require_hosted()
    from ..auth import passkeys
    rp_id, origin = _rp(request)
    with _control_conn() as conn:
        try:
            r = passkeys.login_verify(
                conn, str(body.get("challenge_id") or ""),
                body.get("credential") or {}, rp_id, origin)
        except ValueError as e:
            log.warning("passkey login rejected (ValueError): %s rp_id=%s origin=%s",
                        e, rp_id, origin, extra={"no_capture": True})
            raise HTTPException(401, str(e))
        except Exception as e:                    # noqa: BLE001 — lib errors
            # Surface the exception type + short message so the login page
            # can show something actionable (origin/rp mismatch etc.) without
            # leaking crypto material.
            msg = f"{type(e).__name__}: {e}"
            log.warning("passkey login failed: %s rp_id=%s origin=%s",
                        msg[:200], rp_id, origin, extra={"no_capture": True})
            raise HTTPException(401, f"passkey sign-in failed: "
                                     f"{type(e).__name__}")
    return _login_response(r["user_id"], r["tenant_id"],
                           request.headers.get("user-agent", ""),
                           request=request)


# ---- session management ----------------------------------------------------

@app.get("/api/sessions")
def sessions_list(request: Request, user: dict = Depends(current_user)):
    """The user's live sessions. Exposes the surrogate UUID id only —
    token hashes never leave the server."""
    # Demo instances expose NOTHING about who is signed in. The demo
    # account is shared, so the session list is a
    # roster of strangers — their browsers, their activity times, and a
    # revoke button pointed at each other. Empty list, not 403: the SPA
    # hides the card, and an empty roster is the honest answer for an
    # instance where sessions are nobody's business.
    from . import demoguard
    if demoguard.is_demo(user["tenant_id"]):
        return {"sessions": [], "hidden": True}
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    with _control_conn() as conn:
        rows = sessions.list_sessions(conn, user["user_id"], token)
    return {"sessions": [{
        "id": str(r["id"]),
        "created_at": r["created_at"].isoformat(),
        "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
        # both clients print these on every row; leaving them out crashes
        # the native Security screen on a fresh account
        "expires_at": r["expires_at"].isoformat() if r.get("expires_at") else None,
        "ip": r.get("ip"),
        "user_agent": r["user_agent"] or "",
        "current": bool(r["current"]),
    } for r in rows]}


# Throttled like every other password-verifying door — unthrottled it is
# one more re-auth surface a session thief can grind.
@app.post("/api/sessions/revoke",
          dependencies=[Depends(limit("sessions_revoke", 10, 3600))])
def sessions_revoke(request: Request, user: dict = Depends(current_user),
                    body: dict = Body(...)):
    """{"all_others": true} — sign out everywhere else;
    {"id": "<uuid>"} — revoke one session (own sessions only)."""
    # The list is empty on demo; revoke must not still kick every
    # concurrent explorer on the shared account.
    demoguard.deny(user)
    import uuid as uuid_mod
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    with _control_conn() as conn:
        if body.get("all_others"):
            # "sign out everywhere
            # else" is destructive (a thief could kick the real owner to
            # hold an exclusive window) — gate it on the password like the
            # other posture changes. Revoking ONE named session stays
            # frictionless (you managing your own device list).
            # A TOTP account owes a live code here too, exactly as it does
            # for passkey add/remove: kicking every other session is how a
            # password+session thief locks the owner out of their own
            # account, and the password alone is precisely the thing the
            # second factor exists to stop being sufficient.
            _require_elevation(
                user, password=str(body.get("password") or ""),
                totp_code=str(body.get("totp_code") or ""),
                recovery_code=str(body.get("recovery_code") or ""))
            n = sessions.revoke_all_others(conn, user["user_id"], token)
            # The Settings card lists phones next to browsers and the
            # bulk button is "everywhere else" — leaving oikd_ tokens
            # live after this click is the same session the user just
            # thought they kicked.
            device_tokens.revoke_all_for_user(
                conn, user["user_id"],
                keep_device_id=user.get("device_id"))
            # ...and the unspent mint ticket from any of those logins: a
            # 5-minute bearer that plants a fresh 90-day device token, so
            # left alive it survives the very click meant to evict it.
            # Script tokens are deliberately NOT touched here: they are
            # not sessions, they are listed and revoked one by one under
            # Settings → Connections, and a password change already kills
            # them all.
            device_tokens.revoke_mint_tickets(conn, user["user_id"])
            # "everywhere else" includes the other browsers' push: the
            # requesting browser names its own endpoint to keep it
            _drop_web_push(conn, user["user_id"],
                           str(body.get("keep_push_endpoint") or ""))
        elif body.get("id"):
            try:
                sid = uuid_mod.UUID(str(body["id"]))
            except ValueError:
                raise HTTPException(400, "bad session id")
            # Revoking ANOTHER session is the same destructive
            # "kick the owner to hold an exclusive window" action as
            # all_others — a session-only thief could otherwise LOOP per-id
            # revokes (the list endpoint hands out every id) to reproduce it,
            # bypassing the all_others step-up. Gate it the same way. Revoking
            # your OWN current session is just logout — stays frictionless.
            cur = conn.execute("SELECT id FROM sessions WHERE token_hash=%s",
                               (sessions._h(token),)).fetchone()
            if not (cur and cur["id"] == sid):
                # Same live-code requirement as all_others — otherwise the
                # per-id loop is a password-only route to the identical
                # outcome, which is the hole the all_others step-up closed.
                _require_elevation(
                    user, password=str(body.get("password") or ""),
                    totp_code=str(body.get("totp_code") or ""),
                    recovery_code=str(body.get("recovery_code") or ""))
            n = sessions.revoke_by_id(conn, user["user_id"], sid)
            # Same mint-ticket burn as all_others — the per-id route must
            # not be the weaker of the two doors to the same outcome; and
            # revoking your OWN session is a logout, which burns it too.
            device_tokens.revoke_mint_tickets(conn, user["user_id"])
            # A push subscription is not tied to a session, so the kicked
            # browser's cannot be singled out: kicking ANOTHER session
            # drops every subscription but this browser's own. The one
            # someone reaches for after spotting a stranger's session must
            # not be the door that leaves that stranger's push alive.
            if not (cur and cur["id"] == sid):
                _drop_web_push(conn, user["user_id"],
                               str(body.get("keep_push_endpoint") or ""))
        else:
            raise HTTPException(400, "pass all_others: true or id")
    return {"ok": True, "revoked": n}


# A session that just proved the account credentials vouches for itself
# for this long; past it, planting persistence takes the password again.
_DEVICE_MINT_FRESH_AUTH = dt.timedelta(minutes=5)


@app.post("/api/devices", dependencies=[Depends(limit("device_mint", 20, 3600))])
def device_mint_ticketed(request: Request, body: dict = Body(...)):
    """The cookie-free half of the mint, for native apps.

    A `mint_ticket` from the login response stands in for the fresh
    session the cookie path proves: same freshness window, same one
    grant, and single-use — so it authenticates this request by itself
    and never reaches `current_user`. Without a ticket the request falls
    through to the session-cookie path below, which is what browsers and
    every existing client use.
    """
    ticket = body.get("mint_ticket")
    if not isinstance(ticket, str) or not ticket:
        return _device_mint_session(request, current_user(request), body)
    # One transaction for the whole exchange. Redeeming is a DELETE, so
    # anything that raises below — a closed account, the device ceiling,
    # a dropped connection — rolls the redemption back and the phone can
    # simply retry. A ticket must never be spent by a mint that did not
    # happen: its owner would be told to sign in again with nothing wrong
    # at their end.
    with _control_conn() as conn:
        with conn.transaction():
            user_id = device_tokens.redeem_mint_ticket(conn, ticket)
            if user_id is None:
                raise HTTPException(401, "sign in again on the device")
            row = conn.execute(
                """SELECT u.id, u.tenant_id, u.role,
                          (u.totp_secret IS NOT NULL
                           OR u.second_factor_waived) AS has_totp,
                          coalesce(t.status, 'active') AS tenant_status
                     FROM users u JOIN tenants t ON t.id = u.tenant_id
                    WHERE u.id = %s""", (user_id,)).fetchone()
            if row is None:
                raise HTTPException(401, "sign in again on the device")
            user = {"user_id": str(row["id"]),
                    "tenant_id": str(row["tenant_id"])}
            demoguard.deny(user)
            _assert_standing_allows_device(conn, row)
            _replace_device(conn, user["user_id"], body)
            minted = device_tokens.mint(
                conn, user["user_id"], user["tenant_id"],
                str(body.get("device_name") or ""),
                str(body.get("platform") or ""))
            if minted is None:
                raise _device_limit_error(conn, user["user_id"])
    token, drow = minted
    return {"token": token, "id": str(drow["id"]),
            "device_name": drow["device_name"]}


class _MintRefused(Exception):
    """Internal: unwinds the mint transaction so a rolled-back swap is
    reported with the roster as it stands AFTER the rollback."""


def _device_limit_error(conn, user_id) -> HTTPException:
    """The ceiling refusal, WITH the roster needed to act on it.

    A bare sentence — "revoke an unused device in Settings first" —
    reaches a phone sitting on a sign-in screen it cannot get past, so the
    Settings it names are on the other side of the door and the only way
    through is to find another device. The list travels with the error
    instead, and the phone offers to sign one out
    and carry on; nothing here is a secret the caller has not already
    proved they own (they completed a full login seconds ago), and it is
    the same roster /api/devices serves that account.
    """
    rows = device_tokens.list_for_user(conn, user_id)
    return HTTPException(409, {
        "error": "device_limit",
        "message": (f"You're signed in on {len(rows)} devices, which is the "
                    f"limit. Sign one out to add this one."),
        "limit": device_tokens.MAX_ACTIVE,
        "devices": [{"id": str(r["id"]),
                     "device_name": r["device_name"],
                     "platform": r["platform"],
                     "created_at": r["created_at"].isoformat(),
                     "last_seen": (r["last_seen"].isoformat()
                                   if r["last_seen"] else None)}
                    for r in rows],
    })


def _replace_device(conn, user_id, body: dict) -> None:
    """Sign out one of this user's devices to make room for this one.

    Revoking someone else's device normally takes a password step-up (and
    a live code with TOTP on) — the "kick a device" posture change. This
    door is exempt on purpose and the exemption is narrow: the caller has
    just completed a FULL login on this device, second factor included,
    which is strictly more than the step-up re-proves, and the revoke runs
    in the same transaction as the mint that motivated it — so a mint that
    still fails takes the revocation back with it and nobody loses a
    device for nothing.
    """
    replace = body.get("replace")
    if not replace:
        return
    if not isinstance(replace, str):
        raise HTTPException(400, "replace must be a device id")
    try:
        uuid.UUID(replace)
    except ValueError:
        raise HTTPException(400, "replace must be a device id")
    # scoped to the owner inside revoke_by_id — an id from another
    # account revokes nothing and is reported as not found rather than
    # silently succeeding, which would read as "I signed that out" about
    # a device that is still live
    if not device_tokens.revoke_by_id(conn, user_id, replace):
        raise HTTPException(404, "that device is not on your account")


def _assert_standing_allows_device(conn, row) -> None:
    """The account-standing gates `current_user` applies to every other
    write, applied here too.

    The ticket authenticates the request by itself and so never reaches
    `current_user` — which is where suspension, pending deletion, every
    standing an installed gate enforces and the hosted second-factor
    requirement are all applied. A device token is durable, 90-day,
    full-access persistence, so minting one is exactly the act those
    gates exist to stop; skipping them would let an account that may not
    write at all walk away holding a standing credential.
    """
    status = row["tenant_status"]
    if status == "suspended":
        raise HTTPException(403, "this account is suspended — contact "
                                 "support")
    if status == "pending_delete":
        raise HTTPException(403, "this account is scheduled for deletion")
    # A read-only standing does NOT refuse the mint. The phone keeps no
    # cookie, so a fresh sign-in on the phone is the ONLY way its holder
    # reaches /api/billing — and current_user already confines a token
    # from such an account to exactly that door (402 everywhere else).
    # Refusing here would leave a phone that was away when the standing
    # lapsed with no way to resolve it at all.
    if env_flag("OIKONOME_HOSTED") and not row["has_totp"]:
        has_passkey = conn.execute(
            "SELECT 1 FROM passkeys WHERE user_id=%s LIMIT 1",
            (row["id"],)).fetchone() is not None
        if not has_passkey:
            raise HTTPException(
                403, "enroll a second factor (authenticator or passkey) "
                     "before adding a device")


def _device_mint_session(request: Request, user: dict, body: dict):
    """Mint a mobile device token for the signed-in user. Browser-session
    callers only: a device token minting further device tokens would be
    self-propagating persistence (revoking the phone should end the
    phone), and script tokens never reach here (path allowlist). The
    plaintext is returned exactly once.

    A device token is durable full-access persistence — the same class of
    grant as a script token, whose mint takes a password step-up because a
    stolen session could otherwise convert a dying cookie into a credential
    that outlives it. The one caller that must stay frictionless is the
    mobile app, which mints immediately after a full credential login and
    sends no password with the mint: a session still inside its first
    minutes IS the credential proof. Anything older owes the same
    _step_up as every other persistence-planting door."""
    demoguard.deny(user)
    if user.get("script_token") or user.get("device_id"):
        raise HTTPException(403, "sign in on the device to add it")
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    with _control_conn() as conn:
        row = conn.execute(
            "SELECT created_at > now() - %s AS fresh FROM sessions "
            "WHERE token_hash=%s",
            (_DEVICE_MINT_FRESH_AUTH, sessions._h(token))).fetchone()
    if not (row and row["fresh"]):
        # Same extra factor as script-token mint: a session+password
        # thief must not convert a dying cookie into a 90-day bearer
        # that never asks TOTP again.
        _require_elevation(
            user, password=str(body.get("password") or ""),
            totp_code=str(body.get("totp_code") or ""),
            recovery_code=str(body.get("recovery_code") or ""))
    minted = None
    err = None
    with _control_conn() as conn:
        # one transaction, same reason as the ticket path: a swap that
        # ends in a refused mint must not leave a device signed out for
        # nothing. The refusal is built INSIDE the transaction (it reads
        # the roster) and raised outside it, so the rollback happens
        # first and the list the caller receives is the one that is
        # still true afterwards.
        try:
            with conn.transaction():
                _replace_device(conn, user["user_id"], body)
                minted = device_tokens.mint(
                    conn, user["user_id"], user["tenant_id"],
                    str(body.get("device_name") or ""),
                    str(body.get("platform") or ""))
                if minted is None:
                    err = _device_limit_error(conn, user["user_id"])
                    raise _MintRefused()
        except _MintRefused:
            pass
    if err is not None:
        raise err
    token, row = minted
    return {"token": token, "id": str(row["id"]),
            "device_name": row["device_name"]}


@app.get("/api/devices")
def devices_list(request: Request, user: dict = Depends(current_user)):
    """The user's live mobile devices — surrogate ids only, same contract
    as the sessions list (and the same demo-instance silence: the shared
    account's device roster is nobody's business)."""
    if demoguard.is_demo(user["tenant_id"]):
        return {"devices": [], "hidden": True}
    auth = request.headers.get("authorization", "")
    current = auth[len("Bearer "):] \
        if auth.startswith("Bearer " + device_tokens.PREFIX) else ""
    with _control_conn() as conn:
        rows = device_tokens.list_for_user(conn, user["user_id"], current)
    return {"devices": [{
        "id": str(r["id"]),
        "device_name": r["device_name"],
        "platform": r["platform"],
        "created_at": r["created_at"].isoformat(),
        "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
        "current": bool(r["current"]),
        "push": r["push_state"],       # "on" | "dead" | None (never asked)
    } for r in rows]}


@app.post("/api/devices/push",
          dependencies=[Depends(limit("device_push", 30, 3600))])
def device_push_register(request: Request, user: dict = Depends(current_user),
                         body: dict = Body(...)):
    """{"token": "<platform push token>", "platform": "fcm"|"apns"} —
    register this device's native push routing token, or clear it with
    {"token": null}. Device-token callers only: the push token belongs
    to the device that the platform issued it to, and binding it to the
    caller's own row means revoking the device also silences it."""
    demoguard.deny(user)
    if not user.get("device_id"):
        raise HTTPException(403, "sign in from the mobile app to register "
                                 "for push")
    token = body.get("token")
    if token is None:
        with _control_conn() as conn:
            device_tokens.set_push(conn, user["user_id"],
                                   user["device_id"], None)
        return {"ok": True, "registered": False}
    if not isinstance(token, str) or not 10 <= len(token) <= 4096:
        raise HTTPException(400, "bad push token")
    platform = str(body.get("platform") or "")
    if platform not in ("fcm", "apns"):
        raise HTTPException(400, "platform must be fcm or apns")
    # Enforce the platform's token alphabet at the door: APNs tokens are
    # hex, FCM tokens are URL-safe. The token later travels to the push
    # relay, which splices it into vendor URLs — a "token" carrying path or
    # query syntax must never get that far. The relay independently
    # enforces the same shape; this is the first of the two fences.
    shape = (r"[0-9a-fA-F]{16,200}" if platform == "apns"
             else r"[A-Za-z0-9_\-:.~%]{10,4096}")
    if not re.fullmatch(shape, token):
        raise HTTPException(400, "malformed token")
    with _control_conn() as conn:
        n = device_tokens.set_push(conn, user["user_id"],
                                   user["device_id"], token, platform)
    if not n:
        raise HTTPException(404, "device not found")
    return {"ok": True, "registered": True}


@app.post("/api/devices/revoke",
          dependencies=[Depends(limit("devices_revoke", 10, 3600))])
def devices_revoke(request: Request, user: dict = Depends(current_user),
                   body: dict = Body(...)):
    """{"id": "<uuid>"} — revoke one device (own devices only). The
    device revoking ITSELF (mobile sign-out) is frictionless; revoking
    any other is the same "kick the owner's device" posture change as
    session revocation, so it takes the password step-up — plus a live
    authenticator code when TOTP is enrolled."""
    demoguard.deny(user)
    if user.get("script_token"):
        raise HTTPException(403, "script tokens cannot manage devices")
    import uuid as uuid_mod
    try:
        did = uuid_mod.UUID(str(body.get("id") or ""))
    except ValueError:
        raise HTTPException(400, "bad device id")
    if str(user.get("device_id") or "") != str(did):
        # A phone is a signed-in session by another name, so kicking one
        # carries the same live-code requirement as session revocation.
        _require_elevation(
            user, password=str(body.get("password") or ""),
            totp_code=str(body.get("totp_code") or ""),
            recovery_code=str(body.get("recovery_code") or ""))
    with _control_conn() as conn:
        n = device_tokens.revoke_by_id(conn, user["user_id"], did)
    return {"ok": True, "revoked": n}


# ---- account deletion (the CCPA path) ---------------------------------------

# lives in tenancy.delete_tenant_rows (the demo hourly reset shares it);
# this alias serves existing callers/tests
_delete_tenant_rows = tenancy.delete_tenant_rows


# Same throttle as password_change — both take a password guess, and
# a stolen session could otherwise brute the password offline-speed against
# the single most destructive endpoint (full tenant wipe).
@app.post("/api/account/delete",
          dependencies=[Depends(limit("account_delete", 5, 3600))])
def account_delete(request: Request, user: dict = Depends(current_user),
                   password: str = Form(""), totp_code: str = Form(""),
                   recovery_code: str = Form("")):
    """Full erasure: a FRESH elevation (seconds old — an irreversible act
    must not ride a minutes-old proof), or the legacy in-body password
    (and the current TOTP code when 2FA is on); then deletes every tenant
    row across all tenant tables plus the control-plane user/sessions,
    atomically."""
    demoguard.deny(user)
    # viewers are already stopped by current_user's write gate
    # (this path isn't in VIEWER_WRITE_OK), but household erasure must not
    # depend on a path-prefix allowlist staying correct — gate it here too.
    _owner_only(user)
    if password or recovery_code:
        # LEGACY: the store apps re-prove in the body; the checks and their
        # wording are exactly what they were
        with _control_conn() as conn:
            u = conn.execute(
                "SELECT password_hash, totp_secret FROM users WHERE id=%s",
                (user["user_id"],)).fetchone()
        if not u or not passwords.verify_password(u["password_hash"],
                                                  password):
            security.record_auth_failure(security.current_ip() or "-",
                                         user.get("email", ""),
                                         route="account_delete")
            raise HTTPException(401, "wrong password")
        # The seed is stored encrypted at rest — decrypt before
        # verifying, like every other TOTP site. Verifying the raw
        # ciphertext would make erasure a permanent 401 for any 2FA user
        # on a keyed install, and invisibly so: without a master key
        # decrypt_cp passes plaintext straight through.
        if u["totp_secret"] and not _totp_consume(
                user["user_id"], u["totp_secret"], totp_code):
            raise HTTPException(401, "current one-time code required")
        # Irreversible wipe — a passkey-only account must present a
        # recovery code (password alone is insufficient, login refuses it)
        _require_passkey_stepup(user["user_id"], u["totp_secret"],
                                recovery_code)
    else:
        _require_elevation(user, fresh_seconds=60)
    # Release everything held at external services BEFORE the rows go —
    # aggregator items, the platform-side aggregator user, whatever an
    # installed gate holds. Best-effort: a vendor outage must not block an erasure the
    # user is legally owed. (SMS holds no per-tenant state at the provider
    # — see erasure.release_external, which records that the channel was
    # considered; that module's docstring is the erasure contract.)
    from .. import erasure as _erasure
    admin = tenancy.admin_connect()
    try:
        # An in-flight sync resurrects erased data: the domain tables
        # (items/accounts/transactions/…) have no FK to tenants — only the
        # explicit delete sweep removes their rows — so a sync that started
        # before the wipe happily upserts rows for a tenant whose erasure
        # already committed, with no owner left to notice. Two defenses:
        # take the tenant off 'active' first (delete_after=now() so the
        # nightly purge finishes the job if anything below fails), so the
        # worker's next sweep never picks it up; then BLOCK on the same
        # per-tenant advisory lock every sync door takes (worker.sync_tenant,
        # the wizard/manual sync doors, the reaper) — a sync already running
        # finishes and releases before the first row is deleted, and any new
        # sync's try-lock fails until the wipe is committed.
        admin.execute(
            "UPDATE tenants SET status='pending_delete', delete_after=now() "
            "WHERE id=%s", (user["tenant_id"],))
        admin.execute("SELECT pg_advisory_lock(hashtext(%s))",
                      (f"oikonome:sync:{user['tenant_id']}",))
        released = _erasure.release_external(user["tenant_id"])
        # Capture the non-secret retry identifiers WHILE the rows still
        # exist, then durably audit the release outcome (admin_audit
        # survives the wipe) — a transient vendor outage cannot
        # silently orphan a live external record with no trace and no
        # retry handle.
        retry_ids = _erasure.collect_retry_ids(user["tenant_id"])
        _erasure.audit_release(admin, user["tenant_id"], released, retry_ids)
        _delete_tenant_rows(admin, user["tenant_id"])
    finally:
        # explicit unlock — close() below would release it too (admin
        # connections are not pooled), but a failed erasure retried on this
        # connection must not stack lock acquisitions
        try:
            admin.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                          (f"oikonome:sync:{user['tenant_id']}",))
        except Exception:                        # noqa: BLE001
            pass
        admin.close()
    # the one mail that outlives the account: a receipt that the erasure
    # happened, to the address that asked for it (best-effort). An
    # installed gate appends whatever its own rails could NOT release, so
    # the receipt names anything the holder still has to cancel
    # themselves.
    from ..notify import account_mail
    xp, xh = ext.gate.erasure_receipt(None, user["tenant_id"], released)
    account_mail.account_deleted(user.get("email") or "",
                                 extra_plain=xp, extra_html=xh)
    resp = Response('{"ok": true}', media_type="application/json")
    _clear_session_cookie(resp, request)
    return resp


@app.post("/api/me/email",
          dependencies=[Depends(limit("me_email", 30, 3600))])
def me_email(user: dict = Depends(current_user), body: dict = Body(...)):
    """This login's own daily-email delivery: mute or unmute MY address in
    the household's `email_muted` list. Viewers may do this (it is in
    VIEWER_WRITE_OK): a household member controls whether the daily
    verdict reaches their own inbox; the schedule — hour, verdict-only vs
    full — stays the owner's. Nothing else in config is
    touched; the row lock in config_txn keeps a concurrent owner save
    from being lost."""
    demoguard.deny(user)
    if "muted" not in body or not isinstance(body["muted"], bool):
        raise HTTPException(400, "muted must be true or false")
    mine = (user.get("email") or "").strip().lower()
    if not mine:
        raise HTTPException(400, "no email on this account")
    tc = tenancy.tenant_connect(user["tenant_id"])
    try:
        with budget.config_txn(tc) as cfg:
            muted = cfg.get("email_muted") or []
            if isinstance(muted, str):
                muted = [x.strip() for x in muted.split(",") if x.strip()]
            muted = [m for m in muted if str(m).strip().lower() != mine]
            if body["muted"]:
                muted.append(mine)
            cfg["email_muted"] = muted
    finally:
        tc.close()
    return {"ok": True, "muted": body["muted"]}


@app.post("/api/account/leave",
          dependencies=[Depends(limit("account_leave", 5, 3600))])
def account_leave(request: Request, user: dict = Depends(current_user),
                  password: str = Form(""), totp_code: str = Form(""),
                  recovery_code: str = Form("")):
    """A household MEMBER deletes their own login. The
    owner's account and every tenant row stay exactly as they are — only
    this user's row goes (sessions, passkeys, device tokens, recovery
    codes cascade with it) plus their address off the household's mail
    lists. Same re-auth as the owner's erasure door. Owners cannot use
    it: an owner leaving would orphan the household — that is
    /api/account/delete."""
    demoguard.deny(user)
    if user["role"] == "owner":
        raise HTTPException(400, "the owner deletes the account under "
                                 "Settings → Delete account")
    if password or recovery_code:
        # LEGACY: in-body re-proof, checks and wording unchanged
        with _control_conn() as conn:
            u = conn.execute(
                "SELECT password_hash, totp_secret FROM users WHERE id=%s",
                (user["user_id"],)).fetchone()
        if not u or not passwords.verify_password(u["password_hash"],
                                                  password):
            security.record_auth_failure(security.current_ip() or "-",
                                         user.get("email", ""),
                                         route="account_leave")
            raise HTTPException(401, "wrong password")
        if u["totp_secret"] and not _totp_consume(
                user["user_id"], u["totp_secret"], totp_code):
            raise HTTPException(401, "current one-time code required")
        _require_passkey_stepup(user["user_id"], u["totp_secret"],
                                recovery_code)
    else:
        _require_elevation(user)
    mine = (user.get("email") or "").strip().lower()
    # off the household's recipient / mute lists (tenant config, row lock)
    tc = tenancy.tenant_connect(user["tenant_id"])
    try:
        with budget.config_txn(tc) as cfg:
            for key in ("email_recipients", "email_muted"):
                lst = cfg.get(key) or []
                if isinstance(lst, str):
                    lst = [x.strip() for x in lst.split(",") if x.strip()]
                cfg[key] = [m for m in lst
                            if str(m).strip().lower() != mine]
    finally:
        tc.close()
    admin = tenancy.admin_connect()
    try:
        admin.execute("DELETE FROM users WHERE id=%s AND role != 'owner'",
                      (user["user_id"],))
    finally:
        admin.close()
    resp = Response('{"ok": true}', media_type="application/json")
    _clear_session_cookie(resp, request)
    return resp


@app.post("/api/email/change",
          dependencies=[Depends(limit("email_change", 5, 3600))])
def email_change(request: Request, user: dict = Depends(current_user),
                 new_email: str = Form(...), password: str = Form(""),
                 totp_code: str = Form(""), recovery_code: str = Form(""),
                 keep_push_endpoint: str = Form("")):
    """Change the account email (= the login username). Re-auth like the
    other account-security doors (password + TOTP when enrolled). The write
    goes through the ADMIN connection on purpose: the app role is
    column-scoped OUT of users.email precisely so injected SQL cannot
    rewrite it, so this legitimate path uses admin and nothing reopens that
    grant. On a hosted instance the new address starts unverified and a
    verification mail goes out (the banner returns)."""
    demoguard.deny(user)
    new_email = new_email.strip().lower()
    if "@" not in new_email or len(new_email) > 254:
        raise HTTPException(400, "that doesn't look like an email address")
    # LEGACY: the store apps re-prove in the body, with the checks and
    # wording they were built against; anything else reads the window
    legacy = bool(password or recovery_code)
    with _control_conn() as conn:
        u = conn.execute(
            "SELECT password_hash, totp_secret, email FROM users WHERE id=%s",
            (user["user_id"],)).fetchone()
    if legacy and (not u or not passwords.verify_password(u["password_hash"],
                                                          password)):
        # a wrong password here is a password guess — feed the same
        # strike/lock/human-check signals as the login door
        security.record_auth_failure(security.current_ip() or "-",
                                     (u or {}).get("email", ""),
                                     route="email_change")
        raise HTTPException(401, "wrong password")
    if not u:
        raise HTTPException(401, "not signed in")
    if new_email == u["email"]:
        return {"ok": True, "email": new_email}   # no-op, not an error
    # Refuse the predictable failures BEFORE burning single-use factors.
    # Consuming the TOTP counter or a one-time recovery code and THEN
    # answering 409 for an address already in use would cost a passkey-only
    # user one of their eight codes on every attempt at a taken address.
    # Best-effort here; the authoritative check
    # re-runs under the advisory lock below (TOCTOU).
    with _control_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email=%s AND id!=%s",
                        (new_email, user["user_id"])).fetchone():
            raise HTTPException(409, "that email is already in use")
    # The email IS the login identifier and the verdict-email /
    # recovery channel — rotating it is a posture change. For a passkey-only
    # account the password alone is insufficient (mirrors password_change /
    # account_delete); demand a recovery code so a session+password thief
    # can't redirect the account's mail to themselves.
    if legacy:
        if u["totp_secret"] and not _totp_consume(
                user["user_id"], u["totp_secret"], totp_code):
            raise HTTPException(401, "current one-time code required")
        keep_passkey = _require_passkey_stepup(
            user["user_id"], u["totp_secret"], recovery_code)
    else:
        _require_elevation(user)
        # the ONE key the eviction below may keep — the credential that
        # proved this caller, not merely one that was used recently
        keep_passkey = _elevation_passkey(user)
    # …and only while that key still exists: a name whose row was deleted
    # in the meantime would evict every OTHER key and leave none.
    keep_passkey = _proved_passkey_or_refuse(user["user_id"], keep_passkey)
    hosted = env_flag("OIKONOME_HOSTED")
    token = request.cookies.get(sessions.COOKIE_NAME, "")
    # ONE transaction for the whole rotation, on ONE connection — the same
    # rule password_change records, for the same reason. The email IS the
    # login identifier, so changing it and evicting everything the old one
    # could still reach are a single act, and a rotation that half happens
    # is worse than one that does not. The eviction below can still REFUSE
    # (a passkey deleted between the pre-flight check and its lock), and
    # split across two autocommit connections that refusal would arrive
    # AFTER the address had already changed: the caller told 403 while
    # their login email was in fact new, their other sessions gone, and
    # their tokens, devices, push subscriptions and pending invites still
    # live. In one transaction the refusal rolls the whole act back, so
    # the answer is true either way.
    #
    # It is the ADMIN connection that carries both halves, because the
    # two halves are not symmetric: the app role is column-scoped OUT of
    # users.email (see db/migrate — so injected SQL cannot rewrite a login
    # identifier), while the owner role it connects as holds every
    # privilege the revocations need. Only the higher level can do all of
    # it, so the two halves need one connection at ONE level, not two.
    # Every table touched here is control-plane with no RLS, so the admin
    # connection reads and writes exactly the rows the app role would; and
    # every statement is a parameter-bound write keyed on this user's id,
    # so nothing user-supplied reaches the elevated session as SQL.
    admin = tenancy.admin_connect()
    try:
        # serialize on the target address (same TOCTOU guard as signup) so
        # two concurrent changes to one email can't both pass the check
        with admin.transaction():
            admin.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                          ("oikonome:email:" + new_email,))
            # and the per-user factor lock BEFORE the users write, the
            # canonical order — `_evict_passkeys` below takes it, and
            # reaching it only after this transaction held the users row
            # would invert the order the TOTP doors use.
            _lock_user_factors(admin, user["user_id"])
            if admin.execute("SELECT 1 FROM users WHERE email=%s AND id!=%s",
                             (new_email, user["user_id"])).fetchone():
                raise HTTPException(409, "that email is already in use")
            admin.execute(
                "UPDATE users SET email=%s, verified_at=%s WHERE id=%s",
                (new_email,
                 None if hosted else dt.datetime.now(dt.timezone.utc),
                 user["user_id"]))
            vtoken = email_verify.create(admin, user["user_id"]) if hosted \
                else None
            # the email IS the login identifier — rotate it like a
            # credential. Other sessions (a hijacker's, most importantly)
            # and script tokens don't get to ride through the change; this
            # session stays, same shape as password_change.
            sessions.revoke_all_others(admin, user["user_id"], token)
            admin.execute("UPDATE api_tokens SET revoked_at=now() "
                          "WHERE created_by=%s AND revoked_at IS NULL",
                          (user["user_id"],))
            # keep the CALLING device, like password_change and every other
            # posture-change door — a phone that just proved full step-up
            # must not sign itself out by changing its own email
            device_tokens.revoke_all_for_user(
                admin, user["user_id"],
                keep_device_id=user.get("device_id"))
            device_tokens.revoke_mint_tickets(admin, user["user_id"])
            # and any unspent reset link: one minted to the OLD mailbox by
            # whoever held it would otherwise outlive this rotation and
            # strip every factor an hour later
            admin.execute("UPDATE password_resets SET used_at=now() "
                          "WHERE user_id=%s AND used_at IS NULL",
                          (user["user_id"],))
            # Passkeys die with the rotation too, mirroring password_change
            # and the reset path: a hijacked session could have enrolled its
            # own passkey, and WebAuthn login would keep working for the
            # attacker no matter how many sessions and tokens are revoked
            # above. The owner re-adds passkeys after; the count comes back
            # in the response so the client can say what happened. Same
            # last-factor rule as password_change — the key that PROVED this
            # caller survives when it is the only factor left, or the
            # rotation would re-enable password-only login. Only that key: a
            # stranger's freshly-used one is not a proof of anything.
            passkeys_removed, passkeys_kept = _evict_passkeys(
                admin, user["user_id"], keep_passkey)
            # Pending family invites are the same pre-planted persistence
            # password change and reset already burn: an invite minted by a
            # hijacker is a share URL that needs no mailbox, so it would stay
            # claimable after the owner rotated the address.
            admin.execute("DELETE FROM invites WHERE created_by=%s "
                          "AND used_at IS NULL", (user["user_id"],))
            # and the other browsers' web push, as on a password change
            _drop_web_push(admin, user["user_id"], keep_push_endpoint)
    finally:
        admin.close()
    # whatever was recorded about the OLD address is now irrelevant to
    # this account, and the new one starts with a clean slate — a stale row
    # from a previous owner of the same address would otherwise pause the
    # first send before it was ever tried. No upstream reactivation here:
    # nobody has proved this mailbox works yet, the verification mail about
    # to go out is that proof, and reactivating on an unproven address would
    # hand anyone a way to scrub the provider's suppression list by typing
    # an address into a form.
    #
    # `email_delivery_state` is keyed on the ADDRESS with no tenant column,
    # so this clear is global. That it cannot wipe somebody ELSE's live
    # failure row rests on two guards that are not obviously about this:
    # `users.email` is UNIQUE instance-wide (so no other account can hold
    # this address), and hosted recipients must be members. If either ever
    # relaxes — per-tenant email uniqueness, free-form recipients on a
    # multi-tenant box — this line starts clearing a bounce that belongs to
    # a different household, and their mail resumes to a dead address until
    # it bounces again. Scope the table before relaxing either.
    from ..notify import delivery as _delivery
    _delivery.clear(new_email, reactivate=False)
    if vtoken:
        import threading
        threading.Thread(target=_deliver_verification,
                         args=(new_email, vtoken, user["tenant_id"]),
                         daemon=True).start()
    # a "did you mean…?" for an address that looks like a typo of a
    # major provider. Advisory ONLY — the change has already been saved by
    # the time this is computed, because a form that refuses your own email
    # is worse than a rare undelivered message, and some real domains do look
    # like typos of big ones.
    from ..notify import emailhint as _emailhint
    return {"ok": True, "email": new_email, "verify_sent": bool(vtoken),
            "passkeys_removed": passkeys_removed,
            "passkeys_kept": passkeys_kept,
            "hint": _emailhint.suggest(new_email)}


@app.get("/today")
def today_page(user: dict = Depends(current_user)):
    # Jinja page retired: the SPA owns the UI (today_body.html lives on
    # as the daily email's template — see todayview.render_email)
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/app/", status_code=303)


@app.post("/alerts/dismiss")
def alerts_dismiss(request: Request, user: dict = Depends(current_user),
                   kind: str = Form(...), message: str = Form(...),
                   back: str = Form("/today")):
    from fastapi.responses import RedirectResponse
    from ..engine import alerts as alerts_mod
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        alerts_mod.dismiss(conn, kind, message)
    finally:
        conn.close()
    dest = (back if back.startswith("/") and not back.startswith("//")
            and "\\" not in back else "/today")
    return RedirectResponse(dest, status_code=303)


@app.post("/alerts/restore")
def alerts_restore(request: Request, user: dict = Depends(current_user),
                   kind: str = Form(...), message: str = Form(...),
                   back: str = Form("/today")):
    from fastapi.responses import RedirectResponse
    from ..engine import alerts as alerts_mod
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        alerts_mod.restore(conn, kind, message)
    finally:
        conn.close()
    dest = (back if back.startswith("/") and not back.startswith("//")
            and "\\" not in back else "/today")
    return RedirectResponse(dest, status_code=303)


@app.get("/doctor")
def doctor_page(user: dict = Depends(current_user)):
    # Jinja page retired: the SPA owns the UI
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/app/doctor", status_code=303)


@app.get("/api/doctor")
def doctor_api(user: dict = Depends(current_user)):
    """The same named checks the Jinja /doctor page renders, plus an
    overall verdict — the SPA Doctor page consumes this."""
    from . import doctor
    checks = doctor.checks(str(user["tenant_id"]))
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


@app.get("/api/doctor/bundle")
def doctor_bundle(user: dict = Depends(current_user)):
    from . import doctor
    return doctor.bundle(str(user["tenant_id"]))


@app.get("/api/doctor/scripts")
def doctor_scripts(user: dict = Depends(current_user)):
    """Push-heartbeats for the Doctor page's Data-collectors panel: one
    row per script-fed source with a computed fresh/stale/off status."""
    from ..sync import heartbeat
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        return {"scripts": heartbeat.panel(conn)}
    finally:
        conn.close()


@app.post("/api/doctor/scripts/{source}")
def doctor_scripts_update(source: str, user: dict = Depends(current_user),
                          body: dict = Body(...)):
    """Watch settings for one source: expected_hours (0 = never warn)
    and/or alerts (opt-in stale email). Clearing the alert edge on any
    change lets a re-armed alert fire again."""
    sets, vals = [], []
    if "expected_hours" in body:
        try:
            hours = int(body["expected_hours"])
        except (TypeError, ValueError):
            raise HTTPException(422, "expected_hours must be an integer")
        if not 0 <= hours <= 24 * 90:
            raise HTTPException(422, "expected_hours out of range")
        sets += ["expected_hours=%s"]
        vals += [hours]
    if "alerts" in body:
        sets += ["alerts=%s"]
        vals += [1 if body["alerts"] else 0]
    if not sets:
        raise HTTPException(422, "nothing to update")
    sets += ["alerted_at=NULL"]
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        n = conn.execute(
            f"UPDATE script_heartbeats SET {', '.join(sets)} WHERE source=%s",
            (*vals, source)).rowcount
    finally:
        conn.close()
    if not n:
        raise HTTPException(404, "unknown source")
    return {"ok": True}


@app.get("/api/today")
def today(user: dict = Depends(current_user)):
    """The verdict — the product's core loop, straight from the engine."""
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        from .. import localtime
        st = budget.month_status(
            conn, localtime.now_local(budget.load_config(conn)).date())
    finally:
        conn.close()
    b = st["buckets"]
    return {
        "verdict": st["verdict"],
        "variance": round(st["variance"], 2),
        "variable_actual": round(st["variable_actual"], 2),
        "variable_expected": round(st["variable_expected"], 2),
        "reasons": st["reasons"],
        "buckets": {k: {"actual": round(v["actual"], 2),
                        "expected": round(v["expected"], 2),
                        "month_budget": round(v["month_budget"], 2)}
                    for k, v in b.items()},
        "days_in_month": st["days_in_month"],
    }


# management API (JSON, SPA-facing) — imported last: api resolves
# current_user lazily, so this is cycle-safe
from . import api as _api  # noqa: E402
from . import pages as _pages  # noqa: E402
from . import testing as _testing  # noqa: E402

app.include_router(_api.router)
from . import reporting_api as _rapi  # noqa: E402
app.include_router(_rapi.router)
from . import help as _help  # noqa: E402
app.include_router(_help.router)
from . import logos as _logos  # noqa: E402
app.include_router(_logos.router)
app.include_router(_pages.router)
app.include_router(_testing.router)
from . import adminconsole as _adminconsole  # noqa: E402
app.include_router(_adminconsole.router)
# routers an installed add-on registers; none on a plain instance
for _r in ext.routers():
    app.include_router(_r)
from . import plaid_webhook as _plaid_webhook  # noqa: E402
app.include_router(_plaid_webhook.router)
# the mail provider reporting that a message bounced. Every route on it
# 404s unless OIKONOME_EMAIL_WEBHOOK_TOKEN is set, so a self-host install
# with a local relay exposes nothing.
from . import postmark_webhook as _postmark_webhook  # noqa: E402
app.include_router(_postmark_webhook.router)

# feedback packages include the app's recent log lines — start capturing
# them now (in-process ring buffer; container logs aren't reachable)
from . import feedback as _feedback  # noqa: E402

_feedback.install_ring_logger()

# ---- SPA (webapp/dist) at /app — static assets + client-route fallback ----
from pathlib import Path as _Path  # noqa: E402

def _find_spa_dist() -> "_Path | None":
    import os as _os
    cands = [_Path(_os.environ["OIKONOME_SPA_DIST"])] if _os.environ.get(
        "OIKONOME_SPA_DIST") else []
    cands += [_Path(__file__).resolve().parents[3] / "webapp" / "dist",
              _Path.cwd() / "webapp" / "dist"]
    for c in cands:
        if (c / "index.html").exists():
            return c
    return None


# the server-rendered pages' shared JS (static/pages.js) — an unhashed
# filename, so no-cache like the SPA shell: heuristic caching otherwise
# serves week-old code across deploys.

from fastapi.staticfiles import StaticFiles as _StaticFiles  # noqa: E402


class _PageStatic(_StaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


app.mount("/static", _PageStatic(directory=_Path(__file__).parent / "static"),
          name="pages-static")

_SPA_DIST = _find_spa_dist()
if _SPA_DIST is not None:
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    class _HashedAssets(StaticFiles):
        """Vite content-hashes every filename under assets/ — a given URL's
        bytes can never change, so tell browsers to keep them for a year."""
        def file_response(self, *args, **kwargs):
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp

    app.mount("/app/assets", _HashedAssets(directory=_SPA_DIST / "assets"),
              name="spa-assets")

    # the shell (index.html + PWA files) is UNhashed — with no
    # Cache-Control, browsers heuristically cache it across deploys and go
    # on loading last week's bundle, which also blunts the service-worker
    # purge (that only runs once a NEW shell loads). no-cache = always
    # revalidate (304 when unchanged via ETag), never serve blind from
    # cache.
    _SHELL_CACHE = "no-cache, must-revalidate"

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        # Browsers (and bookmark managers) fetch /favicon.ico
        # unconditionally, regardless of the <link rel="icon"> — a 404 here
        # leaves bare bookmarks showing a blank globe. Serve the same SVG
        # mark every page already links (/app/icon.svg); SVG favicons are
        # fine under any filename when the type is right.
        return FileResponse(_SPA_DIST / "icon.svg",
                            media_type="image/svg+xml",
                            headers={"Cache-Control": _SHELL_CACHE})

    @app.get("/app{rest:path}")
    def spa(rest: str = ""):
        f = (_SPA_DIST / rest.lstrip("/")) if rest.strip("/") else None
        if f and f.is_file() and _SPA_DIST in f.resolve().parents:
            # theme-boot.js is render-blocking in <head> (it pins the theme
            # before first paint), so no-cache would put a revalidation
            # round trip in front of EVERY paint. Let the browser paint from
            # its copy
            # and revalidate behind the paint; index.html cannot get the
            # same treatment because it names hashed assets a deploy
            # replaces, and a stale shell would 404 its own bundle.
            cc = ("max-age=0, stale-while-revalidate=86400"
                  if f.name == "theme-boot.js" else _SHELL_CACHE)
            return FileResponse(f, headers={"Cache-Control": cc})
        return FileResponse(_SPA_DIST / "index.html",
                            headers={"Cache-Control": _SHELL_CACHE})
