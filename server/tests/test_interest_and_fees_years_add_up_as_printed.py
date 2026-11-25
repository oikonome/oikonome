"""A year of the "Interest & fees paid" table adds up in the dollars it
prints: interest + fees − refunds = net, decided once by the server
(specs/shown-figures.md). Rounded cell by cell, a year of small fees and a
slightly larger refund printed parts that did not give its net.
"""
import datetime as dt
import pathlib
import unittest

from oikonome.engine import reporting

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)
_ROOT = pathlib.Path(__file__).resolve().parents[2]


class YearsAddUpAsPrinted(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _fee(self, day, amount, name="MONTHLY SERVICE FEE"):
        add_txn(self.conn, day, amount, name, account="chk",
                primary="BANK_FEES")

    def test_fees_less_a_larger_refund_give_the_printed_net(self):
        # 20.40 of fees and 21.70 refunded: -1.30, printed -$1. Cell by
        # cell that read "$20 fees − $22 refunded".
        self._fee(dt.date(2026, 2, 3), 20.40)
        self._fee(dt.date(2026, 2, 9), -21.70, "FEE REFUND")
        y = reporting.compute_cash_costs(self.conn, TODAY)["by_year"][0]
        sh = y["shown"]
        self.assertEqual(sh["net"], -1)
        self.assertEqual(sh["interest"] + sh["fees"] - sh["refunds"],
                         sh["net"])
        # each part within a dollar of its own rounding (20 and 22)
        self.assertLessEqual(abs(sh["fees"] - 20), 1)
        self.assertLessEqual(abs(sh["refunds"] - 22), 1)

    def test_every_year_adds_up_and_ytd_is_its_year(self):
        self._fee(dt.date(2026, 2, 3), 20.40)
        self._fee(dt.date(2026, 2, 9), -21.70, "FEE REFUND")
        self._fee(dt.date(2024, 5, 6), 90.45)
        self._fee(dt.date(2024, 5, 7), 12.45, "INTEREST CHARGE")
        self._fee(dt.date(2024, 8, 1), -0.60, "FEE REFUND")
        r = reporting.compute_cash_costs(self.conn, TODAY)
        for y in r["by_year"]:
            sh = y["shown"]
            self.assertEqual(sh["interest"] + sh["fees"] - sh["refunds"],
                             sh["net"], y["year"])
            self.assertEqual(sh["net"], reporting.whole_dollars(y["net"]))
        self.assertEqual(r["ytd"]["shown"], r["by_year"][0]["shown"])

    def test_the_tiles_differ_by_the_two_figures_printed(self):
        self._fee(dt.date(2026, 2, 3), 20.40)
        r = reporting.compute_cash_costs(self.conn, TODAY)
        sh = r["shown"]
        self.assertEqual(sh["paid_ytd"], 20)
        self.assertEqual(sh["vs_earned_ytd"],
                         sh["earned_ytd"] - sh["paid_ytd"])

    def test_the_largest_charge_is_dated_in_full(self):
        self._fee(dt.date(2024, 5, 14), 35.0, "SERVICE FEE")
        self._fee(dt.date(2024, 8, 21), 5.0)
        y = reporting.compute_cash_costs(self.conn, TODAY)["by_year"][0]
        self.assertEqual(y["biggest"][2], "2024-05-14")
        self.assertTrue(y["biggest_text"].endswith("$35 · 05/14/24"),
                        y["biggest_text"])

    def test_both_clients_print_the_shown_cells(self):
        web = (_ROOT / "webapp/src/pages/CashFlow.tsx").read_text()
        mob = (_ROOT / "mobile/src/app/cashflow.tsx").read_text()
        self.assertTrue("signedMoney(y.shown.net, money)" in web)
        # the phone prints the same cells; an older server's cent fields
        # stand in for them, the same figures
        self.assertTrue("y.shown ?? y" in mob)
        for src, who in ((web, "web"), (mob, "phone")):
            self.assertTrue("y.biggest_text" in src, who)
            self.assertFalse("m$(y.net)" in src or "money(y.net)" in src,
                             who)
            # the charge's date is the server's, never month/day cut here
            self.assertFalse("mmdd(y.biggest[2])" in src, who)


if __name__ == "__main__":
    unittest.main()
