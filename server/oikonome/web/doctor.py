"""Doctor — the self-diagnosis surface: the goal is that most would-be
support issues are answered here before they're filed.

`checks` returns a list of {section, name, ok, severity, detail} rows;
the SPA Doctor page groups them by section and auto-refreshes, /api/doctor
serves them raw, and the diagnostic bundle includes the output. Data-side
evidence over introspection: a dead worker shows up as a stale heartbeat,
not a green systemd status. The rows are full instance telemetry rather
than a flat checklist — system resources, per-connection last-sync, LLM
health, categorization stats.
"""

from __future__ import annotations

import datetime as dt
import os
from ..envnum import env_flag
import shutil

from ..db import crypto, tenancy


def _row(section: str, name: str, ok: bool, detail: str,
         severity: str = "bad", progress: float | None = None,
         busy: bool = False) -> dict:
    out = {"section": section, "name": name, "ok": ok,
           "severity": "ok" if ok else severity, "detail": detail}
    if progress is not None:
        out["progress"] = round(max(0.0, min(100.0, progress)), 1)
    if busy:
        out["busy"] = True
    return out


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _ago(t, now) -> str:
    h = (now - t).total_seconds() / 3600
    if h < 1:
        return f"{h * 60:.0f}m ago"
    if h < 48:
        return f"{h:.1f}h ago"
    return f"{h / 24:.0f}d ago"


def _system_rows(conn) -> list[dict]:
    import platform
    out = []
    ver = os.environ.get("OIKONOME_VERSION") or "dev"
    out.append(_row("System", "version", True,
                    f"{ver} · python {platform.python_version()}"))
    # On the hosted platform the database, disk and memory are SHARED
    # infrastructure — their sizes are fleet telemetry, not this tenant's
    # health, and any signed-in user could read (and feedback-bundle) them.
    # Self-host keeps the rows: there the host IS the operator's own box.
    if env_flag("OIKONOME_HOSTED"):
        return out
    try:
        size = conn.execute(
            "SELECT pg_database_size(current_database()) AS s"
        ).fetchone()["s"]
        out.append(_row("System", "database size", True, _fmt_bytes(size)))
    except Exception as e:                        # noqa: BLE001
        out.append(_row("System", "database size", True,
                        f"unavailable ({type(e).__name__})"))
    try:
        du = shutil.disk_usage("/")
        frac = du.free / du.total
        out.append(_row("System", "disk (app container)", frac > 0.05,
                        f"{_fmt_bytes(du.free)} free of "
                        f"{_fmt_bytes(du.total)}", "warn"))
    except OSError:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                mem[k] = int(v.strip().split()[0]) * 1024
        avail, total = mem.get("MemAvailable", 0), mem.get("MemTotal", 0)
        if total:
            out.append(_row("System", "memory (host)", avail / total > 0.05,
                            f"{_fmt_bytes(avail)} available of "
                            f"{_fmt_bytes(total)}", "warn"))
    except OSError:
        pass
    return out


def _service_rows(conn, now, demo: bool = False) -> list[dict]:
    """One row per stack service, probed from inside the app container —
    the closest an in-app page can get to `podman ps` (engine-level detail
    stays with `./oikonome.sh status`)."""
    out = [_row("Services", "app", True,
                f"up · version {os.environ.get('OIKONOME_VERSION') or 'dev'}")]
    try:
        t0 = dt.datetime.now()
        conn.execute("SELECT 1")
        ms = (dt.datetime.now() - t0).total_seconds() * 1000
        out.append(_row("Services", "postgres", True,
                        f"responding · {ms:.0f} ms"))
    except Exception as e:                        # noqa: BLE001
        out.append(_row("Services", "postgres", False,
                        f"query failed ({type(e).__name__})"))
    try:
        import redis as _redis
        _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                        socket_connect_timeout=1,
                        socket_timeout=1).ping()
        out.append(_row("Services", "redis (job queue)", True, "responding"))
    except Exception as e:                        # noqa: BLE001
        out.append(_row("Services", "redis (job queue)", False,
                        f"unreachable ({type(e).__name__}) — background "
                        "sync/detection/email cannot run"))
    # the export keeps its findings in job_runs too, but a download is a
    # request, not the worker: counting it would call a dead worker alive
    from .pages import EXPORT_LAST_JOB
    last = conn.execute(
        "SELECT MAX(ran_at) AS t FROM job_runs WHERE job <> %s",
        (EXPORT_LAST_JOB,)).fetchone()["t"]
    if last is None:
        if demo:
            # a fresh demo has no connections to sync — nothing has had a
            # reason to run yet, and a warning here reads as breakage
            out.append(_row("Services", "worker", True,
                            "no jobs yet (fresh demo) — the nightly run "
                            "starts tonight"))
        else:
            out.append(_row("Services", "worker", False,
                            "no job has ever run — check the worker "
                            "container", "warn"))
    else:
        h = (now - last).total_seconds() / 3600
        out.append(_row("Services", "worker", h <= 26,
                        f"last job {_ago(last, now)}"
                        + ("" if h <= 26 else
                           " — expected at least daily; check the worker "
                           "container"), "warn"))
    return out


