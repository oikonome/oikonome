"""Connection helpers — the ONLY way app code touches Postgres.

Design:
  * app connects as `oikonome_app` (non-superuser, non-owner) ⇒ RLS enforced.
  * a connection is scoped to ONE tenant by `SET app.tenant_id = ...`;
    every domain table's RLS policy and tenant_id DEFAULT read that setting,
    so ported engine SQL needs no tenant plumbing.
  * autocommit=True gives the engine sqlite-like semantics (each statement
    commits; use `with conn.transaction():` for atomic groups) — the ported
    engine came from sqlite and expects writes to stick without commit().
  * admin_connect() is for control-plane work only (create tenant/user,
    migrations); it uses the owner DSN and BYPASSES RLS. Never hand it to
    engine code.
"""

import logging
import os
import re
import threading
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

APP_DSN = os.environ.get(
    "OIKONOME_DSN",
    "postgresql://oikonome_app:apppass@127.0.0.1:5433/oikonome")
ADMIN_DSN = os.environ.get(
    "OIKONOME_ADMIN_DSN",
    "postgresql://postgres:devpass@127.0.0.1:5433/oikonome")


# THE definition of "accounts the money math must ignore", as SQL.
#
# There are two consumers of this idea and they must never drift: this
# session variable (`app.shadow_ids`, read by reporting.py's SPEND_WHERE,
# forecast.py's runway queries and report.py's card-debt sum) and the Python
# helper `engine.links.shadow_ids()` (read by budget, bills, savings,
# retirement, amazon_match).
#
# Two spellings of one rule drift: a hidden account vanishing from Today
# while still counting on Reports, or — during a live failover — one side
# summing the dead source while the other sums the live one.
#
# So the rule is derived, not restated: the health predicate below is
# generated from `links.LIVE_AGGREGATORS`/`STALE_HOURS`, and picking the
# primary with DISTINCT ON (healthy DESC, home_rank) is the SQL spelling of
# `next((m for m in members if m["healthy"]), members[0])`. There is one
# source for the aggregator list and one for the staleness bound.
#
# `starts_with()` rather than `LIKE 'error%'` on purpose: this string is
# interpolated into a larger statement, and a literal % would have to be
# doubled at some call sites and not others.
# The entity half of the per-checkout scope: which accounts belong to a
# business entity, and whether the household views business and personal
# money combined. One definition, shared by tenant_connect and by every
# writer that changes either input mid-connection (`set_entity_scope`), so
# a request that assigns an account and then reads personal money on the
# same connection sees the assignment — and the two computations cannot
# drift apart.
ENTITY_SCOPE_SQL = (
    "set_config('app.entity_account_ids', COALESCE((SELECT "
    "string_agg(id, ',') FROM accounts WHERE entity_id IS NOT NULL), "
    "''), false),"
    " set_config('app.combine_entities', COALESCE((SELECT "
    "config->>'combine_entities' FROM tenant_settings), 'false'), "
    "false)")


def set_entity_scope(conn) -> None:
    """Recompute app.entity_account_ids and app.combine_entities on this
    connection. tenant_connect stamps them once per checkout; a writer that
    changes an account's entity or the combined toggle calls this so later
    reads on the same connection do not use the stale snapshot."""
    conn.execute(f"SELECT {ENTITY_SCOPE_SQL}")


