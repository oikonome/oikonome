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


def admin_connect(dsn: str | None = None) -> psycopg.Connection:
    """Owner/superuser connection: migrations + tenant/user lifecycle only."""
    return psycopg.connect(dsn or ADMIN_DSN, row_factory=dict_row,
                           autocommit=True)


# ---- app-role connection pool ----------------------------------------------
# One pool per DSN (tests redirect APP_DSN to the test database). Connections
# come out UNSCOPED; tenant_connect wraps one with the tenant SET and the
# wrapper's close() RESETs the scope and returns it to the pool — callers
# keep the exact conn.close() discipline they always had.

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
                kwargs={"row_factory": dict_row, "autocommit": True},
                # A managed provider restarts the server under us (weekly
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
# fights for the few free slots under a managed-PG cap. Control-plane
# queries (users/
# sessions/tenants — no RLS, no tenant SET) now come from this small
# shared pool instead.
#
# Connection budget against a small managed-PG 25-connection cap:
#   ~5-8  the provider's own agents/metrics  (outside the instance's control)
#    10   web tenant pool                    (OIKONOME_POOL_MAX)
#     4   web control pool                   (OIKONOME_CONTROL_POOL_MAX)
#   ~2-3  worker steady state — its pools share these same env caps but
#         min_size=0 + max_idle shrink them to what jobs actually hold
#         (hourly sync touches tenants serially), plus its short-lived
#         admin_connect() conns
#   ~1-2  headroom (migrations, admin console, psql)
# Raise OIKONOME_POOL_MAX / OIKONOME_CONTROL_POOL_MAX only together with
# a PG plan whose connection cap has room for the new sum.
#
# Checkout timeout is deliberately SHORT (default 5 s vs psycopg_pool's
# 30 s): a starved control pool means every request is queueing for the
# session lookup, and failing fast lets the web layer shed load with a
# 503 (which an edge maintenance page understands) instead of hanging
# 30 s and then 500ing.

_control_pools: dict[str, ConnectionPool] = {}
CONTROL_POOL_TIMEOUT = float(os.environ.get("OIKONOME_CONTROL_POOL_TIMEOUT")
                             or "5")


def _control_pool(dsn: str) -> ConnectionPool:
    with _pools_lock:
        p = _control_pools.get(dsn)
        if p is None:
            p = ConnectionPool(
                dsn, min_size=0,
                max_size=int(os.environ.get("OIKONOME_CONTROL_POOL_MAX")
                             or "4"),
                kwargs={"row_factory": dict_row, "autocommit": True},
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
        return self._conn.execute(*a, **kw)

    def transaction(self, *a, **kw):
        self._refuse_if_discarded()
        return self._conn.transaction(*a, **kw)

    def close(self):
        if self._returned:
            return
        self._returned = True
        self._pool.putconn(self._conn)

    def discard(self):
        """Drop the connection instead of pooling it — see
        _PooledConn.discard."""
        if self._returned:
            return
        self._returned = True
        self._discarded = True
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
        return self._conn.execute(*a, **kw)

    def transaction(self, *a, **kw):
        self._refuse_if_discarded()
        return self._conn.transaction(*a, **kw)

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
        # Each COALESCEs to a no-op default when empty/unset.
        conn.execute(
            f"SELECT set_config('app.shadow_ids', COALESCE(({shadow_ids_sql()}"
            "), ''), false),"
            " set_config('app.entity_account_ids', COALESCE((SELECT "
            "string_agg(id, ',') FROM accounts WHERE entity_id IS NOT NULL), "
            "''), false),"
            " set_config('app.combine_entities', COALESCE((SELECT "
            "config->>'combine_entities' FROM tenant_settings), 'false'), "
            "false)")
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
    # invited, and its bounce row is still this tenant's to erase.
    for r in admin.execute(
            "SELECT email FROM recipient_invites WHERE tenant_id = %s",
            (tenant_id,)).fetchall():
        if isinstance(r["email"], str):
            emails.add(r["email"].strip().lower())
    emails.discard("")
    # whatever the installed add-on must keep about the account beyond its
    # life is archived out of its own tables by it now, before the sweep
    # below takes the rows; best-effort — an erasure never fails over it
    try:
        from .. import ext
        ext.gate.on_tenant_purge(admin, tenant_id)
    except Exception:                                    # noqa: BLE001
        logging.getLogger(__name__).warning(
            "add-on purge hook failed for %s", tenant_id, exc_info=True)
    with admin.transaction():
        if emails:
            admin.execute(
                "DELETE FROM email_delivery_state WHERE lower(email) = ANY(%s)",
                (sorted(emails),))
        for t in ordered:
            admin.execute(sql.SQL("DELETE FROM {} WHERE tenant_id = %s")
                          .format(sql.Identifier(t)), (tenant_id,))
        admin.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
