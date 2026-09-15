"""The hosted admin console — the operator's surface, deliberately minimal.

Answers "who's on the box and is their sync healthy" and runs the everyday
support actions (invites, approvals, verification resends) without psql.

Deliberately NOT part of the tenant app's auth world:
 * Enabled only when the operator sets OIKONOME_ADMIN_TOKEN (a long
   random secret in docker/.env). Unset — every route 404s, so a
   self-host install exposes nothing.
 * The token is the BREAK-GLASS credential (compared constant-time); the
   real one is an operator passkey (auth/admin_passkeys.py) —
   hardware-bound, unphishable, and attributable, since the audit trail
   names the credential that acted rather than only an IP. Set
   OIKONOME_ADMIN_REQUIRE_PASSKEY once a key is enrolled and the token
   alone stops minting sessions. The token cannot simply be deleted: a
   self-host install on plain http:// has no secure context and can
   enrol nothing.
 * Either way a successful login mints an admin session cookie (own name,
   path=/admin, 4 h, sha256 at rest in admin_sessions) — entirely
   separate from tenant sessions, so no tenant session can ever reach
   these routes.
 * Destructive host-agent commands (restore/reset/uninstall) additionally
   demand a FRESH passkey assertion at the moment of the click, so a
   stolen console cookie does not reach them.
 * When Cloudflare Access fronts /admin*, the app verifies its assertion
   itself (web/cfaccess.py) — an edge policy is worthless if the origin
   is directly reachable.
 * Optional OIKONOME_ADMIN_IPS (comma-separated CIDRs) allowlists the
   client address on top — outside it the console 404s (cloaked, not
   advertised), using the same trusted-proxy client-IP policy as rate
   limiting.
 * All reads and writes run on admin_connect; the app role holds NO
   grants on admin_sessions/admin_audit (migration 026), so SQLi via
   the tenant app can't forge an operator session or scrub the trail.
 * Every state-changing action writes an admin_audit row.

Tenant DATA stays out of scope: the console reads control-plane rows and
per-tenant COUNTS/timestamps only. Support access to actual data goes
through an explicit, time-boxed consent grant and is audited
(tenant_export).
"""

from __future__ import annotations

import contextvars
import datetime as dt
import hashlib
import ipaddress
import logging
import os
import secrets
import threading
import time
import uuid as _uuid

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import ext
from ..db import tenancy
from ..envnum import env_flag
from . import cfaccess
from .security import client_ip, limit, request_scheme

log = logging.getLogger(__name__)

COOKIE = "oikonome_admin"
# 4 h: this session reaches restore/reset/uninstall on the host, so its
# validity stays short. Signing in again is one passkey touch.
SESSION_TTL = dt.timedelta(hours=4)

# Who is acting, for the audit trail — set once per request by _session()
# and read by _audit(), so the two dozen action routes need no new
# parameter. Starlette copies the context per request, so this cannot leak
# an actor from one request into another.
_ACTOR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "admin_actor", default=None)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _enabled() -> bool:
    """The console exists at all. Two ways to say yes:

    * `OIKONOME_ADMIN_TOKEN` — the original switch, still how a self-host
      turns it on;
    * `OIKONOME_ADMIN_ENABLED` — console ON with **no token at all**, for
      an operator who wants a passkey and nothing else. A passkey is then
      the only credential that exists, and the way back in after losing one
      is `oikonome admin-enrol` on the host.

    On a HOSTED instance either switch turns the console on, but only a
    passkey can ever sign in — see `_token_login_enabled()`. A token left
    in a hosted `.env` is therefore just an on-switch, not a credential;
    the tidy configuration is `OIKONOME_ADMIN_ENABLED=1` with no token, so
    there is no unused secret sitting in plaintext on the box.
    """
    return bool(os.environ.get("OIKONOME_ADMIN_TOKEN")
                or env_flag("OIKONOME_ADMIN_ENABLED"))


def _token_login_enabled() -> bool:
    """Can the operator token sign in at all?

    **Never on a HOSTED instance** — hosted is passkey-only, and the
    operator token stays available to non-hosted instances only. A
    hosted box has a real domain and a cert, so it can always enrol a
    passkey — there is no reason to keep a phishable shared secret alive
    there, and `oikonome admin-enrol` on the host covers both bootstrap and
    lost-key recovery. Leaving a token in `.env` on such a box does not
    re-open the door; it is simply inert.

    Self-host keeps it, because a plain-http LAN install has no secure
    context and can enrol nothing (`OIKONOME_ADMIN_REQUIRE_PASSKEY` /
    `OIKONOME_ADMIN_ENABLED` are there for a self-host that DOES have TLS
    and wants the same posture).

    The empty check is load-bearing, not cosmetic: `compare_digest(_h(""),
    _h(""))` is TRUE, so an unset token must never reach the compare.
    """
    if env_flag("OIKONOME_HOSTED"):
        return False
    return bool(os.environ.get("OIKONOME_ADMIN_TOKEN"))


def _require_passkey() -> bool:
    """Opt-in passkey-only enforcement for a SELF-HOST that has a token
    (hosted no longer needs it — the token cannot sign in there at all).

    Ignored while ZERO credentials are enrolled: the bootstrap path is
    token-first (sign in, enrol, then flip the flag), and enrolment itself
    needs a session — so honouring the flag on an empty table would brick
    a fresh box into needing an .env edit + restart. Not a downgrade
    attack: removing credentials needs a live session AND a step-up, and
    the last one cannot be removed while the flag is set.

    Irrelevant once the token is gone entirely — there is nothing left to
    refuse. `_passkey_only()` is that stronger posture.
    """
    return env_flag("OIKONOME_ADMIN_REQUIRE_PASSKEY")


def _passkey_only() -> bool:
    """Console on, no token configured: a passkey is the ONLY credential."""
    return _enabled() and not _token_login_enabled()


def _ip_allowed(request: Request) -> bool:
    allow = (os.environ.get("OIKONOME_ADMIN_IPS") or "").strip()
    if not allow:
        return True
    try:
        ip = ipaddress.ip_address(client_ip(request))
    except ValueError:
        return False
    for net in allow.split(","):
        try:
            if ip in ipaddress.ip_network(net.strip(), strict=False):
                return True
        except ValueError:
            log.warning("OIKONOME_ADMIN_IPS entry %r is not a valid "
                        "CIDR — ignored", net.strip())
    return False


def _cloak(request: Request):
    """404 (not 401/403) when the console is off, the caller is outside the
    IP allowlist, or Cloudflare Access is configured and this request does
    not carry a valid assertion — an unauthenticated probe learns nothing
    about whether a console exists here. A ROUTER dependency, so it runs
    before form validation (an in-handler check leaked a 422 for missing
    fields on a cloaked instance)."""
    from fastapi import HTTPException
    if not _enabled() or not _ip_allowed(request):
        raise HTTPException(404)
    try:
        cfaccess.check(request)
    except ValueError as e:
        # The edge policy only binds if the origin enforces it too.
        # Log the reason (the operator debugging their own
        # Access config gets nothing from a bare 404) but tell the caller
        # nothing.
        log.warning("admin console: Cloudflare Access check failed: %s", e,
                    extra={"no_capture": True})
        raise HTTPException(404)


router = APIRouter(prefix="/admin/console",
                   dependencies=[Depends(_cloak)])


def _session(request: Request) -> dict | None:
    """The live admin session for this request, or None. Also records WHO
    is acting so every audit row written downstream is attributed without
    threading the request through two dozen call sites."""
    _ACTOR.set(None)
    token = request.cookies.get(COOKIE, "")
    if not token:
        return None
    admin = tenancy.admin_connect()
    try:
        admin.execute("DELETE FROM admin_sessions WHERE expires_at <= now()")
        row = admin.execute(
            """SELECT s.token_hash, s.credential_id, c.label
               FROM admin_sessions s
               LEFT JOIN admin_credentials c ON c.id = s.credential_id
               WHERE s.token_hash=%s AND s.expires_at > now()""",
            (_h(token),)).fetchone()
    finally:
        admin.close()
    if row is None:
        return None
    _ACTOR.set(_actor_name(row))
    return row


def _actor_name(row) -> str:
    """"passkey:<label>" or "token" — the audit trail's answer to "who"."""
    if row and row.get("credential_id"):
        return "passkey:" + (row.get("label") or str(row["credential_id"])[:8])
    return "token"


def _authed(request: Request) -> bool:
    return _session(request) is not None


def _audit(admin, action: str, target: str | None = None,
           detail: str | None = None, ip: str | None = None,
           actor: str | None = None) -> None:
    admin.execute(
        "INSERT INTO admin_audit (action, target, detail, ip, actor) "
        "VALUES (%s, %s, to_jsonb(%s::text), %s, %s)",
        (action, target, detail, ip, actor or _ACTOR.get()))


# ---- pages ---------------------------------------------------------------


def _multi_tenant() -> bool:
    """Does this instance take signups at all?

    Everything the console offers around ONBOARDING A STRANGER —
    access requests, minting a signup invite, provisioning an instance,
    chasing unverified users, an add-on's own columns — presumes
    `/signup` exists. It does not on a self-hosted box: that instance bootstraps
    through its setup link and stays one household, so `signup_page`
    redirects to /login. Offering those doors anyway minted an invite
    whose link was dead on arrival, with nothing to say why.

    So the console asks exactly what the signup page asks, and the two
    can never disagree.
    """
    from .app import DEV_MODE
    return bool(env_flag("OIKONOME_HOSTED") or DEV_MODE)


