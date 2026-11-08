"""The Cash Flow picture caches each closed month's fixed total. A write
to the ledger may re-walk only the months whose figures it can reach — a
new charge today must not send the all-time window back over every month
of history — and whatever it serves must be, to the cent, what a cold walk
computes."""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import budget, links, reporting
from oikonome.db import tenancy

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 9, 15)
FIRST = dt.date(2023, 1, 1)


def _months(first: dt.date, stop: dt.date):
    cur = first
    while cur < stop:
        yield cur
        cur = (cur + dt.timedelta(days=32)).replace(day=1)


class CacheFollowsOnlyTheMonthsAWriteReaches(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        reporting._FIXED_MONTH_CACHE.clear()
        self.addCleanup(reporting._FIXED_MONTH_CACHE.clear)
        self.rent: dict[dt.date, str] = {}
        for m in _months(FIRST, TODAY.replace(day=1) + dt.timedelta(days=32)):
            if m > TODAY:
                break
            self.rent[m] = add_txn(self.conn, m.replace(day=10), 1500.0,
                                   "RENT", primary="RENT_AND_UTILITIES",
                                   account="chk")
            add_txn(self.conn, m.replace(day=12), 40.0, "GROCER",
                    primary="FOOD_AND_DRINK")
            add_txn(self.conn, m.replace(day=5), -5000.0, "ACME PAYROLL",
                    primary="INCOME", account="chk")
        add_bill(self.conn, "RENT", 1500.0, next_due=dt.date(2026, 10, 10))
        add_bill(self.conn, "ACME PAYROLL", 5000.0,
                 next_due=dt.date(2026, 10, 5), income=True)

    def refresh(self):
        """What the next request's fresh checkout would do."""
        tenancy.set_entity_scope(self.conn)
        links.set_shadow_scope(self.conn)
        self.conn._memo = None

    def walk(self):
        """flow_breakdown('all') and the closed months it walked."""
        real = budget.month_status
        walked: list[dt.date] = []

        def counting(conn, day, **kw):
            if day < TODAY.replace(day=1):
                walked.append(day.replace(day=1))
            return real(conn, day, **kw)

        with mock.patch.object(budget, "month_status", counting):
            out = reporting.flow_breakdown(self.conn, TODAY, "all")
        return out, walked

    def cold(self):
        reporting._FIXED_MONTH_CACHE.clear()
        out, _ = self.walk()
        return out

    def test_a_write_to_the_current_month_walks_no_closed_month(self):
        self.walk()
        add_txn(self.conn, TODAY, 12.0, "COFFEE", primary="FOOD_AND_DRINK")
        self.conn.execute(
            "INSERT INTO manual_categories (transaction_id, category) "
            "VALUES (%s, 'FOOD_AND_DRINK')", (self.rent[TODAY.replace(day=1)],))
        self.refresh()
        served, walked = self.walk()
        # only the closed months whose own match reaches today's month: a
        # closed month is matched through the month after it plus the
        # prepay window (which runs into the month after that), so the
        # last three may move with today's row
        floor = reporting._months_back(TODAY.replace(day=1), 3)
        self.assertTrue(all(m >= floor for m in walked),
                        f"older closed months re-walked for today's row: {walked}")
        self.assertEqual(served, self.cold())

    def test_withdrawing_the_other_leg_of_a_reimbursement_misses_both_months(self):
        """January's rent is half reimbursed by a deposit in February; the
        bank then withdraws the deposit. Only February's row changed, but
        January's net rent moved: the served picture must be the cold one."""
        jan = dt.date(2025, 1, 1)
        dep = add_txn(self.conn, dt.date(2025, 2, 3), -750.0, "LANDLORD",
                      primary="TRANSFER_IN", account="chk")
        self.conn.execute(
            "INSERT INTO reimbursements (expense_id, reimburse_id, partial, "
            "amount) VALUES (%s, %s, 1, 750)", (self.rent[jan], dep))
        self.refresh()
        self.walk()
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s",
                          (dep,))
        self.refresh()
        served, walked = self.walk()
        self.assertIn(jan, walked)
        self.assertEqual(served, self.cold())

    def test_a_write_during_the_walk_files_nothing(self):
        """A charge re-categorised while the walk runs (and put back after)
        brings the stamps back to the values read before the walk: what the
        walk computed with it in must not be filed under them."""
        real = budget.month_status
        old = dt.date(2024, 3, 1)
        done = []

        def writing(conn, day, **kw):
            if not done:
                done.append(1)
                conn.execute(
                    "INSERT INTO manual_categories (transaction_id, category) "
                    "VALUES (%s, 'FOOD_AND_DRINK')", (self.rent[old],))
            return real(conn, day, **kw)

        with mock.patch.object(budget, "month_status", writing):
            reporting.flow_breakdown(self.conn, TODAY, "all")
        self.assertEqual(
            [k for k in reporting._FIXED_MONTH_CACHE], [],
            "the walk filed months under stamps read before its own write")

    def test_a_write_to_an_old_month_walks_only_the_months_it_reaches(self):
        before, _ = self.walk()
        old = dt.date(2024, 3, 1)
        # the March 2024 rent leaves the ledger: that month's fixed total
        # really moves
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s",
                          (self.rent[old],))
        self.refresh()
        served, walked = self.walk()
        # March itself, the months before it whose closed match reaches
        # into it (through the month after plus the prepay window), the
        # two months whose prepay window looks back into it, and the rest
        # of 2024 (an annual envelope's pool reads the calendar year to
        # date) — never earlier than Dec 2023, never 2025 on
        self.assertTrue(walked, "the edited month was served from the cache")
        self.assertIn(old, walked)
        self.assertTrue(all(dt.date(2023, 12, 1) <= m <= dt.date(2024, 12, 1)
                            for m in walked), walked)
        self.assertNotEqual(before["out"]["fixed"], served["out"]["fixed"])
        self.assertEqual(served, self.cold())

    def test_what_hangs_off_an_old_charge_misses_its_month(self):
        """A split, a category override and a reimbursement are rows of
        their own tables, dated by the charge they hang off."""
        old = dt.date(2023, 6, 1)
        rid = self.rent[old]
        deposit = add_txn(self.conn, dt.date(2023, 6, 20), -1500.0,
                          "LANDLORD REFUND", primary="TRANSFER_IN",
                          account="chk")
        changes = [
            lambda: self.conn.execute(
                "INSERT INTO transaction_splits (txn_id, line, category, amount)"
                " VALUES (%s, 1, 'RENT_AND_UTILITIES', 1000), "
                "(%s, 2, 'FOOD_AND_DRINK', 500)", (rid, rid)),
            lambda: self.conn.execute(
                "INSERT INTO manual_categories (transaction_id, category) "
                "VALUES (%s, 'TRANSFER_OUT')", (rid,)),
            lambda: self.conn.execute(
                "INSERT INTO reimbursements (expense_id, reimburse_id) "
                "VALUES (%s, %s)", (rid, deposit)),
        ]
        for i, change in enumerate(changes):
            with self.subTest(change=i):
                self.refresh()
                self.walk()
                change()
                self.refresh()
                served, walked = self.walk()
                self.assertIn(old, walked)
                self.assertEqual(served, self.cold())

    def test_moving_a_charge_between_months_misses_both(self):
        self.walk()
        self.conn.execute("UPDATE transactions SET date=%s WHERE id=%s",
                          (dt.date(2025, 2, 10), self.rent[dt.date(2023, 2, 1)]))
        self.refresh()
        served, walked = self.walk()
        self.assertIn(dt.date(2023, 2, 1), walked)
        self.assertIn(dt.date(2025, 2, 1), walked)
        self.assertEqual(served, self.cold())


if __name__ == "__main__":
    unittest.main()
