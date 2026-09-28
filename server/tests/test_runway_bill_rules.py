"""The runway's bill math is the forecast's bill math.

Summing raw `month_occurrences` in a date window is not the same rule as
the forecast's, which matches occurrences against month-to-date spend and
re-dates overdue bills. Same bills, two answers, both feeding cash surfaces:

| Situation           | summed occurrences          | forecast |
|---------------------|-----------------------------|----------|
| bill paid early     | still reserved (too tight)  | skipped  |
| bill overdue unpaid | dropped entirely (too rosy) | due soon |

`due_total` drives Today's headroom ("checking now · bills before next pay")
and the daily email's cash line, so either error is user-visible. Both
callers read budget.upcoming_bill_occurrences.

Fixture note: payees need a 4+ letter token (`_key_token`) or they are not
matchable bills at all — "Gym" is invisible by design, "Fitness Club" is not.
"""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import report

from .util import TODAY, add_bill, add_txn, make_db, write_config


class RunwayBillRulesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # a paycheck two weeks out gives the runway a horizon to work with
        add_bill(self.conn, "Paycheck Co", 5000.0, frequency="WEEKLY",
                 interval=2, next_due=TODAY + dt.timedelta(days=14),
                 income=True, last_seen=TODAY)

    def tearDown(self):
        self.conn.close()

    def _runway(self):
        return report.gather(self.conn, TODAY)["runway"]

    def _upcoming(self, end=None):
        return budget.upcoming_bill_occurrences(
            self.conn, budget.load_config(self.conn), TODAY,
            end or TODAY + dt.timedelta(days=14))

    def test_bill_paid_early_is_not_reserved_again(self):
        # due in 5 days, already paid today: the money is gone from checking,
        # so reserving it again double-counts and understates headroom
        add_bill(self.conn, "Rent", 2000.0,
                 next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=30))
        add_txn(self.conn, TODAY, 2000.0, "Rent", primary="RENT_AND_UTILITIES")
        rw = self._runway()
        self.assertEqual(rw["due_total"], 0.0)
        self.assertEqual(rw["due_count"], 0)
        self.assertEqual(self._upcoming(), [])

    def test_overdue_unpaid_bill_is_still_owed(self):
        # due 3 days ago, never paid: it does not stop being owed just
        # because the due date slipped past — a plain date window drops it
        add_bill(self.conn, "Rent", 2000.0,
                 next_due=TODAY - dt.timedelta(days=3),
                 last_seen=TODAY - dt.timedelta(days=33))
        rw = self._runway()
        self.assertEqual(rw["due_total"], 2000.0)
        self.assertEqual(rw["due_count"], 1)
        # and it is re-dated to tomorrow, the soonest it can be paid
        self.assertEqual([b["due"] for b in self._upcoming()],
                         [TODAY + dt.timedelta(days=1)])

    def test_unpaid_bill_in_window_still_counts(self):
        # the ordinary case must not regress while fixing the edges
        add_bill(self.conn, "Rent", 2000.0,
                 next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=30))
        self.assertEqual(self._runway()["due_total"], 2000.0)

    def test_runway_counts_exactly_the_shared_rule_bills(self):
        # the point: one shared rule, so the surfaces cannot drift apart.
        # overdue + upcoming + paid-early, all at once.
        add_bill(self.conn, "Rent", 2000.0,
                 next_due=TODAY - dt.timedelta(days=2),
                 last_seen=TODAY - dt.timedelta(days=32))
        add_bill(self.conn, "Fitness Club", 50.0,
                 next_due=TODAY + dt.timedelta(days=4),
                 last_seen=TODAY - dt.timedelta(days=26))
        add_bill(self.conn, "Netflix", 20.0,
                 next_due=TODAY + dt.timedelta(days=6),
                 last_seen=TODAY - dt.timedelta(days=24))
        add_txn(self.conn, TODAY, 20.0, "Netflix", primary="ENTERTAINMENT")

        shared = self._upcoming()
        self.assertEqual(sorted(b["payee"] for b in shared),
                         ["Fitness Club", "Rent"])   # Netflix paid, excluded
        rw = self._runway()
        self.assertEqual(rw["due_total"], 2050.0)
        self.assertEqual(rw["due_count"], 2)
        # the runway total IS the shared rule's total, by construction
        self.assertEqual(rw["due_total"],
                         sum(b["planned"] for b in shared))

    def test_intermediate_months_are_not_skipped(self):
        # A window that expands only its endpoint months loses a bill in a
        # month strictly between today and the horizon.
        start = TODAY.replace(day=1)
        mid = (start + dt.timedelta(days=40)).replace(day=15)
        add_bill(self.conn, "MidMonth", 300.0, next_due=mid, last_seen=start)
        end = (start + dt.timedelta(days=80)).replace(day=28)
        self.assertIn("MidMonth", [b["payee"] for b in self._upcoming(end)])


if __name__ == "__main__":
    unittest.main()