def _age_label(created, now=None) -> str:
    """How old a household is, at the precision a fleet glance needs: days
    inside the first month (``3d``), whole months inside the first year
    (``4mo``), then years to one decimal with a trailing ``.0`` dropped
    (``1yr``, ``1.5yr``)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    days = max(0, (now - created).days)
    if days < 30:
        return f"{days}d"
    if days < 365:
        return f"{max(1, int(days / 30.44))}mo"
    years = days / 365.25
    label = f"{years:.1f}".rstrip("0").rstrip(".")
    return f"{label}yr"


def _render(request: Request, **ctx) -> HTMLResponse:
    from . import todayview
    # login state is a centered auth card like every other auth page
    # (convention); the dashboard gets the full-width shell
    ctx.setdefault("centered", not ctx.get("authed"))
    # DEFAULT, not per-call: an undefined `token_login` is falsy in Jinja,
    # so a caller that forgot it would silently hide the token form on a
    # perfectly normal token-only install (caught by test_admin_console).
    ctx.setdefault("token_login", _token_login_enabled())
    ctx.setdefault("multi_tenant", _multi_tenant())
    ctx.setdefault("age_label", _age_label)
    return HTMLResponse(todayview._env.get_template("admin.html").render(
        title="Admin console", **ctx))


_SINGLE_HOUSEHOLD_NOTICE = (
    "This instance is a single household — it does not take signups, so "
    "invites, access requests and instance provisioning do not apply here. "
    "To share it, use Settings → Security & household → Invite.")

_NOTICE_CODES = {
    "rebooting": ("Reboot requested — users see a 60-second countdown, "
                  "then the host restarts. The broadcast clears itself "
                  "once the worker is back up."),
    "command": ("Operation queued — the host runs it in a few seconds. "
                "Refresh to see the result in Host operations below."),
}


@router.get("", dependencies=[Depends(limit("admin_page", 120, 3600))])
def console(request: Request):
    # 120/h: an operator refreshing a dashboard never notices it; a scanner
    # walking the door does. The unauthed branch below is the console's
    # discovery surface, so it should not be free to hammer.
    if not _authed(request):
        return _render(request, authed=False, error=None,
                       passkeys_enrolled=_passkey_count())
    return _dashboard(request, notice=_NOTICE_CODES.get(
        request.query_params.get("notice", "")))


@router.get("/infra")
def infra_fragment(request: Request):
    """The infra-stress section alone — pages.js re-fetches this every
    ~20s into the dashboard. A direct 401 response, NOT a raised
    HTTPException: the app-level 401 handler redirects non-/api paths to
    /login, and a fetch that followed that 303 would swap the panel for the
    login page."""
    if not _authed(request):
        return HTMLResponse("not signed in", status_code=401)
    from . import todayview
    win = request.query_params.get("win", "24h")
    # summary=1 (the strip is collapsed): render only the one-line status
    # from the latest sample — skip the full-window scan + SVG generation.
    # The heavy chart fragment is fetched only while the strip is expanded.
    summary_only = request.query_params.get("summary") == "1"
    return HTMLResponse(
        todayview._env.get_template("admin_infra.html")
        .render(infra=_infra_panel(win, summary_only=summary_only)))


def _mail_health(admin=None) -> dict:
    """{failing, reason} for the red banner — outbound mail is the one
    thing the operator cannot be emailed about. Pass the connection a
    caller already holds; the default opens (and closes) its own."""
    try:
        from ..notify import mailhealth
        st = mailhealth.status(admin)
        return {"failing": st["failing"], "reason": st["reason"]}
    except Exception:                                     # noqa: BLE001
        return {"failing": False, "reason": ""}


def _dashboard(request: Request, minted_link: str | None = None,
               notice: str | None = None,
               reboot_armed: bool = False,
               reboot_nonce: str = "",
               cmd_armed: str = "",
               cmd_nonce: str = "",
               mail_health: dict | None = None) -> HTMLResponse:
    admin = tenancy.admin_connect()
    try:
        # handlers that already asked (to word their notice) pass the
        # answer in, so one request asks the database once
        if mail_health is None:
            mail_health = _mail_health(admin)
        # one grouped scan per table, NOT correlated subqueries per tenant
        # — a live box holds a handful of tenants, but the shared test
        # database holds thousands (one per test ever run), and the
        # correlated form was O(tenants × table scans) there. Capped: a
        # fleet view past 500 rows needs pagination, not scrolling.
        tenants = admin.execute("""
            WITH owners AS (SELECT DISTINCT ON (tenant_id) tenant_id, email
                            FROM users ORDER BY tenant_id, created_at),
            uc AS (SELECT tenant_id, count(*) AS users,
                          count(*) FILTER (WHERE verified_at IS NULL)
                            AS unverified
                   FROM users GROUP BY tenant_id),
            se AS (SELECT tenant_id, max(last_seen) AS last_active
                   FROM sessions GROUP BY tenant_id),
            ac AS (SELECT tenant_id, count(*) AS accounts
                   FROM accounts GROUP BY tenant_id),
            tx AS (SELECT tenant_id, count(*) AS txns
                   FROM transactions GROUP BY tenant_id),
            sy AS (SELECT tenant_id,
                          count(*) FILTER (WHERE error IS NOT NULL AND
                            item_id <> 'import-overlap' AND
                            ran_at > now() - interval '24 hours')
                            AS sync_errors
                   FROM sync_log GROUP BY tenant_id)
            SELECT t.id, t.created_at, t.status, o.email AS owner,
                   coalesce(uc.users, 0) AS users,
                   coalesce(uc.unverified, 0) AS unverified,
                   se.last_active, coalesce(ac.accounts, 0) AS accounts,
                   coalesce(tx.txns, 0) AS txns,
                   coalesce(sy.sync_errors, 0) AS sync_errors
            FROM tenants t
            LEFT JOIN owners o ON o.tenant_id = t.id
            LEFT JOIN uc ON uc.tenant_id = t.id
            LEFT JOIN se ON se.tenant_id = t.id
            LEFT JOIN ac ON ac.tenant_id = t.id
            LEFT JOIN tx ON tx.tenant_id = t.id
            LEFT JOIN sy ON sy.tenant_id = t.id
            ORDER BY t.created_at LIMIT 500""").fetchall()
        # whatever an installed add-on shows about each household, plus
        # its own panes' data
        extra = ext.gate.console_context(admin, tenants, request)
        unverified = admin.execute("""
            SELECT u.id, u.email, u.created_at FROM users u
            WHERE u.verified_at IS NULL
            ORDER BY u.created_at DESC LIMIT 50""").fetchall()
        invites = admin.execute("""
            SELECT email, note, created_at, expires_at, options
            FROM signup_invites
            WHERE used_at IS NULL AND expires_at > now()
            ORDER BY created_at DESC""").fetchall()
        # an installed add-on may name audit actions that are machine
        # bookkeeping written on every click — left in the feed they evict
        # the human operator actions this window exists to show
        audit = admin.execute("""
            SELECT at, action, target, detail, ip FROM admin_audit
            WHERE NOT (action = ANY(%s))
            ORDER BY id DESC LIMIT 20""",
            (list(ext.gate.audit_noise()),)).fetchall()
        deployments = admin.execute("""
            SELECT version, deployed_at, migrations, summary FROM deployments
            ORDER BY id DESC LIMIT 15""").fetchall()
        bc = admin.execute(
            "SELECT message, severity, set_at FROM broadcast "
            "WHERE id = 1").fetchone()
        from ..auth import admin_passkeys
        admin_creds = admin_passkeys.credentials(admin)
    finally:
        admin.close()
    health = _server_health()
    from . import feedback as _feedback
    from . import spamdefense
    try:
        spam = spamdefense.stats(admin_conn := tenancy.admin_connect())
        admin_conn.close()
    except Exception:                                       # noqa: BLE001
        spam = None
    win = request.query_params.get("win", "24h")
    # an add-on's panes take their place in the nav after the core tab
    # they name; a pane with a `when` renders only when its data is there
    panes = [pn for pn in ext.admin_panes()
             if not pn.get("when") or extra.get(pn["when"])]
    nav = list(_CORE_TABS)
    for pn in panes:
        anchor = next((i for i, (k, _) in enumerate(nav)
                       if k == pn.get("after")), len(nav) - 1)
        nav.insert(anchor + 1, (pn["key"], pn["label"]))
    return _render(request, authed=True, tenants=tenants,
                   infra=_infra_panel(win),
                   unverified=unverified, invites=invites,
                   spam=spam, audit=audit,
                   admin_panes=panes, nav_tabs=nav,
                   # a pane may ship its own nav glyph under `icon`
                   pane_icons={pn["key"]: pn["icon"] for pn in panes
                               if pn.get("icon")},
                   deployments=deployments, broadcast=bc,
                   version_history=_version_history(),
                   host_state=_host_state(),
                   applogs=_feedback.recent_logs()[-8000:],
                   version=os.environ.get("OIKONOME_VERSION", "dev"),
                   queue_depth=health.get("queue_depth"), health=health,
                   minted_link=minted_link, notice=notice,
                   mail_health=mail_health,
                   reboot_armed=reboot_armed, reboot_nonce=reboot_nonce,
                   command_results=_command_results(),
                   cmd_watcher_ready=_cmd_dir_writable(),
                   cmd_armed=cmd_armed, cmd_nonce=cmd_nonce,
                   # operator access
                   admin_creds=admin_creds,
                   # The enrol form's data-stepup gate reads
                   # passkeys_enrolled, which only the UNAUTHED renders
                   # passed — Jinja's Undefined is falsy, so the authed
                   # dashboard emitted the form with NO step-up ticket and
                   # every second-key enrolment 403'd after a full WebAuthn
                   # ceremony.
                   passkeys_enrolled=bool(admin_creds),
                   require_passkey=_require_passkey(),
                   passkey_only=_passkey_only(),
                   actor=_ACTOR.get() or "token",
                   cf_access=cfaccess.enabled(),
                   secure_ctx=request_scheme(request) == "https"
                   or (request.url.hostname or "") in ("localhost",
                                                       "127.0.0.1"),
                   **extra)


# the console's own tabs, in nav order; an add-on's panes slot in after
# the tab each one names
_CORE_TABS = (("overview", "Overview"), ("tenants", "Users"),
              ("ops", "Ops"), ("deploys", "Deploys"), ("audit", "Audit"))


def _queue_depth() -> int | None:
    """arq jobs waiting in Redis; None = redis unreachable (shown as such,
    never an error — the console must render with the queue down)."""
    try:
        import redis as _redis
        r = _redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            socket_connect_timeout=2, socket_timeout=2)
        return int(r.zcard("arq:queue"))
    except Exception:                                     # noqa: BLE001
        return None


def _version_history(limit: int = 2000) -> list[dict]:
    """Every version this codebase has ever had, newest first.

    Read from the manifest `scripts/gen-version-history.sh` bakes into the
    package — the image carries no .git (docker/Dockerfile copies only
    server/oikonome, webapp/dist and docs), so git is not shellable here.

    NOT the same thing as `deployments`, and deliberately not merged into
    it: a deployments row means "this version was actually deployed to this
    box at this time" and only exists from migration 029 onward. This is
    BUILD history — every version that has existed, deployed or not. Faking
    deployment rows for the pre-029 past would invent events that never
    happened and destroy the distinction. Two panels, two meanings.

    Missing or unreadable manifest returns [] and the panel says so.
    Default limit is large enough for the full build history (500+ rows);
    a smaller one silently clips the earliest builds off the bottom.
    """
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "version_history.tsv"
    out: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 4:
                    continue
                out.append({"version": parts[0], "date": parts[1],
                            "sha": parts[2], "subject": parts[3],
                            "loc": int(parts[4]) if len(parts) > 4
                                   and parts[4].isdigit() else None})
    except OSError:
        return []
    out.reverse()                      # manifest is oldest-first; show newest
    return out[:limit]


def _server_health() -> dict:
    """Operational health for the fleet header — DB server, host load/mem,
    users online now, worker heartbeats, migrations. Every probe is
    individually guarded: the console must render even with pieces down
    (a value of None reads as 'unknown' in the template, never a 500)."""
    h: dict = {"redis_ok": _queue_depth() is not None,
               "queue_depth": _queue_depth()}
    # Whatever an installed add-on reports about its own configuration. A
    # partially configured rail is otherwise silent — the client renders a
    # live control and the request fails at the far end — and an operator
    # should learn that from this page, not from the person it happened to.
    h.update(ext.gate.health())
    # --- database ---
    admin = tenancy.admin_connect()
    try:
        h["db_version"] = admin.execute(
            "SELECT current_setting('server_version')").fetchone()[
                "current_setting"]
        h["db_size"] = admin.execute(
            "SELECT pg_size_pretty(pg_database_size(current_database()))"
            ).fetchone()["pg_size_pretty"]
        h["db_conns"] = admin.execute(
            "SELECT count(*) n FROM pg_stat_activity "
            "WHERE datname = current_database()").fetchone()["n"]
        h["tenants"] = admin.execute(
            "SELECT count(*) n FROM tenants").fetchone()["n"]
        h["users"] = admin.execute(
            "SELECT count(*) n FROM users").fetchone()["n"]
        # active sessions in the last 15 min = "online now"
        h["online"] = admin.execute(
            "SELECT count(DISTINCT user_id) n FROM sessions "
            "WHERE last_seen > now() - interval '15 minutes' "
            "AND expires_at > now()").fetchone()["n"]
        h["sessions_active"] = admin.execute(
            "SELECT count(*) n FROM sessions WHERE expires_at > now()"
            ).fetchone()["n"]
        # worker heartbeats (job_runs is per-tenant but we want the fleet
        # max — admin_connect bypasses RLS)
        for job in ("sync_all", "nightly", "email_all"):
            row = admin.execute(
                "SELECT max(ran_at) t FROM job_runs WHERE job = %s",
                (job,)).fetchone()
            h[f"job_{job}"] = row["t"] if row else None
        h["migrations"] = admin.execute(
            "SELECT count(*) n FROM schema_migrations").fetchone()["n"]
    except Exception:                                     # noqa: BLE001
        pass
    finally:
        admin.close()
    # pending migrations = files on disk not yet recorded
    try:
        from ..db import migrate as _m
        files = {f.name for f in (_m.HERE / "migrations").glob("*.sql")}
        applied = set()
        a = tenancy.admin_connect()
        try:
            applied = {r["name"] for r in a.execute(
                "SELECT name FROM schema_migrations").fetchall()}
        finally:
            a.close()
        h["migrations_pending"] = len(files - applied)
    except Exception:                                     # noqa: BLE001
        h["migrations_pending"] = None
    # --- host (shared kernel: /proc reflects the host) ---
    try:
        with open("/proc/loadavg") as f:
            h["load"] = " ".join(f.read().split()[:3])
        h["cpus"] = os.cpu_count()
    except Exception:                                     # noqa: BLE001
        h["load"] = None
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                mem[k] = v.strip()
        total = int(mem["MemTotal"].split()[0]) / 1024 / 1024
        avail = int(mem["MemAvailable"].split()[0]) / 1024 / 1024
        h["mem_used_gb"] = round(total - avail, 1)
        h["mem_total_gb"] = round(total, 1)
    except Exception:                                     # noqa: BLE001
        h["mem_total_gb"] = None
    return h


# ---- infra-stress panel ----------------------------------------

# selectable history windows (24h default, 14d = the retention bound)
_INFRA_WINDOWS = {"6h": dt.timedelta(hours=6), "24h": dt.timedelta(hours=24),
                  "7d": dt.timedelta(days=7), "14d": dt.timedelta(days=14)}
_GAP_S = 300           # >5 missed samples between rows = a visible gap
_STALE_S = 180         # latest sample older than this = sampler down (amber)

# chart catalog: (row key, label, unit, amber, red, delta?) — thresholds
# come from jobs/infra_metrics.THRESHOLDS so gauge colors, spike markers
# and the sampler always agree
_INFRA_CHARTS = (
    ("pg_total_conns", "PG connections", "conns", True, False),
    ("api_p95_ms", "/api p95", "ms", True, False),
    ("pool_timeouts", "Pool timeouts", "per sample", True, True),
    ("host_load1", "Host load (1m)", "", False, False),
    ("mem_used_gb", "Host memory", "GB", False, False),
    ("disk_used_pct", "Disk used", "%", True, False),
    ("redis_mem_mb", "Redis memory", "MB", False, False),
)


# chart geometry (viewBox units; the svg scales to 100% width, aspect kept)
_CHART_W = 480
_CHART_H = 150
_M_L, _M_R, _M_T, _M_B = 46, 12, 12, 22     # plot margins for the axes
# X-axis tick spacing per window, and the tick label format (UTC, matching
# the rest of the panel): hours for the short windows, dates for the long
_TIME_TICK_S = {"6h": 3600, "24h": 4 * 3600, "7d": 86400, "14d": 2 * 86400}


def _fmt_axis(v: float, unit: str) -> str:
    """Compact Y-axis tick label (unit shown in the header, not here)."""
    if v == 0:
        return "0"
    if unit == "ms":
        return f"{v / 1000:.1f}s" if v >= 1000 else f"{v:.0f}"
    a = abs(v)
    if a >= 1000:
        return f"{v / 1000:.1f}k"
    if a >= 10:
        return f"{v:.0f}"
    if a >= 1:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _fmt_val(v: float, unit: str) -> str:
    """Human tooltip value for one sample (unit inline)."""
    if unit == "ms":
        return _fmt_ms(v)
    if unit == "%":
        return f"{v:g}%"
    s = f"{v:g}"
    return f"{s} {unit}" if unit else s


def _chart_svg(series, t0: float, t1: float, win: str, unit: str = "",
               amber=None, red=None) -> dict:
    """A proper inline-SVG time-series chart for one metric: a labeled time
    X-axis (ticks appropriate to the window), a labeled value Y-axis, the
    dashed amber/red threshold guide lines, red/amber breach markers, and
    the line itself broken on NULLs and sample gaps (>_GAP_S) so a dead
    sampler shows a hole, never a lying straight line.

    Returns {"svg": markup, "points": [...]} where each point carries its
    viewBox pixel position plus the timestamp/value — pages.js reads that
    JSON to draw the hover tooltip (nothing user-typed enters this markup;
    all numbers are server-generated)."""
    span = max(t1 - t0, 1.0)
    x0, x1 = _M_L, _CHART_W - _M_R
    y0, y1 = _M_T, _CHART_H - _M_B         # y0 = top (vmax), y1 = baseline (0)
    vals = [v for _, v, _ in series if v is not None]
    vmax = max(vals + ([red] if red is not None else [])) if vals else 1.0
    vmax = vmax * 1.10 or 1.0

    def px(t):
        return x0 + (t - t0) / span * (x1 - x0)

    def py(v):
        return y1 - (v / vmax) * (y1 - y0)

    parts = [f'<svg viewBox="0 0 {_CHART_W} {_CHART_H}" role="img" '
             f'style="width:100%;height:auto;display:block;touch-action:none">']
    # --- axes ---
    parts.append(f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" '
                 f'style="stroke:var(--line);stroke-width:1"/>')
    parts.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" '
                 f'style="stroke:var(--line);stroke-width:1"/>')
    # Y ticks: 0, mid, max
    for v in (0.0, vmax / 2, vmax):
        y = py(v)
        parts.append(f'<line x1="{x0 - 3}" y1="{y:.1f}" x2="{x0}" '
                     f'y2="{y:.1f}" style="stroke:var(--line);'
                     f'stroke-width:1"/>')
        parts.append(f'<text x="{x0 - 5}" y="{y + 3:.1f}" '
                     f'text-anchor="end" class="ax">'
                     f'{_fmt_axis(v, unit)}</text>')
    # X ticks: aligned to nice window-appropriate boundaries
    step = _TIME_TICK_S.get(win, 4 * 3600)
    fmt = "%H:%M" if win in ("6h", "24h") else "%m/%d"
    tick = (int(t0) // step + 1) * step
    while tick <= t1:
        x = px(tick)
        parts.append(f'<line x1="{x:.1f}" y1="{y1}" x2="{x:.1f}" '
                     f'y2="{y1 + 3}" style="stroke:var(--line);'
                     f'stroke-width:1"/>')
        label = dt.datetime.fromtimestamp(
            tick, dt.timezone.utc).strftime(fmt)
        parts.append(f'<text x="{x:.1f}" y="{_CHART_H - 7}" '
                     f'text-anchor="middle" class="ax">{label}</text>')
        tick += step
    # --- threshold guide lines (kept from the sparkline) ---
    for lvl, color in ((amber, "var(--amber)"), (red, "var(--red)")):
        if lvl is not None and lvl <= vmax:
            y = py(lvl)
            parts.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" '
                         f'y2="{y:.1f}" style="stroke:{color};'
                         f'stroke-width:1;stroke-dasharray:3 4;opacity:.6"/>')
    # --- data line (segments break on NULL / sample gaps) + breach marks ---
    segs: list[list[str]] = [[]]
    marks: list[str] = []
    points: list[dict] = []
    prev_t = None
    for t, v, level in series:
        if v is None or (prev_t is not None and t - prev_t > _GAP_S):
            if segs[-1]:
                segs.append([])
        if v is not None:
            x, y = px(t), py(v)
            segs[-1].append(f"{x:.1f},{y:.1f}")
            points.append({
                "x": round(x, 1), "y": round(y, 1),
                "t": dt.datetime.fromtimestamp(
                    t, dt.timezone.utc).isoformat(),
                "d": _fmt_val(v, unit),
                "lv": level or ""})
            if level:
                color = "var(--red)" if level == "red" else "var(--amber)"
                marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" '
                             f'style="fill:{color}"/>')
        prev_t = t
    for seg in segs:
        if len(seg) == 1:
            x, y = seg[0].split(",")
            parts.append(f'<circle cx="{x}" cy="{y}" r="1.6" '
                         f'style="fill:var(--ink)"/>')
        elif seg:
            parts.append(f'<polyline points="{" ".join(seg)}" '
                         f'style="fill:none;stroke:var(--ink);'
                         f'stroke-width:1.3;stroke-linejoin:round"/>')
    parts += marks
    parts.append("</svg>")
    return {"svg": "".join(parts), "points": points}


def _infra_panel(win: str = "24h", summary_only: bool = False) -> dict:
    """Everything the Infra-stress section renders: current gauges colored
    against the triggers, per-metric sparklines over the window, spike
    (breach) timestamps, and sampler-gap detection. Every probe guarded —
    the console must render with zero rows (fresh install) and with the
    sampler dead.

    summary_only (the collapsed strip's 20s refresh): render only the
    one-line status + sampler-gap flag from the LATEST sample — the cheap
    path that skips the full-window scan, charts and spike list entirely."""
    from ..jobs import infra_metrics as _im
    if win not in _INFRA_WINDOWS:
        win = "24h"
    out: dict = {"win": win, "windows": list(_INFRA_WINDOWS),
                 "rows": 0, "charts": [], "spikes": [], "gaps": 0,
                 "stale": None, "latest": None, "gauges": [], "summary": []}
    if summary_only:
        return _infra_summary_panel(win, out)
    rows: list[dict] = []
    admin = tenancy.admin_connect()
    try:
        rows = admin.execute(
            "SELECT * FROM infra_metrics WHERE ts > now() - %s "
            "ORDER BY ts", (_INFRA_WINDOWS[win],)).fetchall()
    except Exception:                                     # noqa: BLE001
        log.warning("infra metrics read failed", exc_info=True)
    finally:
        admin.close()
    out["rows"] = len(rows)
    now = dt.datetime.now(dt.timezone.utc)
    # per-row breach classification (pool-timeout delta needs the previous
    # cumulative counter; a counter drop = web restart = clamped to 0)
    breach_by_id: dict[int, dict] = {}
    prev_pt = None
    for r in rows:
        breach_by_id[r["id"]] = _im.breaches(r, prev_timeouts=prev_pt)
        if r["pool_timeouts"] is not None:
            prev_pt = r["pool_timeouts"]
    if rows:
        latest = rows[-1]
        out["latest"] = latest
        age = (now - latest["ts"]).total_seconds()
        if age > _STALE_S:
            out["stale"] = round(age / 60)      # minutes since last sample
        out["gauges"] = _infra_gauges(latest, breach_by_id[latest["id"]])
        out["summary"] = _infra_summary(latest, breach_by_id[latest["id"]])
        for i in range(1, len(rows)):
            if (rows[i]["ts"] - rows[i - 1]["ts"]).total_seconds() > _GAP_S:
                out["gaps"] += 1
        t0 = (now - _INFRA_WINDOWS[win]).timestamp()
        t1 = now.timestamp()
        for key, label, unit, has_thresh, is_delta in _INFRA_CHARTS:
            series = []
            prev = None
            for r in rows:
                v = r[key]
                if is_delta and v is not None:
                    v = max(v - prev, 0) if prev is not None else 0
                    prev = r[key]
                lv = breach_by_id[r["id"]].get(
                    "pool_timeout_delta" if is_delta else key)
                # mem/load breaches key differently — surface them on
                # their own charts too
                if lv is None and key == "mem_used_gb":
                    lv = breach_by_id[r["id"]].get("mem_pct")
                if lv is None and key == "host_load1":
                    lv = breach_by_id[r["id"]].get("load_per_cpu")
                series.append((r["ts"].timestamp(), v, lv))
            if all(v is None for _, v, _ in series):
                continue                        # source never reported: skip
            amber = red = None
            if has_thresh and key == "pg_total_conns":
                amber, red = _im.pg_conn_lines(latest.get("pg_max_conns"))
            elif has_thresh and key in _im.THRESHOLDS:
                amber, red = _im.THRESHOLDS[key]
            chart = _chart_svg(series, t0, t1, win, unit=unit,
                               amber=amber, red=red)
            import json as _json
            out["charts"].append({
                "label": label, "unit": unit, "svg": chart["svg"],
                "points_json": _json.dumps(chart["points"],
                                           separators=(",", ":")),
                "vw": _CHART_W, "vh": _CHART_H})
        # spike list: samples that breached a trigger, newest first
        spikes = []
        for r in reversed(rows):
            b = breach_by_id[r["id"]]
            if b:
                spikes.append({"ts": r["ts"],
                               "what": ", ".join(
                                   f"{k} {v}" for k, v in b.items()),
                               "red": "red" in b.values()})
            if len(spikes) >= 12:
                break
        out["spikes"] = spikes
    return out


def _infra_summary_panel(win: str, out: dict) -> dict:
    """The cheap path for the collapsed strip's 20s refresh: only the latest
    sample, so only the one-line summary + sampler-gap flag. No full-window
    scan, no charts, no spike list — the detail (hidden while collapsed)
    stays empty until the strip is expanded and the full fragment is fetched.
    Breaches computed with no previous counter: the summary's metrics are all
    level (never the pool-timeout delta), so a single row is enough."""
    from ..jobs import infra_metrics as _im
    latest = None
    admin = tenancy.admin_connect()
    try:
        latest = admin.execute(
            "SELECT * FROM infra_metrics ORDER BY ts DESC LIMIT 1").fetchone()
    except Exception:                                     # noqa: BLE001
        log.warning("infra summary read failed", exc_info=True)
    finally:
        admin.close()
    if latest:
        out["rows"] = 1
        out["latest"] = latest
        b = _im.breaches(latest)
        out["gauges"] = _infra_gauges(latest, b)
        out["summary"] = _infra_summary(latest, b)
        age = (dt.datetime.now(dt.timezone.utc) - latest["ts"]).total_seconds()
        if age > _STALE_S:
            out["stale"] = round(age / 60)
    return out


def _fmt_ms(v) -> str:
    if v is None:
        return "—"
    return f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.0f} ms"


def _infra_gauges(latest: dict, b: dict) -> list[dict]:
    """The current-state gauge row, colored by the latest sample's breach
    levels (None level renders as the calm default)."""
    def g(label, value, level=None, sub=""):
        return {"label": label, "value": value, "level": level, "sub": sub}
    pool = ("—" if latest["pool_size"] is None else
            f"{latest['pool_size'] - (latest['pool_available'] or 0)}"
            f"/{latest['pool_size']}")
    mem = ("—" if latest["mem_used_gb"] is None else
           f"{latest['mem_used_gb']:.1f}/{latest['mem_total_gb']:.1f} GB")
    return [
        g("PG connections",
          "—" if latest["pg_total_conns"] is None
          else f"{latest['pg_total_conns']} / {latest.get('pg_max_conns') or 25}",
          b.get("pg_total_conns"),
          f"{latest['pg_active'] or 0} active · "
          f"{latest['pg_idle'] or 0} idle"),
        g("/api p95", _fmt_ms(latest["api_p95_ms"]), b.get("api_p95_ms"),
          f"{latest['api_req_count'] or 0} reqs in window"),
        g("Pool", pool, b.get("pool_timeout_delta"),
          "—" if latest["pool_timeouts"] is None
          else f"{latest['pool_timeouts']} timeouts total"),
        g("Host load", "—" if latest["host_load1"] is None
          else f"{latest['host_load1']:.2f}",
          b.get("load_per_cpu"),
          f"{latest['host_cpus'] or '?'} vCPU"),
        g("Memory", mem, b.get("mem_pct"), ""),
        g("Disk", "—" if latest["disk_used_pct"] is None
          else f"{latest['disk_used_pct']}%", b.get("disk_used_pct"), ""),
        g("Redis", "—" if latest["redis_mem_mb"] is None
          else f"{latest['redis_mem_mb']:.0f} MB", None, ""),
    ]


def _infra_summary(latest: dict, b: dict) -> list[dict]:
    """The collapsed one-line status: the handful of gauges that matter,
    each colored by the SAME threshold as its expanded gauge — green
    (pill g) = calm, amber/red = breach, muted (pill m, no color) when the
    metric is missing/NULL. Short enough to fit one tight line."""
    def item(label, value, level, present):
        cls = "m" if not present else {"red": "r", "amber": "a"}.get(level, "g")
        return {"label": label, "value": value, "cls": cls}
    pg_ok = latest["pg_total_conns"] is not None
    mem_ok = latest["mem_used_gb"] is not None and latest["mem_total_gb"]
    disk_ok = latest["disk_used_pct"] is not None
    load_ok = latest["host_load1"] is not None
    p95_ok = latest["api_p95_ms"] is not None
    p95 = "—"
    if p95_ok:
        v = latest["api_p95_ms"]
        p95 = f"{v / 1000:.1f}s" if v >= 1000 else f"{v:.0f}ms"
    return [
        item("PG", (f"{latest['pg_total_conns']}/"
                    f"{latest.get('pg_max_conns') or 25}") if pg_ok else "—",
             b.get("pg_total_conns"), pg_ok),
        item("Mem",
             (f"{latest['mem_used_gb'] / latest['mem_total_gb'] * 100:.0f}%"
              if mem_ok else "—"), b.get("mem_pct"), mem_ok),
        item("Disk", f"{latest['disk_used_pct']}%" if disk_ok else "—",
             b.get("disk_used_pct"), disk_ok),
        item("load", f"{latest['host_load1']:.2g}" if load_ok else "—",
             b.get("load_per_cpu"), load_ok),
        item("p95", p95, b.get("api_p95_ms"), p95_ok),
    ]


# ---- auth ----------------------------------------------------------------


def _passkey_count() -> int:
    from ..auth import admin_passkeys
    admin = tenancy.admin_connect()
    try:
        return admin_passkeys.count(admin)
    except Exception:                                        # noqa: BLE001
        # a database older than the passkey tables (upgrade in flight):
        # behave as "none enrolled",
        # which leaves the token path working rather than locking the
        # operator out mid-migration
        return 0
    finally:
        admin.close()


def _issue_session(request: Request, resp, credential_id=None,
                   actor: str = "token"):
    """Shared tail of both sign-in paths: row + audit + cookie on `resp`."""
    cookie = secrets.token_urlsafe(32)
    ip = client_ip(request)
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "INSERT INTO admin_sessions (token_hash, expires_at, ip, "
            "credential_id) VALUES (%s, now() + %s, %s, %s)",
            (_h(cookie), SESSION_TTL, ip, credential_id))
        _audit(admin, "login", ip=ip, actor=actor)
    finally:
        admin.close()
    resp.set_cookie(COOKIE, cookie, httponly=True, samesite="strict",
                    secure=request_scheme(request) == "https",
                    path="/admin",
                    max_age=int(SESSION_TTL.total_seconds()))
    return resp


def _mint_session(request: Request, credential_id=None,
                  actor: str = "token") -> RedirectResponse:
    return _issue_session(request,
                          RedirectResponse("/admin/console", status_code=303),
                          credential_id, actor)


@router.post("/login",
             dependencies=[Depends(limit("admin_login", 5, 3600))])
def login(request: Request, token: str = Form("")):
    if not _token_login_enabled():
        # Hosted, or no token configured: nothing here can be right. Checked
        # through the helper, never against the raw env, so a token left in
        # a hosted .env stays inert instead of quietly still working.
        return _render(request, authed=False,
                       passkeys_enrolled=_passkey_count(),
                       token_login=False,
                       error="This console signs in with a passkey.")
    real = os.environ.get("OIKONOME_ADMIN_TOKEN", "")
    # compare_digest raises on unequal-length (or non-ASCII) str —
    # hashing both sides first makes the compare fixed-length and total,
    # so a wrong-length token is a clean "not right", not a 500 oracle.
    if not secrets.compare_digest(_h(token), _h(real)):
        return _render(request, authed=False,
                       error="That token is not right.",
                       passkeys_enrolled=_passkey_count())
    # the token is break-glass. Where the operator has said so,
    # knowing it is not enough — check AFTER the compare so a wrong token
    # still reads as a wrong token, and count the enrolled keys only for a
    # right one (no probe can measure the flag).
    if _require_passkey() and _passkey_count() > 0:
        admin = tenancy.admin_connect()
        try:
            _audit(admin, "login_refused_token_only",
                   ip=client_ip(request), actor="token")
        finally:
            admin.close()
        return _render(request, authed=False, passkeys_enrolled=1,
                       error="This console requires a passkey — the "
                             "operator token alone is not accepted here.")
    return _mint_session(request)


@router.post("/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE, "")
    _session(request)                       # names the actor on the row
    if token:
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM admin_sessions WHERE token_hash=%s",
                          (_h(token),))
            _audit(admin, "logout", ip=client_ip(request))
        finally:
            admin.close()
    resp = RedirectResponse("/admin/console", status_code=303)
    # same attributes as the set, or browsers may ignore the delete
    resp.delete_cookie(COOKIE, path="/admin",
                       secure=request_scheme(request) == "https",
                       httponly=True, samesite="strict")
    return resp


def _require(request: Request):
    """Auth for the action routes (the router dependency already
    cloaked): valid operator session or bounce to the login page."""
    if not _authed(request):
        return RedirectResponse("/admin/console", status_code=303)
    return None


# ---- operator passkeys -----------------------------------------
#
# JSON endpoints under the console's own prefix, so they inherit the cloak,
# the IP allowlist and the Cloudflare Access check for free. The browser
# half lives in static/pages.js ([data-admin-passkey]).


def _norm_origin(scheme: str, host: str) -> str:
    """`scheme://host[:port]` with the default port dropped, so the same
    origin written two ways compares equal."""
    host = host.strip().rstrip("/")
    if ":" in host:
        name, _, port = host.rpartition(":")
        if port.isdigit() and ((scheme == "https" and port == "443")
                               or (scheme == "http" and port == "80")):
            host = name
    return f"{scheme}://{host}".lower()


def _console_origins() -> list[str]:
    """Origins this console may act as a WebAuthn relying party for.

    OIKONOME_BASE_URL always counts. OIKONOME_ADMIN_ORIGINS adds more — a
    private-network hostname (a Tailscale MagicDNS name, say), which is how
    a PHONE reaches the console without any DNS configuration at all (a
    phone cannot use an /etc/hosts entry). Each extra origin gets its own
    passkey, because
    WebAuthn binds a credential to one relying party.

    An ALLOWLIST, never the bare Host header: rp_id decides which
    credentials a browser will offer and which origin an assertion is
    valid for, so letting a forged Host choose it would let an attacker
    who can reach the console move the relying party. Here a forgery can
    only ever select something the operator already wrote down.
    """
    out = []
    from urllib.parse import urlsplit
    for raw in [(os.environ.get("OIKONOME_BASE_URL") or "")] + \
            (os.environ.get("OIKONOME_ADMIN_ORIGINS") or "").split(","):
        raw = raw.strip().rstrip("/")
        if not raw:
            continue
        u = urlsplit(raw)
        if u.scheme in ("http", "https") and u.hostname:
            out.append(_norm_origin(u.scheme, u.netloc))
    return out


def _rp(request: Request) -> tuple[str, str]:
    """(rp_id, origin) for the CONSOLE's WebAuthn.

    Not the tenant derivation (web/app.py::_rp), which pins to
    OIKONOME_BASE_URL — that has to stay the public hostname because
    tenant email links are built from it, so pinning would make every
    other console origin unusable. Instead: if the request arrived at an
    origin the operator listed, be that relying party; otherwise fall
    back to the tenant derivation, which is exactly today's behaviour.
    """
    from urllib.parse import urlsplit

    from .security import forwarded_host
    here = _norm_origin(request_scheme(request), forwarded_host(request))
    if here in _console_origins():
        return urlsplit(here).hostname or "", here
    from .app import _rp as _tenant_rp
    return _tenant_rp(request)


def _json_error(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


@router.get("/enrol",
            dependencies=[Depends(limit("admin_enrol", 20, 3600))])
def enrol_page(request: Request):
    """The host-issued recovery door for passkey-only installs.

    `oikonome admin-enrol` on the host prints a link here. It is the ONLY
    way into a console whose single key is lost, because there is no token
    to fall back to. Nothing here trusts the URL beyond letting the page
    render: the ticket is checked again on the enrolment call and burned
    only when a credential actually lands.

    Rate-limited even though the ticket is 24 random bytes and cannot be
    guessed: this is an UNAUTHENTICATED route doing a database lookup per
    request, and an unmetered one of those is a load amplifier whatever the
    odds of a hit."""
    from ..auth import admin_passkeys
    ticket = request.query_params.get("t", "")
    admin = tenancy.admin_connect()
    try:
        ok = admin_passkeys.enrol_ticket_valid(admin, ticket)
    finally:
        admin.close()
    if not ok:
        return _render(request, authed=False, token_login=_token_login_enabled(),
                       passkeys_enrolled=_passkey_count(),
                       error="That enrolment link is expired or already "
                             "used. Run `oikonome admin-enrol` on the host "
                             "for a new one.")
    return _render(request, authed=False, enrol_ticket=ticket,
                   token_login=False, passkeys_enrolled=0,
                   secure_ctx=request_scheme(request) == "https"
                   or (request.url.hostname or "") in ("localhost",
                                                       "127.0.0.1"))


@router.post("/passkey/options",
             dependencies=[Depends(limit("admin_passkey", 20, 3600))])
def passkey_register_options(request: Request, body: dict = Body(default=None)):
    from ..auth import admin_passkeys
    ticket = str((body or {}).get("ticket") or "")
    rp_id, _ = _rp(request)
    admin = tenancy.admin_connect()
    try:
        if not (_authed(request)
                or admin_passkeys.enrol_ticket_valid(admin, ticket)):
            return _json_error(401, "not signed in")
        return admin_passkeys.register_options(
            admin, rp_id, label=str((body or {}).get("label") or ""))
    finally:
        admin.close()


@router.post("/passkey",
             dependencies=[Depends(limit("admin_passkey", 20, 3600))])
def passkey_register(request: Request, body: dict = Body(...)):
    """Enrol an operator key.

    Two ways in, and both have to prove something:

    * a host-issued enrolment ticket, which stands in for a session — the
      recovery path for a passkey-only console. Burned here, on success, so
      one ticket buys exactly one key.
    * a console session — but ONCE A KEY EXISTS that session must also
      produce a fresh assertion with a key it already holds.

    That second clause is load-bearing. It is tempting to argue no step-up
    is needed because the first key must be bootstrappable and later
    sessions are already passkey sessions wherever
    OIKONOME_ADMIN_REQUIRE_PASSKEY is set — but the bootstrap argument only
    covers `count == 0`, and the second half is a setting, not a guarantee.
    Without the clause a stolen console cookie can enrol the thief's OWN
    authenticator and keep independent, durable access to a console that
    reaches the root host-agent, surviving session expiry and every
    credential the operator rotates afterwards. The tenant side takes the
    same position (`app.passkey_register` demands `_step_up` for exactly
    this reason).

    Bootstrap is unaffected (`count == 0` needs no assertion), and losing
    your only key is not a lockout: `oikonome admin-enrol` on the host
    issues a ticket, which takes the `by_ticket` path above."""
    from ..auth import admin_passkeys
    sess = _session(request)
    ticket = str(body.get("ticket") or "")
    rp_id, origin = _rp(request)
    admin = tenancy.admin_connect()
    try:
        # A valid host-issued ticket authorizes on its own, session or not:
        # an operator recovering a lost key may still hold a console cookie,
        # and the step-up branch below would demand the passkey they just
        # lost. Minting a ticket needs shell on the host, which is
        # strictly stronger proof than a console session, and the ticket is
        # single-use and short-lived, so honouring it is not a weakening.
        by_ticket = bool(ticket) and admin_passkeys.enrol_ticket_valid(
            admin, ticket)
        if sess is None and not by_ticket:
            return _json_error(401, "not signed in")
        if sess is not None and not by_ticket \
                and admin_passkeys.count(admin) > 0 \
                and not _stepup_ok(sess, admin, str(body.get("stepup") or "")):
            return _json_error(
                403, "confirm with a passkey you already have, then enrol "
                     "the new one. If you have lost every key, run "
                     "`oikonome admin-enrol` on the host for a link.")
        try:
            out = admin_passkeys.register_verify(
                admin, str(body.get("challenge_id") or ""),
                body.get("credential") or {}, rp_id, origin,
                label=str(body.get("label") or ""))
        except ValueError as e:
            return _json_error(400, str(e))
        except Exception as e:                    # noqa: BLE001 — lib errors
            log.warning("admin passkey enrol rejected: %s rp_id=%s origin=%s",
                        type(e).__name__, rp_id, origin,
                        extra={"no_capture": True})
            return _json_error(400, f"passkey rejected: {type(e).__name__}")
        if by_ticket and not admin_passkeys.burn_enrol_ticket(admin, ticket):
            # The peek above is not a claim: two redemptions of the same
            # ticket can both pass it and both store a key. The DELETE …
            # RETURNING burn is the only atomic claim, so a burn miss
            # means another redemption won — take back the key this one
            # just stored, so one ticket buys exactly one key.
            admin_passkeys.delete(admin, out["id"])
            _audit(admin, "admin_passkey_enrol_refused",
                   detail="enrol ticket already redeemed",
                   ip=client_ip(request), actor="host-enrol-link")
            return _json_error(
                409, "that enrolment link was already used — run "
                     "`oikonome admin-enrol` on the host for a new one")
        _audit(admin, "admin_passkey_enrolled",
               target=out["label"] or out["id"], ip=client_ip(request),
               actor="host-enrol-link" if by_ticket else _actor_name(sess))
    finally:
        admin.close()
    return out


@router.post("/passkey/delete",
             dependencies=[Depends(limit("admin_passkey", 20, 3600))])
def passkey_delete(request: Request, cred_id: str = Form(...),
                   stepup: str = Form("")):
    """Removing a key is a posture change — it is also the downgrade path
    back to token-only, so it needs a fresh assertion like the destructive
    host commands do, and the LAST key cannot go while
    OIKONOME_ADMIN_REQUIRE_PASSKEY is set (that would brick the console)."""
    if (sess := _session(request)) is None:
        return RedirectResponse("/admin/console", status_code=303)
    from ..auth import admin_passkeys
    admin = tenancy.admin_connect()
    try:
        n = admin_passkeys.count(admin)
        if (_require_passkey() or _passkey_only()) and n <= 1:
            back = ("`oikonome admin-enrol` on the host" if _passkey_only()
                    else "`oikonome admin-keys --reset` on the host")
            return _dashboard(request, notice=(
                "Not removed — this is the only operator passkey and this "
                "console has no other way in, so removing it from here would "
                f"lock it. Enrol the replacement first, or use {back}."))
        if n and not _stepup_ok(sess, admin, stepup):
            return _dashboard(request, notice="Not removed — confirm with "
                              "your passkey and try again.")
        # Revoking the CREDENTIAL must also kill every session it
        # authenticated; otherwise those live on for the rest of
        # SESSION_TTL. Where the console is passkey-only the passkey IS the
        # credential, so "remove this key" has to mean the key can no longer
        # act, not merely that it cannot sign in again — a stolen
        # authenticator that had already signed in would otherwise keep full
        # cross-tenant console access (and the console drives the root
        # host-agent) for hours after being revoked.
        #
        # ORDER IS LOAD-BEARING. `admin_sessions.credential_id` is declared
        # `REFERENCES admin_credentials(id) ON DELETE SET NULL`, so deleting
        # the credential NULLs this exact column on every session it
        # authenticated. Revoke first and the rows are found; revoke after
        # `admin_passkeys.delete` and the WHERE clause matches nothing — code
        # that reads correctly and protects nothing. A test cannot catch the
        # wrong order by querying `credential_id` afterwards either, because
        # once the FK has cleared it the query returns empty either way; only
        # a fails-before run distinguishes them.
        #
        # Deleting the session AHEAD of admin_passkeys.delete also puts it
        # ahead of that function's blanket except, which would otherwise
        # absorb a malformed cred_id — an unparseable value reaches
        # `%s::uuid` and 500s. Parse it here instead.
        try:
            key_uuid = str(_uuid.UUID(str(cred_id)))
        except (ValueError, AttributeError, TypeError):
            return _dashboard(request, notice="No such passkey.")
        killed = admin.execute(
            "DELETE FROM admin_sessions WHERE credential_id = %s::uuid",
            (key_uuid,)).rowcount
        if not admin_passkeys.delete(admin, key_uuid):
            return _dashboard(request, notice="No such passkey.")
        _audit(admin, "admin_passkey_removed", target=cred_id,
               ip=client_ip(request))
        if killed:
            _audit(admin, "admin_sessions_revoked_with_passkey",
                   target=cred_id, ip=client_ip(request))
    finally:
        admin.close()
    return RedirectResponse("/admin/console#ops", status_code=303)


@router.post("/passkey/login/options",
             # Its own bucket — sharing "admin_login" meant ~5
             # passkey ceremonies/hour locked the console's ONLY credential out
             dependencies=[Depends(limit("admin_passkey_options", 20, 3600))])
def passkey_login_options(request: Request):
    from ..auth import admin_passkeys
    rp_id, _ = _rp(request)
    admin = tenancy.admin_connect()
    try:
        return admin_passkeys.login_options(admin, rp_id)
    except ValueError as e:
        return _json_error(400, str(e))
    finally:
        admin.close()


@router.post("/passkey/login",
             dependencies=[Depends(limit("admin_passkey_login", 10, 3600))])
def passkey_login(request: Request, body: dict = Body(...)):
    from ..auth import admin_passkeys
    rp_id, origin = _rp(request)
    admin = tenancy.admin_connect()
    try:
        try:
            cred = admin_passkeys.login_verify(
                admin, str(body.get("challenge_id") or ""),
                body.get("credential") or {}, rp_id, origin)
        except ValueError as e:
            return _json_error(401, str(e))
        except Exception as e:                    # noqa: BLE001 — lib errors
            log.warning("admin passkey login failed: %s rp_id=%s origin=%s",
                        type(e).__name__, rp_id, origin,
                        extra={"no_capture": True})
            return _json_error(401, f"passkey sign-in failed: "
                                    f"{type(e).__name__}")
    finally:
        admin.close()
    # fetch()-driven login page: JSON + the session cookie, and the page
    # navigates itself (a 303 through fetch would be followed invisibly)
    return _issue_session(
        request, JSONResponse({"ok": True, "next": "/admin/console"}),
        credential_id=cred["id"],
        actor="passkey:" + (cred["label"] or cred["id"][:8]))


@router.post("/passkey/stepup/options",
             dependencies=[Depends(limit("admin_passkey", 30, 3600))])
def passkey_stepup_options(request: Request):
    if (sess := _session(request)) is None:
        return _json_error(401, "not signed in")
    from ..auth import admin_passkeys
    rp_id, _ = _rp(request)
    admin = tenancy.admin_connect()
    try:
        return admin_passkeys.stepup_options(admin, sess["token_hash"], rp_id)
    except ValueError as e:
        return _json_error(400, str(e))
    finally:
        admin.close()


@router.post("/passkey/stepup",
             dependencies=[Depends(limit("admin_passkey", 30, 3600))])
def passkey_stepup(request: Request, body: dict = Body(...)):
    if (sess := _session(request)) is None:
        return _json_error(401, "not signed in")
    from ..auth import admin_passkeys
    rp_id, origin = _rp(request)
    admin = tenancy.admin_connect()
    try:
        try:
            ticket = admin_passkeys.stepup_verify(
                admin, sess["token_hash"], str(body.get("challenge_id") or ""),
                body.get("credential") or {}, rp_id, origin)
        except ValueError as e:
            return _json_error(401, str(e))
        except Exception as e:                    # noqa: BLE001 — lib errors
            return _json_error(401, f"passkey check failed: "
                                    f"{type(e).__name__}")
    finally:
        admin.close()
    return {"stepup_ticket": ticket}


def _stepup_ok(sess, admin, ticket: str) -> bool:
    """Redeem a step-up ticket against THIS session. Single use — a replay
    of the same form finds the row already gone."""
    if sess is None:
        return False
    from ..auth import admin_passkeys
    return admin_passkeys.redeem_ticket(admin, sess["token_hash"],
                                        (ticket or "").strip())


# ---- actions (all audit-logged) ------------------------------------------


@router.post("/invite",
             dependencies=[Depends(limit("admin_action", 30, 3600))])
async def invite(request: Request, email: str = Form(...),
                 days: int = Form(14), note: str = Form("")):
    # every field beyond the product's own is the installed gate's to read
    form = await request.form()
    extra = {k: str(v) for k, v in form.multi_items()
             if k not in ("email", "days", "note")}
    return _mint_invite(request, email=email, days=days, note=note,
                        extra=extra)


def _mint_invite(request: Request, *, email: str = "", days: int = 14,
                 note: str = "", extra: dict | None = None,
                 request_id: str | None = None):
    """The one mint path, shared by the invite form and by approving an
    access request. When `request_id` is given the address comes from the
    installed gate's request row, and the row is read, checked and claimed
    INSIDE the same advisory-locked transaction as the mint — see the
    comment on the lock below."""
    if (bounce := _require(request)) is not None:
        return bounce
    if not _multi_tenant():
        # a stale form or a curl must not mint what /signup will refuse
        return _dashboard(request, notice=_SINGLE_HOUSEHOLD_NOTICE)
    from urllib.parse import quote

    from .. import ext
    from ..auth import signup_invites
    email = email.strip().lower()
    # what the new account is born with is the installed gate's business;
    # the invite just carries it
    options = ext.gate.invite_options(extra or {})
    # The invite's own lifetime — the one number on this form that rides a
    # raw form field into arithmetic. `mint` now applies this identical bound itself, so
    # every caller (this form, the CLI) inherits it; the clamp stays here
    # because `days` is also written into the audit line below, and an audit
    # that records the number typed rather than the lifetime actually minted
    # is worse than no audit line at all.
    days = signup_invites.bounded_days(days)
    admin = tenancy.admin_connect()
    try:
        # stop intake at the cap the installed gate reports. Enforced at
        # MINT time only — an invite already in someone's inbox stays
        # claimable, because stranding an invited user to defend a cap is
        # the worse failure. No cap configured is never at cap.
        #
        # Because claims are never refused, the mint IS the promise of a
        # seat — so the gate counts seats already promised (live unclaimed
        # invites), not just registered accounts, and the count-and-mint is
        # one atomic act under an instance-wide advisory lock. A bare
        # accounts-vs-cap read let any number of invites out while one seat
        # remained, and two concurrent mints both read the same count and
        # both passed. The target address's own outstanding invite is
        # excluded from the count: mint burns older invites for the email,
        # so a re-invite replaces a promised seat rather than adding one.
        with admin.transaction():
            admin.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                          ("oikonome:intake-mint",))
            if request_id is not None:
                # Approving an access request reads-checks-claims here, under
                # the SAME lock as the mint, because minting BURNS every
                # older unused invite for the address. A pre-check on its
                # own connection let two overlapping approvals (a
                # double-click, two tabs) both see invited_at IS NULL and
                # both mint: the second invite invalidated the first one,
                # which had already been emailed. Serialized here, the
                # loser reads the claim the winner wrote and is a no-op.
                claimed = ext.gate.claim_intake_request(admin, request_id)
                if claimed is None:
                    # the finally below closes the connection; _dashboard
                    # opens its own
                    return _dashboard(request, notice="That request is gone "
                                                      "or already approved.")
                email = claimed
            # accounts_used_guarded, not `usage`: this door reads only
            # the account count, and `usage` also scans `sessions` and
            # counts open access requests — two queries held under the
            # instance-wide intake-mint lock for nothing. The guard is a
            # SAVEPOINT because this transaction goes on to mint: a bare
            # except around a failed statement leaves the transaction
            # aborted, so the promised fail-open degrade was a 500.
            accounts = ext.gate.accounts_used_guarded(admin)
            promised = admin.execute(
                "SELECT count(*) AS n FROM signup_invites "
                "WHERE used_at IS NULL AND expires_at > now() "
                "AND email <> %s", (email,)).fetchone()["n"]
            if ext.gate.at_cap(accounts + promised):
                cap = ext.gate.intake_cap()
                _audit(admin, "invite_refused", target=email,
                       detail=(f"at capacity: {accounts} accounts + "
                               f"{promised} open invites / {cap}"),
                       ip=client_ip(request))
                # the finally below closes the connection; _dashboard
                # opens its own
                return _dashboard(request, notice=(
                    f"Invite NOT minted — intake is at capacity "
                    f"({accounts} accounts plus {promised} open "
                    f"invites, of {cap}). Raise the intake cap to admit "
                    f"more accounts."))
            token = signup_invites.mint(admin, email, days=days,
                                        note=note or None, options=options)
            ext.gate.on_invite_minted(admin, email)
            _audit(admin, "invite", target=email,
                   detail=(f"days={days}"
                           + (f" note={note}" if note else "")
                           + "".join(f" {k}={v}" for k, v in options.items())),
                   ip=client_ip(request))
    finally:
        admin.close()
    path = f"/signup?invite={token}&email={quote(email)}"
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    link = f"{base}{path}" if base else path
    # deliver by email too (same path as the approve-by-email flow);
    # the link renders once so the operator can also hand it over directly
    from .app import _deliver_invite
    threading.Thread(target=_deliver_invite, args=(email, path),
                     daemon=True).start()
    tail = ext.gate.invite_summary(options)
    # the send runs in a thread, so "emailed" is a promise, not a fact.
    # When the relay is known to be down say so HERE, at the moment the
    # operator is looking, and point at the link they can hand over.
    mh = _mail_health()
    if mh["failing"]:
        notice = (f"Invite minted for {email}{tail}. OUTBOUND MAIL IS "
                  f"FAILING ({mh['reason']}) — the email will most likely "
                  f"not arrive; send them the link below yourself.")
    else:
        notice = f"Invite minted and emailed to {email}{tail}."
    return _dashboard(request, minted_link=link, notice=notice,
                      mail_health=mh)


@router.post("/block",
             dependencies=[Depends(limit("admin_action", 30, 3600))])
def block_domain(request: Request, value: str = Form(...)):
    """Manually block a domain or full email address."""
    if (bounce := _require(request)) is not None:
        return bounce
    from . import spamdefense
    admin = tenancy.admin_connect()
    try:
        kind = spamdefense.manual_block(admin, value, added_by="operator")
        _audit(admin, "spam_block", target=value.strip().lower(),
               ip=client_ip(request))
    finally:
        admin.close()
    return _dashboard(request, notice=f"Blocked {kind}: {value.strip().lower()}.")


@router.post("/unblock",
             dependencies=[Depends(limit("admin_action", 30, 3600))])
def unblock_domain(request: Request, value: str = Form(...)):
    """Remove a domain/address block (undo a mistake)."""
    if (bounce := _require(request)) is not None:
        return bounce
    from . import spamdefense
    admin = tenancy.admin_connect()
    try:
        spamdefense.unblock(admin, value)
        _audit(admin, "spam_unblock", target=value.strip().lower(),
               ip=client_ip(request))
    finally:
        admin.close()
    return _dashboard(request, notice=f"Unblocked {value.strip().lower()}.")


@router.post("/refresh-blocklist",
             dependencies=[Depends(limit("admin_action", 30, 3600))])
def refresh_blocklist(request: Request):
    """Enqueue the public-list refresh on the worker (fetch is slow — never
    run it in-request)."""
    if (bounce := _require(request)) is not None:
        return bounce
    admin = tenancy.admin_connect()
    try:
        if not _enqueue("blocklist_refresh"):
            _audit(admin, "run_job_failed", target="blocklist_refresh",
                   ip=client_ip(request))
            return _dashboard(request, notice="Couldn't reach the job queue — "
                                              "is redis up?")
        _audit(admin, "refresh_blocklist", ip=client_ip(request))
    finally:
        admin.close()
    return _dashboard(request, notice="Blocklist refresh queued.")


@router.post("/resend-verification",
             dependencies=[Depends(limit("admin_action", 30, 3600))])
def resend_verification(request: Request, user_id: str = Form(...)):
    if (bounce := _require(request)) is not None:
        return bounce
    from ..auth import email_verify
    admin = tenancy.admin_connect()
    try:
        u = admin.execute(
            "SELECT id, email, tenant_id, verified_at FROM users "
            "WHERE id=%s", (user_id,)).fetchone()
        if u is None or u["verified_at"] is not None:
            return _dashboard(request,
                              notice="User missing or already verified.")
        token = email_verify.create(admin, u["id"])
        _audit(admin, "resend_verification", target=u["email"],
               ip=client_ip(request))
    finally:
        admin.close()
    from .app import _deliver_verification
    threading.Thread(target=_deliver_verification,
                     args=(u["email"], token, u["tenant_id"]),
                     daemon=True).start()
    return _dashboard(request,
                      notice=f"Verification link re-sent to {u['email']}.")


@router.post("/clear-2fa",
             dependencies=[Depends(limit("admin_action", 10, 3600))])
def clear_second_factor(request: Request, user_id: str = Form(...),
                        confirm: str = Form("")):
    """The operator door for a person who has lost the password, the
    authenticator AND the recovery codes: void TOTP, passkeys and recovery
    codes, evict every session and device exactly like a reset does, and
    mail the account that it happened. Guarded by typing the address —
    the same confirmation the destructive tenant actions take — because
    the operator is asserting an identity check they made out of band,
    and the audit row is the record that they did.

    The password is left alone: the person sets a new one through the
    ordinary reset, which now needs no code. The operator can hand them
    that link from the same page."""
    if (bounce := _require(request)) is not None:
        return bounce
    from ..auth import factor_reset
    admin = tenancy.admin_connect()
    try:
        u = admin.execute(
            "SELECT id, email, tenant_id FROM users WHERE id=%s",
            (user_id,)).fetchone()
        if u is None:
            return _dashboard(request, notice="No such user.")
        if confirm.strip().lower() != u["email"].lower():
            return _dashboard(
                request, notice=f"Second factor NOT cleared — type the "
                                f"user's email ({u['email']}) to confirm.")
        removed = factor_reset.clear_second_factor(admin, u["id"])
        admin.commit()
        _audit(admin, "clear_second_factor", target=u["email"],
               detail=(f"totp={'yes' if removed['totp'] else 'no'} "
                       f"passkeys={removed['passkeys']} "
                       f"sessions_evicted={removed['sessions']}"),
               ip=client_ip(request))
    finally:
        admin.close()
    from .app import _deliver_factor_cleared_notice
    threading.Thread(target=_deliver_factor_cleared_notice,
                     args=(u["email"],), daemon=True).start()
    return _dashboard(
        request, notice=(f"Second factor cleared for {u['email']} — "
                         f"TOTP {'removed' if removed['totp'] else 'was off'}, "
                         f"{removed['passkeys']} passkey(s) and every "
                         f"recovery code removed, {removed['sessions']} "
                         f"session(s) signed out. They were emailed. Hand "
                         f"them a reset link next if they need a password."))


@router.post("/tenant-status",
             dependencies=[Depends(limit("admin_action", 30, 3600))])
def tenant_status(request: Request, tenant_id: str = Form(...),
                  to: str = Form(...)):
    """Suspend / reactivate a tenant. Suspension is TOTAL (the current_user
    gate 403s every request except logout) — the abuse and support lever,
    distinct from billing's read-only state."""
    if (bounce := _require(request)) is not None:
        return bounce
    if to not in ("active", "suspended"):
        raise HTTPException(400, "to must be active|suspended")
    admin = tenancy.admin_connect()
    try:
        row = admin.execute(
            "UPDATE tenants SET status=%s WHERE id=%s RETURNING id",
            (to, tenant_id)).fetchone()
        if row is None:
            return _dashboard(request, notice="No such tenant.")
        _audit(admin, f"tenant_{'suspend' if to == 'suspended' else 'activate'}",
               target=tenant_id, ip=client_ip(request))
    finally:
        admin.close()
    return _dashboard(request, notice=f"Tenant {to}.")


@router.post("/sync-now",
             dependencies=[Depends(limit("admin_action", 30, 3600))])
def sync_now(request: Request, tenant_id: str = Form(...)):
    """Enqueue an immediate sync for one tenant — the arq worker runs it,
    so the console never blocks on bank latency."""
    if (bounce := _require(request)) is not None:
        return bounce
    import asyncio

    async def _enqueue():
        from arq import create_pool

        from ..jobs.worker import _redis_settings
        pool = await create_pool(_redis_settings())
        try:
            await pool.enqueue_job("sync_one", tenant_id)
        finally:
            await pool.close()

    admin = tenancy.admin_connect()
    try:
        if admin.execute("SELECT 1 FROM tenants WHERE id=%s",
                         (tenant_id,)).fetchone() is None:
            return _dashboard(request, notice="No such tenant.")
        try:
            asyncio.run(_enqueue())
        except Exception as e:                                # noqa: BLE001
            _audit(admin, "sync_now_failed", target=tenant_id,
                   detail=type(e).__name__, ip=client_ip(request))
            return _dashboard(request,
                              notice="Couldn't reach the job queue — is "
                                     "redis up?")
        _audit(admin, "sync_now", target=tenant_id, ip=client_ip(request))
    finally:
        admin.close()
    return _dashboard(request, notice="Sync queued.")


# ---- host state, broadcast, jobs, logs, drill-down --------------

def _host_state() -> dict:
    """Backup + host-health snapshots via the read-only binds (compose
    mounts <install>/backups and <install>/ops-state at /state). Missing
    paths read as 'not configured' — never an error."""
    import json as _json
    from pathlib import Path
    base = Path(os.environ.get("OIKONOME_STATE_DIR", "/state"))
    out: dict = {"backups": [], "backups_available": False, "host": None}
    bdir = base / "backups"
    if bdir.is_dir():
        out["backups_available"] = True
        now_ts = dt.datetime.now().timestamp()
        # Snapshot each dump's stat ONCE. The nightly backup job rotates
        # (deletes) old dumps, so a file present at glob() can vanish before a
        # later .stat() — repeated stat()s would raise FileNotFoundError and
        # 500 the whole console. Skip anything that disappears mid-read.
        dumps = []
        for p in bdir.glob("oikonome-*.sql.gz"):
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            dumps.append((p.name, st.st_mtime, st.st_size))
        dumps.sort(key=lambda d: d[1], reverse=True)
        out["backups"] = [
            {"name": name, "size_mb": round(size / 1e6, 1),
             "age_h": round((now_ts - mtime) / 3600, 1)}
            for name, mtime, size in dumps[:5]]
        # human-readable rollup: count, total size, the
        # newest/oldest, distinct days covered, and a daily-health verdict
        # (a dump inside the last ~30h means the nightly job is running)
        if dumps:
            newest_mtime = dumps[0][1]
            oldest_mtime = dumps[-1][1]
            days = {dt.datetime.fromtimestamp(m).date()
                    for _n, m, _s in dumps}
            out["backup_summary"] = {
                "count": len(dumps),
                "total_mb": round(sum(s for _n, _m, s in dumps) / 1e6, 1),
                "newest_age_h": round((now_ts - newest_mtime) / 3600, 1),
                "oldest_at": dt.datetime.fromtimestamp(
                    oldest_mtime).strftime("%m/%d/%y"),
                "days_covered": len(days),
                "daily_ok": (now_ts - newest_mtime) <= 30 * 3600,
            }
    hh = base / "ops" / "host-health.json"
    if hh.is_file():
        try:
            out["host"] = _json.loads(hh.read_text())
        except Exception:                                   # noqa: BLE001
            out["host"] = None
    # containers roll up per compose project — a flat list gets busy fast —
    # with green "N up" summaries, and ONLY genuine failures listed
    # individually in red. FOUR states, not two: a cleanly-EXITED (0)
    # container is not broken; a one-shot INIT job (the `migrate`
    # container) that exited 0 is DONE — it's supposed to run once and
    # stop, so it must never read as "stopped" (that alarms on every
    # healthy box); a non-one-shot service that
    # exited 0 is STOPPED (intentionally down, e.g. bundled postgres on a
    # managed-DB stack); only a non-zero exit / unhealthy / restarting / dead
    # container is red. Marking every non-running container red/stopped cried
    # wolf and buried real alerts.
    if out["host"] and out["host"].get("containers"):
        groups: dict[str, dict] = {}
        for c in out["host"]["containers"]:
            name = c.get("name", "")
            project = name.rsplit("-", 2)[0] if name.count("-") >= 2 else name
            g = groups.setdefault(project, {"project": project, "total": 0,
                                            "ok": 0, "done": 0, "stopped": 0,
                                            "bad": []})
            g["total"] += 1
            status = c.get("status") or ""
            state = c.get("state") or ""
            if (state == "running" and "unhealthy" not in status
                    and "starting" not in status):
                g["ok"] += 1
            elif state == "exited" and "Exited (0)" in status:
                # one-shot init jobs (migrate) completed OK → "done", not
                # "stopped"; match the compose service name in `<proj>-<svc>-<n>`
                if "-migrate-" in name or "-init-" in name:
                    g["done"] += 1
                else:
                    g["stopped"] += 1    # a service intentionally down
            else:
                g["bad"].append(c)       # non-zero exit, unhealthy, dead…
        out["container_groups"] = sorted(groups.values(),
                                         key=lambda g: g["project"])
    else:
        out["container_groups"] = []
    return out


def _enqueue(job: str, *args) -> bool:
    import asyncio

    async def _go():
        from arq import create_pool

        from ..jobs.worker import _redis_settings
        pool = await create_pool(_redis_settings())
        try:
            await pool.enqueue_job(job, *args)
        finally:
            await pool.close()
    try:
        asyncio.run(_go())
        return True
    except Exception:                                       # noqa: BLE001
        return False


@router.post("/broadcast",
             dependencies=[Depends(limit("admin_action", 30, 3600))])
def broadcast_set(request: Request, message: str = Form(""),
                  severity: str = Form("info")):
    """Set (non-empty message) or clear (empty) the fleet-wide banner."""
    if (bounce := _require(request)) is not None:
        return bounce
    if severity not in ("info", "warn"):
        raise HTTPException(400, "severity must be info|warn")
    message = message.strip()[:300]
    admin = tenancy.admin_connect()
    try:
        if message:
            admin.execute(
                """INSERT INTO broadcast (id, message, severity, set_at)
                   VALUES (1, %s, %s, now())
                   ON CONFLICT (id) DO UPDATE SET message = EXCLUDED.message,
                     severity = EXCLUDED.severity, set_at = now()""",
                (message, severity))
            _audit(admin, "broadcast_set", detail=message[:120],
                   ip=client_ip(request))
            note = "Broadcast set — every signed-in user sees it."
        else:
            admin.execute("DELETE FROM broadcast WHERE id = 1")
            _audit(admin, "broadcast_clear", ip=client_ip(request))
            note = "Broadcast cleared."
    finally:
        admin.close()
    return _dashboard(request, notice=note)


@router.post("/run-job",
             dependencies=[Depends(limit("admin_action", 30, 3600))])
def run_job(request: Request, job: str = Form(...)):
    """Fleet-wide job triggers — the Makefile verbs that make sense from
    inside the container: hourly sync sweep, nightly detection,
    email sweep. Enqueued to the worker, never run in-request."""
    if (bounce := _require(request)) is not None:
        return bounce
    if job not in ("sync_all", "nightly_all", "email_all"):
        raise HTTPException(400, "unknown job")
    admin = tenancy.admin_connect()
    try:
        if not _enqueue(job):
            _audit(admin, "run_job_failed", target=job, ip=client_ip(request))
            return _dashboard(request,
                              notice="Couldn't reach the job queue — is "
                                     "redis up?")
        _audit(admin, "run_job", target=job, ip=client_ip(request))
    finally:
        admin.close()
    return _dashboard(request, notice=f"{job} queued.")


@router.get("/tenant/{tenant_id}")
def tenant_detail(request: Request, tenant_id: str, msg: str = ""):
    """Per-tenant drill-down: the support view. Control-plane rows and
    per-tenant health only — no tenant DATA (the doctrine)."""
    if not _authed(request):
        return RedirectResponse("/admin/console", status_code=303)
    admin = tenancy.admin_connect()
    try:
        tenant = admin.execute(
            "SELECT id, name, created_at, status, delete_after "
            "FROM tenants WHERE id=%s", (tenant_id,)).fetchone()
        if tenant is None:
            raise HTTPException(404, "no such tenant")
        from .. import tenant_export as _exp
        consent = _exp.consent_status(admin, tenant_id)
        users = admin.execute(
            """SELECT id, email, role, created_at, verified_at,
                      (totp_secret IS NOT NULL) AS totp,
                      (SELECT count(*) FROM passkeys p
                        WHERE p.user_id = users.id) AS passkeys
               FROM users WHERE tenant_id=%s ORDER BY created_at""",
            (tenant_id,)).fetchall()
        sess = admin.execute(
            """SELECT user_agent, created_at, last_seen FROM sessions
               WHERE tenant_id=%s AND expires_at > now()
               ORDER BY last_seen DESC NULLS LAST LIMIT 10""",
            (tenant_id,)).fetchall()
        synclog = admin.execute(
            """SELECT ran_at, item_id, added, error FROM sync_log
               WHERE tenant_id=%s ORDER BY ran_at DESC LIMIT 20""",
            (tenant_id,)).fetchall()
        extra = ext.gate.tenant_context(admin, tenant_id)
        counts = admin.execute(
            """SELECT (SELECT count(*) FROM accounts WHERE tenant_id=%s)
                        AS accounts,
                      (SELECT count(*) FROM transactions WHERE tenant_id=%s)
                        AS txns,
                      (SELECT count(*) FROM items WHERE tenant_id=%s
                         AND COALESCE(status,'ok') != 'archived') AS items""",
            (tenant_id, tenant_id, tenant_id)).fetchone()
    finally:
        admin.close()
    from . import todayview
    return HTMLResponse(todayview._env.get_template("admin_tenant.html")
                        # bookmark sweep: several tenant tabs open at once
                        # must be tellable apart (and apart from the app)
                        .render(title="Admin console — "
                                      f"{tenant['name'] or tenant_id[:8]}",
                                centered=False,
                                tenant=tenant, users=users, sessions=sess,
                                synclog=synclog,
                                counts=counts, consent=consent,
                                # the statuses a restore can lift — the
                                # template tests membership rather than
                                # naming any of them
                                frozen_statuses=_frozen_statuses(),
                                msg=msg[:300], **extra))


def _frozen_statuses() -> list[str]:
    """Tenant statuses a restore can lift: the product's own scheduled
    deletion plus whatever an installed add-on freezes a household under.
    Read from the gate rather than written out here, so the console and
    `current_user` can never disagree about the set."""
    return ["pending_delete", *ext.gate.lockout_statuses()]


def _grace_days() -> int:
    """Days between arming a delete and the worker purging it.
    OIKONOME_DELETE_GRACE_DAYS, default 7; 0 = immediate-only (the arm
    path then wipes immediately)."""
    try:
        return max(0, int(os.environ.get("OIKONOME_DELETE_GRACE_DAYS", "7")))
    except ValueError:
        return 7


def _purge_tenant(admin, tenant_id: str, expected: str, ip: str) -> dict:
    """The actual irreversible wipe — release external services, then
    delete every row. Shared by the immediate path and the worker's
    grace-window purge.

    Holds the tenant's sync advisory lock across the wipe: the domain
    tables have no FK to tenants, so a sync in flight when the rows go
    would upsert them right back — permanently, for a tenant that no
    longer exists. Blocking on the same lock every sync door try-locks
    means an in-flight sync finishes first and none can start mid-wipe.
    (The worker's grace purge additionally takes an xact-scoped lock in
    its own transaction, so the lock outlives this function's unlock
    until the purge actually commits.)"""
    from .. import erasure as _erasure
    admin.execute("SELECT pg_advisory_lock(hashtext(%s))",
                  (f"oikonome:sync:{tenant_id}",))
    try:
        released = _erasure.release_external(tenant_id)
        retry_ids = _erasure.collect_retry_ids(tenant_id)
        _erasure.record_pending_release(admin, tenant_id, released, retry_ids)
        tenancy.delete_tenant_rows(admin, tenant_id)
        _audit(admin, "tenant_delete", target=tenant_id,
               detail=f"owner={expected} plaid_items_released="
                      f"{released['plaid_items']} released={released['released']} "
                      f"mx={released['mx_user']} sms={released.get('sms')}"
                      + (f" plaid_failed={released['plaid_failed']}"
                         if released.get('plaid_failed') else ""), ip=ip)
    finally:
        # session-level, so it must not be left to the connection's close
        try:
            admin.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                          (f"oikonome:sync:{tenant_id}",))
        except Exception:                             # noqa: BLE001
            pass
    return released