def shadow_ids_sql() -> str:
    from ..engine.links import LIVE_AGGREGATORS, STALE_HOURS
    live = ", ".join("'%s'" % a for a in LIVE_AGGREGATORS)
    return f"""
    WITH member AS (
        SELECT l.group_id, l.account_id, l.home_rank,
               (a.user_removed_at IS NULL) AS visible,
               (a.user_removed_at IS NULL
                AND NOT starts_with(COALESCE(i.status, 'ok'), 'error')
                AND (i.aggregator IS NULL
                     OR i.aggregator NOT IN ({live})
                     OR (a.updated_at IS NOT NULL
                         AND a.updated_at
                             >= now() - interval '{STALE_HOURS} hours')))
               AS healthy
          FROM account_links l
          JOIN accounts a ON a.id = l.account_id
          LEFT JOIN items i ON i.id = a.item_id
    ), effective_primary AS (
        SELECT DISTINCT ON (group_id) group_id, account_id
          FROM member ORDER BY group_id, healthy DESC, visible DESC, home_rank
    )
    SELECT string_agg(id, ',') FROM (
        SELECT m.account_id AS id FROM member m
          JOIN effective_primary p ON p.group_id = m.group_id
         WHERE m.account_id <> p.account_id
        UNION
        SELECT id FROM accounts WHERE user_removed_at IS NOT NULL
    ) s
"""


# A connect to a database that has stopped answering otherwise waits on the
# driver's default (over two minutes), holding a request thread — or, from an
# async handler, the whole event loop — for all of it. A DSN that names its
# own connect_timeout keeps it.
ADMIN_CONNECT_TIMEOUT_S = 10


def admin_connect(dsn: str | None = None) -> psycopg.Connection:
    """Owner/superuser connection: migrations + tenant/user lifecycle only.

    Unpooled: when the server is out of connection slots this raises
    psycopg.OperationalError at connect, which the web layer answers as a
    503 with Retry-After (the same brown-out as a starved pool)."""
    dsn = dsn or ADMIN_DSN
    kw = {}
    if "connect_timeout" not in dsn:
        kw["connect_timeout"] = ADMIN_CONNECT_TIMEOUT_S
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True, **kw)


# ---- app-role connection pool ----------------------------------------------
# One pool per DSN (tests redirect APP_DSN to the test database). Connections
# come out UNSCOPED; tenant_connect wraps one with the tenant SET and the
# wrapper's close() RESETs the scope and returns it to the pool — callers
# keep the exact conn.close() discipline they always had.

# seconds an idle pooled connection is kept before the pool closes it
POOL_MAX_IDLE_S = 120.0

_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()

# Symmetry with the control pool: the tenant pool checkout also uses a
# SHORT timeout, not psycopg_pool's 30 s default.
# A starved tenant pool (auth burst, or the worker holding conns mid-sync)
# should fast-fail into the web layer's 503 load-shed instead of pinning an
# anyio threadpool worker for 30 s — a modest burst otherwise starves even the
# control-only endpoints that would have shed in 5 s.
# `or` not a get() default: compose materializes every declared key, so an
# unset knob arrives as an EMPTY STRING, and float("") raises. Empty must
# mean "unset" for every env knob the container declares.
TENANT_POOL_TIMEOUT = float(os.environ.get("OIKONOME_TENANT_POOL_TIMEOUT")
                            or "5")


def _close_pools():
    for p in list(_pools.values()) + list(_control_pools.values()):
        try:
            p.close(timeout=1)
        except Exception:
            pass


import atexit  # noqa: E402

atexit.register(_close_pools)


def _pool(dsn: str) -> ConnectionPool:
    with _pools_lock:
        p = _pools.get(dsn)
        if p is None:
            p = ConnectionPool(
                dsn, min_size=0,
                max_size=int(os.environ.get("OIKONOME_POOL_MAX") or "10"),
                # An idle handle still holds one of the server's connection
                # slots, so a shorter max_idle (two minutes, against the
                # pool's default ten) gives slots back soon after a burst;
                # reconnecting on the next burst is cheap.
                max_idle=POOL_MAX_IDLE_S,
                kwargs={"row_factory": dict_row, "autocommit": True},
                # A managed provider may restart the server under the app (weekly
                # maintenance, major upgrades, failover). Without a check,
                # every idle connection the pool kept across the restart is
                # handed out dead and the first request on each one fails.
                # Pinging before hand-out costs one round-trip and turns
                # the restart into a reconnect instead of an error page.
                check=ConnectionPool.check_connection,
                open=True)
            _pools[dsn] = p
        return p


