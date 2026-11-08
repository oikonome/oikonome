"""The Spending and Income tabs compare a window with the previous equal
one. Where the ledger began after that previous window opened, the months
before the first row were never imported, not empty: totalling them as $0
makes a new household read its whole year as a huge rise vs the year before
and every category as a mover from nothing, and a ledger that covers only
part of the previous window reads as a fake "+150%". The comparison must
come back unknown (null) — the pages hide the delta and the movers on it —
while a ledger that covers the whole previous window still gets a number."""

import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import make_db, seed_accounts, write_config


def _txn(conn, acct, day, amount, name, **cols):
    keys = ["id", "account_id", "date", "amount", "name", *cols]
    vals = [f"prevwin-{name}-{day}", acct, day, amount, name, *cols.values()]
    conn.execute(f"INSERT INTO transactions ({', '.join(keys)}) VALUES "
                 f"({', '.join(['%s'] * len(keys))})", vals)


class PreviousWindowBeforeTheLedgerTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        seed_accounts(self.conn)
        self.bank = self.conn.execute(
            "SELECT id FROM accounts WHERE type='depository' LIMIT 1"
        ).fetchone()["id"]
        # 1y on this day is Oct 2025..Sep 2026; the year before opens Oct 2024
        self.today = dt.date(2026, 10, 30)

    def tearDown(self):
        self.conn.close()

    def _year_of_rows(self, first: dt.date):
        _txn(self.conn, self.bank, first, 120.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, first, -3000.0, "ACME PAYROLL",
             category_primary="INCOME")
        _txn(self.conn, self.bank, dt.date(2026, 8, 10), 80.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 8, 15), -3000.0,
             "ACME PAYROLL", category_primary="INCOME")

    def test_a_ledger_younger_than_the_previous_window_has_no_comparison(self):
        self._year_of_rows(dt.date(2025, 11, 3))
        sp = reporting.spending_window(self.conn, self.today, "1y")
        self.assertEqual(sp["total"], 200.0)
        self.assertIsNone(sp["prev_total"])
        self.assertTrue(sp["categories"])
        self.assertTrue(all(c[2] is None for c in sp["categories"]))
        self.assertTrue(all(m[3] is None for m in sp["merchants"]))
        self.assertEqual(sp["dropped_merchants"], [])
        inc = reporting.income_window(self.conn, self.today, "1y")
        self.assertEqual(inc["total"], 6000.0)
        self.assertIsNone(inc["prev_total"])
        self.assertIsNone(inc["prev_kinds"])
        self.assertTrue(all(s[3] is None for s in inc["sources"]))
        self.assertEqual(inc["dropped_sources"], [])

    def test_a_ledger_covering_part_of_the_previous_window_has_none(self):
        # opens in Mar 2025, five months into the Oct 2024 previous window
        self._year_of_rows(dt.date(2025, 3, 3))
        self.assertIsNone(reporting.spending_window(
            self.conn, self.today, "1y")["prev_total"])
        self.assertIsNone(reporting.income_window(
            self.conn, self.today, "1y")["prev_total"])

    def test_this_month_in_a_ledger_opened_this_month_has_no_comparison(self):
        _txn(self.conn, self.bank, dt.date(2026, 10, 2), 40.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 10, 2), -900.0,
             "ACME PAYROLL", category_primary="INCOME")
        self.assertIsNone(reporting.spending_window(
            self.conn, self.today, "cur")["prev_total"])
        self.assertIsNone(reporting.income_window(
            self.conn, self.today, "cur")["prev_total"])

    def test_a_ledger_older_than_the_previous_window_still_compares(self):
        self._year_of_rows(dt.date(2024, 6, 3))
        _txn(self.conn, self.bank, dt.date(2025, 1, 9), 50.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2025, 1, 9), -2500.0,
             "ACME PAYROLL", category_primary="INCOME")
        sp = reporting.spending_window(self.conn, self.today, "1y")
        self.assertEqual(sp["total"], 80.0)
        self.assertEqual(sp["prev_total"], 50.0)
        self.assertEqual(sp["categories"][0][2], 50.0)
        inc = reporting.income_window(self.conn, self.today, "1y")
        self.assertEqual(inc["total"], 3000.0)
        self.assertEqual(inc["prev_total"], 2500.0)

    def test_a_ledger_opening_on_the_previous_windows_first_day_compares(self):
        # the first row falls on the day the previous window opens: the
        # whole window is covered, so the comparison stands
        self._year_of_rows(dt.date(2024, 10, 1))
        sp = reporting.spending_window(self.conn, self.today, "1y")
        self.assertEqual(sp["prev_total"], 120.0)
        inc = reporting.income_window(self.conn, self.today, "1y")
        self.assertEqual(inc["prev_total"], 3000.0)

    def test_a_ledger_opening_partway_through_the_previous_month_has_none(self):
        # Coverage is the first row's day, not its month: a ledger opening
        # on the 20th never imported the 1st to the 19th, so the previous
        # window's first month is partial even though it is "the same month"
        self._year_of_rows(dt.date(2024, 10, 20))
        self.assertIsNone(reporting.spending_window(
            self.conn, self.today, "1y")["prev_total"])
        self.assertIsNone(reporting.income_window(
            self.conn, self.today, "1y")["prev_total"])

    def test_this_month_against_a_part_imported_last_month_has_none(self):
        # Last month holds only the days from the 14th: set against a whole
        # month so far, it would read as a large rise that is only missing
        # history.
        _txn(self.conn, self.bank, dt.date(2026, 9, 14), 100.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 9, 14), -1000.0,
             "ACME PAYROLL", category_primary="INCOME")
        _txn(self.conn, self.bank, dt.date(2026, 10, 5), 176.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 10, 5), -1715.0,
             "ACME PAYROLL", category_primary="INCOME")
        sp = reporting.spending_window(self.conn, self.today, "cur")
        self.assertEqual(sp["total"], 176.0)
        self.assertIsNone(sp["prev_total"])
        self.assertIsNone(reporting.income_window(
            self.conn, self.today, "cur")["prev_total"])

    def test_this_month_against_a_wholly_imported_last_month_compares(self):
        _txn(self.conn, self.bank, dt.date(2026, 9, 1), 100.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 9, 1), -1000.0,
             "ACME PAYROLL", category_primary="INCOME")
        _txn(self.conn, self.bank, dt.date(2026, 10, 5), 176.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 10, 5), -1715.0,
             "ACME PAYROLL", category_primary="INCOME")
        self.assertEqual(reporting.spending_window(
            self.conn, self.today, "cur")["prev_total"], 100.0)
        self.assertEqual(reporting.income_window(
            self.conn, self.today, "cur")["prev_total"], 1000.0)

    def test_a_month_imported_from_its_first_day_compares_without_spend_on_it(self):
        # The card's first imported row is a payment received on Sep 1;
        # its first purchase is Sep 2. September was imported whole, it
        # just had no spend on its first day, so October still compares.
        _txn(self.conn, "card", dt.date(2026, 9, 1), -250.0, "CARD PAYMENT",
             category_primary="TRANSFER_IN")
        _txn(self.conn, "card", dt.date(2026, 9, 2), 100.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, "card", dt.date(2026, 10, 5), 176.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        sp = reporting.spending_window(self.conn, self.today, "cur")
        self.assertEqual(sp["total"], 176.0)
        self.assertEqual(sp["prev_total"], 100.0)

    def test_rows_that_never_reach_spend_do_not_vouch_for_last_month(self):
        # Nothing on the card before Sep 14. A deposit on Sep 1 on an
        # account that carries no spend, and a removed row on the card on
        # Sep 1, say nothing about whether the card's Sep 1-13 was imported.
        _txn(self.conn, self.bank, dt.date(2026, 9, 1), -1000.0,
             "ACME PAYROLL", category_primary="INCOME")
        _txn(self.conn, "card", dt.date(2026, 9, 1), 30.0, "GROCER",
             category_primary="FOOD_AND_DRINK", removed=1)
        _txn(self.conn, "card", dt.date(2026, 9, 14), -250.0, "CARD PAYMENT",
             category_primary="TRANSFER_IN")
        _txn(self.conn, "card", dt.date(2026, 9, 15), 100.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, "card", dt.date(2026, 10, 5), 176.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        sp = reporting.spending_window(self.conn, self.today, "cur")
        self.assertEqual(sp["total"], 176.0)
        self.assertIsNone(sp["prev_total"])

    def test_the_monthly_pace_still_counts_the_first_month_whole(self):
        # The divisor stays month-rounded: a ledger opening on the 20th
        # spans Oct 2025..Sep 2026 on "1y" — twelve months, not eleven.
        self._year_of_rows(dt.date(2025, 10, 20))
        self.assertEqual(reporting.spending_window(
            self.conn, self.today, "1y")["months"], 12)


if __name__ == "__main__":
    unittest.main()
