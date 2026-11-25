"""Shared fixtures: a fresh TENANT per test — RLS is the isolation.

Every test creates a fresh tenant in a shared `oikonome_test` database
(created lazily, schema via migrate.run) and gets an RLS-scoped connection
— so the entire suite doubles as a continuous adversarial test of tenant
isolation: any cross-tenant leak makes unrelated tests fail loudly.

Env: OIKONOME_TEST_ADMIN_DSN / OIKONOME_TEST_DSN override the defaults
(dev container from README: 127.0.0.1:5433, postgres/devpass).
"""

import datetime as dt
import os
import uuid

import psycopg

from oikonome.db import migrate, tenancy
from oikonome.engine import bills, budget
from oikonome.engine.compat import as_date, jsonb

TODAY = dt.date(2026, 7, 15)          # fixed "today" mid-month, deterministic

# OIKONOME_TEST_DB: parallel workers (separate worktrees) get their own DB —
# control-plane tables have no RLS, so two suites in ONE db collide
# lowercased: an unquoted CREATE DATABASE lowercases the name, and an exact
# compare against pg_database then creates it twice (DuplicateDatabase)
TEST_DB = os.environ.get("OIKONOME_TEST_DB", "oikonome_test").lower()
_ADMIN = os.environ.get("OIKONOME_TEST_ADMIN_DSN",
                        "postgresql://postgres:devpass@127.0.0.1:5433")
_ready = False


def _admin_dsn(db: str) -> str:
    return f"{_ADMIN}/{db}"


