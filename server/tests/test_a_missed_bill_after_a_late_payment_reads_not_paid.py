import datetime as dt
import unittest

from oikonome.web import lenses

from .util import TODAY, add_bill, add_txn, freeze_month, make_db, write_config


class AMissedMonthlyBillAfterALatePaymentReadsNotPaid(unittest.TestCase):
    """A monthly bill paid a few days late homes its previous occurrence in
    this month; that payment answers the previous cycle, not this month's,
    so a cycle this month that found no payment still reads "not paid"."""

    def test_the_missed_cycle_reads_not_paid(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_bill(conn, "Rent", 2000.0, next_due=dt.date(2026, 7, 30),
                 last_seen=dt.date(2026, 6, 2))
        for m in (4, 5, 6):
            add_txn(conn, dt.date(2026, m, 2), 2000.0, "RENT")
        freeze_month(conn, 2026, 6)
        d = lenses.month_summary(conn, 2026, 6, today=TODAY)
        self.assertIn("not paid: Rent $2,000", d["bills"]["notes"])
        self.assertNotIn("$2,000 under plan", d["bills"]["notes"])


if __name__ == "__main__":
    unittest.main()
