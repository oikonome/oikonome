"""After a month ends, nothing in it is "still due" or "awaiting".

A closed month's card kept the running month's words: what was left of
the bill plan read "$X still due", an unmatched occurrence "awaiting" and
"scheduled but not posted". In a month that is over, a bill was paid, paid
for less, or not paid. The server now writes that: "$X under plan" for
what the plan had left (in the Bills row's table), "not paid: NAME $X"
only for a bill with no payment found in the month, and a bill that was paid once that month is
never also listed as unpaid for another of its dates. The month still
running keeps its own words.
"""
import datetime as dt
import unittest

from oikonome.web import lenses

from .util import TODAY, add_bill, add_txn, freeze_month, make_db, write_config

JUNE = (2026, 6)                      # closed: TODAY is mid-July


class AClosedMonthsBillsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        # paid, for a little less than planned
        add_bill(self.conn, "Rent", 2000.0, next_due=dt.date(2026, 7, 1),
                 last_seen=dt.date(2026, 6, 1))
        add_txn(self.conn, dt.date(2026, 6, 1), 1990.0, "RENT")
        # never paid in June
        add_bill(self.conn, "Gym Pass", 50.0, next_due=dt.date(2026, 7, 20),
                 last_seen=dt.date(2026, 5, 20))
        # weekly, paid once in June: its other June dates found no payment
        add_bill(self.conn, "Lawn Care", 30.0, frequency="WEEKLY",
                 next_due=dt.date(2026, 7, 6), last_seen=dt.date(2026, 6, 8))
        add_txn(self.conn, dt.date(2026, 6, 8), 30.0, "LAWN CARE")
        freeze_month(self.conn, *JUNE)
        self.d = lenses.month_summary(self.conn, *JUNE, today=TODAY)
        self.bills = self.d["bills"]

    def _bills_row(self, d=None):
        return next(r for r in (d or self.d)["plan_rows"]
                    if r["label"] == "Bills")

    def test_nothing_is_due_or_awaiting(self):
        self.assertEqual(self.d["status"], "final")
        self.assertEqual(self.bills["awaiting"], 0.0)
        self.assertEqual(self.bills["overdue"], [])
        row = self._bills_row()
        self.assertEqual(row["pending"], 0.0)
        self.assertNotIn("awaiting", row["caption"])
        self.assertNotIn("due", (row["status"] or {}).get("text", ""))

    def test_a_bill_with_no_payment_is_named_not_paid(self):
        self.assertIn("not paid: Gym Pass $50", self.bills["notes"])

    def test_a_bill_paid_that_month_is_never_also_listed_as_unpaid(self):
        self.assertEqual(
            [n for n in self.bills["notes"] if n.startswith("not paid")],
            ["not paid: Gym Pass $50"])

    def test_what_the_plan_had_left_reads_under_plan(self):
        planned, posted = self.bills["planned"], self.bills["posted"]
        self.assertEqual(posted, 2020.0)
        under = planned - posted - 50.0
        self.assertGreater(under, 10.0)
        row = self._bills_row()
        # nothing is still due: the row says how the month came out, not
        # paid first; its table says posted + not paid + under plan is the
        # plan
        self.assertEqual(row["status"]["text"],
                         f"$50 not paid · ${under:,.0f} under plan")
        rows = row["breakdown"]["table"]["rows"]
        self.assertEqual([(r["label"], r["figure"]) for r in rows],
                         [("Paid", "+$2,020"), ("Not paid: Gym Pass", "+$50"),
                          ("Under plan", f"+${under:,.0f}"),
                          ("The month's plan", f"${planned:,.0f}")])

    def test_a_month_paid_in_full_says_nothing(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_bill(conn, "Rent", 2000.0, next_due=dt.date(2026, 7, 1),
                 last_seen=dt.date(2026, 6, 1))
        add_txn(conn, dt.date(2026, 6, 1), 2000.0, "RENT")
        freeze_month(conn, *JUNE)
        d = lenses.month_summary(conn, *JUNE, today=TODAY)
        self.assertEqual(d["bills"]["notes"], [])
        self.assertIsNone(self._bills_row(d)["status"])

    def test_the_month_still_running_keeps_its_own_words(self):
        d = lenses.month_summary(self.conn, TODAY.year, TODAY.month,
                                 today=TODAY)
        # its card names no bill as not paid; its row says still due
        self.assertFalse([n for n in d["bills"]["notes"] if "not paid" in n])
        # rent was due on the 1st and has not posted
        self.assertGreater(d["bills"]["awaiting"], 0.0)
        self.assertTrue(d["bills"]["overdue"])
        status = self._bills_row(d)["status"]
        self.assertTrue(status["text"].endswith(" still due"), status)
        self.assertGreater(status["pending"], 0.0)

if __name__ == "__main__":
    unittest.main()