@router.post("/tenant-delete",
             dependencies=[Depends(limit("admin_action", 10, 3600))])
def tenant_delete(request: Request, tenant_id: str = Form(...),
                  confirm: str = Form(""), mode: str = Form("grace"),
                  stepup: str = Form("")):
    """Schedule a tenant for deletion with a grace window (default), or
    delete immediately (the abuse case). Guarded by a typed confirmation:
    `confirm` must be the owner's email (or the tenant id when no user
    exists).

    mode='grace' (default): flip to status='pending_delete', stamp
    delete_after = now + OIKONOME_DELETE_GRACE_DAYS; the tenant is frozen
    (403 like suspension) and the nightly worker purges it once the window
    lapses. Restore any time before then. mode='immediate': wipe now,
    unrecoverable outside backups."""
    if (bounce := _require(request)) is not None:
        return bounce
    admin = tenancy.admin_connect()
    try:
        t = admin.execute("SELECT id FROM tenants WHERE id=%s",
                          (tenant_id,)).fetchone()
        if t is None:
            return _dashboard(request, notice="No such tenant.")
        from ..db import tenancy as _tenancy
        expected = _tenancy.owner_email(admin, tenant_id) or tenant_id
        if confirm.strip().lower() != expected.lower():
            return _dashboard(
                request, notice=f"Not deleted — type the owner's email "
                                f"({expected}) to confirm.")
        grace = _grace_days()
        if mode == "immediate" or grace == 0:
            # An immediate purge is unrecoverable outside
            # backups — from a customer's perspective it is every bit as
            # destructive as host restore/reset/uninstall, which already
            # demand a fresh single-use passkey assertion so a stolen
            # 4-hour console cookie cannot reach them. Same gate here;
            # skipped only when no operator key exists (plain-http
            # self-host, same rule as run_command). Grace-mode stays
            # ungated: it is restorable for the whole window.
            if _passkey_count():
                sess = _session(request)
                if not _stepup_ok(sess, admin, stepup):
                    _audit(admin, "tenant_delete_refused", target=tenant_id,
                           detail="no fresh passkey assertion",
                           ip=client_ip(request))
                    return _dashboard(request, notice=(
                        "Not deleted — an immediate purge needs a fresh "
                        "passkey confirmation. Try again and confirm with "
                        "your passkey when asked."))
            _purge_tenant(admin, tenant_id, expected, client_ip(request))
            return _dashboard(request,
                              notice=f"Tenant deleted immediately ({expected}).")
        # grace: freeze + schedule; the worker finishes it later. Capture
        # the prior status (a suspended tenant scheduled for deletion must
        # restore back to SUSPENDED, not silently un-suspend). Only stamp
        # it when not already pending_delete, so a
        # double-schedule can't overwrite the true prior state with
        # 'pending_delete'.
        admin.execute(
            "UPDATE tenants SET "
            "status_before_delete = CASE WHEN status='pending_delete' "
            "  THEN status_before_delete ELSE status END, "
            "status='pending_delete', "
            "delete_after = now() + make_interval(days => %s) WHERE id=%s",
            (grace, tenant_id))
        # Tell the installed gate the account is frozen. The freeze is
        # local, so without this hook the gate's own rails would carry on
        # straight through a grace deletion — an account charged for days
        # it cannot open. The hook pauses rather than cancels, which is
        # what keeps the window reversible: a restore lifts the pause.
        paused = ext.gate.on_tenant_delete_scheduled(tenant_id)
        _audit(admin, "tenant_delete_scheduled", target=tenant_id,
               detail=f"owner={expected} grace_days={grace} "
                      f"release={paused}",
               ip=client_ip(request))
    finally:
        admin.close()
    return _dashboard(
        request, notice=f"Tenant scheduled for deletion in {grace} day(s) "
                        f"({expected}) — frozen now, restorable until then.")


