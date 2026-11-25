"""The Cash Flow breakdowns print the server's shares and dollars: the
Spending tab's category shares, the Income tab's payer shares, the flow
picture's nodes and the report email's ring each add up — shares to 100,
dollar parts to the total printed beside them — by largest remainder, so
a row may be a point (or a dollar) off its own rounding
(specs/shown-figures.md)."""

import datetime as dt
import unittest

from oikonome.engine import reporting
from oikonome.engine.reporting import shown_shares
from oikonome.web import lens_email

from .util import make_db, seed_accounts, write_config


def _txn(conn, acct, day, amount, name, **cols):
    keys = ["id", "account_id", "date", "amount", "name", *cols]
    vals = [f"share-{name}-{day}", acct, day, amount, name, *cols.values()]
    conn.execute(f"INSERT INTO transactions ({', '.join(keys)}) VALUES "
                 f"({', '.join(['%s'] * len(keys))})", vals)


class SharesOfABaseTests(unittest.TestCase):
    def test_parts_that_are_the_whole_add_to_100(self):
        self.assertEqual(shown_shares([500, 500, 500]), [34, 33, 33])
        self.assertEqual(shown_shares([500, 500, 500], 1500), [34, 33, 33])
        # ties go to the earlier part
        self.assertEqual(shown_shares([1, 1, 1, 1, 1, 1]),
                         [17, 17, 17, 17, 16, 16])

    def test_parts_of_a_larger_base_add_to_their_own_share(self):
        # $1,000 of $4,000 drawn: 25% in all, split 13 / 12
        self.assertEqual(shown_shares([500, 500], 4000), [13, 12])
        self.assertEqual(shown_shares([0, -5, 300], 600), [None, None, 50])
        self.assertEqual(shown_shares([10, 10], 0), [None, None])

    def test_many_cases_add_up(self):
        for vals in ([7, 13, 19, 23, 29, 31], [0.01, 0.01, 999.98],
                     [333.33, 333.33, 333.34], [2500, 10, 490]):
            with self.subTest(vals=vals):
                self.assertEqual(sum(shown_shares(vals)), 100)
                self.assertEqual(sum(shown_shares(vals, sum(vals))), 100)


class FlowNodesTests(unittest.TestCase):
    def _nodes(self, side):
        return {n["key"]: (n["dollars"], n["share"]) for n in side}

    def test_the_spending_side_adds_to_100(self):
        inc = {"total": 4000.0, "paychecks": 4000.0, "interest": 0.0,
               "other": 0.0}
        ins, outs = reporting._flow_shown(
            inc, 1400.0, {"fixed": 700.0, "variable": 700.0}, 2600.0)
        self.assertEqual(self._nodes(outs), {"fixed": (700, 18),
                                             "variable": (700, 17),
                                             "saved": (2600, 65)})
        self.assertEqual(self._nodes(ins)["paychecks"], (4000, 100))

    def test_income_dollars_add_up_to_came_in(self):
        inc = {"total": 3001.5, "paychecks": 2500.5, "interest": 10.5,
               "other": 490.5}
        ins, _outs = reporting._flow_shown(
            inc, 3000.0, {"fixed": 1000.0, "variable": 2000.0}, 1.5)
        nodes = self._nodes(ins)
        self.assertEqual(sum(d for d, _p in nodes.values()), 3002)
        self.assertEqual(sum(p for _d, p in nodes.values()), 100)

    def test_an_overspend_carries_dollars_and_no_share(self):
        inc = {"total": 1000.0, "paychecks": 1000.0, "interest": 0.0,
               "other": 0.0}
        ins, outs = reporting._flow_shown(
            inc, 1250.0, {"fixed": 500.0, "variable": 750.0}, -250.0)
        self.assertEqual(self._nodes(ins)["overspent"], (250, None))
        self.assertEqual(self._nodes(outs), {"fixed": (500, 40),
                                             "variable": (750, 60)})

    def test_unknown_income_has_no_income_side(self):
        inc = {"total": None, "paychecks": None, "interest": None,
               "other": None}
        ins, outs = reporting._flow_shown(
            inc, 900.0, {"fixed": 300.0, "variable": 600.0}, None)
        self.assertIsNone(ins)
        self.assertEqual(self._nodes(outs), {"fixed": (300, 33),
                                             "variable": (600, 67)})


class WindowSharesTests(unittest.TestCase):
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

    def test_three_equal_categories_read_34_33_33(self):
        _txn(self.conn, self.bank, dt.date(2025, 12, 1), 9.0, "OPENING",
             category_primary="GENERAL_SERVICES")
        for i, cat in enumerate(("FOOD_AND_DRINK", "ENTERTAINMENT",
                                 "TRAVEL")):
            _txn(self.conn, self.bank, dt.date(2026, 8, 3 + i), 500.0,
                 f"SHOP{i}", category_primary=cat)
        # spent on only before: listed after, with no share
        _txn(self.conn, self.bank, dt.date(2026, 4, 2), 80.0, "GYM",
             category_primary="PERSONAL_CARE")
        w = reporting.spending_window(self.conn, self.today, "3m")
        self.assertEqual(len(w["category_shares"]), len(w["categories"]))
        self.assertEqual(w["category_shares"], [34, 33, 33, None])
        self.assertEqual(w["categories"][-1][1], 0.0)

    def test_payer_shares_add_to_100_across_every_payer(self):
        for i in range(18):
            _txn(self.conn, self.bank, dt.date(2026, 8, 1 + i), -(100.0 + i),
                 f"PAYER {chr(65 + i)}", category_primary="INCOME")
        w = reporting.income_window(self.conn, self.today, "3m")
        self.assertEqual(len(w["sources"]), 15)
        self.assertEqual(len(w["source_shares"]), 15)
        # the three left off the list hold the rest of the 100
        self.assertLess(sum(w["source_shares"]), 100)
        self.assertGreater(sum(w["source_shares"]), 80)

    def test_three_equal_payers_read_34_33_33(self):
        for i in range(3):
            _txn(self.conn, self.bank, dt.date(2026, 8, 1 + i), -500.0,
                 f"PAYER {i}", category_primary="INCOME")
        w = reporting.income_window(self.conn, self.today, "3m")
        self.assertEqual(w["source_shares"], [34, 33, 33])


class EmailRingTests(unittest.TestCase):
    def test_the_ring_legend_adds_up(self):
        pie = lens_email._pie([("FOOD_AND_DRINK", 500.0),
                               ("ENTERTAINMENT", 500.0),
                               ("TRAVEL", 500.0)], 0.0)
        self.assertEqual([c["pct"] for c in pie["cats"]], [34, 33, 33])
        self.assertEqual(pie["cat_total_text"], "$1,500")

    def test_dollars_add_up_to_the_total_after_the_fold(self):
        cats = [(f"CAT_{i}", 100.4) for i in range(5)] + [("TINY", 1.4)]
        pie = lens_email._pie(cats, 0.0, max_slices=3)
        dollars = [int(c["amount_text"].lstrip("$").replace(",", ""))
                   for c in pie["cats"]]
        self.assertEqual(sum(dollars), 503)
        self.assertEqual(pie["cat_total_text"], "$503")
        self.assertEqual(sum(c["pct"] for c in pie["cats"]), 100)


if __name__ == "__main__":
    unittest.main()
