"""First-boot claim, and savings goals staying scoped to their tenant."""

import datetime as dt
import os
import threading
import unittest
import uuid

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, make_db, write_config

PW = "correct-horse-battery"


def _control():
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class FirstBootClaimTests(unittest.TestCase):
    """/api/signup must not hand out ownership of a self-hosted box, and two
    racers must not both claim one email."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    # ---- the install window ------------------------------------------------
    def test_signup_403_on_unclaimed_selfhost(self):
        """Zero users, operator hasn't opened the printed setup link yet.
        A gate that only fires on a CLAIMED instance lets signup answer
        during the install window, so the first visitor to reach the port
        becomes owner — OIKONOME_SETUP_TOKEN gates /setup alone."""
        from oikonome.web import setup as setup_mod
        self.appmod.DEV_MODE = False                        # a real self-host box
        orig = setup_mod.instance_has_users
        # the suite shares one control plane with many users; make it look
        # like the fresh box this window belongs to
        setup_mod.instance_has_users = lambda conn: False
        try:
            c = TestClient(self.app)
            r = c.post("/api/signup", data={
                "email": f"squatter-{uuid.uuid4().hex[:8]}@evil.dev",
                "password": PW})
            self.assertEqual(r.status_code, 403, r.text)
            self.assertNotIn("set-cookie", r.headers)       # no session handed out
        finally:
            setup_mod.instance_has_users = orig
            self.appmod.DEV_MODE = True

    def test_selfhost_signup_403_when_claimed_too(self):
        """Claimed or not, off-hosted signup is closed."""
        self.appmod.DEV_MODE = False
        try:
            c = TestClient(self.app)
            r = c.post("/api/signup", data={
                "email": f"attacker-{uuid.uuid4().hex[:8]}@evil.dev",
                "password": PW})
            self.assertEqual(r.status_code, 403, r.text)
        finally:
            self.appmod.DEV_MODE = True
        # hosted/dev signup is untouched
        c2 = TestClient(self.app)
        r2 = c2.post("/api/signup", data={
            "email": f"ok-{uuid.uuid4().hex[:8]}@example.dev", "password": PW})
        self.assertEqual(r2.status_code, 200, r2.text)

    # ---- concurrent claim --------------------------------------------------
    def test_concurrent_signup_same_email_one_winner(self):
        """Without the atomic claim both racers pass the exists-check, both
        mint a tenant, and the loser hits the users.email UNIQUE index — a
        500 plus an orphaned tenant. Argon2 hashing sits inside the window,
        so the race is wide open."""
        email = f"race-{uuid.uuid4().hex[:10]}@example.dev"
        codes = []
        guard = threading.Lock()
        start = threading.Barrier(2)

        def claim():
            c = TestClient(self.app, raise_server_exceptions=False)
            start.wait(timeout=10)
            r = c.post("/api/signup", data={"email": email, "password": PW})
            with guard:
                codes.append(r.status_code)

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(sorted(codes), [200, 409], codes)
        conn = _control()
        try:
            users = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE email=%s",
                (email,)).fetchone()["n"]
            # create_tenant stamps the email as the tenant name, so a loser
            # that minted one before failing shows up right here
            tenants = conn.execute(
                "SELECT COUNT(*) AS n FROM tenants WHERE name=%s",
                (email,)).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(users, 1)
        self.assertEqual(tenants, 1, "losing racer orphaned a tenant")


class UnscopedSavingsGoalTests(unittest.TestCase):
    """A goal with no account_id and no tokens must not measure progress
    against the whole ledger: `_matched_net` unscoped sums EVERY transfer
    row, so a planner-created plan-only goal would report savings and a
    monthly rate composed entirely of unrelated transfers."""

    TODAY = dt.date(2026, 7, 15)

    def setUp(self):
        self.conn = make_db()
        # transfers that are NOT savings contributions (Plaid signs:
        # negative = money in)
        add_txn(self.conn, dt.date(2026, 6, 20), -900.0, "CC PAYMENT INBOUND",
                primary="TRANSFER_IN")
        add_txn(self.conn, dt.date(2026, 7, 1), 400.0, "WIRE OUT",
                primary="TRANSFER_OUT")

    def tearDown(self):
        self.conn.close()

    def _progress(self, goal):
        from oikonome.engine import savings
        write_config(self.conn, savings_goals=[goal])
        cfg_row = {"savings_goals": [goal]}
        return savings.progress(self.conn, cfg_row, self.TODAY)[0]

    def test_plan_only_goal_reports_no_progress(self):
        p = self._progress({"name": "Savings", "target": 0, "tokens": [],
                            "monthly_plan": 1500.0, "start_balance": 0})
        self.assertTrue(p["plan_only"])
        self.assertIsNone(p["saved"])
        self.assertIsNone(p["rate_90d"])
        self.assertIsNone(p["pct"])
        # the PLAN itself still stands — surplus and forecast keep using it
        self.assertEqual(p["monthly_plan"], 1500.0)

    def test_token_scoped_goal_still_measures(self):
        add_txn(self.conn, dt.date(2026, 7, 5), -250.0, "TRIP FUND TRANSFER",
                primary="TRANSFER_IN")
        p = self._progress({"name": "Trip", "target": 1000,
                            "tokens": ["trip fund"], "monthly_plan": 0,
                            "start_balance": 0})
        self.assertFalse(p["plan_only"])
        self.assertEqual(p["saved"], 250.0)

    def test_whitespace_tokens_are_still_plan_only(self):
        p = self._progress({"name": "Savings", "target": 0,
                            "tokens": ["  ", ""], "monthly_plan": 100.0,
                            "start_balance": 0})
        self.assertTrue(p["plan_only"])
        self.assertIsNone(p["saved"])

    def test_milestones_skip_plan_only_goals(self):
        from oikonome.engine import savings
        cfg = {"savings_goals": [{"name": "Savings", "target": 0,
                                  "tokens": [], "monthly_plan": 1500.0}]}
        msgs, state = savings.check_milestones(self.conn, cfg, self.TODAY)
        self.assertEqual(msgs, [])


if __name__ == "__main__":
    unittest.main()
