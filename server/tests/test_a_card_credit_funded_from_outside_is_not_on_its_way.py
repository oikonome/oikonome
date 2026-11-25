"""A card credit whose other leg sits outside the walked checking — a
balance transfer from another card, or a payment made from an excluded
account — is not a payment on its way out of checking
(forecast.payments_on_their_way): its funding already left an account
the walk never reads, so walking it out of checking would charge checking
money it never pays. A credit with no leg anywhere still is.

A leg of a weak shape — a row with no category (a payment imported from a
CSV or a statement), or one filed as a bare loan payment — pairs only when
its name names the credited card (a distinctive word of the card's or its
bank's name, or the card's last four digits): on the walked checking such
a row already took the money, so walking it again would pay the card
twice; anywhere else it is the outside funding. Amount alone never pairs
it: a same-amount car-loan payment or a transfer to a person is a
coincidence. Figures and names are invented."""

import datetime as dt
import unittest

from oikonome.engine import forecast

from .util import TODAY, add_txn, make_db, write_config

LABEL = "Card payment on its way — Test Card"
CREDIT_DAY = TODAY - dt.timedelta(days=1)


class FundedFromOutside(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)
        self.conn.execute("UPDATE accounts SET balance_available=3000, "
                          "balance_current=3000 WHERE id='chk'")
        self.conn.execute("UPDATE accounts SET balance_current=60 "
                          "WHERE id='card'")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES "
            "('card2','it1','Second Card','credit','credit card',1700,NULL),"
            "('chk2','it1','Side Checking','depository','checking',900,900)")
        self.conn.execute("UPDATE accounts SET official_name = "
                          "'Fernwood Rewards Visa', mask = '4417' "
                          "WHERE id = 'card'")
        add_txn(self.conn, CREDIT_DAY, -1300.0, "PAYMENT THANK YOU",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")

    def tearDown(self):
        self.conn.close()

    def _on_way(self):
        f = forecast.build(self.conn, today=TODAY, days=60)
        return [(d, a) for d, a, p in f["events"] if p == LABEL]

    def test_a_balance_transfer_from_another_card_is_not_on_its_way(self):
        add_txn(self.conn, CREDIT_DAY, 1300.0, "BALANCE TRANSFER",
                account="card2", primary="TRANSFER_OUT")
        self.assertEqual(self._on_way(), [])

    def test_a_payment_from_an_excluded_account_is_not_on_its_way(self):
        write_config(self.conn, food_monthly=0, other_monthly=0,
                     excluded_accounts=["chk2"])
        add_txn(self.conn, CREDIT_DAY, 1300.0, "ONLINE PMT CARD",
                account="chk2", primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        self.assertEqual(self._on_way(), [])

    def test_a_loan_payment_from_the_walked_checking_is_no_funding(self):
        # the same amount leaves checking for a car loan: a coincidence
        add_txn(self.conn, CREDIT_DAY, 1300.0, "AUTO LOAN PMT",
                account="chk", primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CAR_PAYMENT")
        self.assertEqual(len(self._on_way()), 1)

    def test_a_p2p_charge_on_another_card_is_no_funding(self):
        add_txn(self.conn, CREDIT_DAY, 1300.0, "PAYAPP SEND",
                account="card2", primary="TRANSFER_OUT",
                detailed="TRANSFER_OUT_TRANSFER_TO_APPS")
        self.assertEqual(len(self._on_way()), 1)

    def test_an_uncategorised_row_on_an_excluded_account_is_no_funding(self):
        write_config(self.conn, food_monthly=0, other_monthly=0,
                     excluded_accounts=["chk2"])
        add_txn(self.conn, CREDIT_DAY, 1300.0, "SOMETHING", account="chk2",
                primary="")
        self.assertEqual(len(self._on_way()), 1)

    def test_an_uncategorised_payment_from_checking_naming_the_card(self):
        # a statement import: the money already left the walked checking
        add_txn(self.conn, CREDIT_DAY, 1300.0, "FERNWOOD CRD EPAY",
                account="chk", primary="")
        self.assertEqual(self._on_way(), [])

    def test_an_uncategorised_payment_from_checking_naming_the_last_four(self):
        add_txn(self.conn, CREDIT_DAY, 1300.0, "PMT TO CARD XXXX4417",
                account="chk", primary="")
        self.assertEqual(self._on_way(), [])

    def test_an_uncategorised_row_on_checking_naming_no_card_is_no_leg(self):
        add_txn(self.conn, CREDIT_DAY, 1300.0, "TRANSFER TO R MARLOWE",
                account="chk", primary="")
        self.assertEqual(len(self._on_way()), 1)

    def test_a_bare_loan_payment_from_an_excluded_account_naming_the_card(
            self):
        write_config(self.conn, food_monthly=0, other_monthly=0,
                     excluded_accounts=["chk2"])
        add_txn(self.conn, CREDIT_DAY, 1300.0, "FERNWOOD ONLINE PMT",
                account="chk2", primary="LOAN_PAYMENTS")
        self.assertEqual(self._on_way(), [])

    def test_a_bare_loan_payment_from_an_excluded_account_naming_no_card(
            self):
        write_config(self.conn, food_monthly=0, other_monthly=0,
                     excluded_accounts=["chk2"])
        add_txn(self.conn, CREDIT_DAY, 1300.0, "BRAMBLE FINANCE PMT",
                account="chk2", primary="LOAN_PAYMENTS")
        self.assertEqual(len(self._on_way()), 1)

    def test_a_car_loan_at_the_cards_bank_is_no_funding(self):
        # filed as another kind of loan: never a card payment, whatever
        # its name says
        add_txn(self.conn, CREDIT_DAY, 1300.0, "FERNWOOD AUTO LOAN",
                account="chk", primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CAR_PAYMENT")
        self.assertEqual(len(self._on_way()), 1)

    def _issuer(self, bank, official, mask):
        self.conn.execute("UPDATE items SET institution_name = %s", (bank,))
        self.conn.execute("UPDATE accounts SET official_name = %s, "
                          "mask = %s WHERE id = 'card'", (official, mask))

    def test_the_issuers_name_pairs_and_its_filler_does_not(self):
        amex = ("American Express", "American Express Blue Cash Everyday",
                "1004")
        cap = ("Capital One", "Capital One Quicksilver World Elite", "7712")
        cases = [
            (amex, "AMEX EPAYMENT ACH PMT", True),
            (amex, "AMERICAN EXPRESS ACH PMT", True),
            (cap, "CAPITAL ONE ONLINE PMT", True),
            (amex, "PAYMENT XXXX1004", True),
            (amex, "AMERICAN AIRLINES 0012345", False),
            (amex, "EXPRESS SCRIPTS PHARMACY", False),
            (amex, "BLUE CROSS OF FERNDALE", False),
            (cap, "CAPITAL CITY UTILITIES", False),
            (cap, "WORLD ACCEPTANCE LOAN", False),
            (amex, "CHECK 1004", False),
            (amex, "CHECK #1004", False),
            (amex, "ACH XFER 20261004", False),
            (amex, "FIRST CITIZENS WORLD", False),
        ]
        for issuer, name, pairs in cases:
            with self.subTest(name=name):
                self.conn.execute("DELETE FROM transactions WHERE amount > 0")
                self._issuer(*issuer)
                add_txn(self.conn, CREDIT_DAY, 1300.0, name, account="chk",
                        primary="")
                self.assertEqual(len(self._on_way()), 0 if pairs else 1)

    def test_a_credit_with_no_leg_anywhere_still_is(self):
        # a same-amount purchase on the credited card itself is no leg
        add_txn(self.conn, CREDIT_DAY, 1300.0, "BLUEWATER SOFAS",
                primary="GENERAL_MERCHANDISE")
        self.assertEqual(len(self._on_way()), 1)


if __name__ == "__main__":
    unittest.main()