def _ollama_target(conn) -> dict | None:
    """The Ollama server the operator's backend points at, when it does —
    read here, under the request's connection, and probed by
    `_ollama_row` after that connection is handed back. The configured
    address is the one probed (an Ollama elsewhere than the bundled
    sidecar is just as real). A household's own endpoint is not probed
    here: the Smart categorization rows probe it through the outbound
    guard."""
    from ..engine import llm_categorize as _llm
    be = _llm._backend(conn)
    url = (be.get("url") or "").rstrip("/")
    if be.get("source") == "tenant" or "ollama" not in url:
        return None
    if url.endswith("/v1"):
        url = url[:-3]
    return {"url": f"{url}/api/version",
            "hosted": env_flag("OIKONOME_HOSTED")}


def _ollama_row(target: dict) -> dict:
    try:
        ver = _probe_json(target["url"]).get("version", "?")
        # on hosted this is the operator's service: up or down is the
        # household's business, its version is not
        return _row("Services", "ollama (LLM)", True,
                    "up" if target["hosted"] else f"up · v{ver}")
    except Exception as e:                        # noqa: BLE001
        return _row("Services", "ollama (LLM)", False,
                    f"unreachable ({type(e).__name__}) — smart "
                    "categorization and receipt parsing are paused", "warn")


# A probe is a GET answered by a server someone else may run, made while a
# person waits on the page: bounded in time as a whole (a server can answer
# one byte every couple of seconds and never trip a per-read timeout) and
# in size, measured after decompression. The deadline sits under every
# socket operation, so it holds through the connect and a status line and
# headers sent a byte at a time, not only through the body.
PROBE_DEADLINE = 5.0
PROBE_READ_TIMEOUT = 2.0
PROBE_MAX_BYTES = 64 * 1024


def _probe_json(url: str, transport=None, headers: dict | None = None):
    import json
    import time

    import httpx

    from ..notify.webhooks import _with_deadline
    end = time.monotonic() + PROBE_DEADLINE
    with httpx.Client(transport=transport, headers=headers or {},
                      timeout=PROBE_READ_TIMEOUT) as c:
        # a transport with no connection pool (a test's) opens no socket
        if hasattr(c._transport, "_pool"):
            _with_deadline(c, end)
        # redirect-free: the vetted address is the only one we talk to
        with c.stream("GET", url, follow_redirects=False) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > PROBE_MAX_BYTES:
                    raise ValueError("reply too large")
                if time.monotonic() > end:
                    raise TimeoutError("probe ran past its deadline")
    return json.loads(bytes(buf))