# ---- control-plane pool ------------------------------------------
# Under load the concurrency limit is DB connections, not host CPU/RAM:
# an unpooled session lookup per request pays TLS + backend spawn and
# fights for the few free slots under the database's connection limit.
# Control-plane queries (users/sessions/tenants — no RLS, no tenant SET)
# come from this small shared pool.
#
# The connection budget, per instance, all of it under the database's
# connection limit:
#   web     OIKONOME_POOL_MAX + OIKONOME_CONTROL_POOL_MAX (10 + 4)
#   worker  the same two caps as the worker process sees them — compose
#           maps OIKONOME_WORKER_POOL_MAX / OIKONOME_WORKER_CONTROL_POOL_MAX
#           onto them, falling back to the web's. The worker really fills
#           its pool: it runs up to WORKER_THREADS - 2 jobs at once, each
#           holding a tenant connection (a new bank's first pull holds one
#           for minutes; at most OIKONOME_FIRST_SYNC_CONCURRENCY of those).
#           min_size=0 + max_idle shrink it again when the queue is quiet.
#   admin   admin_connect() is unpooled: signup, confirmation, reclaim,
#           webhooks and link finishes each open one, bounded only by the
#           web threadpools — up to ~40 in a burst.
#   other   migrations, the admin console, psql, and — on a managed
#           database — the slots the provider reserves for its own agents.
#           Count those against the limit too; the plan's documentation
#           (or its connection view) gives the number.
#
# Checkout timeout is deliberately SHORT (default 5 s vs psycopg_pool's
# 30 s): a starved control pool means every request is queueing for the
# session lookup, and failing fast lets the web layer shed load with a
# 503 (which an edge maintenance page understands) instead of hanging
# 30 s and then 500ing.

_control_pools: dict[str, ConnectionPool] = {}
CONTROL_POOL_TIMEOUT = float(os.environ.get("OIKONOME_CONTROL_POOL_TIMEOUT")
                             or "5")


# Every control connection waits a bounded time, because the pool is four
# connections shared by every request on the instance. A request that
# queues on a lock (the per-user factor lock, a users row) holds its
# connection the whole time it waits, so a few waiters on one account
# would otherwise take the session lookup away from every other household
# for as long as the holder runs. With a lock wait capped, the waiter gives
# up (LockNotAvailable, answered 409 "busy" by the web layer) and its
# connection goes back to the pool. The idle-in-transaction cap ends a
# transaction whose Python side wandered off into slow work while holding
# locks, and the statement cap stops one runaway query; neither is reached
# by anything the control plane does on purpose — its statements are
# single-row reads and writes. Values in milliseconds.
_CONTROL_LIMITS_MS = (
    ("lock_timeout", "OIKONOME_CONTROL_LOCK_TIMEOUT_MS", 5 * 1000),
    ("idle_in_transaction_session_timeout",
     "OIKONOME_CONTROL_IDLE_TXN_TIMEOUT_MS", 30 * 1000),
    ("statement_timeout", "OIKONOME_CONTROL_STATEMENT_TIMEOUT_MS", 30 * 1000),
)


def _control_options() -> str:
    out = []
    for setting, env, default in _CONTROL_LIMITS_MS:
        raw = (os.environ.get(env) or "").strip()
        out.append(f"-c {setting}={int(raw) if raw.isdigit() else default}")
    return " ".join(out)


def _control_pool(dsn: str) -> ConnectionPool:
    with _pools_lock:
        p = _control_pools.get(dsn)
        if p is None:
            p = ConnectionPool(
                dsn, min_size=0,
                max_size=int(os.environ.get("OIKONOME_CONTROL_POOL_MAX")
                             or "4"),
                max_idle=POOL_MAX_IDLE_S,  # see _pool
                # set at connect, so they hold for every checkout and a
                # RESET on the way back to the pool cannot drop them
                kwargs={"row_factory": dict_row, "autocommit": True,
                        "options": _control_options()},
                check=ConnectionPool.check_connection,  # see _pool
                open=True)
            _control_pools[dsn] = p
        return p


