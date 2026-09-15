"""The nightly due-date roll must find the next occurrence of a bill whose
cycle is longer than a year.

`_advance_due_dates` scanned 14 months ahead ("covers yearly bills") and
gave up silently when nothing fell inside. A YEARLY interval-2 bill's next
occurrence is 24 months out, so its `due_on` never moves past the date it
was paid on and the Bills page calls it "overdue" every day after.
"""

import datetime as dt
import json
import unittest

from oikonome.engine import bills

from .util import make_db


class MultiYearDueRollTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _bill(self, bid, due, recurrence):
        self.conn.execute(
            """INSERT INTO bills (id, type, payee, amount, frequency,
                                  monthly_amount, due_on, active,
                                  is_completed, raw)
               VALUES (%s, 'BILL', %s, -671.76, %s, -28, %s, 1, 0, %s::jsonb)""",
            (bid, bid, recurrence["frequency"], due,
             json.dumps({"source": "manual", "bill_type": "occurrence",
                         "dueOn": due, "lastDueOn": None,
                         "recurrence": recurrence})))

    def _due(self, bid):
        r = self.conn.execute(
            "SELECT due_on, raw FROM bills WHERE id=%s", (bid,)).fetchone()
        raw = r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"])
        return r["due_on"], raw

    def test_every_two_years_rolls_to_the_next_cycle(self):
        self._bill("biennial", "2026-08-18",
                   {"frequency": "YEARLY", "interval": 2, "byMonth": [8]})
        stats = {"due_advanced": 0}
        bills._advance_due_dates(self.conn, dt.date(2026, 9, 1), stats)
        due, raw = self._due("biennial")
        self.assertEqual(due, dt.date(2028, 8, 18), "due_on did not advance")
        self.assertEqual(raw["dueOn"], "2028-08-18")
        self.assertEqual(raw["lastDueOn"], "2026-08-18")
        self.assertEqual(stats["due_advanced"], 1)

    def test_every_eighteen_months_rolls_too(self):
        self._bill("domain", "2026-08-10",
                   {"frequency": "MONTHLY", "interval": 18, "byMonthDay": [10]})
        bills._advance_due_dates(self.conn, dt.date(2026, 9, 1),
                                 {"due_advanced": 0})
        due, _ = self._due("domain")
        self.assertEqual(due, dt.date(2028, 2, 10))

    def test_yearly_and_current_bills_unchanged(self):
        self._bill("annual", "2026-08-18",
                   {"frequency": "YEARLY", "interval": 1, "byMonth": [8]})
        self._bill("future", "2026-10-01",
                   {"frequency": "YEARLY", "interval": 2, "byMonth": [10]})
        bills._advance_due_dates(self.conn, dt.date(2026, 9, 1),
                                 {"due_advanced": 0})
        self.assertEqual(self._due("annual")[0], dt.date(2027, 8, 18))
        self.assertEqual(self._due("future")[0], dt.date(2026, 10, 1))


if __name__ == "__main__":
    unittest.main()
