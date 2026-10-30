"""The first-run budget seed reads a row with no merchant name the way the
flow pass reads it: by the bank's whole statement line, routed only on
explicit wording.

Some banks spell an ordinary purchase "WITHDRAWAL POS #123 <store>" and a
utility's online bill "<biller> E-PAYMENT". The flow pass leaves those in
spend, so the verdict counts them; a seed that read them with the looser
merchant rules would leave them out as bank mechanics, and a new
household's starting budget would come out below what it actually spends.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, llm_categorize

from .util import TODAY, add_txn, make_db


def _last_month(day: int) -> dt.date:
    return (TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=day)


def _bare(conn, date, amount, line):
    """An uncategorized row whose source sent no merchant name."""
    tid = add_txn(conn, date, amount, line, primary=None)
    conn.execute("UPDATE transactions SET merchant_name = NULL WHERE id = %s",
                 (tid,))
    return tid


class SeedReadsBareStatementLinesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        # one complete closed month of history
        add_txn(self.conn, _last_month(1), 100.0, "HARDWARE BARN")

    def tearDown(self):
        self.conn.close()

    def test_a_bare_withdrawal_purchase_is_seeded(self):
        _bare(self.conn, _last_month(9), 80.0,
              "WITHDRAWAL POS #123 NORTHWIND GROCERY")
        seed = budget._seed_config(self.conn, today=TODAY)
        self.assertEqual(seed["other_monthly"], 180.0)

    def test_a_billers_e_payment_is_seeded(self):
        _bare(self.conn, _last_month(12), 60.0, "FABRIKAM WATER E-PAYMENT")
        seed = budget._seed_config(self.conn, today=TODAY)
        self.assertEqual(seed["other_monthly"], 160.0)

    def test_explicit_card_payment_and_transfer_lines_stay_out(self):
        _bare(self.conn, _last_month(6), 900.0, "CONTOSO CREDIT CARD PAYMENT")
        _bare(self.conn, _last_month(7), 500.0, "ONLINE XFER TO SAVINGS")
        seed = budget._seed_config(self.conn, today=TODAY)
        self.assertEqual(seed["other_monthly"], 100.0)

    def test_the_seed_leaves_out_exactly_what_the_flow_pass_routes(self):
        lines = {"WITHDRAWAL POS #123 NORTHWIND GROCERY": 80.0,
                 "FABRIKAM WATER E-PAYMENT": 60.0,
                 "CONTOSO CREDIT CARD PAYMENT": 900.0,
                 "ONLINE XFER TO SAVINGS": 500.0}
        for i, (line, amount) in enumerate(lines.items()):
            _bare(self.conn, _last_month(5 + i), amount, line)
        seed = budget._seed_config(self.conn, today=TODAY)
        llm_categorize.flow_classify(self.conn)
        left_in = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
            " WHERE merchant_name IS NULL AND category_primary IS NULL"
        ).fetchone()["s"]
        self.assertEqual(seed["other_monthly"], 100.0 + float(left_in))
        self.assertEqual(float(left_in), 140.0)


if __name__ == "__main__":
    unittest.main()
