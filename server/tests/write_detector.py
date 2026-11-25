"""Count every row written to a household table, per household and table
(specs/invariants-and-fuzzing.md, F2 and F4).

`install()` gives every table carrying a `tenant_id` column (the RLS
tables and the control-plane ones: users, sessions, invites, tokens) a
row-level AFTER INSERT/UPDATE/DELETE trigger that counts the write in an
unlogged side table. The count is part of the writer's transaction, so a
write that rolls back leaves no count — exactly the rule under test: on
a refusal, no write commits. `uninstall()` drops all of it; the fuzz
modules call it from tearDownClass in a `finally`.

`Detector.delta()` answers what was written since the last call, as
{(tenant id, table): rows}; `refusal_writes(delta)` keeps the ones a
refusal must not make (REFUSAL_MAY_WRITE names the others, each with why).
"""

from __future__ import annotations

from oikonome.db import tenancy

from .util import TEST_DB, _admin_dsn, _ensure_db

SIDE = "fuzz_write_counts"
FN = "fuzz_count_write"
TRIGGER = "zz_fuzz_count_write"

# Tables a refused request may still write, each with why. Reviewed, and
# empty: every refusal the fuzz reaches, the failed step-ups and re-auths
# included, leaves every household table as it was (their counters live
# outside the household tables). A table joins only with its reason.
REFUSAL_MAY_WRITE: dict[str, str] = {}


def _tables(conn) -> list[str]:
    return [r["relname"] for r in conn.execute(
        """SELECT c.relname FROM pg_class c
             JOIN pg_namespace n ON n.oid = c.relnamespace
             JOIN pg_attribute a ON a.attrelid = c.oid
                  AND a.attname = 'tenant_id' AND NOT a.attisdropped
            WHERE n.nspname = 'public' AND c.relkind = 'r'
              AND NOT c.relispartition AND c.relname <> %s
            ORDER BY 1""", (SIDE,)).fetchall()]


def _admin():
    _ensure_db()
    return tenancy.admin_connect(_admin_dsn(TEST_DB))


def install() -> None:
    conn = _admin()
    try:
        with conn.transaction():
            conn.execute(f"""CREATE UNLOGGED TABLE IF NOT EXISTS {SIDE} (
                tenant_id text NOT NULL, tbl text NOT NULL,
                n bigint NOT NULL, PRIMARY KEY (tenant_id, tbl))""")
            conn.execute(f"""CREATE OR REPLACE FUNCTION {FN}()
                RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
                SET search_path = public AS $$
                DECLARE t text;
                BEGIN
                  t := to_jsonb(CASE WHEN TG_OP = 'DELETE' THEN OLD
                                     ELSE NEW END) ->> 'tenant_id';
                  INSERT INTO {SIDE} VALUES (coalesce(t, ''),
                                             TG_TABLE_NAME, 1)
                  ON CONFLICT (tenant_id, tbl)
                  DO UPDATE SET n = {SIDE}.n + 1;
                  RETURN NULL;
                END $$""")
            for t in _tables(conn):
                conn.execute(f'DROP TRIGGER IF EXISTS {TRIGGER} ON "{t}"')
                conn.execute(f'CREATE TRIGGER {TRIGGER} AFTER INSERT OR '
                             f'UPDATE OR DELETE ON "{t}" FOR EACH ROW '
                             f'EXECUTE FUNCTION {FN}()')
    finally:
        conn.close()


def uninstall() -> None:
    conn = _admin()
    try:
        with conn.transaction():
            for t in _tables(conn):
                conn.execute(f'DROP TRIGGER IF EXISTS {TRIGGER} ON "{t}"')
            conn.execute(f"DROP FUNCTION IF EXISTS {FN}() CASCADE")
            conn.execute(f"DROP TABLE IF EXISTS {SIDE}")
    finally:
        conn.close()


class Detector:
    """An admin observer of the counts (and of the pool, for F5)."""

    def __init__(self):
        self.conn = _admin()
        self.seen = self._read()
        # Backends stuck when the module begins are an earlier test's, not
        # a fuzzed request's, and the module refuses to begin beside them
        # (fuzz_checks.FuzzCase), since each is a pooled connection that
        # hands its locks to the next borrower. Every test joins the
        # threads the product started during it (tests/thread_hygiene.py),
        # and any started outside a test (a class's setup) is joined here,
        # so a backend still stuck now is a leak: none is excused, and none
        # is waited out.
        self.stuck_before: set = set()
        import threading

        from .util import join_product_threads
        join_product_threads({threading.current_thread()})
        self.stuck_named = self.stuck_backends()

    def _read(self) -> dict:
        return {(r["tenant_id"], r["tbl"]): r["n"] for r in self.conn.execute(
            f"SELECT tenant_id, tbl, n FROM {SIDE}").fetchall()}

    def delta(self) -> dict:
        now = self._read()
        out = {k: n - self.seen.get(k, 0) for k, n in now.items()
               if n != self.seen.get(k, 0)}
        self.seen = now
        return out

    def stuck_backends(self) -> list[str]:
        """F5: any other backend of this database left idle inside a
        transaction, or idle while holding an advisory lock
        (lock_order_util.stuck_backends)."""
        from .lock_order_util import stuck_backends
        return [s for s in stuck_backends(self.conn)
                if int(s.split()[0]) not in self.stuck_before]

    def close(self):
        self.conn.close()


def refusal_writes(delta: dict) -> dict:
    """The writes of `delta` a refusal may not make."""
    return {k: n for k, n in delta.items() if k[1] not in REFUSAL_MAY_WRITE}