@router.post("/tenant-restore",
             dependencies=[Depends(limit("admin_action", 10, 3600))])
def tenant_restore(request: Request, tenant_id: str = Form(...)):
    """Cancel a scheduled deletion (grace window still open) and reactivate
    the tenant. Also doubles as the manual override for an add-on's own
    frozen status, since it is the same frozen+delete_after shape."""
    if (bounce := _require(request)) is not None:
        return bounce
    admin = tenancy.admin_connect()
    try:
        # restore to the status held BEFORE the delete was scheduled
        # (suspended stays suspended), defaulting to active — a row frozen
        # by an add-on never sets status_before_delete, so it always
        # restores to active. FOR UPDATE + the same predicate serialize
        # against the worker purge: whichever grabs the row lock first
        # wins, and the loser sees the other's outcome.
        with admin.transaction():
            row = admin.execute(
                "SELECT COALESCE(status_before_delete,'active') AS prev "
                "FROM tenants WHERE id=%s "
                "AND status = ANY(%s) "
                "FOR UPDATE", (tenant_id, _frozen_statuses())).fetchone()
            if row is None:
                return _dashboard(request,
                                  notice="Not scheduled for deletion and "
                                         "not frozen — nothing to restore.")
            admin.execute(
                "UPDATE tenants SET status=%s, delete_after=NULL, "
                "status_before_delete=NULL WHERE id=%s",
                (row["prev"], tenant_id))
            _audit(admin, "tenant_delete_restored", target=tenant_id,
                   detail=f"restored_to={row['prev']}", ip=client_ip(request))
    finally:
        admin.close()
    # Tell the installed gate the deletion was called off, so the pause
    # the scheduling put on its rails comes back off. Outside the
    # transaction: a round trip to an outside service must not be held
    # across the row lock this door takes against the worker's purge. A
    # no-op for a tenant that was never paused, which is most of them.
    ext.gate.on_tenant_restored(tenant_id)
    return _dashboard(request, notice=f"Tenant restored to {row['prev']}.")