class ConnectionDiscarded(RuntimeError):
    """A pooled handle was used after `release_lock` threw its connection
    away. The session could not be trusted back into the pool — its
    advisory unlock failed — so it was closed, and this handle names a
    connection that no longer exists.

    A named exception rather than psycopg's "the connection is closed":
    the single-flight helpers release in a `finally` and hand control back
    to a caller that goes on using `conn`, so the failure surfaces on
    whatever statement happens to run next, several frames from the unlock
    that caused it. The fix in that caller is always the same — take a
    fresh `tenant_connect`/`control_connect` — and the message has to say
    so, because nothing about "connection is closed" points at it."""


class _Discardable:
    """The part of a pooled handle that knows it can be taken away
    underneath its holder. `_discarded` is a CLASS default so `__getattr__`
    can consult it before (and regardless of) `__init__`."""

    _discarded = False
    # answers a request asks for more than once (the schedule's merchant
    # identities, the shadow accounts, the tenant config): good for this
    # checkout, dropped the moment a statement writes a table the answer
    # was read from, and never returned to the pool. A class default so a
    # handle is usable before (and regardless of) __init__.
    _memo: dict | None = None      # key -> (tables it depends on, value)

    def memo_get(self, key, default=None):
        hit = (self._memo or {}).get(key)
        return default if hit is None else hit[1]

    def memo_put(self, key, value, *tables: str) -> None:
        """Remember `value` for this checkout until any of `tables` is
        written on it. Every memoised lookup names what it read."""
        if self._memo is None:
            self._memo = {}
        self._memo[key] = (frozenset(t.lower() for t in tables), value)

    def _forget_written(self, q) -> None:
        """A statement is about to run: drop every remembered answer a
        write in it could change."""
        if not self._memo:
            return
        if not isinstance(q, str) or _DDL_RE.search(q):
            self._memo.clear()
            return
        hit = {m.lower() for m in _WRITE_TARGET_RE.findall(q)}
        # A write a statement never names: deleting a business entity
        # rewrites accounts.entity_id and transactions.entity_id through
        # ON DELETE SET NULL, so a memo over either must go with it.
        for t in list(hit):
            hit |= _CASCADES_INTO.get(t, frozenset())
        if hit:
            for k in [k for k, (deps, _) in self._memo.items() if deps & hit]:
                del self._memo[k]

    def _refuse_if_discarded(self) -> None:
        if self._discarded:
            raise ConnectionDiscarded(
                "this pooled connection was discarded after its advisory "
                "unlock failed and must not be used again — open a fresh "
                "one (tenant_connect / control_connect)")


class _ControlConn(_Discardable):
    """Pooled control-plane connection. Same duck-typed surface as
    _PooledConn, but no tenant scope was ever SET so close() skips the
    RESET round-trips and just returns the conn to the pool."""

    def __init__(self, pool: ConnectionPool, conn: psycopg.Connection):
        self._pool = pool
        self._conn = conn
        self._returned = False

    def execute(self, *a, **kw):
        self._refuse_if_discarded()
        self._forget_written(a[0] if a else kw.get("query"))
        return self._conn.execute(*a, **kw)

    def transaction(self, *a, **kw):
        self._refuse_if_discarded()
        return self._conn.transaction(*a, **kw)

    def close(self):
        if self._returned:
            return
        self._returned = True
        self._memo = None
        self._pool.putconn(self._conn)

    def discard(self):
        """Drop the connection instead of pooling it — see
        _PooledConn.discard."""
        if self._returned:
            return
        self._returned = True
        self._discarded = True
        self._memo = None
        try:
            self._conn.close()
        finally:
            self._pool.putconn(self._conn)

    def __getattr__(self, name):
        self._refuse_if_discarded()
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def control_connect(dsn: str | None = None) -> _ControlConn:
    """A pooled app-role connection for control-plane queries (users,
    sessions, tenants, …). UNSCOPED — RLS-protected domain tables return
    zero rows on it, by design. Raises psycopg_pool.PoolTimeout after
    CONTROL_POOL_TIMEOUT seconds when the pool is starved; the web layer
    turns that into a 503 (load shed)."""
    pool = _control_pool(dsn or APP_DSN)
    return _ControlConn(pool, pool.getconn(timeout=CONTROL_POOL_TIMEOUT))


