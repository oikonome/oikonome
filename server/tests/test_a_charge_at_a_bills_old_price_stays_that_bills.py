"""Two bills at one merchant: a charge at one bill's OLD price belongs to
that bill, not to whichever bill's current amount happens to sit nearer.

The bill-category stamp breaks a tie between bills that both accept a
charge by its distance to each bill's amounts — the same rule the ledger
matcher uses (budget.amount_gap over every band the bill knows). Measured
against the current amount only, a price change made the bill's own
earlier payments wear the other bill's category."""

import datetime as dt
import unittest

from oikonome.engine import bills
from oikonome.engine.compat import jsonb

from .util import TODAY, add_bill, add_txn, make_db, write_config


class OldPriceTieBreak(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def test_a_charge_at_the_old_price_wears_its_own_bills_category(self):
        day = TODAY - dt.timedelta(days=5)
        # the membership went from $50 to $70 a week ago; the locker is $56
        add_bill(self.conn, "Harbor Gym", 70.00,
                 txn_category="PERSONAL_CARE")
        add_bill(self.conn, "Harbor Gym Locker", 56.00,
                 txn_category="GENERAL_SERVICES")
        self.conn.execute(
            "UPDATE bills SET raw = raw || %s::jsonb WHERE payee = 'Harbor Gym'",
            (jsonb({"amount_history": [
                {"amount": 50.0, "until": (TODAY - dt.timedelta(days=2)).isoformat()}]}),))
        t = add_txn(self.conn, day, 50.00, "HARBOR GYM", merchant="Harbor Gym")
        bills.apply_txn_categories(self.conn)
        row = self.conn.execute(
            "SELECT t.category_override, b.payee FROM transactions t "
            "JOIN manual_categories m ON m.transaction_id = t.id "
            "JOIN bills b ON b.id = m.bill_id WHERE t.id = %s", (t,)).fetchone()
        self.assertIsNotNone(row, "the charge was stamped by neither bill")
        self.assertEqual((row["payee"], row["category_override"]),
                         ("Harbor Gym", "PERSONAL_CARE"))


if __name__ == "__main__":
    unittest.main()
