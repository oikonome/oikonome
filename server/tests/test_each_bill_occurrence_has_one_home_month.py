"""Every bill occurrence belongs to exactly one month.

An occurrence's home is the month its payment is dated in when it was
paid, and its due month when it was not. That month alone plans it,
expects it and calls it overdue. A closed month therefore looks for the
payment past its own month end: rent due the 30th that posts on the 2nd
is paid, so it is neither planned nor overdue in the month it was due —
the next month, where the cash left, plans it. Read the other way, every
month would plan the rent twice (once unpaid at its month end, once as
the next month's late-paid decoy), call a paid rent overdue, and the
report card would say the bills ran a rent under plan.

The matrix: paid late / early / on time × closed / open month, read
through the Month lens, the Year lens sum and the overdue list.
"""
import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import lenses

from .util import add_bill, add_txn, make_db, write_config

RENT = 1450.0
TODAY = dt.date(2026, 9, 15)       # July and August are closed


def _fixed(st):
    f = st["buckets"]["fixed"]
    return f["month_budget"], f["actual"]


class _Case(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=600)

    def tearDown(self):
        self.conn.close()

    def freeze_closed_months(self):
        """The Year lens judges a closed month only under its snapshot."""
        for m in (7, 8):
            budget.snapshot_month(self.conn, dt.date(2026, m, 20))
        self.conn.commit()

    def month(self, m, today=TODAY):
        return lenses.month_state(self.conn, 2026, m, today)


class PaidLateAcrossMonthEnd(_Case):
    """Due the 30th, every payment posts on the 1st or 2nd."""

    def setUp(self):
        super().setUp()
        add_bill(self.conn, "Harbor Lofts", RENT,
                 next_due=dt.date(2026, 7, 30),
                 last_seen=dt.date(2026, 6, 30))
        for d in (dt.date(2026, 7, 2), dt.date(2026, 8, 1),
                  dt.date(2026, 9, 2)):
            add_txn(self.conn, d, RENT, "HARBOR LOFTS", account="chk",
                    primary="RENT_AND_UTILITIES")
        self.conn.commit()

    def test_a_closed_month_plans_the_rent_once_and_calls_none_overdue(self):
        for m in (7, 8):
            st = self.month(m)
            self.assertEqual(_fixed(st), (RENT, RENT), m)
            self.assertEqual(st["overdue_unpaid"], [], m)

    def test_the_year_lens_plans_one_rent_per_payment(self):
        """July and August each plan the one rent they paid; only the open
        September also carries its own still-unpaid occurrence."""
        self.freeze_closed_months()
        y = lenses.year_summary(self.conn, 2026, TODAY)
        planned = y["buckets_annual"]["fixed"]["month_budget"]
        actual = y["buckets_annual"]["fixed"]["actual"]
        self.assertAlmostEqual(actual, 3 * RENT)
        self.assertAlmostEqual(planned, actual + RENT)

    def test_the_open_month_still_owes_its_own_occurrence(self):
        """Live, the September rent has not been paid yet: it is planned
        and, before its day, not overdue."""
        st = self.month(9)
        self.assertEqual(_fixed(st), (2 * RENT, RENT))
        self.assertEqual(st["overdue_unpaid"], [])

    def test_a_closed_month_whose_payment_has_not_posted_still_owes_it(self):
        """Read on the 1st, before the late payment exists, the month that
        was due still calls it overdue; it moves only once paid."""
        self.conn.execute("DELETE FROM transactions WHERE date = %s",
                          (dt.date(2026, 9, 2),))
        st = self.month(8, today=dt.date(2026, 9, 1))
        self.assertEqual(_fixed(st), (2 * RENT, RENT))
        self.assertEqual([o[0] for o in st["overdue_unpaid"]],
                         ["Harbor Lofts"])
        # …and the open September does not plan it a second time
        sep = self.month(9, today=dt.date(2026, 9, 1))
        self.assertEqual(sep["overdue_unpaid"], [])

    def test_the_variable_side_never_sees_next_months_rows(self):
        st = self.month(7)
        self.assertAlmostEqual(st["buckets"]["other"]["actual"], 0.0)
        self.assertAlmostEqual(st["buckets"]["food"]["actual"], 0.0)