# the table a DML statement writes (every occurrence: a CTE may carry
# several), and the statements that can change anything at all
_WRITE_TARGET_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|MERGE\s+INTO)"
    r"\s+(?:ONLY\s+)?([a-z_][a-z0-9_]*)", re.I)
# Tables whose rows a delete in the key rewrites by foreign-key action.
_CASCADES_INTO = {
    "business_entity": frozenset({"accounts", "compliance_obligation", "entity_membership", "equity_movement", "mileage_log", "transactions"}),
}

_DDL_RE = re.compile(r"\b(ALTER|DROP|CREATE|CALL|DO)\b", re.I)


class _MemoAwareTxn:
    """A transaction block on a pooled handle: leaving it by an exception
    rolls its writes back, so an answer memoised inside it may describe
    a state that never committed — the memo is dropped on that path."""

    def __init__(self, handle, txn):
        self._handle = handle
        self._txn = txn

    def __enter__(self):
        return self._txn.__enter__()

    def __exit__(self, et, ev, tb):
        if et is not None:
            self._handle._memo = None
        return self._txn.__exit__(et, ev, tb)


class _PooledConn(_Discardable):
    """Duck-types the psycopg Connection surface our code uses (execute /
    transaction / close). close() returns the underlying connection to the
    pool with the tenant scope RESET — never leaks one tenant's scope into
    another's checkout."""

    def __init__(self, pool: ConnectionPool, conn: psycopg.Connection):
        self._pool = pool
        self._conn = conn
        self._returned = False

    def execute(self, *a, **kw):
        self._refuse_if_discarded()
        self._forget_written(a[0] if a else kw.get("query"))
        return self._conn.execute(*a, **kw)

    def transaction(self, *a, **kw):
        self._refuse_if_discarded()
        return _MemoAwareTxn(self, self._conn.transaction(*a, **kw))

    def discard(self):
        """Drop the underlying connection instead of returning it: for a
        session whose state cannot be trusted back into the pool — an
        advisory lock whose unlock failed would otherwise ride along to
        the next borrower and hold the tenant's sync/email lock until the
        backend died.

        The handle is marked dead in the same move. Dropping the
        connection without that left the caller holding a live-looking
        object over a closed session — and every door that releases a lock
        does it in a `finally`, then carries on using `conn`."""
        if self._returned:
            return
        self._returned = True
        self._discarded = True
        self._memo = None
        try:
            self._conn.close()
        finally:
            self._pool.putconn(self._conn)     # a closed conn is dropped

    def close(self):
        if self._returned:
            return
        self._returned = True
        try:
            # every ambient setting a checkout stamped, not just two of
            # them — the entity scope rode along to the next borrower
            self._conn.execute("RESET app.shadow_ids")
            self._conn.execute("RESET app.entity_account_ids")
            self._conn.execute("RESET app.combine_entities")
            self._conn.execute("RESET app.tenant_id")
            self._memo = None
        except Exception:                # broken conn: drop it from the pool
            self._pool.putconn(self._conn)
            return
        self._pool.putconn(self._conn)

    def __getattr__(self, name):
        self._refuse_if_discarded()
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def release_lock(conn, key: str) -> bool:
    """Release the session-level advisory lock `key` and, if that fails,
    DROP the connection rather than let it go back to the pool.

    Every single-flight door (sync, snapshot, reap, nightly, email, the
    enrich and restore jobs) holds `pg_advisory_lock(hashtext(key))` on a
    POOLED connection. close() RESETs the tenant scope and hands the
    session back with the lock still on it, so a failed unlock followed by
    an ordinary close leaks the lock to the next borrower — and every later
    try-lock for that tenant is refused until the backend dies. Several
    doors meant to guard against that by calling close() a second time,
    which is the same return-to-pool. This is the one place the rule lives.

    Returns True when the connection was discarded — the caller must not
    use it again (its close() is a no-op after this, so calling it is
    harmless).

    Callers that ignore the return value are not left guessing: a
    discarded handle refuses every later statement with
    `ConnectionDiscarded`, which names the unlock as the cause. Silence
    was the worse failure — the doors release in a `finally` and then go
    on using `conn`, so psycopg's bare "the connection is closed" landed
    on whatever ran next."""
    try:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        return False
    except Exception:                                    # noqa: BLE001
        log.exception("could not release advisory lock %s — dropping the "
                      "connection rather than returning a locked one to "
                      "the pool", key)
        try:
            (conn.discard if hasattr(conn, "discard") else conn.close)()
        except Exception:                                # noqa: BLE001
            pass
        return True


