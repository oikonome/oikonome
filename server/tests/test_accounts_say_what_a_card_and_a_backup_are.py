"""The Accounts page says what a card's due date and a backup login mean.

A card read "due 11/15" two days after the 15th; it now reads "paid 11/14"
when a payment came in after the statement was issued (the bank's last
payment date, or a credit on the card) and "past due since 11/15"
otherwise. A login whose accounts are all linked backups had a "$0"
heading over full balances (its balances count through the serving copy);
the server now sends "backup of <serving connection> — not counted" for
it, and no subtotal."""

import datetime as dt
import json
import unittest

from oikonome.engine import links, reporting
from oikonome.web import data

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)


def _due(conn, **raw):
    conn.execute("INSERT INTO liabilities (account_id, raw) VALUES "
                 "('card', %s)", (json.dumps(raw),))


def _words(conn):
    rows = [dict(r) for r in data.accounts_detail(conn, counts=False)]
    data.card_due_words(conn, rows, TODAY)
    return {r["id"]: r["card_due_text"] for r in rows}


class CardDueWords(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()           # the card owes $250
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_due_date_ahead_reads_due(self):
        _due(self.conn, next_payment_due_date="2026-11-25")
        self.assertEqual(_words(self.conn)["card"], "due 11/25")

    def test_a_passed_due_date_with_no_payment_reads_past_due(self):
        _due(self.conn, next_payment_due_date="2026-11-15",
             last_statement_issue_date="2026-10-20",
             last_payment_date="2026-10-14")
        self.assertEqual(_words(self.conn)["card"], "past due since 11/15")

    def test_a_payment_after_the_statement_reads_paid(self):
        _due(self.conn, next_payment_due_date="2026-11-15",
             last_statement_issue_date="2026-10-20",
             last_payment_date="2026-11-14")
        self.assertEqual(_words(self.conn)["card"], "paid 11/14")

    def test_a_credit_on_the_card_in_the_ledger_reads_paid(self):
        _due(self.conn, next_payment_due_date="2026-11-15")
        add_txn(self.conn, dt.date(2026, 11, 12), -180.0, "PAYMENT THANK YOU",
                account="card", primary="LOAN_PAYMENTS")
        self.assertEqual(_words(self.conn)["card"], "paid 11/12")

    def test_a_refund_after_the_statement_is_not_a_payment(self):
        _due(self.conn, next_payment_due_date="2026-11-15",
             last_statement_issue_date="2026-10-20")
        add_txn(self.conn, dt.date(2026, 11, 3), -42.0, "RETURN CREDIT",
                account="card", primary="GENERAL_MERCHANDISE")
        add_txn(self.conn, dt.date(2026, 11, 9), -180.0, "PAYMENT THANK YOU",
                account="card", primary="LOAN_PAYMENTS", pending=1)
        self.assertEqual(_words(self.conn)["card"], "past due since 11/15")

    def test_the_banks_last_payment_date_decides(self):
        # the bank says the last payment came before this statement; a
        # credit the ledger files as a payment does not overrule it
        _due(self.conn, next_payment_due_date="2026-11-15",
             last_statement_issue_date="2026-10-20",
             last_payment_date="2026-10-14")
        add_txn(self.conn, dt.date(2026, 11, 5), -60.0, "ONLINE PAYMENT",
                account="card", primary="LOAN_PAYMENTS")
        self.assertEqual(_words(self.conn)["card"], "past due since 11/15")

    def test_nothing_owed_says_nothing(self):
        _due(self.conn, next_payment_due_date="2026-11-15")
        self.conn.execute("UPDATE accounts SET balance_current=0 "
                          "WHERE id='card'")
        self.assertIsNone(_words(self.conn)["card"])


class ABackupLogin(unittest.TestCase):
    """A second login feeds a copy of the checking account, linked to it
    and standing by as its backup."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES ('it2','plan_csv','Test Bank Export','x')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('chk2','it2','Checking copy',"
            "'depository','checking',5000)")
        links.create(self.conn, ["chk", "chk2"])

    def tearDown(self):
        self.conn.close()

    def test_the_backup_login_has_words_and_no_subtotal(self):
        self.assertEqual(links.shadow_ids(self.conn), ["chk2"])
        t = reporting.account_totals(self.conn)
        self.assertEqual(t["backups"],
                         {"it2": "backup of Test Bank — not counted"})
        bank = t["sections"]["personal"]["Bank"]
        self.assertNotIn("it2", bank["by_item"])
        self.assertNotIn("it2", bank["shown"]["by_item"])

if __name__ == "__main__":
    unittest.main()
