"""Discover's "Payments and Credits" category routes card payments
through the flow map.

Discover's activity CSV uses ONE category for two very different things:
card payments (an internal settlement — counting it plus the card's own
charges double-counts) and merchant credits/refunds (real money back that
should offset spend). Discover names payments explicitly ("DIRECTPAY FULL
BALANCE", "INTERNET PAYMENT - THANK YOU"), so the description decides:
payment-shaped rows land spend-excluded as
LOAN_PAYMENTS_CREDIT_CARD_PAYMENT, everything else in the category is a
refund and rides through untouched.
"""

import unittest

from oikonome.sync import csvimport, flowmap

from .util import make_db, write_config

# the Discover community script pushes this exact shape through the
# import hub with amount_sign=plaid (positive = purchase = money out)
DISCOVER = ("Trans. Date,Post Date,Description,Amount,Category\n"
            "07/10/2026,07/10/2026,SAFEWAY STORE 123,42.50,Supermarkets\n"
            "07/11/2026,07/11/2026,DIRECTPAY FULL BALANCE,-300.00,"
            "Payments and Credits\n"
            "07/12/2026,07/12/2026,AMAZON MKTPL REFUND,-25.00,"
            "Payments and Credits\n")

MAPPING = {"date": "Trans. Date", "amount": "Amount",
           "name": "Description", "category": "Category"}


class FlowCategoryTests(unittest.TestCase):
    def test_payment_shaped_rows_map_to_cc_payment(self):
        for name in ("DIRECTPAY FULL BALANCE", "INTERNET PAYMENT - THANK YOU",
                     "PHONE PAYMENT", "AUTOPAY 4417"):
            self.assertEqual(
                flowmap.flow_category("Payments and Credits", -300.0,
                                      name=name),
                ("LOAN_PAYMENTS", flowmap.CC_PAYMENT_DETAILED), name)

    def test_credit_rows_ride_through_as_refunds(self):
        # merchant credits are REAL money back, not settlements
        self.assertEqual(
            flowmap.flow_category("Payments and Credits", -25.0,
                                  name="AMAZON MKTPL REFUND"),
            (None, None))

    def test_other_categories_unaffected(self):
        self.assertEqual(flowmap.flow_category("Supermarkets", 42.5,
                                               name="SAFEWAY"),
                         (None, None))
        self.assertEqual(flowmap.flow_category("Credit Card Payment", -300.0),
                         ("LOAN_PAYMENTS", flowmap.CC_PAYMENT_DETAILED))


class DiscoverCsvTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_discover_csv_payment_row_is_spend_excluded(self):
        csvimport.import_csv(self.conn, "card", DISCOVER, MAPPING,
                             amount_sign="plaid")
        rows = {r["name"]: r for r in self.conn.execute(
            "SELECT name, amount, category_primary, category_detailed "
            "FROM transactions WHERE id LIKE 'csv:%'").fetchall()}
        pay = rows["DIRECTPAY FULL BALANCE"]
        self.assertEqual(pay["category_detailed"],
                         "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        self.assertEqual(pay["category_primary"], "LOAN_PAYMENTS")
        self.assertEqual(pay["amount"], -300.0)     # money in (to the card)
        # the refund stays a plain negative row — real money back
        refund = rows["AMAZON MKTPL REFUND"]
        self.assertIsNone(refund["category_detailed"])
        self.assertEqual(refund["amount"], -25.0)
        # ordinary purchase untouched
        self.assertEqual(rows["SAFEWAY STORE 123"]["category_primary"],
                         "Supermarkets")


if __name__ == "__main__":
    unittest.main()
