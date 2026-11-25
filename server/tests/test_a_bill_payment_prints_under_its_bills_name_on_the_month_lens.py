"""A bill payment among the Month lens's biggest transactions prints under
the bill's name — the name the Bills page lists it by — not the bank's
descriptor the payment posted under.

A payment the ledger has no merchant name for falls back to its identity
key, which for a utility paid online is a statement string with a
reference number on it; the same month's Bills page calls it by the
bill's name. Every other row keeps the ledger's merchant name
(merchant_sql.shown_payee).
"""

import datetime as dt
import unittest

from oikonome.engine import merchant_dedup
from oikonome.web import lenses

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 28)


class ABillPaymentPrintsUnderItsBillsName(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Lakeshore Power", 212.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 6, 14), by_month_day=14,
                 last_seen=dt.date(2026, 1, 1))
        add_txn(self.conn, dt.date(2026, 6, 14), 212.0,
                "LAKESHORE POWER EPAY 4417")
        add_txn(self.conn, dt.date(2026, 7, 14), 212.0,
                "LAKESHORE POWER EPAY 4417")
        # a charge that is no bill, under a merchant the person named
        add_txn(self.conn, dt.date(2026, 7, 9), 640.0, "PURCHASE TLWD 0091",
                merchant="Tlwd")
        merchant_dedup.rename(self.conn, "Tlwd", "Tallowood Outfitters")

    def tearDown(self):
        self.conn.close()

    def test_the_payment_reads_as_the_bill_and_the_rest_as_the_ledger(self):
        r = lenses.month_summary(self.conn, 2026, 7, today=TODAY)
        names = {t["amount"]: t["payee"] for t in r["biggest"]}
        self.assertEqual(names[212.0], "Lakeshore Power")
        self.assertEqual(names[640.0], "Tallowood Outfitters")


if __name__ == "__main__":
    unittest.main()
