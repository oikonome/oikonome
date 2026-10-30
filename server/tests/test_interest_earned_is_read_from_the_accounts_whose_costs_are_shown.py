"""The interest-earned figure beside "Interest and fees paid" is read from
the same accounts the costs are.

The cost side leaves an investment provider's accounts out (their fee drag
is the fees report's subject). The earned side read every depository
account instead, so a provider's cash account added its interest to
"earned" while its fees were never counted as paid — "net vs earned" set
one set of accounts against another. Both sides now read one set; the
earned side its depository half, because a card's interest refund is
already netted on the cost side.
"""

import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import TODAY, add_txn, make_db, write_config


class InterestEarnedOnTheSameAccounts(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        tok = reporting._INV_PROVIDERS[0]
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, access_token)"
            " VALUES ('itp','test',%s,'tok-p')", (f"{tok.title()} Contoso",))
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('pcash','itp','Provider Cash','depository','savings',0)")

    def _report(self):
        return reporting.compute_cash_costs(self.conn, today=TODAY)

    def test_a_providers_cash_interest_is_not_counted_as_earned(self):
        add_txn(self.conn, dt.date(TODAY.year, 3, 31), -40.0, "INTEREST PAID",
                primary="INCOME", detailed="INCOME_INTEREST_EARNED",
                account="pcash")
        add_txn(self.conn, dt.date(TODAY.year, 3, 31), -2.0, "INTEREST PAID",
                primary="INCOME", detailed="INCOME_INTEREST_EARNED",
                account="chk")
        # its fee is not a cash cost either — the two sides agree
        add_txn(self.conn, dt.date(TODAY.year, 4, 1), 5.0, "MONTHLY FEE",
                primary="BANK_FEES", detailed="BANK_FEES_OTHER_BANK_FEES",
                account="pcash")
        r = self._report()
        self.assertEqual(r["earned_ytd"], 2.0)
        self.assertEqual(r["by_year"], [])

    def test_a_card_interest_refund_is_not_earned_interest(self):
        add_txn(self.conn, dt.date(TODAY.year, 2, 20), 30.0,
                "PURCHASE INTEREST CHARGE", primary="BANK_FEES",
                detailed="BANK_FEES_INTEREST_CHARGE", account="card")
        add_txn(self.conn, dt.date(TODAY.year, 3, 2), -30.0,
                "INTEREST CHARGE REVERSAL", primary="BANK_FEES",
                detailed="BANK_FEES_INTEREST_CHARGE", account="card")
        r = self._report()
        self.assertEqual(r["earned_ytd"], 0.0)
        self.assertEqual(r["by_year"][0]["refunds"], 30.0)

    def test_the_bank_side_sets_match(self):
        bank = set(reporting._cash_account_ids(self.conn,
                                                types=("depository",)))
        self.assertEqual(bank, {"chk"})
        self.assertEqual(set(reporting._cash_account_ids(self.conn)),
                         {"chk", "card"})


if __name__ == "__main__":
    unittest.main()