@router.post("/tenant-export",
             dependencies=[Depends(limit("admin_export", 10, 3600))])
def tenant_export(request: Request, tenant_id: str = Form(...)):
    """Portable data export (CCPA/GDPR portability). Always audited; the
    audit records whether the tenant had a live support consent on
    file."""
    if (bounce := _require(request)) is not None:
        return bounce
    from .. import tenant_export as _exp
    admin = tenancy.admin_connect()
    try:
        t = admin.execute("SELECT name FROM tenants WHERE id=%s",
                          (tenant_id,)).fetchone()
        if t is None:
            return _dashboard(request, notice="No such tenant.")
        operator = "operator"   # admin sessions are token-only (no email)
        consented = _exp.has_live_consent(admin, tenant_id)
        if not consented:
            # Settings tells every household, without qualification:
            # "Oikonome support can never see your data unless you grant
            # it. A grant is time-boxed, revocable, and every access it
            # allows is logged." An operator export without a live grant
            # would make that sentence false. A portability request needs
            # no operator: the household can export itself from Settings.
            _audit(admin, "tenant_export_refused", target=tenant_id,
                   detail="no live support consent", ip=client_ip(request))
            return _dashboard(
                request,
                notice="Export refused: this household has not granted "
                       "support access. Ask them to grant it in Settings → "
                       "Support access; they can also export their own data "
                       "there without you.")
        _audit(admin, "tenant_export", target=tenant_id,
               detail=f"consent_on_file={consented}", ip=client_ip(request))
    finally:
        admin.close()
    try:
        data = _exp.build_archive(tenant_id, operator=operator,
                                  consented=consented)
    except ValueError:
        return _dashboard(request, notice="No such tenant.")
    from fastapi.responses import Response
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="oikonome-export-'
                 f'{tenant_id[:8]}-{stamp}.zip"'})


