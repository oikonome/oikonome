"""Income series run the occurrence engine.

The Recurring page's received column never reacted to a posted paycheck:
budget._recurring_bills loaded only amount<0 rows and _month_bill_stats
matched against _spend_rows (outflows), so an income row always carried
_EMPTY_BILL_STATS — "·" forever, even the day the deposit landed. Income
occurrences now match against posted non-transfer inflows on depository
accounts (budget._inflow_rows), and the SPA shows a "received" pill.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, budget
from oikonome.engine.compat import as_dict
from oikonome.web.api import _month_bill_stats

from .util import TODAY, add_bill, add_txn, make_db, write_config

PAYCHECK = "NORTHWIND HEALTH NORTHWIND~ Future Amount: 4200.0 ~ Tran: DDIR"


class IncomeReceivedTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Northwind Health", 4100.0,
                 frequency="WEEKLY", interval=2, next_due=TODAY,
                 last_seen=TODAY - dt.timedelta(days=14), income=True)
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _stats(self):
        return _month_bill_stats(self.conn, self.cfg, TODAY).get(
            "Northwind Health")

    def test_posted_paycheck_marks_received(self):
        """THE reported case: deposit posted on the due date — the row must
        show received, not '·'."""
        add_txn(self.conn, TODAY, -4200.00, PAYCHECK, account="chk",
                primary="INCOME")
        st = self._stats()
        self.assertIsNotNone(st, "income row absent from occurrence stats")
        self.assertGreaterEqual(st["occurrences"], 1)
        self.assertEqual(st["paid"], 1)
        self.assertAlmostEqual(st["paid_amount"], 4200.00, places=2)

    def test_unposted_paycheck_counts_occurrence_unpaid(self):
        st = self._stats()
        self.assertIsNotNone(st)
        self.assertGreaterEqual(st["occurrences"], 1)
        self.assertEqual(st["paid"], 0)

    def test_transfer_in_is_not_a_paycheck(self):
        # a savings→checking move of paycheck size must not mark it received
        add_txn(self.conn, TODAY, -4100.0,
                "Northwind Health transfer from savings", account="chk",
                primary="TRANSFER_IN")
        st = self._stats()
        self.assertEqual(st["paid"], 0)

    def test_credit_account_inflow_ignored(self):
        # a card refund with matching tokens is not income
        add_txn(self.conn, TODAY, -4100.0, PAYCHECK, account="card",
                primary="INCOME")
        st = self._stats()
        self.assertEqual(st["paid"], 0)

    def test_outflow_bills_unaffected(self):
        # a regular bill still matches spend rows exactly as before
        add_bill(self.conn, "Northside Power", 180.0, frequency="MONTHLY",
                 next_due=TODAY, last_seen=TODAY - dt.timedelta(days=30))
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 180.0,
                "NORTHSIDE POWER payment", account="chk",
                primary="UTILITIES")
        st = _month_bill_stats(self.conn, self.cfg, TODAY).get(
            "Northside Power")
        self.assertIsNotNone(st)
        self.assertEqual(st["paid"], 1)


class PaydayRollTests(unittest.TestCase):
    """The Next date rolls to the real next occurrence the moment the
    deposit posts — not at the nightly pass."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Northwind Health", 4100.0,
                 frequency="WEEKLY", interval=2, next_due=TODAY,
                 last_seen=TODAY - dt.timedelta(days=14), income=True)

    def tearDown(self):
        self.conn.close()

    def _row(self):
        r = self.conn.execute(
            "SELECT due_on, raw FROM bills "
            "WHERE payee='Northwind Health'").fetchone()
        return r["due_on"], as_dict(r["raw"])

    def test_deposit_on_payday_rolls_due_on(self):
        add_txn(self.conn, TODAY, -4200.00, PAYCHECK, account="chk",
                primary="INCOME")
        self.assertEqual(
            bills.advance_received_income(self.conn, TODAY), 1)
        due, raw = self._row()
        self.assertEqual(due, TODAY + dt.timedelta(days=14))
        self.assertEqual(raw["lastDueOn"], TODAY.isoformat())
        # idempotent: the rolled date is in the future, nothing re-fires
        self.assertEqual(
            bills.advance_received_income(self.conn, TODAY), 0)

    def test_early_deposit_rolls_on_the_due_date(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=2), -4100.0, PAYCHECK,
                account="chk", primary="INCOME")
        self.assertEqual(
            bills.advance_received_income(self.conn, TODAY), 1)
        self.assertEqual(self._row()[0], TODAY + dt.timedelta(days=14))

    def test_no_deposit_keeps_todays_date(self):
        self.assertEqual(
            bills.advance_received_income(self.conn, TODAY), 0)
        self.assertEqual(self._row()[0], TODAY)

    def test_transfer_does_not_roll(self):
        add_txn(self.conn, TODAY, -4100.0,
                "Northwind Health transfer from savings", account="chk",
                primary="TRANSFER_IN")
        self.assertEqual(
            bills.advance_received_income(self.conn, TODAY), 0)

    def test_bills_never_roll_here(self):
        add_bill(self.conn, "Northside Power", 180.0, frequency="MONTHLY",
                 next_due=TODAY, last_seen=TODAY - dt.timedelta(days=30))
        add_txn(self.conn, TODAY, 180.0, "NORTHSIDE POWER payment",
                account="chk", primary="UTILITIES")
        bills.advance_received_income(self.conn, TODAY)
        r = self.conn.execute("SELECT due_on FROM bills "
                              "WHERE payee='Northside Power'").fetchone()
        self.assertEqual(r["due_on"], TODAY)