def _connection_rows(conn, now, public: bool = False) -> list[dict]:
    """One row per live connection. `public` (the diagnostic bundle) names
    each by its aggregator and an ordinal instead of the institution, and
    gives file-fed sources as a count: which banks a household uses, and
    the labels it typed for its imports, are its own business."""
    out = []
    rows = conn.execute(
        """SELECT i.id, i.aggregator, i.institution_name, i.status,
                  MAX(s.ran_at) FILTER (WHERE s.error IS NULL) AS last_ok
           FROM items i LEFT JOIN sync_log s ON s.item_id = i.id
           WHERE COALESCE(i.status,'') != 'archived'
           GROUP BY i.id, i.aggregator, i.institution_name, i.status
           ORDER BY i.aggregator, i.institution_name""").fetchall()
    label = {"plaid": "Plaid", "mx": "MX", "coinbase": "Coinbase",
             "simplefin": "SimpleFIN", "simplefin-org": "SimpleFIN"}
    # File-fed items (one-time historical imports, collector pushes) never
    # "sync" — judging them as stale connections would warn forever about
    # e.g. a one-time historical statement import. They collapse into
    # one informational row; script freshness lives under Jobs (heartbeats).
    file_items = [it for it in rows
                  if it["aggregator"] in ("csv", "manual")]
    rows = [it for it in rows if it["aggregator"] not in ("csv", "manual")]
    if file_items:
        names = sorted({it["institution_name"] or it["id"]
                        for it in file_items})
        out.append(_row(
            "Connections", "file-fed sources", True,
            f"{len(file_items)} import-fed (no live sync — that's normal)"
            + ("" if public else ": " + ", ".join(names[:6])
               + ("…" if len(names) > 6 else ""))))
    seen: dict[str, int] = {}
    for it in rows:
        if it["aggregator"] == "simplefin":
            # the bridge holds the token; its per-bank children below carry
            # the user-meaningful state — show the bridge only on error
            if (it["status"] or "ok") == "ok":
                continue
        status_ok = (it["status"] or "ok") == "ok"
        if it["last_ok"] is None:
            fresh, when = False, "never synced"
        else:
            fresh = (now - it["last_ok"]).total_seconds() < 26 * 3600
            when = f"last sync {_ago(it['last_ok'], now)}"
        agg = label.get(it["aggregator"], it["aggregator"])
        if public:
            seen[agg] = seen.get(agg, 0) + 1
            name = f"{agg} connection {seen[agg]}"
        else:
            name = f"{it['institution_name'] or it['id']} ({agg})"
        out.append(_row(
            "Connections", name, status_ok and fresh,
            when if status_ok else f"{it['status']} · {when}", "warn"))
    if not rows:
        out.append(_row("Connections", "connections", True,
                        "no aggregator connected — CSV/OFX import mode"))
    return out


# The categorization backlog is a full-ledger GROUP BY answered only to print
# a count on this page; it is remembered per tenant for a few minutes so
# reloading Doctor does not rescan the ledger. Counts only — never rows.
_BACKLOG_TTL = 300.0
_backlog_cache: dict[str, tuple[float, int]] = {}


def _pending_backlog(conn) -> int:
    import time
    from ..engine import llm_categorize as llm
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]
    now = time.monotonic()
    hit = _backlog_cache.get(tid)
    if hit is not None and hit[0] > now:
        return hit[1]
    n = len(llm.pending_merchants(conn))
    _backlog_cache[tid] = (now + _BACKLOG_TTL, n)
    return n


def _llm_rows(conn, public: bool = False) -> list[dict]:
    """Every Smart categorization row, on one connection. checks() runs the
    same three parts but probes after handing its connection back."""
    plan = _llm_plan(conn)
    return (_llm_probe_rows(plan, public)
            + _llm_tail_rows(conn, bool(plan), public))


def _llm_plan(conn) -> list[dict]:
    """What to probe, read from settings — no network here."""
    from ..engine import llm_categorize as llm
    # one health pair per DISTINCT routed backend across ALL roles —
    # probing only the categorize role's backend would show all green while
    # a dead vision backend fails every receipt/tax-document upload. Rows
    # are labeled with the role(s) a backend serves, unless a single
    # backend serves everything (the common case keeps the plain names).
    probes: dict[str, dict] = {}
    for role in llm.ROLES:
        be = llm._backend(conn, role=role)
        if not be["url"]:
            continue
        base = llm.base_url(be["url"])
        p = probes.setdefault(base, {"be": be, "roles": [], "models": set()})
        p["roles"].append(role)
        model = (be.get("vision_model") or be["model"]
                 if role == "vision" else be["model"])
        if model:
            p["models"].add(model)
    return [{"base": base, "suffix": (f" ({', '.join(p['roles'])})"
                                      if len(probes) > 1 else ""), **p}
            for base, p in probes.items()]