def _ensure_db() -> None:
    global _ready
    try:
        from oikonome.web import security
        security._limiter._hits.clear()
    except Exception:
        pass
    if _ready:
        return
    with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname=%s",
                         (TEST_DB,)).fetchone():
            c.execute(f"CREATE DATABASE {TEST_DB}")
    # OIKONOME_TEST_PREPARED=1: the runner migrated this database just
    # before starting the process (a sharded runner does, one database at
    # a time) — migrate.run's role statements are cluster-global, and four
    # processes running them at once race ("tuple concurrently updated")
    if os.environ.get("OIKONOME_TEST_PREPARED") != "1":
        migrate.run(_admin_dsn(TEST_DB))
    # redirect module-level DSNs so code that opens its OWN connections
    # (job task bodies, web routes under test) hits the test database
    tenancy.ADMIN_DSN = _admin_dsn(TEST_DB)
    tenancy.APP_DSN = os.environ.get(
        "OIKONOME_TEST_DSN",
        f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
    # every TestClient suite signs up once — don't let the signup rate
    # limit (5/h per IP) trip across suites in one run
    try:
        from oikonome.web import security
        security._limiter._hits.clear()
    except Exception:
        pass
    _ready = True


MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def all_routes(app):
    """Every concrete route on the app, flattened.

    FastAPI 0.139 stopped copying an included router's routes into
    `app.routes` — `include_router` now appends ONE `_IncludedRouter`
    wrapper holding the original router. So `app.routes` shows 70 entries
    for an app that actually serves 268, and any inventory test walking it
    naively would silently check a tenth of the surface and pass. Recurse
    through `original_router` to get the real list.
    """
    out: list = []

    def walk(routes) -> None:
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                walk(inner.routes)
            elif getattr(r, "methods", None) and getattr(r, "endpoint", None):
                out.append(r)

    walk(app.routes)
    return out


def mutating_routes(app):
    """Flattened routes that change state (POST/PUT/PATCH/DELETE)."""
    return [r for r in all_routes(app)
            if set(r.methods) & MUTATING_METHODS]


def route_key(route) -> str:
    """Stable `METHOD path` identity for allowlists. A route with several
    verbs yields the mutating one (they never mix in this codebase)."""
    verb = sorted(set(route.methods) & MUTATING_METHODS)[0]
    return f"{verb} {route.path}"


def handler_source(route) -> str:
    """Source of the route's handler, '' if unavailable."""
    import inspect
    try:
        return inspect.getsource(route.endpoint)
    except (OSError, TypeError):
        return ""


def give_totp_factor(email: str) -> None:
    """Mark an account as having a second factor enrolled — the realistic
    state for a HOSTED user doing writes (the server blocks hosted writes
    until a factor exists). The stored
    value only needs to be non-null for the has-factor gate; policy tests
    that don't subsequently TOTP-login use this to represent an enrolled
    hosted account."""
    admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
    try:
        admin.execute(
            "UPDATE users SET totp_secret='enc:cp:test-factor' WHERE email=%s",
            (email.strip().lower(),))
    finally:
        admin.close()


def clear_totp_burn(email: str | None = None) -> None:
    """Forget which TOTP step has already been spent.

    One-time codes are one-time account-wide: every door that accepts a
    code burns its step (login, password change, TOTP re-enroll/disable,
    recovery regenerate, account erasure, email change).

    A test that presents the same code at two doors is therefore doing a
    replay, and gets refused. Most of these tests exercise the step-up GATE,
    not replay protection, and cannot mint a second code inside the same
    30-second step. Calling this first says plainly "this test is not about
    replay"; the replay contract itself is pinned by test_auth_hardening's
    `test_totp_code_is_single_use_across_doors`.

    Do NOT reach for this in a test that is about the burn.
    """
    admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
    try:
        if email:
            admin.execute(
                "UPDATE users SET totp_last_counter=NULL WHERE email=%s",
                (email.strip().lower(),))
        else:
            admin.execute("UPDATE users SET totp_last_counter=NULL")
    finally:
        admin.close()


def make_db(_tmpdir=None) -> psycopg.Connection:
    """Fresh tenant + RLS-scoped connection, seeded with the standard
    fixture accounts (checking $5,000 / card $250). `_tmpdir` is accepted
    and ignored, for signature compatibility with older callers."""
    _ensure_db()
    admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
    tid = tenancy.create_tenant(admin, f"test-{uuid.uuid4().hex[:8]}")
    admin.close()
    app_dsn = os.environ.get(
        "OIKONOME_TEST_DSN",
        f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
    conn = tenancy.tenant_connect(tid, app_dsn)
    seed_accounts(conn)
    return conn


def seed_accounts(conn) -> None:
    """The standard fixture accounts (checking $5,000 / card $250) for a
    tenant-scoped connection — used by make_db and API-signup tenants."""
    conn.execute("INSERT INTO items (id, aggregator, institution_name, "
                 "access_token) VALUES ('it1','test','Test Bank','tok-test') "
                 "ON CONFLICT (tenant_id, id) DO NOTHING")
    conn.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                 "balance_current,balance_available) VALUES "
                 "('chk','it1','Test Checking','depository','checking',5000,5000)"
                 " ON CONFLICT (tenant_id, id) DO NOTHING")
    conn.execute("INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
                 " VALUES ('card','it1','Test Card','credit','credit card',250)"
                 " ON CONFLICT (tenant_id, id) DO NOTHING")


def write_config(conn, **over) -> None:
    """Per-tenant config."""
    cfg = {"food_monthly": 1000, "other_monthly": 1000,
           "dynamic_variable_budget": False}
    cfg.update(over)
    budget.save_config(conn, cfg)


def accept_recipient_invite(tenant_id, *emails) -> None:
    """Pretend these addresses opened their invitation and said yes.

    A configured recipient is only MAILED once they accept, so a test that
    wants plain "configured ⇒ mailed" behaviour has to say which people
    agreed. That is not a workaround for the gate — it is the gate's
    contract written out: somebody, somewhere, clicked."""
    admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
    try:
        for email in emails:
            admin.execute(
                """INSERT INTO recipient_invites (tenant_id, email,
                                                  accepted_at)
                   VALUES (%s, %s, now())
                   ON CONFLICT (tenant_id, email) DO UPDATE
                       SET accepted_at = now(), declined_at = NULL,
                           token_hash = NULL""",
                (tenant_id, email.strip().lower()))
    finally:
        admin.close()


_n = [0]


def add_txn(conn, date, amount, name, *, primary="GENERAL_MERCHANDISE",
            account="card", override=None, detailed=None, pending=0,
            merchant=None, txn_id=None):
    _n[0] += 1
    txn_id = txn_id or f"t{_n[0]:04d}"
    conn.execute(
        """INSERT INTO transactions (id,account_id,date,amount,name,
               merchant_name,category_primary,category_detailed,category_override,
               pending,removed,raw) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s)
           ON CONFLICT (tenant_id, id) DO UPDATE SET
               date=EXCLUDED.date, amount=EXCLUDED.amount, name=EXCLUDED.name""",
        (txn_id, account, as_date(date), amount, name, merchant or name,
         primary, detailed, override, pending, jsonb({})))
    return txn_id


def freeze_month(conn, year: int, month: int) -> None:
    """The month's budget snapshot, as the nightly job's last write of the
    month leaves it: the saved budget and the bill schedule as they stand.
    A fixture that means "the household had this budget that month" needs
    it — a closed month without one is from before the budget, and is not
    judged."""
    import calendar
    assert budget.snapshot_month(
        conn, dt.date(year, month, calendar.monthrange(year, month)[1]))


def add_ledger_history(conn, before, *, account="card", days=120):
    """A cent of interest `days` before `before`: the ledger is older than
    the month under test. The live verdict paces a ledger that starts
    inside the month from its first day, so a fixture that means an
    established household with one row this month needs this."""
    return add_txn(conn, as_date(before) - dt.timedelta(days=days), -0.01,
                   "INTEREST PAID", primary="INCOME", account=account)


def add_bill(conn, payee, amount, *, frequency="MONTHLY", interval=1,
             next_due=None, bill_type="occurrence", income=False,
             last_seen=None, source="detected", **kw):
    # source defaults to "detected" (machine origin): source="manual" bills
    # get a manual_edited_at stamp and are drift-immune for GRACE_DAYS, which
    # is its own behavior under test — not what generic fixtures want.
    # **kw passes through to save_bill (category, show_today, match_category).
    # A fixture's bill has no day it was set up (bills.created_on NULL, as
    # for every bill stored before the day was kept) unless `created_on`
    # names one: the default is the real calendar's day, which a fixture
    # dated in the past would read as a bill set up after its months.
    # An edit (a second add_bill of one payee) keeps the day the bill has.
    created_on = kw.pop("created_on", None)
    existed = conn.execute("SELECT 1 FROM bills WHERE id = %s",
                           (bills._slug(payee),)).fetchone() is not None
    out = bills.save_bill(
        conn, payee=payee, amount=amount, bill_type=bill_type,
        frequency=frequency, interval=interval, next_due=next_due,
        income=income, last_seen=last_seen, source=source, **kw)
    if created_on is not None or not existed:
        conn.execute("UPDATE bills SET created_on = %s WHERE id = %s",
                     (created_on, bills._slug(payee)))
    return out


# ---- the JSON API's source, wherever its handlers live ---------------------
# web/api.py is split into one module per area (specs/api-routers.md):
# `api.py` mounts them in order, `api_common.py` holds what several share,
# and each `api_<area>.py` holds one area's routes. A test that reads the
# API's SOURCE reads all of them through these helpers, so moving a handler
# between modules can never hide it from a check.

def api_source_paths() -> list:
    """Every module of the JSON API: api.py first, then the others by name."""
    import pathlib
    web = pathlib.Path(__file__).resolve().parents[1] / "oikonome" / "web"
    return [web / "api.py"] + sorted(web.glob("api_*.py"))


def api_source_files() -> list:
    """[(file name, source text)] for every API module — for a check whose
    window must not run from one module into the next."""
    return [(p.name, p.read_text()) for p in api_source_paths()]


def api_sources() -> str:
    """The whole API's source as one text, module after module."""
    return "\n\n".join(src for _, src in api_source_files())


def api_module_names() -> frozenset:
    """Dotted names of every API module, for `endpoint.__module__` checks."""
    return frozenset(f"oikonome.web.{p.stem}" for p in api_source_paths())


def is_api_endpoint(route) -> bool:
    """Is this route's handler one of the JSON API's (any API module)?"""
    return getattr(route.endpoint, "__module__", None) in api_module_names()


def api_file_of(name: str) -> str:
    """The file name of the API module that defines top-level `name`."""
    import ast
    for fname, src in api_source_files():
        for node in ast.parse(src).body:
            if getattr(node, "name", None) == name:
                return fname
    raise LookupError(f"{name} is not defined in any API module")


def api_function(name: str):
    """The top-level object `name` from whichever API module defines it."""
    import importlib
    return getattr(importlib.import_module(
        "oikonome.web." + api_file_of(name)[:-3]), name)


def join_product_threads(before, timeout: float = 30.0) -> None:
    """Join every thread the product started since `before` (a set of
    threads): the work a door handed to a thread of its own."""
    import threading
    for t in threading.enumerate():
        target = getattr(t, "_target", None)
        if t in before or t is threading.current_thread() or not target:
            continue
        if (getattr(target, "__module__", "") or "").startswith("oikonome."):
            t.join(timeout)


def outlive_no_threads(test, timeout: float = 30.0) -> None:
    """Join, when `test` ends, every thread the product started during it.

    A door that hands its work to a thread (the restore job, the
    categorize pass after an import) returns before the work is done, and
    the work holds a pooled connection: inside a transaction, or holding
    the household's sync lock. Left running, it is still holding them when
    the next test begins, which then sees another test's lock on its own
    household's pool (specs/invariants-and-fuzzing.md, L6 and F5)."""
    import threading
    before = set(threading.enumerate())
    test.addCleanup(join_product_threads, before, timeout)
