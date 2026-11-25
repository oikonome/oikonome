"""A change against an earlier period is the printed figure minus the
printed figure (reporting.change): a $0 difference as printed reads
unchanged, the percent is of the exact figures with halves rounded the
same way up as down, and every tab that prints one — Spending, Income,
"What changed", the Year lens — reads it from the server
(specs/shown-figures.md, "Changes vs before")."""

import datetime as dt
import unittest

from oikonome.engine import reporting
from oikonome.engine.reporting import change
from oikonome.web import lenses

from .util import make_db, seed_accounts, write_config


def _txn(conn, acct, day, amount, name, **cols):
    keys = ["id", "account_id", "date", "amount", "name", *cols]
    vals = [f"chg-{name}-{day}-{amount}", acct, day, amount, name,
            *cols.values()]
    conn.execute(f"INSERT INTO transactions ({', '.join(keys)}) VALUES "
                 f"({', '.join(['%s'] * len(keys))})", vals)


class ChangeTests(unittest.TestCase):
    def test_a_difference_that_prints_as_nothing_is_unchanged(self):
        self.assertEqual(change(1000.40, 1000.0),
                         {"amount": 0, "pct": None, "unchanged": True})
        self.assertEqual(change(1000.0, 1000.40),
                         {"amount": 0, "pct": None, "unchanged": True})

    def test_the_amount_is_the_printed_minus_the_printed(self):
        # $1,000.60 prints $1,001 and $1,000.40 prints $1,000
        self.assertEqual(change(1000.60, 1000.40)["amount"], 1)
        # halves to even, as money() prints them: $2.50 → $2, $3.50 → $4
        self.assertEqual(change(3.50, 2.50)["amount"], 2)

    def test_halves_round_the_same_way_up_and_down(self):
        self.assertEqual(change(1025.0, 1000.0)["pct"], 2)
        self.assertEqual(change(975.0, 1000.0)["pct"], -2)
        self.assertEqual(change(1035.0, 1000.0)["pct"], 4)
        self.assertEqual(change(965.0, 1000.0)["pct"], -4)
        self.assertEqual(change(1012.0, 1000.0)["pct"], 1)

    def test_a_percent_that_says_nothing_is_left_out(self):
        # forty times the earlier figure: the dollars say it, a four-digit
        # percent does not
        self.assertEqual(change(4100.0, 100.0),
                         {"amount": 4000, "pct": None, "unchanged": False})
        self.assertEqual(change(1099.0, 100.0)["pct"], 999)
        self.assertIsNone(change(1100.0, 100.0)["pct"])
        # +$6 on a large figure is not "+0%"
        self.assertIsNone(change(100006.0, 100000.0)["pct"])
        # a fall can never pass −100%, and −100% is still said
        self.assertEqual(change(0.0, 500.0)["pct"], -100)

    def test_a_percent_of_a_small_earlier_figure_is_left_out(self):
        # from $8 to $48: the dollars say it; five times of eight dollars
        # is not a percent anyone can use
        self.assertEqual(change(48.0, 8.0),
                         {"amount": 40, "pct": None, "unchanged": False})
        self.assertIsNone(change(20.0, 49.99)["pct"])
        self.assertIsNone(change(20.0, -30.0)["pct"])
        # at the floor and above it the percent is said
        self.assertEqual(change(75.0, 50.0)["pct"], 50)
        self.assertEqual(change(25.0, 50.0)["pct"], -50)

    def test_nothing_before_has_no_percent_and_no_earlier_figure_no_change(self):
        self.assertEqual(change(50.0, 0.0),
                         {"amount": 50, "pct": None, "unchanged": False})
        self.assertIsNone(change(50.0, None))


class WindowChangesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        seed_accounts(self.conn)
        self.bank = self.conn.execute(
            "SELECT id FROM accounts WHERE type='depository' LIMIT 1"
        ).fetchone()["id"]
        self.today = dt.date(2026, 9, 1)

    def tearDown(self):
        self.conn.close()

    def test_spending_changes_per_month_and_movers(self):
        _txn(self.conn, self.bank, dt.date(2025, 12, 1), 9.0, "OPENING",
             category_primary="GENERAL_SERVICES")
        # previous 3 months: dining $1,000, travel $400, a gym paid only then
        _txn(self.conn, self.bank, dt.date(2026, 4, 3), 1000.0, "BISTRO",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 4, 4), 400.0, "AIRLINE",
             category_primary="TRAVEL")
        _txn(self.conn, self.bank, dt.date(2026, 5, 2), 90.0, "GYM",
             category_primary="PERSONAL_CARE")
        # this window: dining $1,000.40, travel $600
        _txn(self.conn, self.bank, dt.date(2026, 7, 3), 1000.40, "BISTRO",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 8, 4), 600.0, "AIRLINE",
             category_primary="TRAVEL")
        w = reporting.spending_window(self.conn, self.today, "3m")
        cats = {c[0]: i for i, c in enumerate(w["categories"])}
        ch = w["category_changes"]
        self.assertEqual(len(ch), len(w["categories"]))
        self.assertTrue(ch[cats["FOOD_AND_DRINK"]]["unchanged"])
        self.assertEqual(ch[cats["TRAVEL"]],
                         {"amount": 200, "pct": 50, "unchanged": False})
        self.assertEqual(w["change"]["amount"],
                         reporting.whole_dollars(w["total"])
                         - reporting.whole_dollars(w["prev_total"]))
        self.assertEqual(w["per_month"], round(w["total"] / w["months"], 2))
        self.assertEqual(len(w["per_month_by_category"]),
                         len(w["categories"]))
        self.assertEqual(len(w["merchant_changes"]), len(w["merchants"]))
        # "What changed": travel up $200, the gym down $90; dining moved
        # 40 cents and is not listed
        movers = [(m["kind"], m["label"], m["change"]["amount"])
                  for m in w["movers"]]
        self.assertIn(("category", "TRAVEL", 200), movers)
        self.assertIn(("category", "PERSONAL_CARE", -90), movers)
        self.assertNotIn("FOOD_AND_DRINK", [m[1] for m in movers])

    def test_all_has_no_previous_window_and_no_changes(self):
        _txn(self.conn, self.bank, dt.date(2026, 8, 4), 600.0, "AIRLINE",
             category_primary="TRAVEL")
        w = reporting.spending_window(self.conn, self.today, "all")
        self.assertIsNone(w["change"])
        self.assertEqual(w["movers"], [])
        self.assertTrue(all(c is None for c in w["category_changes"]))

    def test_income_changes(self):
        _txn(self.conn, self.bank, dt.date(2025, 12, 1), 9.0, "OPENING",
             category_primary="GENERAL_SERVICES")
        _txn(self.conn, self.bank, dt.date(2026, 4, 1), -1000.0, "EMPLOYER",
             category_primary="INCOME")
        _txn(self.conn, self.bank, dt.date(2026, 7, 1), -1025.0, "EMPLOYER",
             category_primary="INCOME")
        w = reporting.income_window(self.conn, self.today, "3m")
        self.assertEqual(w["change"],
                         {"amount": 25, "pct": 2, "unchanged": False})
        self.assertEqual(w["source_changes"],
                         [{"amount": 25, "pct": 2, "unchanged": False}])
        self.assertEqual(set(w["kind_changes"]), set(w["kinds"]))


class YearLensChangesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        seed_accounts(self.conn)
        self.bank = self.conn.execute(
            "SELECT id FROM accounts WHERE type='depository' LIMIT 1"
        ).fetchone()["id"]

    def tearDown(self):
        self.conn.close()

    def test_each_category_and_the_total_carry_the_servers_change(self):
        _txn(self.conn, self.bank, dt.date(2025, 3, 3), 1000.0, "BISTRO",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 3, 3), 1000.40, "BISTRO",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, self.bank, dt.date(2026, 4, 3), 300.0, "AIRLINE",
             category_primary="TRAVEL")
        d = lenses.year_summary(self.conn, 2026, today=dt.date(2026, 12, 31))
        by = {c[0]: d["changes"]["categories"][i]
              for i, c in enumerate(d["categories"])}
        self.assertEqual(len(d["changes"]["categories"]), len(d["categories"]))
        self.assertTrue(by["FOOD AND DRINK"]["unchanged"])
        self.assertEqual(by["TRAVEL"]["amount"], 300)
        self.assertEqual(d["changes"]["spend"],
                         change(d["totals"]["spend"],
                                d["prev_totals"]["spend"]))


if __name__ == "__main__":
    unittest.main()
