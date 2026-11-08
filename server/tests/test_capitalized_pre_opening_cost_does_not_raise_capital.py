"""A capitalized pre-opening cost does not raise the capital account.

A cost dated before the business opened is a start-up (or organizational)
cost: the P&L keeps it out of operating expenses because it is deducted
through the start-up allowance instead — the immediate part in the year the
business begins, the rest amortized over 180 months. The capital account's
retained earnings must take that same deduction. Counting only operating
net income, an owner who capitalizes a pre-opening fee they paid personally
gets the contribution credited while the cost it funded is never charged,
so the capital account reads the fee forever.
"""
import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import books, entities, equity, selfemploy

from .util import _admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts


class CapitalizedPreOpeningCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"cap195-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def _entity(self, start):
        return entities.create_entity(
            self.conn, name="Example Studio LLC",
            structure="single_member_llc", business_start_date=start)["id"]

    def _year_end(self, ent, year, today):
        from oikonome.web import api
        return api._year_end_sheet(
            self.conn, ent, year, today,
            books.entity_transactions(self.conn, ent, None))

    def test_a_small_pre_opening_fee_nets_the_capital_account_to_zero(self):
        # a $300 filing fee paid on a personal card before opening, then
        # capitalized: contribution +300, cost wholly deductible the year
        # the business begins (under the $5,000 immediate allowance)
        ent = self._entity(dt.date(2025, 3, 1))
        fee = add_txn(self.conn, dt.date(2025, 1, 10), 300.0,
                      "STATE FILING OFFICE", account="card")
        equity.contribute_expense(self.conn, ent, fee)
        today = dt.date(2026, 7, 15)
        cap = equity.capital_summary(self.conn, ent, today=today)
        self.assertEqual(cap["contributions"], 300.0)
        self.assertEqual(cap["net_income"], -300.0)
        self.assertEqual(cap["capital_balance"], 0.0)
        # the business owns no account, so its assets are zero too: the
        # balance sheet's capital account agrees with assets − liabilities
        sheet = selfemploy.balance_sheet(self.conn, ent)
        self.assertEqual(sheet["net"], 0.0)
        self.assertEqual(sheet["capital_account"], 0.0)
        # and the past year-end sheet in the package says the same
        ye = self._year_end(ent, 2025, today)
        self.assertEqual(ye["capital_account"], 0.0)
        self.assertEqual(ye["net"], 0.0)

    def test_a_fee_before_the_business_begins_stays_in_capital(self):
        # until the business begins nothing is deductible yet: the cost is
        # still carried, so the contribution stands in the capital account
        ent = self._entity(dt.date(2027, 2, 1))
        fee = add_txn(self.conn, dt.date(2026, 5, 4), 300.0,
                      "STATE FILING OFFICE", account="card")
        equity.contribute_expense(self.conn, ent, fee)
        cap = equity.capital_summary(self.conn, ent,
                                     today=dt.date(2026, 7, 15))
        self.assertEqual(cap["capital_balance"], 300.0)

    def test_a_large_start_up_cost_takes_only_the_allowance_so_far(self):
        # $8,000 of pre-opening cost, business begins July 2025: $5,000
        # immediate in 2025, the other $3,000 amortized over 180 months
        # from July 2025 — the capital account is reduced by what has been
        # deducted through the as-of year, not by the whole cost
        ent = self._entity(dt.date(2025, 7, 1))
        cost = add_txn(self.conn, dt.date(2025, 5, 1), 8000.0,
                       "FIT-OUT CONTRACTOR", account="card")
        equity.contribute_expense(self.conn, ent, cost)
        ded = books.startup_deduction(8000.0)
        begins = dt.date(2025, 7, 1)
        y2025 = books.deduction_for_year(
            ded, 2025, begins)["deductible_this_year"]
        y2026 = books.deduction_for_year(
            ded, 2026, begins)["deductible_this_year"]
        self.assertEqual(y2025, 5100.02)           # 5000 + 6 × 16.67
        self.assertEqual(y2026, 200.04)            # 12 × 16.67
        cap = equity.capital_summary(self.conn, ent,
                                     today=dt.date(2026, 7, 15))
        self.assertEqual(cap["capital_balance"],
                         round(8000.0 - y2025 - y2026, 2))
        # the estimate deducts exactly these per-year shares; the capital
        # account must take the same ones, year by year
        for y, share in ((2025, y2025), (2026, y2026)):
            p = books._pnl_core(self.conn, ent, y)
            self.assertEqual(
                p["startup_deduction"]["deductible_this_year"]
                + p["organizational_deduction"]["deductible_this_year"],
                share)
        # a year-end sheet for 2025 counts only 2025's deduction
        ye = self._year_end(ent, 2025, dt.date(2026, 7, 15))
        self.assertEqual(ye["capital_account"], round(8000.0 - y2025, 2))


    def test_a_year_end_sheet_dates_the_opening_from_the_whole_history(self):
        # no start date recorded: the business begins the month after its
        # LATEST pre-opening cost. A 2024 cost and a 2025 cost put the
        # opening in April 2025, so 2024 deducts nothing — and the 2024
        # year-end sheet, which only sees rows up to that year end, must
        # not date the opening from the 2024 cost alone
        ent = self._entity(None)
        first = add_txn(self.conn, dt.date(2024, 6, 10), 3000.0,
                        "WORKSHOP LEASE DEPOSIT", account="card")
        second = add_txn(self.conn, dt.date(2025, 3, 5), 2000.0,
                         "TOOLING SUPPLIER", account="card")
        for t in (first, second):
            equity.contribute_expense(self.conn, ent, t)
            books.classify(self.conn, t, "startup_195")
        today = dt.date(2026, 7, 15)
        p2024 = books._pnl_core(self.conn, ent, 2024)
        self.assertEqual(p2024["startup_deduction"]["begins"], "2025-04-01")
        self.assertEqual(
            p2024["startup_deduction"]["deductible_this_year"], 0.0)
        ye = self._year_end(ent, 2024, today)
        # contributions to date plus 2024's retained earnings (no revenue,
        # no operating cost, no deduction yet)
        self.assertEqual(ye["capital_account"], 3000.0)

    def test_a_year_end_sheet_nets_a_start_up_refund_like_the_year_pnl(self):
        # no start date: a later start-up cost (2025) puts the opening in
        # July 2025, so the deduction year is 2025 and a 2024 refund of the
        # 2023 start-up cost still nets against the start-up total. The
        # 2024 year-end sheet only sees rows up to that year end; judged on
        # those alone the opening would be December 2023 and the refund an
        # operating recovery, crediting capital with income the 2024 P&L
        # never shows
        ent = self._entity(None)
        cost = add_txn(self.conn, dt.date(2023, 11, 15), 3000.0,
                       "BENCH FABRICATOR", account="card")
        refund = add_txn(self.conn, dt.date(2024, 2, 10), -1000.0,
                         "BENCH FABRICATOR", account="card")
        later = add_txn(self.conn, dt.date(2025, 6, 1), 2000.0,
                        "SIGNAGE PRINTER", account="card")
        for t in (cost, later):
            equity.contribute_expense(self.conn, ent, t)
            books.classify(self.conn, t, "startup_195")
        entities.assign_transaction(self.conn, refund, ent)
        p2024 = books._pnl_core(self.conn, ent, 2024)
        self.assertEqual(p2024["startup_deduction"]["begins"], "2025-07-01")
        self.assertEqual(p2024["startup_195"], -1000.0)
        self.assertEqual(p2024["net_operating"], 0.0)
        self.assertEqual(
            p2024["startup_deduction"]["deductible_this_year"], 0.0)
        p2023 = books._pnl_core(self.conn, ent, 2023)
        retained = p2023["net_operating"] + p2024["net_operating"]
        ye = self._year_end(ent, 2024, dt.date(2026, 7, 15))
        # contributions through 2024 (the 2023 cost) plus the retained
        # earnings the 2023 and 2024 P&Ls report — the refund adds nothing
        self.assertEqual(ye["capital_account"], round(3000.0 + retained, 2))
        self.assertEqual(ye["capital_account"], 3000.0)


if __name__ == "__main__":
    unittest.main()
