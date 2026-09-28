"""A vendor refund lands on the bucket of the cost it reverses.

A refund reverses a cost, so it belongs to that cost's bucket — a
pre-opening laptop returned after the business opened takes the §195
start-up total back down, it does not reduce operating expenses. Bucketed
by its OWN date instead, it lowers operating expenses (and opens a
negative Schedule C line with no expense beside it) while the start-up
total keeps a cost that came back: taxable profit overstated by the whole
refund, the one-time allowance judged on a figure too high.

The reversed cost is found by origin: the same merchant's charge on or
before the refund, the exact amount preferred, else the most recent one,
within a year. A refund the user classified keeps that class; one with no
matching charge falls back to the date rule. A refund of an
organizational or start-up cost that arrives after the year the deduction
is taken is a recovery in its own year (operating), so the deduction
already filed for the earlier year never moves. Every reader agrees — the
year and lifetime P&L, the §195/§248 lifetime scan, the Schedule C line
and the worksheet row's `effective_bucket`.
"""
import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import books, entities, selfemploy

from .util import _admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts


class RefundOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"rfo-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Acme LLC", structure="sole_prop",
            business_start_date="2026-03-01")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('biz','it1','Biz Checking','depository','checking',1000)")
        entities.assign_account(self.conn, "biz", self.ent["id"])
        self.eid = self.ent["id"]

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def _txn(self, day, amount, name, txn_id, primary="GENERAL_MERCHANDISE"):
        add_txn(self.conn, day, amount, name, account="biz",
                primary=primary, txn_id=txn_id)

    def test_refund_of_a_startup_cost_after_opening_reverses_startup(self):
        self._txn(dt.date(2026, 2, 20), 3000.0, "LAPTOP STORE", "lap")
        self._txn(dt.date(2026, 3, 10), -3000.0, "LAPTOP STORE", "lap-back")
        for p in (books.pnl(self.conn, self.eid, 2026),
                  books.pnl(self.conn, self.eid)):
            self.assertEqual(p["operating_expenses"], 0.0)
            self.assertEqual(p["net_operating"], 0.0)
            self.assertEqual(p["operating_by_line"], {})
            self.assertEqual(p["startup_195"], 0.0)
            self.assertEqual(p["startup_deduction"]["total"], 0.0)

    def test_refund_of_an_organizational_cost_reverses_organizational(self):
        self._txn(dt.date(2026, 3, 5), 1000.0, "LAW FIRM", "form",
                  primary="GENERAL_SERVICES")
        books.classify(self.conn, "form", "organizational")
        self._txn(dt.date(2026, 3, 20), -400.0, "LAW FIRM", "form-back",
                  primary="GENERAL_SERVICES")
        p = books.pnl(self.conn, self.eid, 2026)
        self.assertEqual(p["organizational"], 600.0)
        self.assertEqual(p["organizational_deduction"]["total"], 600.0)
        self.assertEqual(p["operating_expenses"], 0.0)
        self.assertEqual(p["operating_by_line"], {})

    def test_refund_in_a_later_year_finds_its_cost_in_the_earlier_one(self):
        """The year-scoped P&L sees a cost in last year's books: an
        operating refund in January still reaches its December charge
        (the line test below), and a start-up cost refunded in the year
        after the deduction was taken is recognised as that cost — and
        counted as a recovery, not re-bucketed by its date as orphan
        money."""
        self._txn(dt.date(2026, 2, 20), 3000.0, "LAPTOP STORE", "lap")
        self._txn(dt.date(2027, 1, 10), -3000.0, "LAPTOP STORE", "lap-back")
        p = books.pnl(self.conn, self.eid, 2027)
        self.assertEqual(p["startup_195"], 0.0)
        self.assertEqual(p["startup_deduction"]["total"], 3000.0)
        self.assertEqual(p["operating_expenses"], -3000.0)
        self.assertEqual(p["net_operating"], 3000.0)

    def test_a_later_year_refund_leaves_the_filed_deduction_alone(self):
        """A refund of a start-up cost that arrives after the year the
        §195 deduction was taken must not rewrite that year: the earlier
        figure is already on a filed return. It is a recovery in its own
        year instead, so that year's taxable profit rises by it — lost
        otherwise, understating the later year's profit and estimate."""
        self._txn(dt.date(2026, 2, 20), 3000.0, "LAPTOP STORE", "lap")
        self._txn(dt.date(2026, 6, 1), -20000.0, "CLIENT", "rev26",
                  primary="INCOME")
        self._txn(dt.date(2027, 6, 1), -20000.0, "CLIENT", "rev27",
                  primary="INCOME")
        self._txn(dt.date(2027, 1, 10), -3000.0, "LAPTOP STORE", "lap-back")
        p26 = books.pnl(self.conn, self.eid, 2026)
        self.assertEqual(
            p26["startup_deduction"]["deductible_this_year"], 3000.0)
        self.assertEqual(
            selfemploy.estimated_tax(self.conn, self.eid, 2026)
            ["taxable_profit"], 17000.0)
        p27 = books.pnl(self.conn, self.eid, 2027)
        self.assertEqual(p27["net_operating"], 23000.0)
        self.assertEqual(
            p27["startup_deduction"]["deductible_this_year"], 0.0)
        self.assertEqual(
            selfemploy.estimated_tax(self.conn, self.eid, 2027)
            ["taxable_profit"], 23000.0)
        rows = {r["id"]: r for r in
                books.entity_transactions(self.conn, self.eid, 2027)}
        self.assertEqual(rows["lap-back"]["effective_bucket"], "operating")

    def test_a_later_year_refund_leaves_earlier_amortization_alone(self):
        """Above the $5,000 immediate allowance the rest amortizes; a
        refund after the deduction year must not shrink the amortization
        already shown for the earlier year."""
        self._txn(dt.date(2026, 2, 20), 8600.0, "LAPTOP STORE", "lap")
        self._txn(dt.date(2027, 2, 1), -8600.0, "LAPTOP STORE", "lap-back")
        ded = books.pnl(self.conn, self.eid, 2026)["startup_deduction"]
        self.assertEqual(ded["total"], 8600.0)
        self.assertEqual(ded["immediate"], 5000.0)
        self.assertEqual(ded["amortization_this_year"], 200.0)

    def test_a_refund_before_the_deduction_year_ends_still_nets(self):
        """Inside the year the deduction is taken the refund reduces the
        start-up total it reverses — nothing has been filed yet."""
        self._txn(dt.date(2026, 2, 20), 3000.0, "LAPTOP STORE", "lap")
        self._txn(dt.date(2026, 12, 31), -1000.0, "LAPTOP STORE", "lap-back")
        p = books.pnl(self.conn, self.eid, 2026)
        self.assertEqual(p["startup_195"], 2000.0)
        self.assertEqual(p["startup_deduction"]["deductible_this_year"],
                         2000.0)
        self.assertEqual(p["operating_expenses"], 0.0)
        rows = {r["id"]: r for r in
                books.entity_transactions(self.conn, self.eid, 2026)}
        self.assertEqual(rows["lap-back"]["effective_bucket"], "startup_195")

    def test_the_exact_amount_picks_which_cost_came_back(self):
        """One merchant, a pre-opening charge and a later operating one:
        each refund goes back to the charge it matches."""
        self._txn(dt.date(2026, 2, 20), 3000.0, "LAPTOP STORE", "lap")
        self._txn(dt.date(2026, 3, 5), 50.0, "LAPTOP STORE", "cable")
        books.classify(self.conn, "cable", "operating",
                       sched_c_line="Supplies")
        self._txn(dt.date(2026, 3, 10), -3000.0, "LAPTOP STORE", "lap-back")
        self._txn(dt.date(2026, 3, 12), -50.0, "LAPTOP STORE", "cable-back")
        p = books.pnl(self.conn, self.eid, 2026)
        self.assertEqual(p["startup_195"], 0.0)
        self.assertEqual(p["operating_expenses"], 0.0)
        self.assertEqual(p["operating_by_line"], {})

    def test_an_operating_refund_takes_its_costs_line_across_years(self):
        self._txn(dt.date(2026, 12, 20), 200.0, "CHAIR SHOP", "chair")
        books.classify(self.conn, "chair", "operating",
                       sched_c_line="Supplies")
        self._txn(dt.date(2026, 12, 21), 80.0, "INK CO", "ink")
        self._txn(dt.date(2027, 1, 5), -200.0, "CHAIR SHOP", "chair-back")
        p = books.pnl(self.conn, self.eid, 2027)
        self.assertEqual(p["operating_by_line"], {"Supplies": -200.0})

    def test_a_refund_the_user_classified_keeps_its_class(self):
        self._txn(dt.date(2026, 2, 20), 3000.0, "LAPTOP STORE", "lap")
        self._txn(dt.date(2026, 3, 10), -3000.0, "LAPTOP STORE", "lap-back")
        books.classify(self.conn, "lap-back", "operating")
        p = books.pnl(self.conn, self.eid, 2026)
        self.assertEqual(p["startup_195"], 3000.0)
        self.assertEqual(p["operating_expenses"], -3000.0)

    def test_a_refund_with_no_matching_cost_falls_back_to_its_date(self):
        self._txn(dt.date(2026, 3, 10), -120.0, "UNKNOWN SHOP", "orphan")
        p = books.pnl(self.conn, self.eid, 2026)
        self.assertEqual(p["operating_expenses"], -120.0)
        self.assertEqual(p["startup_195"], 0.0)

    def test_a_cost_more_than_a_year_back_is_not_the_one_refunded(self):
        self._txn(dt.date(2025, 1, 5), 3000.0, "LAPTOP STORE", "lap")
        self._txn(dt.date(2026, 3, 10), -3000.0, "LAPTOP STORE", "lap-back")
        p = books.pnl(self.conn, self.eid, 2026)
        self.assertEqual(p["operating_expenses"], -3000.0)

    def test_the_worksheet_row_carries_the_bucket_the_pnl_used(self):
        """Clients that total a period from the worksheet rows read the
        same bucket the server P&L used, so a refund is not re-bucketed by
        its date on the client."""
        self._txn(dt.date(2026, 2, 20), 3000.0, "LAPTOP STORE", "lap")
        self._txn(dt.date(2026, 3, 10), -3000.0, "LAPTOP STORE", "lap-back")
        self._txn(dt.date(2026, 3, 11), -900.0, "CLIENT", "rev",
                  primary="INCOME")
        self._txn(dt.date(2026, 3, 12), 40.0, "INK CO", "ink")
        for year in (2026, None):
            rows = {r["id"]: r for r in
                    books.entity_transactions(self.conn, self.eid, year)}
            self.assertEqual(rows["lap-back"]["effective_bucket"],
                             "startup_195")
            self.assertEqual(rows["lap"]["effective_bucket"], "startup_195")
            self.assertEqual(rows["ink"]["effective_bucket"], "operating")
            self.assertIsNone(rows["rev"]["effective_bucket"])


if __name__ == "__main__":
    unittest.main()