# single-use confirmation nonces for the reboot flow — a browser
# replaying the "go" POST (tab restore / refresh resubmission) finds its
# nonce consumed and gets a
# harmless notice instead of a reboot. In-memory on purpose:
# a restart forgetting nonces fails toward refusing.
_REBOOT_NONCES: dict[str, float] = {}


@router.post("/host-reboot",
             dependencies=[Depends(limit("admin_action", 5, 3600))])
def host_reboot(request: Request, stage: str = Form("arm"),
                nonce: str = Form(""), confirm: str = Form(""),
                stepup: str = Form("")):
    """Request a HOST reboot. The first click ARMS — the dashboard
    re-renders with a confirm banner; only the second submit, carrying the
    single-use nonce, the typed word "reboot" and (once an operator key is
    enrolled) a fresh passkey assertion, drops the flag. Same gates as the
    destructive host commands: rebooting takes every instance on the box
    down, so a stolen 4-hour cookie must not be able to do it alone. The
    container cannot reboot the box — it writes /state/cmd/reboot-requested
    and a root path-unit on the host (scripts/install-host-reboot-watcher.sh)
    validates freshness, removes it, and reboots. Also auto-sets the
    broadcast."""
    if (sess := _session(request)) is None:
        return RedirectResponse("/admin/console", status_code=303)
    import time as _time
    for k in [k for k, exp in _REBOOT_NONCES.items() if exp < _time.time()]:
        _REBOOT_NONCES.pop(k, None)
    if stage != "go":
        # armed: the dashboard renders a top-of-page red confirm banner
        # carrying a fresh single-use nonce (5 min)
        n = secrets.token_urlsafe(16)
        _REBOOT_NONCES[n] = _time.time() + 300
        return _dashboard(request, reboot_armed=True, reboot_nonce=n)
    if _REBOOT_NONCES.pop(nonce, None) is None:
        # replayed/expired confirmation — NEVER reboot from a stale form
        return _dashboard(request,
                          notice="That reboot confirmation was already "
                                 "used or expired — nothing happened. "
                                 "Start again if you mean it.")
    if confirm.strip().lower() != "reboot":
        return _dashboard(request,
                          notice="Not rebooted — type “reboot” to confirm.")
    if _passkey_count():
        # last gate before the root watcher: a live assertion, redeemed
        # here so it cannot be replayed onto anything else
        admin = tenancy.admin_connect()
        try:
            ok = _stepup_ok(sess, admin, stepup)
            if not ok:
                _audit(admin, "host_reboot_refused",
                       detail="no fresh passkey assertion",
                       ip=client_ip(request))
        finally:
            admin.close()
        if not ok:
            return _dashboard(request, notice=(
                "Not rebooted — a reboot needs a fresh passkey "
                "confirmation. Start again and confirm with your passkey "
                "when asked."))
    from pathlib import Path
    cmd = Path(os.environ.get("OIKONOME_STATE_DIR", "/state")) / "cmd"
    if not cmd.is_dir() or not os.access(cmd, os.W_OK):
        return _dashboard(request,
                          notice="Not configured — the host reboot watcher "
                                 "isn't installed (see scripts/"
                                 "install-host-reboot-watcher.sh).")
    admin = tenancy.admin_connect()
    try:
        # 60-second grace: signed-in users get a live countdown (the SPA
        # reads the deadline), then their open tab flips to the
        # reconnecting overlay and reloads itself when the box is back
        admin.execute(
            """INSERT INTO broadcast (id, message, severity, set_at, deadline)
               VALUES (1, 'Maintenance restart in about a minute — your '
                          'session reconnects automatically.', 'warn',
                       now(), now() + interval '60 seconds')
               ON CONFLICT (id) DO UPDATE SET message = EXCLUDED.message,
                 severity = 'warn', set_at = now(),
                 deadline = now() + interval '60 seconds'""")
        _audit(admin, "host_reboot", ip=client_ip(request))
    finally:
        admin.close()
    (cmd / "reboot-requested").write_text(
        dt.datetime.now(dt.timezone.utc).isoformat())
    # PRG: redirect so refresh/tab-restore replays a harmless GET, never
    # the action itself — a replayed POST is enough to reboot the host
    return RedirectResponse("/admin/console?notice=rebooting",
                            status_code=303)