def _llm_probe_rows(plan: list[dict], public: bool = False) -> list[dict]:
    """The endpoint and model rows: one probe per distinct backend, made
    with no database connection held — the probed server decides how long
    it takes to answer."""
    from ..engine import llm_categorize as llm
    out = []
    if not plan:
        out.append(_row("Smart categorization", "LLM", True,
                        "not configured — brand rules + your corrections "
                        "still categorize; the ambiguous tail stays manual"))
    for p in plan:
        base, be, suffix = p["base"], p["be"], p["suffix"]
        models = sorted(p["models"])
        # On hosted the env backend is the OPERATOR's infrastructure —
        # Settings withholds its URL and model, and this page (read by
        # every member, carried in every feedback ZIP) must not hand them
        # back, nor the model list its /v1/models answers with.
        operators = (be.get("source") != "tenant"
                     and env_flag("OIKONOME_HOSTED"))
        # the bundle (meant for a public issue) names no address: a LAN
        # host or tailnet name is a map of the household's network
        where = ("address withheld from the bundle" if public
                 else llm.redact_url(base))
        out.append(_row(
            "Smart categorization", f"endpoint{suffix}", True,
            "provided by the host" if operators else
            f"{where} · model {' / '.join(models) or '(unset)'}"))
        # same use-time gate as _chat: a tenant-sourced
        # URL is re-checked before we fetch it — the probe must not be an
        # owner-triggered SSRF for rows that predate the save-time guard.
        # The operator's env/bundled backend is deliberately exempt.
        blocked = None
        pin = None
        if be.get("source") == "tenant":
            from .netguard import BlockedURL, check_url, pinned_transport
            try:
                check_url(base, what="the configured LLM endpoint")
            except BlockedURL as e:
                blocked = e
            # probe through the same DNS-pinning transport the
            # real fetch uses (hosted only; None on self-host)
            pin = pinned_transport()
        if blocked:
            out.append(_row("Smart categorization", f"model{suffix}", False,
                            "endpoint not probed — the outbound guard "
                            "refuses its address" if public else
                            f"endpoint not probed — {blocked}", "warn"))
            continue
        try:
            # bounded in time and size and redirect-free: a tenant's
            # endpoint decides what it answers and how slowly, and anyone
            # who can open this page (or build a feedback bundle) triggers
            # the read
            # with the backend's own credentials: a cloud provider lists
            # its models only to a key, and Anthropic's endpoint wants
            # the native API's version header too
            listing = _probe_json(f"{base}/v1/models", transport=pin,
                                  headers=llm.request_headers(be, base))
            # the endpoint's own answer, bounded before it is rendered:
            # this row is read by every member and rides in every feedback
            # ZIP, and an id that is null (join crashes) or a kilobyte long
            # is the backend's choice, not ours
            data = (listing.get("data") if isinstance(listing, dict)
                    else None)
            ids = [str(m.get("id"))[:60] for m in (data or [])
                   if isinstance(m, dict) and m.get("id")]
            missing = [m for m in models if m not in ids] if ids else []
            if missing and operators:
                out.append(_row(
                    "Smart categorization", f"model{suffix}", False,
                    "the host's AI endpoint is up but its model is not "
                    "loaded", "warn"))
            elif missing:
                out.append(_row(
                    "Smart categorization", f"model{suffix}", False,
                    f"endpoint is up but model "
                    f"'{', '.join(missing)}' is not "
                    f"loaded there (has: {', '.join(ids[:5]) or 'none'})",
                    "warn"))
            else:
                out.append(_row("Smart categorization", f"model{suffix}",
                                True, "endpoint reachable · model available"))
        except Exception as e:                    # noqa: BLE001
            out.append(_row(
                "Smart categorization", f"model{suffix}", False,
                f"endpoint unreachable ({type(e).__name__}) — work routed "
                f"to it ({', '.join(p['roles'])}) is paused; it retries "
                "on every sync", "warn"))
    return out