class PaidEarlyAcrossMonthStart(_Case):
    """Due the 1st, every payment posts on the 29th/30th before."""

    def setUp(self):
        super().setUp()
        add_bill(self.conn, "Harbor Lofts", RENT,
                 next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        for d in (dt.date(2026, 6, 29), dt.date(2026, 7, 30),
                  dt.date(2026, 8, 29)):
            add_txn(self.conn, d, RENT, "HARBOR LOFTS", account="chk",
                    primary="RENT_AND_UTILITIES")
        self.conn.commit()

    def test_each_closed_month_plans_the_rent_it_paid(self):
        for m in (7, 8):
            st = self.month(m)
            self.assertEqual(_fixed(st), (RENT, RENT), m)
            self.assertEqual(st["overdue_unpaid"], [], m)

    def test_the_open_month_paid_ahead_owes_nothing(self):
        st = self.month(9)
        self.assertEqual(_fixed(st), (0.0, 0.0))
        self.assertEqual(st["overdue_unpaid"], [])

    def test_the_year_lens_plans_one_rent_per_payment(self):
        self.freeze_closed_months()
        y = lenses.year_summary(self.conn, 2026, TODAY)
        f = y["buckets_annual"]["fixed"]
        self.assertAlmostEqual(f["actual"], 2 * RENT)
        self.assertAlmostEqual(f["month_budget"], f["actual"])


class PaidOnTime(_Case):
    def setUp(self):
        super().setUp()
        add_bill(self.conn, "Harbor Lofts", RENT,
                 next_due=dt.date(2026, 7, 15),
                 last_seen=dt.date(2026, 6, 15))
        for d in (dt.date(2026, 7, 15), dt.date(2026, 8, 15)):
            add_txn(self.conn, d, RENT, "HARBOR LOFTS", account="chk",
                    primary="RENT_AND_UTILITIES")
        self.conn.commit()

    def test_closed_and_open_months_plan_their_own_rent(self):
        for m in (7, 8):
            self.assertEqual(_fixed(self.month(m)), (RENT, RENT), m)
        # due today, not yet paid: planned, not overdue before the day ends
        st = self.month(9)
        self.assertEqual(_fixed(st), (RENT, 0.0))

    def test_an_unpaid_closed_month_is_overdue_there_only(self):
        self.conn.execute("DELETE FROM transactions WHERE date = %s",
                          (dt.date(2026, 8, 15),))
        aug = self.month(8)
        self.assertEqual(_fixed(aug), (RENT, 0.0))
        self.assertEqual([o[0] for o in aug["overdue_unpaid"]],
                         ["Harbor Lofts"])
        # September lists only its own occurrence (due today), never
        # August's
        self.assertEqual([o[2] for o in self.month(9)["overdue_unpaid"]],
                         ["2026-09-15"])


class AnnualBillPrepaidFiveWeeksEarly(_Case):
    """An annual premium due Aug 3, paid Jun 29 — 35 days early, inside the
    long-cycle prepay window, two months before its due month."""

    def setUp(self):
        super().setUp()
        add_bill(self.conn, "Fernhill Mutual", 1200.0, frequency="YEARLY",
                 next_due=dt.date(2026, 8, 3),
                 last_seen=dt.date(2025, 8, 3))
        add_txn(self.conn, dt.date(2026, 6, 29), 1200.0, "FERNHILL MUTUAL",
                account="chk", primary="LOAN_PAYMENTS")
        self.conn.commit()

    def test_the_payments_month_plans_it_and_no_month_counts_it_as_spend(self):
        june = self.month(6)
        self.assertEqual(_fixed(june), (1200.0, 1200.0))
        for m in (7, 8):
            self.assertEqual(self.month(m)["overdue_unpaid"], [], m)
            self.assertEqual(_fixed(self.month(m))[0], 0.0, m)


if __name__ == "__main__":
    unittest.main()