# ---- host-agent command runner (oikonome.sh parity) ---------------------
# Allowlist of oikonome.sh ops the console may run via the watcher. Anything
# touching data/the stack goes through the root watcher, which re-validates.
_CMD_ALLOW = ("backup", "status", "logs", "upgrade",
              "restore", "reset", "uninstall")
# Everything that can take the stack down or replace its data. `upgrade`
# belongs here because it rebuilds and restarts the whole stack from
# whatever the checkout holds — one POST from a stolen 4-hour cookie must
# not be able to do that any more than it can restore or reset.
_CMD_DESTRUCTIVE = {"restore", "reset", "uninstall", "upgrade"}
_CMD_LABELS = {"backup": "Back up now", "status": "Status",
               "logs": "Recent logs", "upgrade": "Rebuild / restart",
               "restore": "Restore a backup", "reset": "Reset (wipe data)",
               "uninstall": "Uninstall"}
# nonce -> (command it was armed for, expiry). The command is half the key:
# a nonce that redeems for ANY destructive op means the typed confirmation
# locks in nothing — arming "reset" and posting stage=go/command=uninstall
# with the same nonce would run the uninstall. On a passkey-less self-host this
# arm/confirm pair is the ONLY server-side friction before the root
# host-agent, so it has to confirm exactly the operation that was armed.
_CMD_NONCES: dict[str, tuple[str, float]] = {}