def _llm_tail_rows(conn, configured: bool,
                   public: bool = False) -> list[dict]:
    """The rows read from this household's own data: the last run, the
    merchant map, the receipt queue and a running sync's progress."""
    from ..engine import llm_categorize as llm
    out = []
    # what the last nightly run actually did — the probe says the backend
    # is reachable NOW; this says whether the run that matters produced
    # anything, and names the failure when it did not (the bundle keeps
    # the failure's words out: they can carry the endpoint's address)
    if configured:
        last = llm.last_run(conn)
        if last and last.get("state") == "error":
            out.append(_row(
                "Smart categorization", "last run", False,
                f"failed {_ago(last['at'], dt.datetime.now(dt.timezone.utc))}"
                + ("" if public else f" — {last.get('error')}")
                + " · merchant categories and Amazon "
                "summaries are stalled until it is fixed", "warn"))
        elif last:
            out.append(_row(
                "Smart categorization", "last run", True,
                f"{_ago(last['at'], dt.datetime.now(dt.timezone.utc))} · "
                f"{last.get('produced', 0)} answers"))
    by_src = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM merchant_categories "
        "GROUP BY source").fetchall()}
    backlog = _pending_backlog(conn)
    parts = [f"{by_src.get('seed', 0)} by brand rules",
             f"{by_src.get('llm', 0)} by the model",
             f"{by_src.get('user', 0)} by your corrections"]
    out.append(_row(
        "Smart categorization", "merchant map",
        True, " · ".join(parts)
        + (f" · {backlog} merchants awaiting categorization"
           if backlog else " · no backlog")))
    # the pending LLM work, visible — receipts in flight/queued
    # and the live categorize counters of a running sync
    rc = {r["status"]: r for r in conn.execute(
        """SELECT status, COUNT(*) AS n, MIN(parsed_at) AS oldest,
                  COUNT(*) FILTER (WHERE txn_id IS NULL) AS waiting
           FROM receipts WHERE status IN ('parsing','uploaded','failed')
           GROUP BY status""").fetchall()}
    now = dt.datetime.now(dt.timezone.utc)
    if rc:
        bits, level_ok, busy = [], True, False
        if "parsing" in rc:
            n = rc["parsing"]["n"]
            oldest = rc["parsing"]["oldest"]
            age = f" (started {_ago(oldest, now)})" if oldest else ""
            bits.append(f"{n} parsing now{age}")
            busy = True
        if "uploaded" in rc:
            bits.append(f"{rc['uploaded']['n']} stored, awaiting the "
                        "nightly sweep (or press parse now)")
        if "failed" in rc:
            # a receipt still waiting for its charge has no transaction,
            # so no receipt panel to retry from: it is retried (or its
            # total typed in) on the Receipts page's waiting list instead
            waiting = rc["failed"]["waiting"] or 0
            attached = rc["failed"]["n"] - waiting
            if attached:
                bits.append(f"{attached} failed — retry from the "
                            "transaction's receipt panel")
            if waiting:
                bits.append(f"{waiting} failed while waiting for its "
                            "charge — retry or type the total in under "
                            "Receipts → Waiting receipts")
            level_ok = False
        out.append(_row("Smart categorization", "receipt queue", level_ok,
                        " · ".join(bits), "warn", busy=busy))
    else:
        out.append(_row("Smart categorization", "receipt queue", True,
                        "empty — receipts parse on upload"))
    jp = conn.execute(
        "SELECT state, progress FROM job_progress WHERE id='sync'"
    ).fetchone()
    if jp and jp["state"] == "running":
        from ..engine.compat import as_dict
        prog = as_dict(jp["progress"]) or {}
        cat = prog.get("categorize") or {}
        done, total = cat.get("done"), cat.get("total")
        if total:
            out.append(_row(
                "Smart categorization", "live categorize", True,
                f"sync running — {done}/{total} merchants classified",
                progress=100.0 * (done or 0) / total, busy=True))
        else:
            out.append(_row("Smart categorization", "live sync", True,
                            "sync running — categorization follows",
                            busy=True))
    return out


