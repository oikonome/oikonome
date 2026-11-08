"""The Year lens reads the year's ledger once, and answers as month by month.

Each closed month matches its bills against rows from five weeks before its
1st to five weeks past the end of the month after (a late payment homes an
occurrence in the month it posts). Read month by month, those windows
overlap so much that a Year page pulled the year's rows about four times
over, and the annual-envelope pool re-read and re-matched the year to date
for every month — quadratic in the month number. One read of the year's
span, sliced per month, and one pass over the year's envelope claims must
give exactly the page the month-by-month reads gave.
"""
import datetime as dt
import json
import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.web import lenses

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 7)
RENT = 1613.0
PAY = 2604.5


def _seed(conn):
    write_config(conn, food_monthly=700, other_monthly=800)
    add_bill(conn, "Harbor Lofts", RENT, next_due=dt.date(2026, 11, 30),
             last_seen=dt.date(2026, 10, 30))
    add_bill(conn, "Brightwater Power", 83.0,
             next_due=dt.date(2026, 11, 12),
             last_seen=dt.date(2026, 10, 12))
    add_bill(conn, "Namehost", 120.0, bill_type="envelope", frequency=None,
             interval=12)
    add_bill(conn, "Bean Haus", 40.0, bill_type="envelope", frequency=None,
             interval=1)
    cents = 0
    d = dt.date(2025, 1, 1)
    while d <= TODAY:
        cents += 1
        # amounts never repeat, so every read orders its rows one way
        add_txn(conn, d, 20 + (d.toordinal() % 61) + cents / 1000,
                "GREENLEAF MARKET", primary="FOOD_AND_DRINK")
        cents += 1
        add_txn(conn, d, 9 + (d.toordinal() % 37) + cents / 1000,
                "CORNER GOODS")
        if d.day == 3:
            add_txn(conn, d, 6 + d.month + cents / 1000, "BEAN HAUS")
        if d.day == 14 and d.month in (2, 6, 9):
            add_txn(conn, d, 21 + d.month, "NAMEHOST RENEWAL")
        if d.day == 12:
            add_txn(conn, d, 80 + d.month / 4, "BRIGHTWATER POWER",
                    account="chk", primary="RENT_AND_UTILITIES")
        if d.day == 1 and d.month % 3 != 1:
            # the rent posts across the month line: some on the 30th,
            # some on the 1st or 2nd
            pay = d + dt.timedelta(days=(d.month % 4) - 2)
            if pay <= TODAY:
                add_txn(conn, pay, RENT, "HARBOR LOFTS", account="chk",
                        primary="RENT_AND_UTILITIES")
        if d.weekday() == 4 and d.toordinal() % 14 < 7:
            add_txn(conn, d, -PAY, "ACME PAYROLL", account="chk",
                    primary="INCOME")
        d += dt.timedelta(days=1)
    for y, m in [(2026, m) for m in range(1, 11)] + [(2025, 12)]:
        budget.snapshot_month(conn, dt.date(y, m, 20))
    conn.commit()


class YearLensReadsOnce(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        _seed(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def page(self, **kw):
        self.conn._memo = None          # no answer carried from a prior read
        return json.loads(json.dumps(
            lenses.year_summary(self.conn, 2026, TODAY, **kw),
            default=str, sort_keys=True))

    def test_the_page_is_the_month_by_month_page(self):
        shared = self.page(with_detail=True)
        with mock.patch.object(budget, "share_spend_window",
                               lambda *a, **k: None):
            alone = self.page(with_detail=True)
        self.assertEqual(shared, alone)

    def test_one_ledger_read_serves_every_month(self):
        reads = []
        real = self.conn.execute

        def execute(q, *a, **kw):
            if isinstance(q, str) and "ORDER BY t.amount DESC" in q:
                reads.append(q)
            return real(q, *a, **kw)

        with mock.patch.object(self.conn, "execute", execute):
            self.page()
        # the spend rows once, and the bank feed's withdrawn rows once
        self.assertLessEqual(len(reads), 2, len(reads))

    def test_each_row_is_offered_to_the_envelopes_at_most_twice(self):
        """Once for the annual pool's year, once in its own month — never
        once more for every month that comes after it."""
        calls = [0]
        real = budget._claim_envelope

        def claim(*a, **kw):
            calls[0] += 1
            return real(*a, **kw)

        n = self.conn.execute(
            "SELECT count(*) AS n FROM transactions "
            "WHERE date >= '2026-01-01' AND amount > 0").fetchone()["n"]
        with mock.patch.object(budget, "_claim_envelope", claim):
            self.page()
        self.assertLessEqual(calls[0], 2 * n, (calls[0], n))


if __name__ == "__main__":
    unittest.main()
