"""The demonstration household is regenerated in place, as of today, without
disturbing the logins issued for it.

Why this exists: the generator is date-relative, so a household seeded in
one month stops at that month. Anyone opening the app weeks later finds an
empty current month — no transactions, every budget bar at zero. The
refresh must move the
data forward while leaving the credentials and the enrolled second factor
exactly where they are, and it must be unable to touch a live household."""

import unittest

from oikonome.db import tenancy

from .util import _ensure_db, TEST_DB


class DemoHouseholdRefreshTests(unittest.TestCase):
    """Runs against a THROWAWAY database: seeding creates a whole
    household, and the shared test DB has thousands of tenants."""

    DB = f"{TEST_DB}_demo_refresh"
    VIEWER = "viewer@example.com"

    @classmethod
    def setUpClass(cls):
        import psycopg

        from oikonome.db import migrate
        from .util import _admin_dsn as dsn
        _ensure_db()
        with psycopg.connect(dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {cls.DB}")
            c.execute(f"CREATE DATABASE {cls.DB}")
        migrate.run(dsn(cls.DB))
        cls._saved = (tenancy.ADMIN_DSN, tenancy.APP_DSN)
        tenancy.ADMIN_DSN = dsn(cls.DB)
        tenancy.APP_DSN = (f"postgresql://oikonome_app:apppass@"
                           f"127.0.0.1:5433/{cls.DB}")

    @classmethod
    def tearDownClass(cls):
        tenancy.ADMIN_DSN, tenancy.APP_DSN = cls._saved

    def _seed(self):
        from oikonome import demo
        return demo.seed(seed=99, years=2, email="owner@example.com",
                         viewer=self.VIEWER)

    def _ensure_seeded(self):
        """unittest runs a class's tests in alphabetical order, so no test
        may rely on another having seeded the household first. Always
        returns a dict carrying tenant_id, seeded or found."""
        admin = tenancy.admin_connect()
        try:
            row = admin.execute(
                "SELECT tenant_id FROM users WHERE email = %s",
                (self.VIEWER,)).fetchone()
        finally:
            admin.close()
        if row:
            return {"tenant_id": str(row["tenant_id"])}
        return self._seed()

    def _users(self):
        admin = tenancy.admin_connect()
        try:
            return {r["email"]: dict(r) for r in admin.execute(
                "SELECT email, password_hash, role, second_factor_waived, "
                "totp_secret FROM users ORDER BY email").fetchall()}
        finally:
            admin.close()

    def test_refresh_moves_data_to_today_and_keeps_both_logins(self):
        import datetime as dt

        from oikonome import demo
        first = self._ensure_seeded()
        before = self._users()

        # age the household the way the calendar does
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE transactions SET date = date - 40")
            admin.commit()
            stale = admin.execute(
                "SELECT max(date) AS m FROM transactions").fetchone()["m"]
        finally:
            admin.close()
        self.assertLess(stale, dt.date.today() - dt.timedelta(days=30))

        r = demo.refresh_demo_household(self.VIEWER, years=2, seed=99)
        self.assertTrue(r["refreshed"])
        self.assertEqual(r["tenant_id"], first["tenant_id"])

        admin = tenancy.admin_connect()
        try:
            fresh = admin.execute(
                "SELECT max(date) AS m, count(*) AS n "
                "FROM transactions").fetchone()
            # the current month is the whole point: opening the app must
            # not land on an empty Today and a zeroed Budget
            this_month = admin.execute(
                "SELECT count(*) AS n FROM transactions "
                "WHERE date >= date_trunc('month', current_date)").fetchone()
        finally:
            admin.close()
        self.assertGreaterEqual(fresh["m"], dt.date.today()
                                - dt.timedelta(days=7))
        self.assertGreater(this_month["n"], 0)
        self.assertGreater(fresh["n"], 0)

        # the issued credentials are untouched, enrolled
        # second factor included
        self.assertEqual(self._users(), before)

    def test_refresh_refuses_a_login_held_to_the_second_factor_rule(self):
        """The waiver is the mark of this login — a real account, and the
        household's own owner, are both held to the rule."""
        from oikonome import demo
        owner = self._users()["owner@example.com"]
        self.assertFalse(owner["second_factor_waived"])
        with self.assertRaises(SystemExit):
            demo.refresh_demo_household("owner@example.com", years=2)

    def test_refresh_refuses_a_tenant_that_is_not_a_demonstration_household(self):
        """A renamed tenant fails closed: the command deletes data, so an
        ambiguous target is never good enough."""
        from oikonome import demo
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET name = 'Family Household'")
            admin.commit()
            with self.assertRaises(SystemExit):
                demo.refresh_demo_household(self.VIEWER, years=2)
            n = admin.execute(
                "SELECT count(*) AS n FROM transactions").fetchone()["n"]
            self.assertGreater(n, 0)
            admin.execute("UPDATE tenants SET name = %s",
                          (demo.DEMO_HOUSEHOLD_NAME,))
            admin.commit()
        finally:
            admin.close()

    def test_refresh_refuses_a_household_with_a_real_item(self):
        from oikonome import demo
        admin = tenancy.admin_connect()
        try:
            tid = admin.execute(
                "SELECT tenant_id FROM users WHERE email = %s",
                (self.VIEWER,)).fetchone()["tenant_id"]
            admin.execute(
                "INSERT INTO items (id, tenant_id, aggregator, "
                "institution_name, status) "
                "VALUES ('real-plaid-item', %s, 'plaid', 'A Real Bank', "
                "'active')", (tid,))
            admin.commit()
            with self.assertRaises(SystemExit):
                demo.refresh_demo_household(self.VIEWER, years=2)
            # and the household it refused is still there
            n = admin.execute(
                "SELECT count(*) AS n FROM transactions").fetchone()["n"]
            self.assertGreater(n, 0)
            admin.execute("DELETE FROM items WHERE id = 'real-plaid-item'")
            admin.commit()
        finally:
            admin.close()

    def test_refresh_refuses_an_unknown_address(self):
        from oikonome import demo
        with self.assertRaises(SystemExit):
            demo.refresh_demo_household("nobody@example.com", years=2)
