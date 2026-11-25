"""A bill approved after the date its offer predicted does not open overdue.

An offer dates the next charge when it is written. Approved later — a new
household working through a first batch built from months-old charges —
that date can already be behind today, and the bill would read "15 days
late" for a day on which it was not yet a bill. The approval rolls the due
date to the series' next occurrence, as the nightly maintenance would.
"""

import datetime as dt
import unittest

from psycopg.types.json import Jsonb

from oikonome import localtime
from oikonome.engine import bills

from .util import make_db, write_config


class ApprovedLateTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _offer(self, pid, payee, due):
        self.conn.execute(
            """INSERT INTO bill_proposals (id, kind, bill_type, payee, amount,
                   frequency, "interval", next_due, evidence, status,
                   created_at)
               VALUES (%s, 'add', 'occurrence', %s, 500, 'MONTHLY', 1, %s,
                       %s, 'pending', now())""",
            (pid, payee, due, Jsonb({})))

    def _due(self, payee):
        return self.conn.execute(
            "SELECT due_on FROM bills WHERE payee=%s", (payee,)
        ).fetchone()["due_on"]

    def test_a_passed_date_rolls_to_the_next_occurrence(self):
        today = localtime.household_day(self.conn)
        past = today - dt.timedelta(days=15)
        self._offer("prop:late", "Northwind Air", past)
        r = bills.apply_proposal(self.conn, "prop:late", "approve")
        self.assertNotIn("error", r, r)
        due = self._due("Northwind Air")
        self.assertGreaterEqual(due, today)
        self.assertLess(due, today + dt.timedelta(days=32))
        # the series keeps its day of the month, clamped only to the
        # length of the month it lands in (a 29th stays a 29th in November)
        import calendar
        self.assertEqual(due.day, min(past.day, calendar.monthrange(
            due.year, due.month)[1]))

    def test_a_date_still_ahead_is_kept(self):
        today = localtime.household_day(self.conn)
        ahead = today + dt.timedelta(days=9)
        self._offer("prop:ahead", "Spinwheel Audio", ahead)
        r = bills.apply_proposal(self.conn, "prop:ahead", "approve")
        self.assertNotIn("error", r, r)
        self.assertEqual(self._due("Spinwheel Audio"), ahead)


if __name__ == "__main__":
    unittest.main()
