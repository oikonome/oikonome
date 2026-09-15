"""The Spending and Income tabs' windowed views: totals, rankings and the
previous-equal-window figures they lead with ("what changed"), over the
site-wide 3m/6m/1y/3y/5y/all timeframes. The windows must use the same
spend/income predicates as every other figure on the page."""

import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import add_bill, make_db, seed_accounts, write_config


def _txn(conn, acct, day, amount, name, **cols):
    keys = ["id", "account_id", "date", "amount", "name", *cols]
    vals = [f"win-{name}-{day}", acct, day, amount, name, *cols.values()]
    conn.execute(f"INSERT INTO transactions ({', '.join(keys)}) VALUES "
                 f"({', '.join(['%s'] * len(keys))})", vals)


class WindowedTabsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        seed_accounts(self.conn)
        self.bank = self.conn.execute(
            "SELECT id FROM accounts WHERE type='depository' LIMIT 1"
        ).fetchone()["id"]
        self.today = dt.date(2026, 8, 31)

    def tearDown(self):
        self.conn.close()

    def test_spending_window_ranks_and_compares_to_the_previous_window(self):
        # inside the 3m window (Jun–Aug)
        _txn(self.conn, self.bank, dt.date(2026, 8, 12), 240.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 7, 2), 60.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 6, 20), 90.0, "CINEMA",
             category_primary="ENTERTAINMENT")
        # inside the PREVIOUS 3m window (Mar–May)
        _txn(self.conn, self.bank, dt.date(2026, 4, 5), 100.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        # before both windows
        _txn(self.conn, self.bank, dt.date(2025, 12, 1), 999.0, "OLD",
             category_primary="GENERAL_SERVICES")
        w = reporting.spending_window(self.conn, self.today, "3m")
        self.assertEqual(w["label"], "Jun–Aug 2026")
        self.assertEqual(w["total"], 390.0)
        self.assertEqual(w["prev_total"], 100.0)
        self.assertEqual(w["categories"][0][:2], ["FOOD AND DRINK", 300.0])
        self.assertEqual(w["categories"][0][2], 100.0)   # the prev figure
        self.assertEqual(w["categories"][1][:3], ["ENTERTAINMENT", 90.0, 0.0])
        payees = {m[0]: m for m in w["merchants"]}
        self.assertEqual(payees["GROCER"][1], 300.0)
        self.assertEqual(payees["GROCER"][2], 2)         # visits
        self.assertEqual(payees["GROCER"][3], 100.0)     # prev
        self.assertEqual(w["by_month"],
                         [["2026-06", 90.0], ["2026-07", 60.0],
                          ["2026-08", 240.0]])

    def test_merchant_variants_differing_only_in_punctuation_fold(self):
        """Over a wide window a merchant renamed by its processor ("H-E-B"
        then "H E B") must rank as ONE merchant, under the bigger
        variant's name, with the previous window compared the same way."""
        _txn(self.conn, self.bank, dt.date(2026, 8, 1), 300.0, "H-E-B",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 7, 1), 200.0, "H E B",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 4, 1), 50.0, "H E B",
             category_primary="FOOD_AND_DRINK")
        w = reporting.spending_window(self.conn, self.today, "3m")
        heb = [m for m in w["merchants"]
               if "h" in m[0].lower() and "b" in m[0].lower()]
        self.assertEqual(len(heb), 1)
        self.assertEqual(heb[0][0], "H-E-B")     # the bigger variant names it
        self.assertEqual(heb[0][1], 500.0)
        self.assertEqual(heb[0][2], 2)
        self.assertEqual(heb[0][3], 50.0)        # prev window, same fold

    def test_spending_all_time_has_no_previous_window(self):
        _txn(self.conn, self.bank, dt.date(2026, 8, 12), 50.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        w = reporting.spending_window(self.conn, self.today, "all")
        self.assertIsNone(w["prev_total"])
        self.assertIsNone(w["categories"][0][2])
        self.assertIsNone(w["merchants"][0][3])

    def test_income_window_splits_kinds_and_compares(self):
        add_bill(self.conn, "ACME PAYROLL", 5000.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 9, 5), income=True)
        _txn(self.conn, self.bank, dt.date(2026, 8, 5), -5000.0, "ACME PAYROLL",
             category_primary="INCOME")
        _txn(self.conn, self.bank, dt.date(2026, 8, 6), -20.0, "INTEREST",
             category_primary="INCOME", category_detailed="INCOME_INTEREST_EARNED")
        # previous window (Mar–May for 3m)
        _txn(self.conn, self.bank, dt.date(2026, 4, 5), -4000.0, "ACME PAYROLL",
             category_primary="INCOME")
        w = reporting.income_window(self.conn, self.today, "3m")
        self.assertEqual(w["total"], 5020.0)
        self.assertEqual(w["kinds"]["paychecks"], 5000.0)
        self.assertEqual(w["kinds"]["interest"], 20.0)
        self.assertEqual(w["prev_total"], 4000.0)
        self.assertEqual(w["prev_kinds"]["paychecks"], 4000.0)
        self.assertEqual(w["by_month"], [["2026-08", 5020.0]])
        # WHO paid: ranked by payer, previous window alongside
        srcs = {s[0]: s for s in w["sources"]}
        self.assertEqual(srcs["ACME PAYROLL"][1], 5000.0)
        self.assertEqual(srcs["ACME PAYROLL"][3], 4000.0)   # prev window
        self.assertEqual(srcs["INTEREST"][1], 20.0)
        both = reporting.income_window(self.conn, self.today, "all")
        self.assertIsNone(both["prev_kinds"])
        self.assertIsNone(both["sources"][0][3])


if __name__ == "__main__":
    unittest.main()