def tenant_connect(tenant_id: str | uuid.UUID,
                   dsn: str | None = None):
    """A connection scoped to one tenant. RLS makes cross-tenant reads
    return zero rows and cross-tenant writes fail the policy check.
    Pooled: close() returns it (scope reset) instead of disconnecting."""
    tid = str(uuid.UUID(str(tenant_id)))   # validate — this goes into SET
    pool = _pool(dsn or APP_DSN)
    conn = pool.getconn(timeout=TENANT_POOL_TIMEOUT)
    try:
        conn.execute(f"SET app.tenant_id = '{tid}'")
        # linked "shadow" accounts (any linked account that is not
        # the lowest home_rank in its group) must be excluded from EVERY
        # money aggregate so two sources of one real account never double-
        # count. Computed once here (RLS-scoped to this tenant, reset on
        # pool return) and read by SPEND_WHERE + the balance queries via
        # current_setting — cheaper and less error-prone than threading a
        # shadow-id param through ~15 call sites. Same set as the Python
        # `links.shadow_ids()`, health-aware included — see shadow_ids_sql().
        # All three session vars in ONE round-trip (this runs on EVERY
        # tenant-scoped request, so keep connection setup cheap):
        # * app.shadow_ids — non-primary linked accounts, excluded
        #    from every money aggregate so two sources don't double-count.
        # * app.entity_account_ids — business-entity accounts, read by
        #    budget.PERSONAL_ONLY_SQL to keep entity money out of personal math.
        # * app.combine_entities — combined-view toggle; 'true' makes
        #    PERSONAL_ONLY_SQL a no-op (business + personal shown together).
        # Each COALESCEs to a no-op default when empty/unset. The id lists
        # are comma-joined and split back on ',' by their readers, which is
        # sound only because accounts.id can never hold a comma (migrations
        # 147 and 149 make the database refuse one).
        conn.execute(
            f"SELECT set_config('app.shadow_ids', COALESCE(({shadow_ids_sql()}"
            f"), ''), false), {ENTITY_SCOPE_SQL}")
    except Exception:
        pool.putconn(conn)
        raise
    return _PooledConn(pool, conn)


def create_tenant(admin: psycopg.Connection, name: str = "") -> str:
    row = admin.execute(
        "INSERT INTO tenants (name) VALUES (%s) RETURNING id", (name,)).fetchone()
    return str(row["id"])


# tables with the natural FK chain — children before parents; every other
# tenant-scoped table is FK-independent and gets deleted first
DELETE_LAST = ["transactions", "liabilities", "accounts", "items"]


def owner_email(admin: psycopg.Connection, tenant_id) -> str | None:
    """The address of the household's OWNER — the person every notice
    about the ACCOUNT itself is for. The earliest user is
    usually them, but an owner re-invited after a member joined is not
    the earliest row, and five call sites each spelled that assumption
    out on their own."""
    row = admin.execute(
        "SELECT email FROM users WHERE tenant_id = %s "
        "ORDER BY (role = 'owner') DESC, created_at LIMIT 1",
        (str(tenant_id),)).fetchone()
    return row["email"] if row else None


