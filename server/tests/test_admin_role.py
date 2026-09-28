"""The least-privilege runtime-admin role (oikonome_admin).

App/worker containers do not carry the postgres superuser DSN — their
OIKONOME_ADMIN_DSN points at oikonome_admin: BYPASSRLS + DML everywhere
(the runtime admin surface: cross-tenant reads, tenant/user lifecycle,
control-plane writes, pg_dump) but NO DDL, NO role management, NOT
superuser. The one-shot migrate container keeps the real superuser.
migrate.run() creates/aligns the role, so the shared test database has it
(local 'adminpass' fallback mirrors the app role's doctrine).
"""

import unittest
import uuid

import psycopg
from psycopg.rows import dict_row

from oikonome.db import tenancy

from .util import TEST_DB, TODAY, _ensure_db, add_txn, make_db

ADMIN_ROLE_DSN = (
    f"postgresql://oikonome_admin:adminpass@127.0.0.1:5433/{TEST_DB}")


def _role_conn():
    return psycopg.connect(ADMIN_ROLE_DSN, row_factory=dict_row,
                           autocommit=True)


class AdminRoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_role_shape(self):
        with _role_conn() as c:
            row = c.execute(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                "FROM pg_roles WHERE rolname = 'oikonome_admin'").fetchone()
        self.assertFalse(row["rolsuper"])
        self.assertTrue(row["rolbypassrls"])
        self.assertFalse(row["rolcreaterole"])
        self.assertFalse(row["rolcreatedb"])

    def test_bypasses_rls_for_cross_tenant_reads(self):
        conn = make_db()
        try:
            tid = conn.execute(
                "SELECT current_setting('app.tenant_id')").fetchone()[
                    "current_setting"]
            add_txn(conn, TODAY, 12.34, "adminrole-visibility")
        finally:
            conn.close()
        # no app.tenant_id set on this connection: RLS would hide the row
        # from the app role; the runtime-admin role must see every tenant
        with _role_conn() as c:
            got = c.execute(
                "SELECT count(*) AS n FROM transactions WHERE tenant_id=%s",
                (tid,)).fetchone()["n"]
        self.assertEqual(got, 1)

    def test_runtime_lifecycle_paths_work(self):
        # the exact calls app/worker make on admin_connect must work when
        # ADMIN_DSN is the least-privilege role
        with _role_conn() as admin:
            tid = tenancy.create_tenant(admin, f"adminrole-{uuid.uuid4().hex[:8]}")
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (%s, %s, 'x')",
                (tid, f"adminrole-{uuid.uuid4().hex[:8]}@example.dev"))
            admin.execute(
                "INSERT INTO admin_audit (action, target) "
                "VALUES ('test', 'adminrole')")
            self.assertTrue(admin.execute(
                "SELECT 1 FROM admin_audit WHERE target='adminrole'").fetchone())

    def test_no_ddl(self):
        with _role_conn() as c:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                c.execute("CREATE TABLE smuggled_table (id int)")

    def test_no_role_management(self):
        with _role_conn() as c:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                c.execute("CREATE ROLE rogue_role LOGIN PASSWORD 'x'")

    def test_cannot_alter_own_privileges(self):
        with _role_conn() as c:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                c.execute("ALTER ROLE oikonome_admin SUPERUSER")


if __name__ == "__main__":
    unittest.main()