def _cmd_dir_writable() -> bool:
    from pathlib import Path
    cmd = Path(os.environ.get("OIKONOME_STATE_DIR", "/state")) / "cmd"
    return cmd.is_dir() and os.access(cmd, os.W_OK)


def _command_results(limit_n: int = 8) -> list[dict]:
    """Recent host-command results the watcher wrote to ops-state/cmd-results
    (read through the read-only /state/ops bind)."""
    import json as _json
    from pathlib import Path
    base = (Path(os.environ.get("OIKONOME_STATE_DIR", "/state"))
            / "ops" / "cmd-results")
    out: list[dict] = []
    if base.is_dir():
        for j in sorted(base.glob("*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)[:limit_n]:
            try:
                d = _json.loads(j.read_text())
            except Exception:                                   # noqa: BLE001
                continue
            log = base / f"{d.get('id', '')}.log"
            # the `logs` command captures the app's stdout, which carries
            # the access log — and with it every one-time link a request
            # line ever held (reset, invite, export ticket). The panel is
            # read by any operator session; scrub it the way the feedback
            # bundle is scrubbed.
            from .feedback import _redact
            # redact BEFORE taking the tail: a cut that lands inside a
            # link leaves a bare token with no prefix for the patterns
            # to recognise
            d["log"] = ("\n".join(_redact(ln) for ln in
                                  log.read_text().splitlines())[-6000:]
                        if log.is_file() else "")
            out.append(d)
    return out


@router.post("/run-command",
             dependencies=[Depends(limit("admin_action", 20, 3600))])
def run_command(request: Request, command: str = Form(...), arg: str = Form(""),
                stage: str = Form(""), nonce: str = Form(""),
                confirm: str = Form(""), stepup: str = Form("")):
    """Run an oikonome.sh operation on the host via the command watcher
    (scripts/install-host-command-watcher.sh). Non-destructive ops fire on
    one click; destructive ones (restore/reset/uninstall/upgrade) ARM first,
    then need a single-use nonce + a typed confirm — the browser half of the
    root watcher's own server-side validation (allowlist, owner, freshness).

    The destructive ones also need a FRESH passkey assertion (minted seconds
    earlier by /passkey/stepup, single-use, bound to this session), so a
    stolen 4-hour cookie cannot reach restore/reset/uninstall/upgrade on its
    own. Skipped only when no operator key is enrolled — a plain-http
    self-host cannot enrol one, and refusing there would take the console's
    own recovery tools away."""
    if (sess := _session(request)) is None:
        return RedirectResponse("/admin/console", status_code=303)
    import time as _time
    command = (command or "").strip().lower()
    if command not in _CMD_ALLOW:
        return _dashboard(request, notice="Unknown operation.")
    if not _cmd_dir_writable():
        return _dashboard(request, notice="Not configured — the host command "
                          "watcher isn't installed (run scripts/install-host-"
                          "command-watcher.sh once as root).")
    for k in [k for k, (_c, exp) in _CMD_NONCES.items()
              if exp < _time.time()]:
        _CMD_NONCES.pop(k, None)
    if command in _CMD_DESTRUCTIVE:
        if stage != "go":
            n = secrets.token_urlsafe(16)
            _CMD_NONCES[n] = (command, _time.time() + 300)
            return _dashboard(request, cmd_armed=command, cmd_nonce=n)
        armed = _CMD_NONCES.pop(nonce, None)
        if armed is None:
            return _dashboard(request, notice="That confirmation expired or "
                              "was already used — nothing happened.")
        if armed[0] != command:
            # burnt above either way: a nonce that arrived attached to the
            # wrong operation is not one to hand back for a second try
            admin = tenancy.admin_connect()
            try:
                _audit(admin, "run_command_refused", target=command,
                       detail=f"confirmation was armed for {armed[0]}",
                       ip=client_ip(request))
            finally:
                admin.close()
            return _dashboard(request, notice=(
                f"Not run — that confirmation was for "
                f"“{_CMD_LABELS.get(armed[0], armed[0])}”, not "
                f"“{_CMD_LABELS.get(command, command)}”. Start again."))
        if confirm.strip().lower() != command:
            return _dashboard(request,
                              notice=f"Not run — type “{command}” to confirm.")
        if command == "restore" and not arg.strip():
            return _dashboard(request, notice="Pick a backup to restore.")
        # last gate before the root host-agent: a live assertion, redeemed
        # here so it cannot be replayed onto a second command
        if _passkey_count():
            admin = tenancy.admin_connect()
            try:
                ok = _stepup_ok(sess, admin, stepup)
                if not ok:
                    _audit(admin, "run_command_refused", target=command,
                           detail="no fresh passkey assertion",
                           ip=client_ip(request))
            finally:
                admin.close()
            if not ok:
                return _dashboard(request, notice=(
                    f"Not run — {command} needs a fresh passkey "
                    "confirmation. Start again and confirm with your "
                    "passkey when asked."))
    import json as _json
    from pathlib import Path
    cmd_dir = Path(os.environ.get("OIKONOME_STATE_DIR", "/state")) / "cmd"
    flag = cmd_dir / "command-requested"
    # Only ONE request flag exists at a time (fixed filename the systemd
    # PathExists unit watches). If a prior request hasn't been consumed yet,
    # a second os.replace would overwrite it while the file continuously
    # exists — no new PathExists edge fires, so the first op is silently lost
    # while the UI reports both queued. Refuse instead: the operator retries
    # once the pending op clears (the watcher removes the flag as it runs).
    # NOTE: no exists() pre-check. A check-then-act lets two operators (or
    # one double-submit) both pass and the second write silently overwrite
    # the first request, so a command the console said was queued would
    # never run. os.link below IS the check: it fails if
    # the flag is already there, atomically.
    cid = secrets.token_hex(8)
    payload = {"id": cid, "cmd": command, "arg": arg.strip(),
               "at": dt.datetime.now(dt.timezone.utc).isoformat()}
    admin = tenancy.admin_connect()
    try:
        _audit(admin, "run_command", target=command,
               detail=(arg.strip() or None), ip=client_ip(request))
    finally:
        admin.close()
    # Write atomically: the host watcher is a systemd PathExists unit that
    # fires the instant the file appears. write_text() creates it empty
    # (O_CREAT) before the content lands, so a bare write races the watcher
    # into cat'ing a partial/empty request → JSON parse fails → the command
    # is silently dropped while the UI reports it queued. tmp + os.replace
    # makes the request appear only once, fully formed.
    tmp = cmd_dir / f".command-requested.{cid}.tmp"
    tmp.write_text(_json.dumps(payload))
    try:
        # link() is atomic and REFUSES when the target exists, so the
        # first writer wins and the second is told so instead of
        # clobbering it. The file still appears only once, fully formed.
        os.link(tmp, flag)
    except FileExistsError:
        age = ""
        try:
            secs = int(time.time() - flag.stat().st_mtime)
            age = (f" It has been sitting for {secs // 60} min"
                   if secs >= 120 else "")
            if secs > 3600:
                # the watcher is meant to consume this within seconds; an
                # hour means it is not running, and every command from here
                # on would be refused with no way to tell why
                age += (" — that is far longer than the watcher takes, so "
                        "it is probably not running. Check the host-agent "
                        "unit.")
            elif age:
                age += "."
        except OSError:
            pass
        return _dashboard(request, notice=(
            "An operation is already queued — wait for it to finish "
            "(refresh Host operations), then try again." + age))
    finally:
        tmp.unlink(missing_ok=True)
    return RedirectResponse("/admin/console?notice=command", status_code=303)


@router.post("/create-instance",
             dependencies=[Depends(limit("admin_action", 30, 3600))])
async def create_instance(request: Request, email: str = Form(...)):
    """The operator provisions a new instance (tenant + owner) directly,
    without waiting for a self-signup. Returns a one-time SET-PASSWORD link
    (password_resets discipline) — the operator never handles a plaintext
    password. The owner is created verified (the operator vouches for the
    address). Complements tenant-delete for the create/delete pair.

    Any extra form field is handed to the installed gate, which decides
    what the account is born with."""
    form = await request.form()
    extra = {k: str(v) for k, v in form.multi_items() if k != "email"}
    if (bounce := _require(request)) is not None:
        return bounce
    if not _multi_tenant():
        # a stale form or a curl must not mint what /signup will refuse
        return _dashboard(request, notice=_SINGLE_HOUSEHOLD_NOTICE)

    from ..auth import passwords, reset as reset_mod
    email = email.strip().lower()
    if "@" not in email or len(email) > 254:
        return _dashboard(request, notice="That doesn't look like an email.")
    admin = tenancy.admin_connect()
    try:
        if admin.execute("SELECT 1 FROM users WHERE email=%s",
                         (email,)).fetchone():
            return _dashboard(request, notice=f"{email} already has an "
                                              "account.")
        tid = tenancy.create_tenant(admin, email)
        # unusable random password_hash → the owner MUST set one via the
        # returned link; verified (operator-provisioned)
        import secrets as _secrets
        uid = admin.execute(
            """INSERT INTO users (tenant_id, email, password_hash, verified_at)
               VALUES (%s, %s, %s, now()) RETURNING id""",
            (tid, email,
             passwords.hash_password(_secrets.token_urlsafe(32)))).fetchone()["id"]
        # WELCOME mechanics, not reset — a fresh user gets 7 days, and
        # the email says "your account is ready", not "someone requested a
        # password reset", which reads like phishing to someone who was
        # expecting an invitation
        token = reset_mod.create_reset(admin, uid, ttl=reset_mod.WELCOME_TTL)
        # what the account is born with is the installed gate's business:
        # it reads the operator's choices off this form and writes
        # whatever standing it dictates, exactly as it does for a
        # self-signup
        options = ext.gate.invite_options(extra, operator_created=True)
        ext.gate.provision(admin, tid, invite={"options": options},
                           ref=None, verified=True, reclaim=False)
        _audit(admin, "create_instance",
               target=email + "".join(f" ({k}:{v})"
                                      for k, v in options.items()),
               ip=client_ip(request))
    finally:
        admin.close()
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    link = f"{base}/reset?token={token}" if base else f"/reset?token={token}"
    # deliver the welcome link by email too (best-effort), like invites
    from .app import _deliver_welcome
    threading.Thread(target=_deliver_welcome, args=(email, token),
                     daemon=True).start()
    tail = ext.gate.invite_summary(options)
    return _dashboard(request, minted_link=link,
                      notice=f"Instance created for {email}{tail} — send them "
                             "the set-password link (also emailed).")
