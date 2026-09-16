"""A merchant search reports what one purchase there costs.

The total answers "how much have I given them"; the average answers "what
does a visit cost", which is usually the question a merchant search is
actually asking. Two things make the number honest, and both are easy to
lose:

  * it is computed over the WHOLE result set, not the page on screen — a
    page-local average would change as you paged through the same search;
  * it counts only what the verdict counts as spending, so a transfer or a
    card payment to the same payee cannot dilute the price of a purchase.

A search that matched no spending has no average at all. $0.00 would read
as a claim about the merchant rather than as "nothing here was a purchase".
"""

import unittest

from oikonome.web import data

from .util import add_txn, make_db


class SearchAverage(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _spend(self, term):
        # count + sum only; biz_count rides in the same dict for the page
        sp = data.search_transactions(self.conn, term)[4]
        return {"count": sp["count"], "sum": sp["sum"]}

    def test_average_is_the_mean_of_the_matching_purchases(self):
        for amt in (10.00, 20.00, 30.00):
            add_txn(self.conn, "2026-08-01", amt, "Daily Grind Coffee")
        spend = self._spend("daily grind")
        self.assertEqual(spend["count"], 3)
        self.assertEqual(spend["sum"], 60.00)
        self.assertEqual(round(spend["sum"] / spend["count"], 2), 20.00)

    def test_transfers_and_card_payments_are_not_purchases(self):
        add_txn(self.conn, "2026-08-01", 40.00, "Harvest Table")
        add_txn(self.conn, "2026-08-02", 800.00, "Harvest Table",
                primary="TRANSFER_OUT")
        add_txn(self.conn, "2026-08-03", 500.00, "Harvest Table",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        spend = self._spend("harvest table")
        # all three rows match the search and belong in its count and total
        rows, total, _amount_sum, _az, _, _hits = data.search_transactions(
            self.conn, "harvest table")
        self.assertEqual(total, 3)
        # ...but only the purchase is averaged
        self.assertEqual(spend["count"], 1)
        self.assertEqual(spend["sum"], 40.00)

    def test_a_manual_transfer_override_excludes_the_row(self):
        # the EFFECTIVE category decides: correcting a row to TRANSFER_OUT
        # must take it out of the average, or the correction only half works
        add_txn(self.conn, "2026-08-01", 40.00, "Sample Payee")
        add_txn(self.conn, "2026-08-02", 900.00, "Sample Payee",
                override="TRANSFER_OUT")
        self.assertEqual(self._spend("sample payee"), {"count": 1, "sum": 40.00})

    def test_money_in_is_not_spending(self):
        add_txn(self.conn, "2026-08-01", 25.00, "Refund Corp")
        add_txn(self.conn, "2026-08-02", -100.00, "Refund Corp")
        self.assertEqual(self._spend("refund corp"), {"count": 1, "sum": 25.00})

    def test_a_search_with_no_purchases_has_no_average(self):
        add_txn(self.conn, "2026-08-01", -2450.00, "Payroll Deposit")
        spend = self._spend("payroll")
        self.assertEqual(spend["count"], 0)
        # the endpoint turns a zero count into a null average rather than 0.00
        self.assertEqual(spend["sum"], 0)

    def test_the_average_covers_every_page_not_just_the_first(self):
        # 150 rows, so page 1 holds 100 of them. A page-local mean would
        # answer differently on page 2; this one must not move.
        for i in range(150):
            add_txn(self.conn, "2026-08-01", 10.00 if i < 100 else 40.00,
                    "Paged Merchant")
        first = data.search_transactions(self.conn, "paged merchant", page=1)
        second = data.search_transactions(self.conn, "paged merchant", page=2)
        self.assertEqual(len(first[0]), 100)
        self.assertEqual(len(second[0]), 50)
        self.assertEqual(first[4], second[4])
        self.assertEqual(first[4]["count"], 150)
        self.assertEqual(first[4]["sum"], 3000.00)   # 100×10 + 50×40


if __name__ == "__main__":
    unittest.main()
