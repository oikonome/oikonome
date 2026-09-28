"""Tenant isolation reads the tenant setting once per statement, and still
isolates.

Every tenant_isolation policy compares tenant_id against the app.tenant_id
setting through a scalar subquery, which the planner evaluates once (an
InitPlan) instead of once per row scanned. The predicate is identical to
the per-row form, so the guarantees it gives must not move: a
scoped connection sees only its own rows, an unset (or empty) setting sees
none, and a write cannot land under another tenant. schema.sql's DO block
and the migration must agree, or a re-run would quietly put the per-row
form back on every table.
"""

import datetime as dt
import unittest

import psycopg

from oikonome.db import tenancy

from .util import add_txn, make_db


class RlsPolicyReadsTenantOnceTests(unittest.TestCase):
    def test_every_tenant_isolation_policy_is_the_initplan_form(self):
        conn = make_db()
        try:
            rows = conn.execute(
                "SELECT tablename, qual, with_check FROM pg_policies "
                "WHERE schemaname = 'public' "
                "AND policyname = 'tenant_isolation'").fetchall()
        finally:
            conn.close()
        self.assertTrue(rows)
        for r in rows:
            # deparsed by Postgres as "( SELECT ... )" — the subquery is
            # what makes the read an InitPlan; a bare current_setting()
            # call in the qual is the per-row form this guards against
            for col in ("qual", "with_check"):
                text = r[col] or ""
                self.assertIn("SELECT", text, (r["tablename"], col, text))
                self.assertIn("current_setting('app.tenant_id'", text,
                              (r["tablename"], col, text))

    def test_schema_sql_creates_the_same_form(self):
        from pathlib import Path
        import oikonome.db as dbpkg
        schema = (Path(dbpkg.__file__).parent / "schema.sql").read_text()
        self.assertIn(
            "USING (tenant_id = (SELECT NULLIF(current_setting(''app.tenant_id'', "
            "true), '''')::uuid))", schema)
        self.assertNotIn(
            "USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', "
            "true), '''')::uuid)", schema)

    def test_scoped_reads_and_writes_still_isolate(self):
        a = make_db()
        b = make_db()
        try:
            add_txn(a, dt.date(2026, 1, 5), 12.0, "ONLY IN A")
            add_txn(b, dt.date(2026, 1, 5), 34.0, "ONLY IN B")
            names_a = {r["name"] for r in a.execute(
                "SELECT name FROM transactions").fetchall()}
            names_b = {r["name"] for r in b.execute(
                "SELECT name FROM transactions").fetchall()}
            self.assertEqual(names_a, {"ONLY IN A"})
            self.assertEqual(names_b, {"ONLY IN B"})
            # the policy's WITH CHECK: a row stamped with the other tenant's
            # id is refused, not silently re-homed
            tid_b = b.execute(
                "SELECT current_setting('app.tenant_id', true) AS t"
            ).fetchone()["t"]
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                a.execute(
                    "INSERT INTO transactions (tenant_id, id, account_id, "
                    "date, amount, name, removed, raw) VALUES "
                    "(%s, 'x-tenant', 'card', '2026-01-06', 1, 'X', 0, '{}')",
                    (tid_b,))
        finally:
            a.close()
            b.close()

    def test_unset_tenant_sees_nothing(self):
        a = make_db()
        try:
            add_txn(a, dt.date(2026, 1, 5), 12.0, "ONLY IN A")
        finally:
            a.close()
        # a pooled app-role connection with no scope set: the NULLIF guard
        # turns the empty setting into NULL and NULL matches no row
        app_dsn = tenancy.APP_DSN
        raw = psycopg.connect(app_dsn, autocommit=True)
        try:
            raw.execute("SET app.tenant_id = ''")
            n = raw.execute(
                "SELECT count(*) AS n FROM transactions").fetchone()[0]
            self.assertEqual(n, 0)
            raw.execute("RESET app.tenant_id")
            n = raw.execute(
                "SELECT count(*) AS n FROM transactions").fetchone()[0]
            self.assertEqual(n, 0)
        finally:
            raw.close()

    def test_policy_plans_as_an_initplan(self):
        conn = make_db()
        try:
            plan = "\n".join(r["QUERY PLAN"] for r in conn.execute(
                "EXPLAIN SELECT count(*) FROM transactions "
                "WHERE removed = 0").fetchall())
        finally:
            conn.close()
        self.assertIn("InitPlan", plan)
        self.assertNotIn("current_setting", plan)


if __name__ == "__main__":
    unittest.main()
