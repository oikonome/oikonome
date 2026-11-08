"""The demonstration household is regenerated in place, as of today, without
disturbing the logins issued for it.

The generator is date-relative, so a household seeded in one month stops
at that month, and anyone opening it weeks later finds an empty current
month — no transactions, every budget bar at zero. The refresh must move
the data forward while leaving the credentials and the enrolled second
factor exactly where they are, and it must be unable to touch a live
household."""

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
            # the refresh places rows by Python's local date; Postgres's
            # current_date is UTC, which is already next month for a few
            # hours on the last evening of a month, so compare on one clock
            this_month = admin.execute(
                "SELECT count(*) AS n FROM transactions WHERE date >= %s",
                (dt.date.today().replace(day=1),)).fetchone()
        finally:
            admin.close()
        self.assertGreaterEqual(fresh["m"], dt.date.today()
                                - dt.timedelta(days=7))
        self.assertGreater(this_month["n"], 0)
        self.assertGreater(fresh["n"], 0)

        # the issued credentials are untouched, enrolled
        # second factor included
        self.assertEqual(self._users(), before)

    def test_refresh_keeps_every_control_plane_row(self):
        """Only household data is regenerated. What the logins hold — the
        home-screen widget's token, the browser's push subscription, a
        support consent, the invitations sent out — is left exactly as it
        was, or the phone widget and the notifications quietly stop after a
        scheduled refresh. (The push subscription here is the owner's; the
        shared login's own are dropped, tested below.)"""
        import datetime as dt

        from oikonome import demo
        tid = self._ensure_seeded()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            uid = admin.execute(
                "SELECT id FROM users WHERE email = %s",
                (self.VIEWER,)).fetchone()["id"]
            owner = admin.execute(
                "SELECT id FROM users WHERE email = 'owner@example.com'"
            ).fetchone()["id"]
            later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)
            admin.execute(
                "INSERT INTO widget_tokens (token_hash, device_id, user_id, "
                "tenant_id) VALUES ('wt-hash', gen_random_uuid(), %s, %s)",
                (uid, tid))
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s, %s, "
                "'https://push.example.com/x', 'p', 'a') "
                "ON CONFLICT (endpoint) DO NOTHING", (owner, tid))
            admin.execute(
                "INSERT INTO support_consents (tenant_id, expires_at) "
                "VALUES (%s, %s)", (tid, later))
            admin.execute(
                "INSERT INTO recipient_invites (tenant_id, email) "
                "VALUES (%s, 'reader@example.com')", (tid,))
            admin.execute(
                "INSERT INTO invites (token_hash, tenant_id, expires_at) "
                "VALUES ('inv-hash', %s, %s)", (tid, later))
            admin.commit()

            tables = ("widget_tokens", "push_subscriptions",
                      "support_consents", "recipient_invites", "invites")

            def counts():
                return {t: admin.execute(
                    f"SELECT count(*) AS n FROM {t} WHERE tenant_id = %s",
                    (tid,)).fetchone()["n"] for t in tables}
            before = counts()
            demo.refresh_demo_household(self.VIEWER, years=2, seed=7)
            self.assertEqual(counts(), before)
            self.assertTrue(all(before.values()), before)
        finally:
            admin.close()

    def test_refresh_drops_the_shared_logins_push_subscriptions(self):
        """A browser subscribed by the shared login belongs to whichever
        visitor held it then; the household handed to the next visitor
        must not keep sending its alerts to that browser."""
        from oikonome import demo
        tid = self._ensure_seeded()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            uid = admin.execute(
                "SELECT id FROM users WHERE email = %s",
                (self.VIEWER,)).fetchone()["id"]
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s, %s, "
                "'https://push.example.com/visitor', 'p', 'a')", (uid, tid))
            admin.commit()
            demo.refresh_demo_household(self.VIEWER, years=2, seed=7)
            n = admin.execute(
                "SELECT count(*) AS n FROM push_subscriptions "
                "WHERE user_id = %s", (uid,)).fetchone()["n"]
            self.assertEqual(n, 0)
        finally:
            admin.close()

    def test_refresh_keeps_the_households_mail_choices(self):
        """A schedule switched off, an address that muted itself and the
        kinds a person turned off are the holders' choices, not generated
        data. The wipe must not hand the defaults back — the daily verdict
        on again, mailed to an address that asked for silence."""
        from oikonome import demo
        from oikonome.engine import budget
        tid = self._ensure_seeded()["tenant_id"]
        off = {"daily": {"on": False, "hour": 7},
               "monthly": {"on": False, "hour": 7},
               "yearly": {"on": False, "hour": 7}}
        conn = tenancy.tenant_connect(tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["email_schedule"] = off
                cfg["email_muted"] = [self.VIEWER]
                cfg["email_prefs"] = {"alerts": [self.VIEWER]}
        finally:
            conn.close()

        demo.refresh_demo_household(self.VIEWER, years=2, seed=99)

        conn = tenancy.tenant_connect(tid)
        try:
            cfg = budget.load_config(conn)
            from oikonome.jobs import worker
            import datetime as dt
            due = worker.emails_due(conn, dt.datetime.now(dt.timezone.utc)
                                    + dt.timedelta(days=1, hours=23))
        finally:
            conn.close()
        self.assertEqual(cfg.get("email_schedule"), off)
        self.assertEqual(cfg.get("email_muted"), [self.VIEWER])
        self.assertEqual(cfg.get("email_prefs"), {"alerts": [self.VIEWER]})
        self.assertEqual(due, [])

    # every stored key that decides whether, when, or to whom the
    # household's mail, texts and pushes go. A refresh that drops one hands
    # the default back — and the default sends.
    MAIL_SAFETY_KEYS = ("email_schedule", "email_recipients", "email_muted",
                        "email_prefs", "email_send_hour_utc",
                        "outbound_paused", "notify_phone",
                        "notify_phone_pending", "timezone")

    def test_every_mail_safety_key_survives_a_refresh(self):
        from oikonome import demo
        missing = set(self.MAIL_SAFETY_KEYS) - set(demo.DEMO_REFRESH_KEEPS)
        self.assertEqual(missing, set())

    def test_refresh_keeps_the_outbound_pause_holding_the_recipients(self):
        """A credential rotation pauses the recipients until the owner
        presses Resume. The refresh carries the recipients, so it must
        carry the pause too, or the next report goes to addresses nobody
        confirmed with nobody having pressed Resume."""
        import datetime as dt
        from oikonome import demo
        from oikonome.engine import budget
        from oikonome.notify import outbound_pause
        tid = self._ensure_seeded()["tenant_id"]
        pause = {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                 "reason": "rotation", "smtp": False,
                 "recipients": ["held@example.com"], "llm": False,
                 "aggregator": False}
        phone = {"number": "+12175551234", "verified": True}
        conn = tenancy.tenant_connect(tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["email_recipients"] = ["held@example.com"]
                cfg["outbound_paused"] = pause
                cfg["notify_phone"] = phone
                cfg["email_send_hour_utc"] = 14
        finally:
            conn.close()

        demo.refresh_demo_household(self.VIEWER, years=2, seed=99)

        conn = tenancy.tenant_connect(tid)
        try:
            cfg = budget.load_config(conn)
        finally:
            conn.close()
        held = outbound_pause.paused(cfg)
        self.assertIsNotNone(held, "the refresh lifted the pause")
        self.assertEqual(held["recipients"], ["held@example.com"])
        self.assertEqual(cfg.get("notify_phone"), phone)
        self.assertEqual(cfg.get("email_send_hour_utc"), 14)

    def test_an_interrupted_refresh_keeps_the_mail_choices_for_the_next(self):
        """A refresh killed or failing midway must not lose the mail
        choices: the next refresh has to find them again, or the defaults
        come back on — mail to an address that asked for silence."""
        from unittest import mock

        from oikonome import demo
        from oikonome.engine import budget
        tid = self._ensure_seeded()["tenant_id"]
        off = {"daily": {"on": False, "hour": 7},
               "monthly": {"on": False, "hour": 7},
               "yearly": {"on": False, "hour": 7}}
        conn = tenancy.tenant_connect(tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["email_schedule"] = off
                cfg["email_muted"] = [self.VIEWER]
        finally:
            conn.close()

        with mock.patch.object(demo, "_write",
                               side_effect=RuntimeError("killed midway")):
            with self.assertRaises(RuntimeError):
                demo.refresh_demo_household(self.VIEWER, years=2, seed=99)
        demo.refresh_demo_household(self.VIEWER, years=2, seed=99)

        conn = tenancy.tenant_connect(tid)
        try:
            cfg = budget.load_config(conn)
        finally:
            conn.close()
        self.assertEqual(cfg.get("email_schedule"), off)
        self.assertEqual(cfg.get("email_muted"), [self.VIEWER])

    def test_a_reader_never_sees_the_household_empty_during_a_refresh(self):
        """The household is in use while it is regenerated: a visitor's
        request that lands mid-rebuild must see the old household or the
        new one, never an empty one ("No transactions", every bar at zero).
        The rebuild is held at its start until a reader on another
        connection has counted the ledger there."""
        import threading
        from unittest import mock

        from oikonome import demo
        tid = self._ensure_seeded()["tenant_id"]
        mid, counted, done = (threading.Event(), threading.Event(),
                              threading.Event())
        counts, mid_counts = [], []
        real_write = demo._write

        def held_write(*a, **kw):
            mid.set()
            counted.wait(10)
            return real_write(*a, **kw)

        def reader():
            conn = tenancy.tenant_connect(tid)
            try:
                while not done.is_set():
                    during = mid.is_set()   # read BEFORE the count starts
                    n = conn.execute("SELECT count(*) AS n FROM "
                                     "transactions").fetchone()["n"]
                    counts.append(n)
                    if during:
                        mid_counts.append(n)
                        counted.set()
            finally:
                conn.close()

        def total():
            admin = tenancy.admin_connect()
            try:
                return admin.execute(
                    "SELECT count(*) AS n FROM transactions "
                    "WHERE tenant_id = %s", (tid,)).fetchone()["n"]
            finally:
                admin.close()

        old = total()
        self.assertGreater(old, 0)
        t = threading.Thread(target=reader)
        t.start()
        try:
            with mock.patch.object(demo, "_write", side_effect=held_write):
                demo.refresh_demo_household(self.VIEWER, years=2, seed=7)
        finally:
            done.set()
            t.join(30)
        self.assertTrue(mid_counts, "the reader never counted mid-rebuild")
        # every count the reader took is a whole household, old or new —
        # no empty window and no half-written one
        self.assertLessEqual(set(counts), {old, total()},
                             f"partial counts seen: {sorted(set(counts))[:5]}")

    def test_a_refresh_that_fails_midway_leaves_the_previous_household(self):
        """A rebuild that raises after the generated rows are written must
        leave the household exactly as it was before the refresh began —
        its ledger and its settings — not an empty one and not half of the
        new one."""
        from unittest import mock

        from oikonome import demo
        tid = self._ensure_seeded()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            # age it, so a rebuild that got through would differ
            admin.execute("UPDATE transactions SET date = date - 40 "
                          "WHERE tenant_id = %s", (tid,))

            def snapshot():
                return (
                    admin.execute(
                        "SELECT md5(string_agg(id || date || amount, ',' "
                        "ORDER BY id)) AS h, count(*) AS n FROM transactions "
                        "WHERE tenant_id = %s", (tid,)).fetchone(),
                    admin.execute(
                        "SELECT config FROM tenant_settings "
                        "WHERE tenant_id = %s", (tid,)).fetchone())
            before = snapshot()
            self.assertGreater(before[0]["n"], 0)
            with mock.patch.object(demo, "_configure",
                                   side_effect=RuntimeError("midway")):
                with self.assertRaises(RuntimeError):
                    demo.refresh_demo_household(self.VIEWER, years=2,
                                                seed=7)
            self.assertEqual(snapshot(), before)
        finally:
            admin.close()

    def test_refresh_takes_what_a_visitor_added_by_hand(self):
        """A manual account or a file import made on the shared login is
        household data: the refresh replaces it like any other row, rather
        than refusing and leaving the household stale for good."""
        from oikonome import demo
        tid = self._ensure_seeded()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            for item, agg in (("manual", "manual"), ("csv:visitor", "csv")):
                admin.execute(
                    "INSERT INTO items (id, tenant_id, aggregator, "
                    "institution_name, access_token) "
                    "VALUES (%s, %s, %s, 'Visitor', NULL)", (item, tid, agg))
            admin.execute(
                "INSERT INTO accounts (id, tenant_id, item_id, name, type, "
                "balance_current) VALUES ('manual:visitor', %s, 'manual', "
                "'Visitor cash', 'depository', 12.5)", (tid,))
            admin.commit()
            out = demo.refresh_demo_household(self.VIEWER, years=2, seed=7)
            self.assertTrue(out["refreshed"])
            left = admin.execute(
                "SELECT count(*) AS n FROM items WHERE tenant_id = %s "
                "AND id NOT LIKE 'demo-%%'", (tid,)).fetchone()["n"]
            self.assertEqual(left, 0)
        finally:
            admin.close()

    def test_refresh_refuses_a_login_held_to_the_second_factor_rule(self):
        """A second-factor waiver marks a shared demonstration login; a real
        account, and the household's own owner, are both held to the rule."""
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

    def test_refresh_refuses_an_item_holding_a_credential(self):
        """Whatever its aggregator, an item that holds a key is a live
        link the wipe would orphan upstream."""
        from oikonome import demo
        admin = tenancy.admin_connect()
        try:
            tid = self._ensure_seeded()["tenant_id"]
            admin.execute(
                "INSERT INTO items (id, tenant_id, aggregator, "
                "institution_name, access_token) VALUES ('coinbase-x', %s, "
                "'coinbase', 'Coinbase', 'opaque-key-material')", (tid,))
            admin.commit()
            with self.assertRaises(SystemExit):
                demo.refresh_demo_household(self.VIEWER, years=2)
            admin.execute("DELETE FROM items WHERE id = 'coinbase-x'")
            admin.commit()
        finally:
            admin.close()

    def test_refresh_refuses_an_unknown_address(self):
        from oikonome import demo
        with self.assertRaises(SystemExit):
            demo.refresh_demo_household("nobody@example.com", years=2)


class ARefreshKeepsTheDaysSendStamps(unittest.TestCase):
    def test_job_runs_are_not_wiped(self):
        """The once-a-day send stamps are not generated data: wiped, the
        day's verdict would be sent again after a refresh."""
        from oikonome import demo
        from oikonome.db import tenancy

        from .util import make_db
        make_db().close()             # the test database, its URL set
        admin = tenancy.admin_connect()
        try:
            wiped, _ = demo._household_tables(admin)
        finally:
            admin.close()
        self.assertNotIn("job_runs", wiped)
