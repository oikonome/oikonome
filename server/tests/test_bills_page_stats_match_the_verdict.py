"""The Bills page's per-bill stats and the month verdict must agree.

`_month_bill_stats` feeds the Bills page (Paid x/y, next date, envelope
Used/left) and `budget.month_status` feeds Today, the email and the month
lens. They are two readings of ONE ledger against ONE schedule, so a bill
the verdict calls settled cannot be overdue on the Bills page, and an
annual envelope's year-to-date pool cannot be spent on one page and
untouched on the other. Whenever they diverge the household is told two
different things about the same money and neither page can be trusted.
"""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web.api import _month_bill_stats

from .util import add_bill, add_txn, make_db, write_config


class AnnualPoolAgreesWithTheVerdict(unittest.TestCase):
    """A category-matched annual envelope and a bill in the same category:
    the rows the bill's occurrences claimed are the BILL's, in the
    year-to-date lookback exactly as in the current month. Both pages must
    say so — otherwise the Bills page reports a pool drained by charges the
    verdict already attributed to the bill.
    """

    TODAY = dt.date(2026, 7, 15)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Services", 1200.0, bill_type="envelope",
                 frequency=None, interval=12, category="GENERAL_SERVICES",
                 match_category=True)
        add_bill(self.conn, "City Power", 100.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 1, 1))
        for m in range(1, 8):           # January–July, one per month
            add_txn(self.conn, dt.date(2026, m, 1), 100.0, "CITY POWER",
                    primary="GENERAL_SERVICES")
        add_txn(self.conn, self.TODAY, 50.0, "HANDYMAN",
                primary="GENERAL_SERVICES")
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_pool_drawn_matches_the_month_status_envelope(self):
        st = budget.month_status(self.conn, self.TODAY)
        verdict = st["buckets"]["fixed"]["envelopes"][0]
        page = _month_bill_stats(self.conn, self.cfg, self.TODAY)["Services"]
        self.assertAlmostEqual(page["period_used"], verdict["period_used"],
                               places=2)
        self.assertAlmostEqual(page["period_left"], verdict["period_left"],
                               places=2)
        # and the shared answer is the right one: only the handyman drew
        self.assertAlmostEqual(page["period_used"], 50.0, places=2)
        self.assertAlmostEqual(page["period_left"], 1150.0, places=2)


class DisabledBillDoesNotDrainThePool(unittest.TestCase):
    """A bill the household DISABLED is out of the budget, so its charges
    are nobody's occurrence — the envelope that category-matches them draws
    on them, on both pages.
    """

    TODAY = dt.date(2026, 7, 15)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, disabled_bills=["City Power"])
        add_bill(self.conn, "Services", 1200.0, bill_type="envelope",
                 frequency=None, interval=12, category="GENERAL_SERVICES",
                 match_category=True)
        add_bill(self.conn, "City Power", 100.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 1, 1))
        for m in range(1, 8):           # January–July, this month included
            add_txn(self.conn, dt.date(2026, m, 1), 100.0, "CITY POWER",
                    primary="GENERAL_SERVICES")
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_disabled_bill_charges_draw_the_pool_on_both_pages(self):
        st = budget.month_status(self.conn, self.TODAY)
        verdict = st["buckets"]["fixed"]["envelopes"][0]
        page = _month_bill_stats(self.conn, self.cfg, self.TODAY)["Services"]
        self.assertAlmostEqual(page["used"], verdict["used"], places=2)
        self.assertAlmostEqual(page["period_used"], verdict["period_used"],
                               places=2)
        self.assertAlmostEqual(page["period_used"], 700.0, places=2)

    def test_the_disabled_bill_still_reports_its_own_occurrence(self):
        """Out of the budget is not off the page: the row is marked
        disabled, and it still says whether July's charge arrived."""
        page = _month_bill_stats(self.conn, self.cfg, self.TODAY)["City Power"]
        self.assertEqual(page["occurrences"], 1)
        self.assertEqual(page["paid"], 1)


