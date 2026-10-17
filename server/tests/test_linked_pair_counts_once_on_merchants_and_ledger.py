"""A dual-sourced card counts once on the merchant catalog and the ledger's
spend figures, and the Accounts page's exclusions reach the ledger too.

One real card reached through two feeds is two account rows carrying the
same purchases; once linked, the second is a shadow and every money figure
must read the primary alone. The merchant catalog's count and total (and
so its spend sort and one-off filter), the merchant card, the ledger's
per-row spend flag and figure, and the search's spend totals must all
agree with the Spending page.
"""
import datetime as dt
import unittest

from oikonome.engine import budget, links, merchant_dedup, reporting
from oikonome.web import data

from .test_reporting import add_account, add_item
from .util import TODAY, add_txn, make_db, write_config


class LinkedPairTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_item(self.conn, "fa", "Example Bank")
        add_item(self.conn, "fb", "Example Bank Mirror")
        add_account(self.conn, "c1", "Card", "credit", "credit card", 0,
                    item="fa", mask="4242")
        add_account(self.conn, "c2", "Card", "credit", "credit card", 0,
                    item="fb", mask="4242")
        d = dt.date(TODAY.year, TODAY.month, 3)
        for acc in ("c1", "c2"):
            add_txn(self.conn, d, 42.00, "HARBORLIGHT BAKERY",
                    merchant="Harborlight Bakery", account=acc,
                    primary="FOOD_AND_DRINK")
        links.create(self.conn, ["c1", "c2"])
        links.set_shadow_scope(self.conn)

    def tearDown(self):
        self.conn.close()

    def _spending_figure(self):
        sp = reporting.compute_spending(self.conn, TODAY)
        return {r[0]: r[1] for r in sp["top_merchants"]}["Harborlight Bakery"]

    def test_listing_counts_the_pair_once(self):
        row = next(r for r in merchant_dedup.listing(self.conn)
                   if r["display"] == "Harborlight Bakery")
        self.assertEqual(row["rows"], 1)
        self.assertEqual(float(row["total"]), self._spending_figure())
        self.assertEqual(row["business_rows"], 0)

    def test_oneoff_filter_sees_one_visit(self):
        names = [r["display"] for r in
                 merchant_dedup.listing(self.conn, filter="oneoff")]
        self.assertIn("Harborlight Bakery", names)

    def test_detail_counts_the_pair_once(self):
        d = merchant_dedup.detail(self.conn, "Harborlight Bakery")
        self.assertEqual(d["rows"], 1)
        self.assertEqual(float(d["total"]), self._spending_figure())

    def test_today_verdict_counts_the_pair_once(self):
        # the Today page and its email render month_status; no separate query
        ms = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(ms["buckets"]["food"]["actual"], 42.00, places=2)


class LedgerSpendFigureTests(unittest.TestCase):
    """`spend_amount` is what a ledger row adds to the day's spending, by
    the verdict's own rule: net of a partial reimbursement, and 0 on an
    account the Accounts page excludes."""

    def setUp(self):
        self.conn = make_db()
        add_item(self.conn, "fx", "Example Bank")
        add_account(self.conn, "side", "Side Card", "credit", "credit card",
                    0, item="fx", mask="7777")
        write_config(self.conn, excluded_accounts=["side"])
        d = dt.date(TODAY.year, TODAY.month, 4)
        add_txn(self.conn, d, 30.00, "MAPLEWORK HARDWARE", txn_id="kept",
                merchant="Maplework Hardware")
        add_txn(self.conn, d, 20.00, "MAPLEWORK HARDWARE", txn_id="excl",
                account="side", merchant="Maplework Hardware")

    def tearDown(self):
        self.conn.close()

    def test_excluded_account_row_adds_nothing(self):
        rows = {r["id"]: r for r in
                data.transactions(self.conn, TODAY.year, TODAY.month)}
        self.assertTrue(rows["kept"]["counts_as_spend"])
        self.assertEqual(rows["kept"]["spend_amount"], 30.00)
        self.assertFalse(rows["excl"]["counts_as_spend"])
        self.assertEqual(rows["excl"]["spend_amount"], 0)
        day = sum(r["spend_amount"] for r in rows.values())
        ms = budget.month_status(self.conn, TODAY)
        b = ms["buckets"]
        self.assertAlmostEqual(
            day, b["fixed"]["actual"] + b["food"]["actual"] + b["other"]["actual"],
            places=2)

    def test_search_spend_totals_drop_the_excluded_account(self):
        _, total, _, _, spend, _ = data.search_transactions(self.conn, "maplework")
        self.assertEqual(total, 2)
        self.assertEqual(spend["count"], 1)
        self.assertEqual(spend["sum"], 30.00)


if __name__ == "__main__":
    unittest.main()