class BoundaryDepositStatsTests(unittest.TestCase):
    """A paycheck posted in the closing days of the PREVIOUS month, for an
    occurrence due on or after the 1st, must mark it received — a
    month-start-only inflow window leaves it '·' forever. The lookback is
    PREPAY_MATCH_DAYS with last month's occurrences as decoys, the same
    shape upcoming_bill_occurrences gives prepaid bills."""

    DUE = TODAY.replace(day=2)                      # 2026-07-02, biweekly

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Northwind Health", 4100.0,
                 frequency="WEEKLY", interval=2, next_due=self.DUE,
                 last_seen=self.DUE - dt.timedelta(days=14), income=True)
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _stats(self):
        return _month_bill_stats(self.conn, self.cfg, TODAY).get(
            "Northwind Health")

    def test_prior_month_deposit_marks_occurrence_received(self):
        # posted June 29 for the July 2 occurrence — 3 days early, across
        # the month boundary
        add_txn(self.conn, self.DUE - dt.timedelta(days=3), -4200.00,
                PAYCHECK, account="chk", primary="INCOME")
        st = self._stats()
        self.assertIsNotNone(st)
        self.assertEqual(st["paid"], 1)
        # one deposit claims exactly one occurrence — July 16 stays unpaid
        self.assertEqual(st["next_unpaid"],
                         (self.DUE + dt.timedelta(days=14)).isoformat())

    def test_prior_occurrence_deposit_stays_on_its_own(self):
        # June 18's own paycheck (14 days from July 2 — outside the
        # half-cycle window) must not claim the July occurrence
        add_txn(self.conn, self.DUE - dt.timedelta(days=14), -4100.0,
                PAYCHECK, account="chk", primary="INCOME")
        st = self._stats()
        self.assertEqual(st["paid"], 0)

    def test_boundary_tie_goes_to_the_decoy(self):
        # June 25 is exactly the window (7 days) from BOTH June 18 and
        # July 2 — the prior-month decoy must win the tie so the July
        # occurrence is not eaten by last month's late paycheck
        add_txn(self.conn, self.DUE - dt.timedelta(days=7), -4100.0,
                PAYCHECK, account="chk", primary="INCOME")
        st = self._stats()
        self.assertEqual(st["paid"], 0)