def delete_tenant_rows(admin: psycopg.Connection, tenant_id) -> None:
    """Erase every tenant-scoped row + the tenant itself (cascades users,
    sessions, password_resets), in ONE transaction on the admin conn (the
    app role can't touch `tenants`, and RLS doesn't bind the owner).
    Table list is discovered, so tables added later are covered. Used by
    the CCPA delete-account door and the demo hourly reset."""
    from psycopg import sql
    # The household's members are locked FIRST. Every other writer that
    # touches users and a per-member table takes users before the table
    # (the assistant log records an answer under a share lock on the
    # asker's row and trims the log in the same transaction); the sweep
    # below reaches users last, through the tenants cascade, so without
    # this an erasure racing an in-flight answer deadlocked, and an answer
    # recorded after the sweep had passed its table outlived the household.
    admin.execute("SELECT 1 FROM users WHERE tenant_id = %s FOR UPDATE",
                  (tenant_id,))
    rows = admin.execute(
        """SELECT DISTINCT table_name FROM information_schema.columns
           WHERE table_schema='public' AND column_name='tenant_id'""").fetchall()
    names = {r["table_name"] for r in rows} - {"tenants", "users", "sessions"}
    ordered = sorted(names - set(DELETE_LAST)) + \
        [t for t in DELETE_LAST if t in names]
    # email_delivery_state is deliberately EMAIL-keyed
    # (a mailbox is not tenant-scoped), so the tenant_id-column discovery
    # above cannot see it and nothing else ever deletes from it — a CCPA
    # erasure left the household's addresses, the provider's verbatim
    # bounce text and its bounce ids behind indefinitely. Collect every
    # address this tenant could have written a row for — its users and its
    # configured extra recipients — BEFORE the rows holding them go. A
    # mailbox shared with another household regenerates its row on that
    # household's next bounce; erasure wins.
    emails = {(r["email"] or "").strip().lower()
              for r in admin.execute(
                  "SELECT email FROM users WHERE tenant_id = %s",
                  (tenant_id,)).fetchall()}
    srow = admin.execute(
        "SELECT config FROM tenant_settings WHERE tenant_id = %s",
        (tenant_id,)).fetchone()
    cfg = (srow or {}).get("config")
    if isinstance(cfg, (str, bytes)):
        import json as _json
        try:
            cfg = _json.loads(cfg)
        except ValueError:
            cfg = None
    rec = (cfg or {}).get("email_recipients") if isinstance(cfg, dict) else None
    if isinstance(rec, str):
        rec = rec.split(",")
    for e in (rec or []):
        if isinstance(e, str) and e.strip():
            emails.add(e.strip().lower())
    # the live config is only the CURRENT list. An address the household
    # mailed and then removed — the usual remedy for one that bounces —
    # is still in recipient_invites, the durable record of everyone ever
    # invited, and its bounce row is still this tenant's to erase. Only an
    # invitation this household actually sent or had answered, though: a
    # decline read out of a restore archive was never mailed from here, and
    # naming an address is not a claim on its record.
    for r in admin.execute(
            "SELECT email FROM recipient_invites WHERE tenant_id = %s"
            " AND (token_hash IS NOT NULL OR send_count > 0"
            "      OR last_sent_at IS NOT NULL OR accepted_at IS NOT NULL)",
            (tenant_id,)).fetchall():
        if isinstance(r["email"], str):
            emails.add(r["email"].strip().lower())
    emails.discard("")
    # The record is shared by every household that mails the address. One
    # another household still has as a user, or has actually invited (the
    # same test as above: sent or answered), is evidence for that household
    # too, and stays. An invitation that household only read out of a
    # restore archive and never sent is not: it was never mailed from
    # there, and naming an address is no claim on its record.
    if emails:
        emails -= {(r["email"] or "").strip().lower() for r in admin.execute(
            """SELECT email FROM users
                WHERE tenant_id <> %s AND lower(email) = ANY(%s)
               UNION
               SELECT email FROM recipient_invites
                WHERE tenant_id <> %s AND lower(email) = ANY(%s)
                  AND (token_hash IS NOT NULL OR send_count > 0
                       OR last_sent_at IS NOT NULL
                       OR accepted_at IS NOT NULL)""",
            (tenant_id, sorted(emails), tenant_id, sorted(emails))).fetchall()}
    # A spam complaint is the recipient's word, and only they can take it
    # back (notify/delivery.clear keeps one the same way). The household's
    # own users' complaints go with the household; a complaint on any other
    # address it named stands.
    own = {(r["email"] or "").strip().lower() for r in admin.execute(
        "SELECT email FROM users WHERE tenant_id = %s",
        (tenant_id,)).fetchall()}
    # whatever the installed add-on must keep about the account beyond its
    # life is archived out of its own tables by it here, before the sweep
    # below takes the rows. Best-effort against the add-on's own failures —
    # an erasure never fails over one of those — but NOT when the add-on is
    # not loaded at all: then nothing can archive its billing record, and
    # sweeping its tables anyway would erase the only copy of it. That
    # stops the erasure with nothing deleted; the caller reports it and the
    # next run finishes once the add-on loads.
    from .. import ext
    try:
        ext.gate.on_tenant_purge(admin, tenant_id)
    except ext.GateUnavailable:
        logging.getLogger(__name__).error(
            "erasure of %s stopped before any row was deleted: the billing "
            "add-on is not loaded to archive its record", tenant_id)
        raise
    except Exception:                                    # noqa: BLE001
        logging.getLogger(__name__).warning(
            "add-on purge hook failed for %s", tenant_id, exc_info=True)
    # signup_invites is control-plane and email-keyed too: the operator's
    # invitation carries the person's address, a free-text note about them
    # and the id of the household it minted, and the discovery above cannot
    # see it. The household's own invitation goes, and so does any spent
    # or lapsed invite to one of its users' addresses — unless another
    # household still has that address as a user, whose invitation it may
    # be, or another household spent it: an address moves between
    # households when a login email changes, and the invitation another
    # household was born from is that household's record, not this one's.
    # A live, unused invite stays: it is the operator's standing offer
    # to that person for a household that does not exist yet — the one a
    # signup reclaim is about to create once this wipe clears the address,
    # or one the operator issued during the erasure's grace period — and
    # deleting it here would quietly drop its terms and note.
    invitees = set(own)
    if invitees:
        invitees -= {(r["email"] or "").strip().lower() for r in admin.execute(
            "SELECT email FROM users WHERE tenant_id <> %s"
            " AND lower(email) = ANY(%s)",
            (tenant_id, sorted(invitees))).fetchall()}
    invitees.discard("")
    with admin.transaction():
        admin.execute(
            "DELETE FROM signup_invites WHERE used_by_tenant = %s"
            " OR (lower(email) = ANY(%s)"
            "     AND (used_by_tenant IS NULL OR used_by_tenant = %s)"
            "     AND (used_at IS NOT NULL OR expires_at <= now()))",
            (tenant_id, sorted(invitees), tenant_id))
        if emails:
            admin.execute(
                "DELETE FROM email_delivery_state WHERE lower(email) = ANY(%s)"
                " AND (state <> 'complained' OR lower(email) = ANY(%s))",
                (sorted(emails), sorted(emails & own)))
        for t in ordered:
            admin.execute(sql.SQL("DELETE FROM {} WHERE tenant_id = %s")
                          .format(sql.Identifier(t)), (tenant_id,))
        # keyed `tenant`, not tenant_id (the webhook that reads it arrives
        # with no tenant), so the discovery above cannot see it; the
        # household's bank-link sessions go with it — any still open were
        # finished and their Items handed back by the erasure's external
        # release (erasure.release_external) first. plaid_release_queue
        # is keyed the same way and deliberately stays: it is what removes
        # the household's Items at Plaid after the rows are gone.
        admin.execute("DELETE FROM plaid_link_sessions WHERE tenant = %s",
                      (str(tenant_id),))
        admin.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
