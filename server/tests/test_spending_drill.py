"""A Spending figure opens exactly the rows it summed.

Every figure on the Spending tab — a category's total, a merchant's, the
window's whole spend, and a dropped merchant's previous-window figure —
links to the rows behind it (reporting.spending_window_ledger, the
`spend=` branch of GET /api/transactions). The drill's amount is the
figure, to the cent, and it lists no row the figure left out: refunds,
transfers, card payments, shadow twins, business rows and excluded
accounts. The design is specs/spending-drill.md.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import entities, reporting
from oikonome.web import api_ledger

from .util import TODAY, add_txn, make_db, write_config

# the stored keys the tab carries and links with
FOOD = "FOOD_AND_DRINK"
SHOPS = "GENERAL_MERCHANDISE"


class SpendingDrill(unittest.TestCase):
    def setUp(self):
        self.conn = c = make_db()
        c.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                  "balance_current) VALUES ('xcard','it1','Spare Card',"
                  "'credit','credit card',0), ('shd','it1','Twin Card',"
                  "'credit','credit card',0)")
        write_config(c, excluded_accounts=["xcard"])
        # the ledger opens long before every window, so each range has a
        # previous window to compare with
        add_txn(c, dt.date(2020, 1, 1), 5.0, "OPENING COFFEE",
                primary="FOOD_AND_DRINK")
        add_txn(c, dt.date(2020, 1, 1), -10.0, "OPENING DEPOSIT",
                account="chk", primary="INCOME")
        june = dt.date(2026, 6, 10)
        may = dt.date(2026, 5, 12)
        jul = dt.date(2026, 7, 3)
        self.kept = []
        for day in (june, may, jul):
            self.kept += [
                add_txn(c, day, 42.50, "HARBOR GROCER",
                        primary="FOOD_AND_DRINK"),
                add_txn(c, day, 12.10, "A-B-C HARDWARE"),
                add_txn(c, day + dt.timedelta(days=1), 7.90, "A B C HARDWARE"),
                # a hold still pending counts like any charge
                add_txn(c, day, 19.99, "CORNER DELI", primary="FOOD_AND_DRINK",
                        pending=1),
            ]
        # a purchase partly paid back: counted for the part kept
        self.reimbursed = add_txn(c, june, 80.0, "TEAM DINNER",
                                  primary="FOOD_AND_DRINK")
        back = add_txn(c, june, -30.0, "FRIEND REPAYS", account="chk",
                       primary="INCOME")
        c.execute("INSERT INTO reimbursements (expense_id, reimburse_id, "
                  "partial, amount) VALUES (%s, %s, 1, 30)",
                  (self.reimbursed, back))
        # one charge split across two categories
        self.split = add_txn(c, june, 100.0, "SUPERSTORE")
        for line, cat, amt in ((1, "FOOD_AND_DRINK", 65.0),
                               (2, "GENERAL_MERCHANDISE", 35.0)):
            c.execute("INSERT INTO transaction_splits (txn_id, line, "
                      "category, amount, source) VALUES (%s,%s,%s,%s,'hand')",
                      (self.split, line, cat, amt))
        # a merchant paid only before the 1m window: a dropped merchant
        self.dropped = add_txn(c, may, 55.0, "OLD GYM", primary="PERSONAL_CARE")
        # rows the figures leave out
        ent = entities.create_entity(c, name="Fixture Works LLC",
                                     structure="sole_prop")
        biz = add_txn(c, june, 300.0, "HARBOR GROCER",
                      primary="FOOD_AND_DRINK")
        c.execute("UPDATE transactions SET entity_id = %s WHERE id = %s",
                  (ent["id"], biz))
        self.left_out = [
            add_txn(c, june, -15.0, "HARBOR GROCER", primary="FOOD_AND_DRINK"),
            add_txn(c, june, 500.0, "MOVE TO SAVINGS", account="chk",
                    override="TRANSFER_OUT"),
            add_txn(c, june, 250.0, "CARD AUTOPAY", account="chk",
                    detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
            add_txn(c, june, 42.50, "HARBOR GROCER", account="shd",
                    primary="FOOD_AND_DRINK"),
            add_txn(c, june, 90.0, "HARBOR GROCER", account="xcard",
                    primary="FOOD_AND_DRINK"),
            biz,
        ]
        c.execute("SELECT set_config('app.shadow_ids', 'shd', false)")

    def tearDown(self):
        self.conn.close()

    def _drill(self, rk, **kw):
        return reporting.spending_window_ledger(self.conn, TODAY, rk, **kw)

    def test_spending_drill_total_equals_figure(self):
        for rk in reporting.FLOW_RANGES:
            w = reporting.spending_window(self.conn, TODAY, rk)
            with self.subTest(range=rk, figure="headline"):
                self.assertEqual(self._drill(rk)["amount"], w["total"])
            for cat, amt, _prev in w["categories"]:
                with self.subTest(range=rk, category=cat):
                    if amt:
                        self.assertEqual(
                            self._drill(rk, category=cat)["amount"], amt)
            for row in w["merchants"]:
                with self.subTest(range=rk, merchant=row[0]):
                    self.assertEqual(
                        self._drill(rk, merchant=row[0])["amount"], row[1])
            if w["prev_total"] is not None:
                with self.subTest(range=rk, figure="previous headline"):
                    self.assertEqual(
                        self._drill(rk, side="previous")["amount"],
                        w["prev_total"])
                for cat, _amt, prev in w["categories"]:
                    with self.subTest(range=rk, previous_category=cat):
                        if prev:
                            self.assertEqual(self._drill(
                                rk, category=cat, side="previous")["amount"],
                                prev)

    def test_spending_drill_lists_no_row_the_figure_left_out(self):
        for rk in ("1m", "3m", "1y", "all"):
            for kw in ({}, {"category": FOOD}, {"merchant": "HARBOR GROCER"}):
                ids = set(self._drill(rk, **kw)["txn_ids"])
                with self.subTest(range=rk, **kw):
                    self.assertFalse(ids & set(self.left_out))
        june = set(self._drill("1m")["txn_ids"])
        self.assertIn(self.reimbursed, june)
        self.assertIn(self.split, june)

    def test_spending_drill_folds_punctuation_variants_like_the_figure(self):
        w = reporting.spending_window(self.conn, TODAY, "1m")
        row = next(r for r in w["merchants"] if "HARDWARE" in r[0])
        self.assertEqual(row[1], 20.0)
        for name in ("A-B-C HARDWARE", "A B C HARDWARE", "a.b.c. hardware"):
            d = self._drill("1m", merchant=name)
            with self.subTest(name=name):
                self.assertEqual(d["amount"], 20.0)
                self.assertEqual(len(d["txn_ids"]), 2)

    def test_spending_drill_split_row_listed_once_counted_by_its_part(self):
        food = self._drill("1m", category=FOOD)
        shops = self._drill("1m", category=SHOPS)
        self.assertEqual(food["txn_ids"].count(self.split), 1)
        self.assertEqual(shops["txn_ids"].count(self.split), 1)
        # Harbor Grocer 42.50 + deli 19.99 + dinner 50 kept + 65 of the split
        self.assertEqual(food["amount"], 177.49)
        self.assertEqual(shops["amount"], 55.0)

    def test_spending_drill_previous_side_matches_dropped_merchant_amount(self):
        w = reporting.spending_window(self.conn, TODAY, "1m")
        gone = next(r for r in w["dropped_merchants"] if r[0] == "OLD GYM")
        d = self._drill("1m", merchant="OLD GYM", side="previous")
        self.assertEqual(d["amount"], gone[3])
        self.assertEqual(d["txn_ids"], [self.dropped])
        self.assertIn("the window before", d["label"])
        mover = next(m for m in w["movers"] if m["label"] == "OLD GYM")
        self.assertEqual(d["amount"], mover["previous"])

    def test_spending_drill_refuses_unknown_range(self):
        for kw in ({"range_key": "2w"}, {"range_key": "1m", "side": "next"},
                   {"range_key": "all", "side": "previous"}):
            with self.subTest(**kw), self.assertRaises(ValueError):
                reporting.spending_window_ledger(self.conn, TODAY, **kw)

    def test_spending_drill_label_names_the_figure_and_its_days(self):
        self.assertEqual(self._drill("1m", category=FOOD)["label"],
                         "Food and Drink · Jun 2026")
        self.assertEqual(self._drill("cur")["label"],
                         "All spending · Jul 1–15 so far")

    def test_transactions_door_lists_the_figures_rows_and_prints_the_figure(self):
        w = reporting.spending_window(self.conn, TODAY, "1m")
        cat_fig = dict((c, a) for c, a, _p in w["categories"])[FOOD]
        with mock.patch("oikonome.web.api_ledger._today",
                        return_value=TODAY):
            out = api_ledger._transactions_payload(
                self.conn, spend="1m", cat=FOOD, q="ignored", acct="chk")
        self.assertEqual(out["amount_sum"], cat_fig)
        self.assertTrue(out["counted"])
        self.assertEqual(out["drill_label"], "Food and Drink · Jun 2026")
        self.assertEqual({r["id"] for r in out["rows"]},
                         set(self._drill("1m", category=FOOD)["txn_ids"]))
        self.assertIsNotNone(out["per_day"])

    def test_a_category_drill_sums_averages_and_dates_the_categorys_part(self):
        """The list's sum, its "avg of N" and each day divider are the
        figure's: the split charge counts for its food part (65), the
        partly repaid dinner for what was kept (50) — never the whole
        charge."""
        with mock.patch("oikonome.web.api_ledger._today",
                        return_value=TODAY):
            out = api_ledger._transactions_payload(self.conn, spend="1m",
                                                   cat=FOOD)
        self.assertEqual(out["spend_sum"], 177.49)
        self.assertEqual(out["spend_count"], 4)
        self.assertEqual(out["spend_avg"], round(177.49 / 4, 2))
        june10 = dt.date(2026, 6, 10).isoformat()
        # Harbor Grocer 42.50 + deli 19.99 + dinner 50 + the split's 65
        self.assertEqual(out["day_spend"][june10], 177.49)

    def test_a_category_drill_narrowed_by_search_keeps_its_rows(self):
        """The tab links a category with the key the ledger stores, so the
        drill's `cat`, kept when the list is narrowed by a search, an
        account or a date, still matches the rows it opened."""
        w = reporting.spending_window(self.conn, TODAY, "1m")
        self.assertIn(FOOD, [c for c, _a, _p in w["categories"]])
        with mock.patch("oikonome.web.api_ledger._today",
                        return_value=TODAY):
            out = api_ledger._transactions_payload(
                self.conn, q="grocer", cat=FOOD)
        self.assertGreater(out["total"], 0)
        self.assertEqual({r["category"] for r in out["rows"]},
                         {"FOOD AND DRINK"})

    def test_an_older_clients_spaced_category_still_drills(self):
        """A phone or a tab built before the key sends the spaced form;
        it opens the same rows and figure."""
        self.assertEqual(self._drill("1m", category="FOOD AND DRINK"),
                         self._drill("1m", category=FOOD))

    def test_transactions_door_refuses_an_unknown_range(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as e:
            api_ledger._transactions_payload(self.conn, spend="2w")
        self.assertEqual(e.exception.status_code, 400)

    def test_older_client_without_spend_unchanged(self):
        out = api_ledger._transactions_payload(self.conn, y=2026, m=6)
        self.assertEqual(out["mode"], "month")
        self.assertNotIn("drill_label", out)


class OneCategoryTwoSpellings(unittest.TestCase):
    """The ledger holds a category both as the bank's underscored key and
    in the spaced form: the tab counts them as one category, named by the
    spelling that carries most of it, and its drill opens both rows."""

    def setUp(self):
        self.conn = c = make_db()
        write_config(c)
        add_txn(c, dt.date(2020, 1, 1), 5.0, "OPENING COFFEE")
        add_txn(c, dt.date(2026, 6, 3), 900.0, "HILLSIDE POWER",
                primary="RENT_AND_UTILITIES")
        add_txn(c, dt.date(2026, 6, 9), 60.0, "HILLSIDE WATER",
                primary="RENT AND UTILITIES")

    def tearDown(self):
        self.conn.close()

    def test_one_row_named_by_the_stored_key_opens_both_charges(self):
        w = reporting.spending_window(self.conn, TODAY, "1m")
        self.assertEqual([c[:2] for c in w["categories"]],
                         [["RENT_AND_UTILITIES", 960.0]])
        d = reporting.spending_window_ledger(self.conn, TODAY, "1m",
                                             category="RENT_AND_UTILITIES")
        self.assertEqual((len(d["txn_ids"]), d["amount"]), (2, 960.0))


if __name__ == "__main__":
    unittest.main()