class MultiCycleRollTests(unittest.TestCase):
    """A row multiple cycles behind (missed nightly runs) rolls ONE cycle
    per matching deposit — one paycheck must not vault the schedule over
    intermediate dues to the first future occurrence."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Northwind Health", 4100.0,
                 frequency="WEEKLY", interval=2,
                 next_due=TODAY - dt.timedelta(days=28),
                 last_seen=TODAY - dt.timedelta(days=42), income=True)

    def tearDown(self):
        self.conn.close()

    def _row(self):
        r = self.conn.execute(
            "SELECT due_on, raw FROM bills "
            "WHERE payee='Northwind Health'").fetchone()
        return r["due_on"], as_dict(r["raw"])

    def test_one_deposit_advances_one_cycle(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=28), -4100.0, PAYCHECK,
                account="chk", primary="INCOME")
        self.assertEqual(
            bills.advance_received_income(self.conn, TODAY), 1)
        due, raw = self._row()
        # today-14, NOT today or today+14 — the intermediate due survives
        self.assertEqual(due, TODAY - dt.timedelta(days=14))
        self.assertEqual(raw["lastDueOn"],
                         (TODAY - dt.timedelta(days=28)).isoformat())
        # no deposit evidence for today-14 → the schedule holds there
        self.assertEqual(
            bills.advance_received_income(self.conn, TODAY), 0)
        self.assertEqual(self._row()[0], TODAY - dt.timedelta(days=14))

    def test_each_cycles_own_deposit_advances_again(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=28), -4100.0, PAYCHECK,
                account="chk", primary="INCOME")
        bills.advance_received_income(self.conn, TODAY)
        add_txn(self.conn, TODAY - dt.timedelta(days=14), -4100.0, PAYCHECK,
                account="chk", primary="INCOME")
        self.assertEqual(
            bills.advance_received_income(self.conn, TODAY), 1)
        due, raw = self._row()
        self.assertEqual(due, TODAY)
        self.assertEqual(raw["lastDueOn"],
                         (TODAY - dt.timedelta(days=14)).isoformat())


class ViewerRollGateTests(unittest.TestCase):
    """A family viewer's GET /api/bills must not mutate due_on — the payday
    roll runs only for non-viewer sessions."""

    @classmethod
    def setUpClass(cls):
        import os
        import uuid
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db, seed_accounts
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from fastapi.testclient import TestClient
        from oikonome.db import tenancy
        cls.owner = TestClient(appmod.app)
        cls.owner.post("/api/signup", data={
            "email": f"roll-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        # the API path uses the real clock — fixture pinned to real today
        cls.today0 = dt.date.today()
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            add_bill(conn, "Northwind Health", 4100.0,
                     frequency="WEEKLY", interval=2, next_due=cls.today0,
                     last_seen=cls.today0 - dt.timedelta(days=14),
                     income=True)
            add_txn(conn, cls.today0, -4200.00, PAYCHECK, account="chk",
                    primary="INCOME")
        finally:
            conn.close()
        inv = cls.owner.post("/api/invites", json={"label": "fam", "password": "correct-horse-battery"}).json()
        token = inv["url"].rsplit("token=", 1)[1]
        cls.viewer = TestClient(appmod.app)
        r = cls.viewer.post("/api/invite/claim", json={
            "token": token,
            "email": f"fam-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        assert r.status_code == 200, r.text

    def _due(self):
        from oikonome.db import tenancy
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT due_on FROM bills "
                "WHERE payee='Northwind Health'").fetchone()["due_on"]
        finally:
            conn.close()

    def test_viewer_load_reads_only_then_owner_rolls(self):
        self.assertEqual(self.viewer.get("/api/me").json()["role"], "viewer")
        r = self.viewer.get("/api/bills")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._due(), self.today0)      # untouched
        r = self.owner.get("/api/bills")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._due(), self.today0 + dt.timedelta(days=14))


if __name__ == "__main__":
    unittest.main()
