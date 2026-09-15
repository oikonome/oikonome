"""reset-data (keep logins): every RLS domain table wiped for the target
tenant, control-plane untouched, other tenants untouched, wizard state
gone so the guided setup runs again."""

import datetime as dt
import unittest
import uuid

from oikonome.db import reset, tenancy

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 15)


class ResetDataTests(unittest.TestCase):
    def _tid(self, conn):
        return str(conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def test_wipes_domain_keeps_other_tenant(self):
        a, b = make_db(), make_db()
        try:
            for conn in (a, b):
                write_config(conn)
                add_txn(conn, TODAY, 50.0, "KROGER", account="chk")
                add_bill(conn, "HARBOR RIDGE MORTGAGE", 1150.00,
                         next_due=dt.date(2026, 8, 1))
            counts = reset.reset_tenant_data(self._tid(a))
            self.assertGreater(sum(counts.values()), 0)
            # transactions may cascade away with their account before the
            # loop reaches them — what matters is the table ends up empty
            self.assertIn("tenant_settings", counts)   # wizard/config reset
            # tenant A: empty domain
            self.assertIsNone(a.execute(
                "SELECT 1 FROM transactions LIMIT 1").fetchone())
            self.assertIsNone(a.execute(
                "SELECT 1 FROM bills LIMIT 1").fetchone())
            self.assertIsNone(a.execute(
                "SELECT 1 FROM tenant_settings LIMIT 1").fetchone())
            # tenant B: untouched
            self.assertIsNotNone(b.execute(
                "SELECT 1 FROM transactions LIMIT 1").fetchone())
            self.assertIsNotNone(b.execute(
                "SELECT 1 FROM tenant_settings LIMIT 1").fetchone())
        finally:
            a.close(); b.close()

    def test_logins_survive(self):
        conn = make_db()
        try:
            tid = self._tid(conn)
            admin = tenancy.admin_connect()
            try:
                email = f"keepme-{uuid.uuid4().hex[:8]}@example.dev"
                admin.execute(
                    "INSERT INTO users (tenant_id, email, password_hash, "
                    "role) VALUES (%s, %s, 'x', 'owner')", (tid, email))
                reset.reset_tenant_data(tid)
                row = admin.execute(
                    "SELECT 1 FROM users WHERE tenant_id=%s AND email=%s",
                    (tid, email)).fetchone()
                self.assertIsNotNone(row)
            finally:
                admin.close()
        finally:
            conn.close()

    def test_domain_tables_come_from_catalog(self):
        conn = make_db()
        try:
            tables = reset.domain_tables(conn)
            for expected in ("transactions", "accounts", "bills",
                             "receipts", "tenant_settings", "income_annual"):
                self.assertIn(expected, tables)
            for control in ("users", "sessions", "passwords", "api_tokens",
                            "invites", "tenants"):
                self.assertNotIn(control, tables)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