class DisabledEnvelopeDoesNotOutbidTheLivePool(unittest.TestCase):
    """Two envelopes over one category — the household disabled the old one
    and made a new one. The claimer is first-match-wins, and the verdict
    never sees the disabled envelope at all, so the live pool must get
    first refusal on the rows here too. Otherwise the Bills page shows the
    new envelope empty while Today shows it spent.
    """

    TODAY = dt.date(2026, 7, 15)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, disabled_bills=["Old Services"])
        add_bill(self.conn, "Old Services", 600.0, bill_type="envelope",
                 frequency=None, interval=1, category="GENERAL_SERVICES",
                 match_category=True)
        add_bill(self.conn, "Services", 1200.0, bill_type="envelope",
                 frequency=None, interval=12, category="GENERAL_SERVICES",
                 match_category=True)
        add_txn(self.conn, self.TODAY, 50.0, "HANDYMAN",
                primary="GENERAL_SERVICES")
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_live_envelope_reports_the_charge(self):
        st = budget.month_status(self.conn, self.TODAY)
        verdict = st["buckets"]["fixed"]["envelopes"][0]
        self.assertEqual(verdict["payee"], "Services")
        page = _month_bill_stats(self.conn, self.cfg, self.TODAY)
        self.assertAlmostEqual(page["Services"]["used"], verdict["used"],
                               places=2)
        self.assertAlmostEqual(page["Services"]["used"], 50.0, places=2)
        # the disabled envelope still has a row, drawing on what is left
        self.assertAlmostEqual(page["Old Services"]["used"], 0.0, places=2)


class PrepaidOccurrenceIsSettledNotOverdue(unittest.TestCase):
    """Rent due the 1st, autopaid on the 28th of the month before. The
    verdict attaches that payment to this month's occurrence (matching
    reaches back PREPAY_MATCH_DAYS), so the Bills page must too — one
    ledger cannot leave the same rent 'paid ✓' on Today and 'overdue' on
    Bills.
    """

    TODAY = dt.date(2026, 8, 5)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Rent", 2000.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1), show_today=True)
        add_txn(self.conn, dt.date(2026, 7, 28), 2000.0, "RENT",
                account="chk", primary="RENT_AND_UTILITIES")
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _page(self):
        return _month_bill_stats(self.conn, self.cfg, self.TODAY)["Rent"]

    def test_early_payment_marks_the_occurrence_paid(self):
        page = self._page()
        self.assertEqual(page["occurrences"], 1)
        self.assertEqual(page["paid"], 1)
        self.assertIsNone(page["next_unpaid"],
                          "an occurrence already paid has no next-unpaid date")

    def test_verdict_does_not_call_it_overdue_either(self):
        st = budget.month_status(self.conn, self.TODAY)
        self.assertEqual(st["overdue_unpaid"], [])

    def test_last_months_cash_is_not_this_months_paid_dollars(self):
        """Cash basis, the rule the verdict uses: the money left in July, so
        July counted it. August shows the occurrence settled and $0 of
        August cash against it."""
        st = budget.month_status(self.conn, self.TODAY)
        self.assertEqual(st["buckets"]["fixed"]["actual"], 0.0)
        self.assertEqual(self._page()["paid_amount"], 0.0)


class NextMonthOccurrencePaidThisMonth(unittest.TestCase):
    """The mirror image: an occurrence due NEXT month, paid in this one. The
    verdict counts that cash as this month's fixed spend (it left here), so
    the Bills page counts the same occurrence and the same dollars — the
    per-bill paid dollars have to add up to the verdict's fixed actual or
    the two pages disagree about what the month cost.
    """

    TODAY = dt.date(2026, 8, 30)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Rent", 2000.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        add_txn(self.conn, dt.date(2026, 8, 1), 2000.0, "RENT",
                account="chk", primary="RENT_AND_UTILITIES")
        add_txn(self.conn, dt.date(2026, 8, 28), 2000.0, "RENT",
                account="chk", primary="RENT_AND_UTILITIES")
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_both_payments_are_this_months_on_both_pages(self):
        st = budget.month_status(self.conn, self.TODAY)
        page = _month_bill_stats(self.conn, self.cfg, self.TODAY)["Rent"]
        self.assertAlmostEqual(st["buckets"]["fixed"]["actual"], 4000.0,
                               places=2)
        self.assertAlmostEqual(page["paid_amount"], 4000.0, places=2)
        self.assertEqual(page["paid"], 2)
        self.assertEqual(page["occurrences"], 2)
        self.assertIsNone(page["next_unpaid"])


class UnpaidNextMonthOccurrenceIsNotThisMonths(unittest.TestCase):
    """The candidate that exists only for matching must not become a row's
    second occurrence: nobody has paid next month's rent, so this month has
    one rent, not two — same rule the verdict applies right after matching.
    """

    TODAY = dt.date(2026, 8, 30)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Rent", 2000.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        add_txn(self.conn, dt.date(2026, 8, 1), 2000.0, "RENT",
                account="chk", primary="RENT_AND_UTILITIES")
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_one_rent_this_month(self):
        page = _month_bill_stats(self.conn, self.cfg, self.TODAY)["Rent"]
        self.assertEqual(page["occurrences"], 1)
        self.assertEqual(page["paid"], 1)
        self.assertAlmostEqual(page["paid_amount"], 2000.0, places=2)
        self.assertIsNone(page["next_unpaid"])


if __name__ == "__main__":
    unittest.main()