def checks(tenant_id: str, public: bool = False) -> list[dict]:
    """Every row. `public` is the diagnostic bundle's view, which is meant
    to be attached to a public issue: the rows that would name a host (the
    base URL, the mail relay, an AI endpoint), a bank, or a label the
    household typed carry only their shape. The signed-in page shows them
    in full.

    The probes of servers outside the database (an AI endpoint, an Ollama
    service) run after the tenant connection is handed back: the probed
    server decides how long it takes, and a pooled connection held for
    that long is one no other household can use."""
    out: list[dict] = []
    deferred: list[tuple[int, object]] = []
    try:
        conn = tenancy.tenant_connect(tenant_id)
    except Exception as e:                        # noqa: BLE001
        return [_row("System", "database", False,
                     f"cannot connect: {type(e).__name__}")]
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
        out.append(_row("System", "database", True,
                        f"connected · {n} migrations applied"))
    except Exception as e:                        # noqa: BLE001
        # The connection came from the pool: a failed first query must
        # still hand it back, or each failing health check strands one
        # slot until the pool is exhausted.
        conn.close()
        return [_row("System", "database", False,
                     f"cannot connect: {type(e).__name__}")]

    try:
        now = dt.datetime.now(dt.timezone.utc)
        from ..engine import budget as _budget
        _cfg = _budget.load_config(conn)
        # a demo instance has no external doors by DESIGN — say
        # so plainly instead of letting the absence read as a problem
        demo = bool(_cfg.get("demo_mode"))
        out += _system_rows(conn)
        out += _service_rows(conn, now, demo=demo)
        _oll = _ollama_target(conn)
        if _oll:
            deferred.append((len(out), lambda: [_ollama_row(_oll)]))
        if demo:
            out.append(_row("Connections", "connections", True,
                            "external data connections disabled — demo "
                            "instance (synthetic data)"))
        else:
            out += _connection_rows(conn, now, public)

        # worker heartbeats (job_runs is RLS-scoped — this tenant's jobs)
        runs = conn.execute("SELECT job, ran_at, note FROM job_runs").fetchall()
        hb = {r["job"]: r["ran_at"] for r in runs}
        hb_note = {r["job"]: r["note"] for r in runs}

        def age_ok(job, hours):
            t = hb.get(job)
            if t is None:
                return False, "never ran"
            h = (now - t).total_seconds() / 3600
            return h <= hours, f"last ran {_ago(t, now)}"

        has_items = conn.execute(
            "SELECT 1 FROM items WHERE aggregator IN "
            "('simplefin','plaid','mx','coinbase') "
            "AND COALESCE(status,'')!='archived' LIMIT 1").fetchone()
        if has_items:
            # the same threshold the alert strip judges a connection by, so
            # a poll interval the operator stretched (OIKONOME_SYNC_BUCKETS)
            # does not read as a failing worker here while Today says the
            # connection is fine
            from ..engine import alerts as _alerts
            limit = _alerts.sync_max_hours()
            ok, d = age_ok("sync", limit)
            out.append(_row("Jobs", "bank sync (worker)", ok,
                            d + f" · expected within {limit:g} h; check the "
                                "worker container and aggregator status",
                            "warn"))
        ok, d = age_ok("nightly-detect", 50)
        if demo and d == "never ran":
            # a just-seeded demo hasn't been through a night yet
            ok, d = True, "not yet run (fresh demo)"
        out.append(_row("Jobs", "nightly detection (worker)", ok,
                        d + " · expected daily at 04:00 UTC", "warn"))
        _sched = _cfg.get("email_schedule")
        # a restore can plant a non-dict under "daily" (settings are
        # validated on the API write path, not on restore), so .get on it
        # would fail this whole page; a malformed entry reads as
        # configured-on so the health row still shows
        _daily = _sched.get("daily") if isinstance(_sched, dict) else None
        daily_on = (not isinstance(_sched, dict)
                    or (bool(_daily.get("on")) if isinstance(_daily, dict)
                        else bool(_daily)))
        if demo:
            out.append(_row("Jobs", "daily email", True,
                            "email sending disabled — demo instance"))
        elif daily_on:
            from ..jobs import worker as _worker
            from ..notify import email_prefs
            ok, d = age_ok("daily-email", 50)
            note = hb_note.get("daily-email")
            if ok and note == email_prefs.NOBODY_OPTED_IN:
                # the worker ran and decided: everyone unticked the daily
                out.append(_row("Jobs", "daily email (worker)", True,
                                d + " · nobody is sent it: every recipient "
                                "turned the daily email off (Settings → "
                                "Email → Who gets which email)"))
            elif ok and note == _worker.NO_RECIPIENTS:
                # the worker ran and found no confirmed, unmuted address:
                # a decision about who to mail, not a relay failure
                out.append(_row("Jobs", "daily email (worker)", True,
                                d + " · nobody is sent it: there is no "
                                "confirmed, unmuted address to mail "
                                "(Settings → Email)"))
            elif ok and note == _worker.NO_BUDGET:
                # the daily verdict judges a budget; with none set there is
                # nothing to judge, so the worker stamps the day and sends
                # nothing — pointing at SMTP would send the reader the
                # wrong way
                out.append(_row("Jobs", "daily email (worker)", True,
                                d + " · not sent: no budget is set yet, so "
                                "there is no daily verdict to mail "
                                "(set one on the Budget page)"))
            else:
                out.append(_row("Jobs", "daily email (worker)", ok,
                                d + " · expected daily; check SMTP settings",
                                "warn"))

        # script push-heartbeats — watched sources only;
        # unwatched rows stay panel-only so one-time imports never warn
        from ..sync import heartbeat
        # say HOW each collector pushes, so a glance shows which
        # are on script tokens vs a container-exec bridge vs in-app
        _via = {"token": "script token (HTTP)", "exec": "container exec",
                "app": "in-app upload"}
        revoked = heartbeat.revoked_tokens(conn)
        n_collector = 0
        for s in heartbeat.panel(conn):
            if s["status"] == "off":
                continue
            fresh = s["status"] == "fresh"
            gone = revoked.get(s["source"])
            n_collector += 1
            # a collector's label is the household's own words, and its
            # source often names the institution it reads
            who = (str(n_collector) if public
                   else s["label"] or s["source"])
            out.append(_row(
                "Jobs", f"collector: {who}",
                fresh and not gone,
                f"last push {s['age_hours']:.1f}h ago · expected every "
                f"{s['expected_hours']}h · via "
                f"{_via.get(s.get('via') or '', s.get('via') or 'unknown')}"
                + (f" — its script token was revoked "
                   f"{gone.strftime('%m/%d/%y')}; mint a new one under "
                   f"Settings → Scripts and update the host" if gone else
                   "" if fresh else
                   " — check the host-side script's timer/journal"),
                "warn"))

        _plan = _llm_plan(conn)
        deferred.append((len(out),
                         lambda: _llm_probe_rows(_plan, public)))
        out += _llm_tail_rows(conn, bool(_plan), public)

        # email configuration (presence, not delivery) — Settings-card
        # config or the operator's env, same order delivery resolves
        from . import report as report_mod
        smtp = report_mod.resolve_smtp(conn)
        if demo:
            out.append(_row("Email", "SMTP", True,
                            "email disabled — demo instance"))
        elif smtp["configured"]:
            # delivery HEALTH, not just presence: the relay probe + the last
            # real send outcome. Instance-wide, so a hosted tenant sees
            # "the host's mail is down" rather than wondering why no
            # email came — honest, and it names no host.
            try:
                from ..notify import mailhealth
                mh = mailhealth.status()
            except Exception:                              # noqa: BLE001
                mh = {"failing": False}
            if mh.get("failing"):
                # This row is readable by every signed-in user and rides in
                # every feedback ZIP, so it carries only the failure class
                # and when it started — the scrubbed message stays on the
                # operator's console.
                pr = mh.get("probe") or {}
                bad = max((x for x in (mh.get("last_fail"),
                                       pr if pr.get("ok") is False else None)
                           if x), key=lambda x: x.get("at", ""), default={})
                klass = str(bad.get("error", "")).partition(":")[0] or "error"
                # status() already decides the failing-since moment; reading
                # it keeps this row agreeing with the console banner instead
                # of recomputing (and possibly disagreeing with) the same thing
                since = (mh.get("since") or "")[:19]
                out.append(_row("Email", "outbound mail", False,
                                f"mail is currently FAILING ({klass}"
                                + (f" since {since}" if since else "")
                                + ")", "warn"))
            # On hosted the resolved host is the OPERATOR's
            # relay — _settings_view withholds it ("the host owns mail"),
            # and this GET (reachable by read-only viewers, embedded in
            # every feedback ZIP) must not hand it back out
            out.append(_row("Email", "SMTP", True,
                            "delivery is managed by the host"
                            if env_flag("OIKONOME_HOSTED")
                            else "configured (relay host withheld from "
                                 "the bundle)" if public
                            else f"delivery via {smtp['host']}"))
        else:
            out.append(_row("Email", "SMTP", False,
                            "no SMTP configured (Settings → Email delivery, "
                            "or OIKONOME_SMTP_* in docker/.env) — email "
                            "cannot send", "warn"))

        # security posture
        if crypto.master_key():
            # A box run WITHOUT a key, then given one, keeps its
            # already-stored secrets in plaintext until the worker's
            # startup sweep re-encrypts them — so untagged values mean the
            # worker has not restarted since the key was set (or its sweep
            # failed). Don't claim "encrypted at rest" while any remain.
            # The count is the sweep's own list (aggregator tokens, EINs,
            # webhook signing secrets, the AI and mail keys in settings),
            # so this line and the sweep cannot disagree about what a
            # secret is.
            from ..db import migrate as _migrate
            plaintext = _migrate.plaintext_tenant_secrets(conn)
            if plaintext:
                out.append(_row(
                    "Security", "token encryption", False,
                    f"OIKONOME_MASTER_KEY set, but {plaintext} stored "
                    "secret(s) predate it and remain in PLAINTEXT — the "
                    "worker re-encrypts them when it starts; restart it",
                    "warn"))
            else:
                out.append(_row("Security", "token encryption", True,
                                "OIKONOME_MASTER_KEY set — secrets encrypted "
                                "at rest"))
        else:
            out.append(_row("Security", "token encryption", False,
                            "OIKONOME_MASTER_KEY not set — aggregator "
                            "tokens stored in PLAINTEXT; generate one with "
                            "`python -m oikonome.db.crypto`", "warn"))
        base_url = os.environ.get("OIKONOME_BASE_URL", "")
        # passkey sign-in exists only in hosted mode (its routes 404
        # elsewhere), so HTTPS alone must not promise it on a self-host
        _pk = env_flag("OIKONOME_HOSTED")
        out.append(_row(
            "Security", "transport", True,
            ("HTTPS (host withheld from the bundle)" if public
             else f"HTTPS via {base_url}")
            + (" — passkeys available" if _pk else "")
            if base_url.startswith("https://")
            else "plain HTTP — fine on a LAN; `./oikonome.sh https "
                 "<domain>` adds TLS"
                 + (" + passkeys" if _pk else "")))
        # A LAN/IP base URL embedded as a link gets the daily email silently
        # spam-filtered by the big mail providers, so emails omit links for
        # an unmailable base. Surface that as a fact, not a fault: linkless
        # email is the correct behavior for a LAN install.
        # An install that CAN mail but has no pinned base URL cannot send a
        # recipient invitation at all: the link would be Host-derived, which
        # is forgeable, so the invite is minted, refused, and written to the
        # server log instead. Settings still reports "invite sent", so the
        # household believes a person was asked who never heard from us, and
        # the cadence to that address stays paused for good. The refusal is
        # right; being quiet about it is not.
        if not base_url:
            try:
                from . import report as _rep
                # The tenant-scoped connection, never an admin one:
                # resolve_smtp reads (and on first run SEEDS) the tenant's
                # settings through budget.load_config, and on a connection
                # that bypasses RLS that seed aggregates EVERY tenant's
                # ledger — a cross-tenant scan that can run for minutes on
                # a busy instance, for a check that only needs this
                # household's mail settings.
                _smtp_ok = bool(_rep.resolve_smtp(conn).get("configured"))
            except Exception:                            # noqa: BLE001
                _smtp_ok = False
            if _smtp_ok:
                out.append(_row(
                    "Email", "recipient invitations", False,
                    "SMTP works but OIKONOME_BASE_URL is not set, so "
                    "invitations to extra recipients cannot be emailed — "
                    "they are written to the server log instead, while "
                    "Settings shows them as sent. Set OIKONOME_BASE_URL to "
                    "your instance's URL.", "warn"))
        from .report import mailable_base
        if base_url and not mailable_base():
            out.append(_row(
                "Email", "links in email", True,
                "emails are sent WITHOUT links — "
                + ("the base URL" if public else base_url)
                + " is a LAN/IP address, and Gmail spam-filters messages linking "
                "to one. A public HTTPS domain (`./oikonome.sh https "
                "<domain>`) restores the links"))

        # data sanity
        counts = conn.execute(
            """SELECT (SELECT COUNT(*) FROM transactions WHERE removed=0)
                          AS txns,
                      (SELECT COUNT(*) FROM accounts) AS accounts,
                      (SELECT COUNT(*) FROM bills WHERE active=1)
                          AS bills""").fetchone()
        out.append(_row(
            "Data", "ledger", counts["txns"] > 0,
            f"{counts['txns']} transactions · {counts['accounts']} accounts "
            f"· {counts['bills']} recurring bills"
            if counts["txns"] else
            "no transactions yet — connect a bank or import a CSV/OFX file",
            "warn"))
    finally:
        conn.close()
    # latest first, so each insertion leaves the earlier positions put
    for at, rows in reversed(deferred):
        out[at:at] = rows()
    return out


# What the bundle leaves out, said in the bundle itself, so whoever reads
# an attached copy knows the shape-only rows are deliberate.
BUNDLE_REDACTED = ("Host names (the instance's base URL, the mail relay, AI "
                   "endpoints), bank and institution names, and the labels "
                   "of imports and collectors are withheld from this "
                   "bundle; the Doctor page on the instance shows them.")


def bundle(tenant_id: str) -> dict:
    """The diagnostic-bundle payload (issue templates require it, and ask
    for it on a public issue). NO tokens, NO transaction contents, and no
    host, bank or household-typed names — check output + versions only."""
    import platform
    ver = os.environ.get("OIKONOME_VERSION")     # git describe, install-time
    if not ver:
        try:
            import importlib.metadata as md
            ver = md.version("oikonome")
        except Exception:                         # noqa: BLE001
            ver = "dev"
    return {"oikonome_version": ver,
            "python": platform.python_version(),
            "checks": checks(tenant_id, public=True),
            "redacted": BUNDLE_REDACTED,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
