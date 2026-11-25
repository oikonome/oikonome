"""One merchant prints under ONE name on every surface.

The ledger names a charge by the merchant row it is filed under (else the
alias, else the bank's own descriptor) — merchant_sql.DISPLAY_MERCHANT.
Every list, tile and alert line that names a charge prints that same name
through merchant_sql.shown_payee; the bank's raw descriptor is printed only
inside an opened transaction. A surface that printed the identity key
instead showed a short aggregator name in one place, the merchant's name in
another, and a long statement string in a third, for one merchant.
"""

import datetime as dt
import unittest

from oikonome.engine import anomalies, merchant_dedup, merchant_sql
from oikonome.web import data, lenses

from .util import TODAY, add_txn, make_db

RAW = "PURCHASE RIVERBEND MTRS WESTGATE"
NAMED = "Riverbend Motors"


class OneMerchantOneName(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        # the aggregator gave the row a short name; the person renamed it
        self.big = add_txn(self.conn, "2026-07-03", 1840.0, RAW,
                           merchant="Rvbnd")
        merchant_dedup.rename(self.conn, "Rvbnd", NAMED)
        # a charge nothing has named yet: it prints as the ledger prints it
        self.plain = add_txn(self.conn, "2026-07-05", 610.0,
                             "UNNAMED STORE NORTH")

    def tearDown(self):
        self.conn.close()

    def _ledger_name(self, tid):
        rows = data.transactions(self.conn, 2026, 7)
        return next(r for r in rows if r["id"] == tid)["payee"]

    def test_the_month_lens_biggest_transactions_print_the_ledgers_name(self):
        r = lenses.month_summary(self.conn, 2026, 7, today=TODAY)
        names = {t["amount"]: t["payee"] for t in r["biggest"]}
        self.assertEqual(names[1840.0], self._ledger_name(self.big))
        self.assertEqual(names[1840.0], NAMED)
        # an unnamed row gets the same fallback the ledger prints
        self.assertEqual(names[610.0], self._ledger_name(self.plain))
        for t in r["biggest_variable"]:
            self.assertNotEqual(t["payee"], "Rvbnd")

    def test_a_double_charge_line_names_the_merchant_as_the_ledger_does(self):
        today = dt.date(2026, 7, 9)
        add_txn(self.conn, "2026-07-08", 45.0, "RVBND SVC", merchant="Rvbnd")
        add_txn(self.conn, "2026-07-09", 45.0, "RVBND SVC", merchant="Rvbnd")
        lines = [m for m in anomalies.detect(self.conn, today)
                 if m.startswith("Possible double charge")]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith(f"Possible double charge: {NAMED} "),
                        lines[0])

    def test_a_first_ever_charge_line_names_the_merchant_as_the_ledger_does(self):
        today = dt.date(2026, 7, 9)
        tid = add_txn(self.conn, "2026-07-09", 930.0, "HLX ORD ONLINE",
                      merchant="Hlx")
        merchant_dedup.rename(self.conn, "Hlx", "Harbor Lights Pottery")
        lines = [m for m in anomalies.detect(self.conn, today)
                 if m.startswith("First-ever")]
        self.assertEqual(lines, [
            f"First-ever charge from {self._ledger_name(tid)}: $930 "
            "— verify you recognize it"])

    def test_the_row_helper_reads_the_ledgers_name_then_the_key(self):
        self.assertEqual(merchant_sql.shown_payee(
            {"display_payee": NAMED, "payee": "Rvbnd"}), NAMED)
        self.assertEqual(merchant_sql.shown_payee(
            {"display_payee": None, "payee": "Rvbnd"}), "Rvbnd")
        self.assertEqual(merchant_sql.shown_payee({"payee": "Rvbnd"}), "Rvbnd")


if __name__ == "__main__":
    unittest.main()
