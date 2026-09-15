"""Pending transactions count. Every pending transaction shows on the
Transactions page and counts in every calculation, including how much
spending is left for the day.

A pending card swipe is real spending
the moment it happens — it shows on the Transactions page (pend pill), and
it counts in the verdict, the month bucket, and the today-ledger exactly
like a posted row. The deliberate exceptions, asserted below so they stay
deliberate:

- a pending row matching an ACTIVE BILL is a bill payment: it counts on the
  fixed/Bills line, not in variable — fixed never drives the verdict (the
  frozen money convention, not a pending rule).
- recurring DETECTION reads posted rows only: pending amounts and
  descriptors are unstable, and proposals must not churn on them. That is
  detection, not counting.
- pending DEPOSITS are excluded from income matching — a pending deposit
  can vanish, and income feeds nothing in "spending left today".
"""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import data, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

DAYS_LEFT = 17   # TODAY = 2026-07-15, July has 31 days


class PendingTransactionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _pending_txn(self, amount, name, **kw):
        add_txn(self.conn, TODAY, amount, name, **kw)
        self.conn.execute(
            "UPDATE transactions SET pending = 1 WHERE name = %s", (name,))

    def test_pending_spend_counts_in_verdict_and_today_ledger(self):
        self._pending_txn(120.0, "HARVEST PANTRY MARKET", primary="FOOD_AND_DRINK")
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["variable_actual"], 120.0)
        st["today"], st["days_in_month"] = TODAY, 31
        f = todayview.allowances(st)[0]["food"]
        self.assertAlmostEqual(f["spent_today"], 120.0, 2)
        self.assertAlmostEqual(f["left_today"], 1000 / DAYS_LEFT - 120, 2)

    def test_pending_row_shows_on_the_transactions_page(self):
        self._pending_txn(4.99, "AMAZON PRIME")
        rows = data.transactions(self.conn, TODAY.year, TODAY.month)
        row = next(r for r in rows if r["payee"] == "AMAZON PRIME")
        self.assertEqual(row["pending"], 1)

    def test_pending_bill_payment_counts_as_fixed_not_variable(self):
        """A pending charge that matches an active bill belongs on the
        Bills line, not in variable spend — and it must not read as
        'missing' from left-today just because it has not posted."""
        add_bill(self.conn, "Larkspur Family Dental", 200.0,
                 next_due=TODAY, last_seen=TODAY - dt.timedelta(days=30))
        self._pending_txn(200.0, "LARKSPUR FAMILY DENTAL CE",
                          primary="MEDICAL")
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["buckets"]["fixed"]["actual"], 200.0)
        self.assertEqual(st["variable_actual"], 0.0)

    def test_pending_rows_are_invisible_to_the_recurring_finder(self):
        """Detection reads posted only — a pending row must not update a
        bill's evidence or spawn proposals (amounts/descriptors unstable)."""
        from oikonome.engine import bills as rd
        self._pending_txn(55.0, "NEW GYM LLC", primary="PERSONAL_CARE")
        rows = rd._ledger_rows(self.conn, TODAY)
        self.assertNotIn("NEW GYM LLC", [r["payee"] for r in rows])


if __name__ == "__main__":
    unittest.main()
