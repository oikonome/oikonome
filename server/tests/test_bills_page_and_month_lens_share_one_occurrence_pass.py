"""The Bills page and the Month lens read a month's bills through ONE pass.

`_month_bill_stats` (the Bills page's Paid x/y and paid dollars) and
`budget.month_status` (Today, the email, the Month lens) both have to decide
the same things about a month: which rows can pay a bill (the window reaches
back PREPAY_MATCH_DAYS and, for a closed month, past its end), which
months' occurrences are offered as candidates, which occurrence a payment
binds to, and which month each occurrence finally belongs to. When each
reader spelled that pass for itself, every fix to one had to be found and
repeated in the other, and a late payment, a skipped week or a series that
ended was the kind of edge where they drifted apart. Both now call
`budget.month_occurrence_pass`.

The month here has all three edges (invented amounts): a rent due on the
30th and paid three days late, a weekly service with one week skipped, and
a membership whose series ended. For every month around them, closed and
live, the page and the verdict must agree on how many occurrences were
paid, what they cost and what is still owed.
"""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web.api import _month_bill_stats

from .util import add_bill, add_txn, make_db, write_config

RENT, LAWN, GYM = 900.0, 35.0, 25.0


def _thursdays(first: dt.date, last: dt.date):
    d = first + dt.timedelta(days=(3 - first.weekday()) % 7)
    while d <= last:
        yield d
        d += dt.timedelta(days=7)


class OneOccurrencePassForBothReaders(unittest.TestCase):
    SKIPPED = dt.date(2026, 4, 16)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        floor = dt.date(2026, 1, 1)
        add_bill(self.conn, "Harbor Rent", RENT, frequency="MONTHLY",
                 next_due=dt.date(2026, 3, 30), by_month_day=30,
                 last_seen=floor)
        add_bill(self.conn, "Lawn Crew", LAWN, frequency="WEEKLY",
                 next_due=dt.date(2026, 3, 5), last_seen=floor)
        add_bill(self.conn, "Gym Pass", GYM, frequency="MONTHLY",
                 next_due=dt.date(2026, 3, 10), last_seen=floor)
        # the membership's series ends after April's charge
        self.conn.execute(
            "UPDATE bills SET raw = jsonb_set(raw, '{recurrence,endOn}', "
            "'\"2026-04-30\"') WHERE payee = 'Gym Pass'")
        # rent: on time in March, April's three days late (posted May 3),
        # May's on time
        add_txn(self.conn, dt.date(2026, 3, 30), RENT, "HARBOR RENT")
        add_txn(self.conn, dt.date(2026, 5, 3), RENT, "HARBOR RENT")
        add_txn(self.conn, dt.date(2026, 5, 30), RENT, "HARBOR RENT")
        # every Thursday but one
        for d in _thursdays(dt.date(2026, 3, 1), dt.date(2026, 6, 15)):
            if d != self.SKIPPED:
                add_txn(self.conn, d, LAWN, "LAWN CREW")
        add_txn(self.conn, dt.date(2026, 3, 10), GYM, "GYM PASS")
        add_txn(self.conn, dt.date(2026, 4, 10), GYM, "GYM PASS")
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _agree(self, today: dt.date, closed: bool):
        st = budget.month_status(self.conn, today, closed=closed)
        fixed = st["buckets"]["fixed"]
        page = _month_bill_stats(self.conn, self.cfg, today, closed=closed)
        price = {"Harbor Rent": RENT, "Lawn Crew": LAWN, "Gym Pass": GYM}
        where = f"{today} closed={closed}"
        self.assertAlmostEqual(
            sum(e["paid_amount"] for e in page.values()), fixed["actual"],
            places=2, msg=where)
        self.assertEqual(sum(e["paid"] for e in page.values()),
                         len(fixed["occ_rows"]), where)
        self.assertAlmostEqual(
            sum(e["occurrences"] * price[p] for p, e in page.items()),
            fixed["month_budget"], places=2, msg=where)
        if closed:
            self.assertAlmostEqual(
                sum((e["occurrences"] - e["paid"]) * price[p]
                    for p, e in page.items()),
                fixed["unpaid_due"], places=2, msg=where)
        return page

    def test_april_closed_agrees_and_sees_each_edge(self):
        page = self._agree(dt.date(2026, 4, 30), closed=True)
        # the late rent belongs to the month its payment is dated in
        self.assertEqual(page.get("Harbor Rent", {}).get("occurrences", 0), 0)
        # the skipped Thursday is due and unpaid on both pages
        lawn = page["Lawn Crew"]
        self.assertEqual(lawn["occurrences"] - lawn["paid"], 1)
        self.assertEqual(page["Gym Pass"]["paid"], 1)

    def test_may_closed_agrees_and_the_ended_series_is_gone(self):
        page = self._agree(dt.date(2026, 5, 31), closed=True)
        self.assertEqual(page.get("Gym Pass", {}).get("occurrences", 0), 0)
        # April's late rent and May's own both land in May
        self.assertEqual(page["Harbor Rent"]["paid"], 2)

    def test_march_closed_agrees(self):
        self._agree(dt.date(2026, 3, 31), closed=True)

    def test_the_live_month_agrees(self):
        self._agree(dt.date(2026, 6, 15), closed=False)



class TheBillsRowDatesByTheHouseholdsDay(unittest.TestCase):
    """A Bills row's Due cell ("today" / "tomorrow") and its cycle words
    ("due tomorrow") count from one day: the household's (its timezone
    setting), never the browser's own, or a member in another zone reads
    the same bill as due today and due tomorrow in one row."""

    def test_the_due_cell_reads_the_households_day(self):
        # the cell and the words are the server's (api_bills.due_words),
        # counted from the day the list is judged on; the page counts none
        import pathlib
        import oikonome
        from oikonome.web import api_bills
        src = (pathlib.Path(oikonome.__file__).parent.parent.parent
               / "webapp" / "src" / "pages" / "Bills.tsx").read_text()
        self.assertIn("b.due_label ??", src)
        self.assertNotIn("dueLabel(", src)
        self.assertNotIn("localToday", src)
        day = dt.date(2026, 4, 30)
        row = {"due_on": "2026-05-01", "used": None, "occurrences": 1,
               "paid": 0, "cadence_value": "MONTHLY:1"}
        w = api_bills.due_words(row, day, live=True)
        self.assertEqual((w["due_label"], w["due_text"]),
                         ("tomorrow", "due tomorrow"))


if __name__ == "__main__":
    unittest.main()
