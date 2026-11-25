"""Under "this month" on Net Worth the server says which parts moved, in
dollars that add up to the figure, and names money withdrawn to the bank
that no linked account has received (specs/counted-figures.md, "The month's
change says what moved"). Without them a withdrawal in transit and a fall in
prices both read as one unexplained loss.
"""
import datetime as dt
import json
import re
import unittest
from unittest import mock

from oikonome.engine import reporting
from oikonome.engine.compat import jsonb

from .util import add_txn, make_db

TODAY = dt.date(2026, 11, 17)


def _held(text: str) -> str:
    """The line as sent: a no-break space between each word and its
    figure, and a word joiner between the sign and the dollars."""
    return re.sub(r"(\w) ([−+])\$", "\\1\u00a0\\2\u2060$", text)


class MonthChangeSaysWhatMoved(unittest.TestCase):
    """Checking 5,000, a card owing 250, a wallet at 2,000 and an IRA at
    9,000 today: 15,750. October closed at 16,650."""

    def setUp(self):
        self.conn = make_db()
        x = self.conn.execute
        x("INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
          " VALUES ('wal','it1','Invented Wallet','investment','crypto',2000),"
          "        ('ira','it1','Invented IRA','investment','ira',9000)")
        self.october = {"Cash": 4950.0, "Card debt": -200.0,
                        "Crypto": 2600.0, "Retirement": 9300.0}
        self.trend = [["2026-10", 16650.0], ["2026-11", 15750.0]]

    def tearDown(self):
        self.conn.close()

    def _record_october(self, by_class=None, total=16650.0):
        self.conn.execute(
            "INSERT INTO networth_snapshot (date,total,by_class) "
            "VALUES (%s,%s,%s)",
            (dt.date(2026, 10, 31), total, jsonb(by_class or self.october)))

    def _withdraw(self, day, usd, txn_id):
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                                         raw, pending, removed)
               VALUES (%s,'wal',%s,%s,'Withdrawal',%s,0,0)""",
            (txn_id, day, usd, json.dumps(
                {"type": "fiat_withdrawal",
                 "native_amount": {"amount": str(-usd)}})))

    def _nw(self):
        with mock.patch.object(reporting, "_reconstruct_trend",
                               return_value=[list(p) for p in self.trend]):
            return reporting.compute_networth(self.conn, today=TODAY)

    def test_the_parts_that_moved_add_up_to_the_figure(self):
        self._record_october()
        nw = self._nw()
        self.assertEqual(nw["month_change"], -900.0)
        # each part's word is held to its figure (a no-break space), so
        # a line wrap never leaves "investments −" alone at a line's end
        self.assertEqual(nw["month_change_text"],
                         _held("crypto −$600 · retirement −$300 · cash +$50 "
                               "· cards −$50"))

    def test_a_part_that_did_not_move_is_not_listed(self):
        self.october = dict(self.october, Cash=5000.0, Retirement=9250.0)
        self._record_october()
        self.assertEqual(self._nw()["month_change_text"],
                         _held("crypto −$600 · retirement −$250 · cards −$50"))

    def test_no_parts_when_last_month_was_not_recorded_by_class(self):
        # the month-end came from somewhere else: the classes on file do
        # not add up to the point the change is measured from
        self._record_october(total=16000.0,
                             by_class=dict(self.october, Crypto=1950.0))
        self.conn.execute("INSERT INTO networth_recorded (month, total) "
                          "VALUES ('2026-10', 16650.0)")
        nw = self._nw()
        self.assertEqual(nw["month_change"], -900.0)
        self.assertIsNone(nw["month_change_text"])

    def test_no_parts_with_nothing_recorded_for_last_month(self):
        self.assertIsNone(self._nw()["month_change_text"])

    def test_a_withdrawal_no_linked_account_received_is_named(self):
        self._record_october()
        self._withdraw("2026-11-03", 300.0, "w1")
        self._withdraw("2026-11-05", 250.0, "w2")
        # the second one arrived in checking the next day
        add_txn(self.conn, "2026-11-06", -250.0, "ACH DEPOSIT",
                account="chk", primary="TRANSFER_IN")
        self.assertEqual(
            self._nw()["month_withdrawn_text"],
            "$300 withdrawn 11/03 has not shown up in a linked account")

    def test_several_unanswered_withdrawals_are_one_sum_over_their_days(self):
        self._record_october()
        self._withdraw("2026-11-03", 300.0, "w1")
        self._withdraw("2026-11-05", 250.0, "w2")
        # one deposit answers for one withdrawal, not for both of a size
        self._withdraw("2026-11-05", 250.0, "w3")
        add_txn(self.conn, "2026-11-06", -250.0, "ACH DEPOSIT",
                account="chk", primary="TRANSFER_IN")
        self.assertEqual(
            self._nw()["month_withdrawn_text"],
            "$550 withdrawn 11/03–11/05 has not shown up in a linked "
            "account")

    def test_nothing_is_said_when_every_withdrawal_arrived(self):
        self._record_october()
        self._withdraw("2026-11-03", 300.0, "w1")
        add_txn(self.conn, "2026-11-04", -300.0, "ACH DEPOSIT",
                account="chk", primary="TRANSFER_IN")
        # last month's withdrawal is not this month's change
        self._withdraw("2026-10-20", 900.0, "w0")
        self.assertIsNone(self._nw()["month_withdrawn_text"])

    def test_the_owner_lens_says_neither(self):
        self._record_october()
        nw = reporting.compute_networth(self.conn, owner="someone")
        self.assertIsNone(nw["month_change_text"])
        self.assertIsNone(nw["month_withdrawn_text"])


class BothClientsPrintTheLines(unittest.TestCase):
    def test_web_and_phone_print_the_two_lines_as_sent(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        web = (root / "webapp/src/pages/NetWorth.tsx").read_text()
        mob = (root / "mobile/src/app/networth.tsx").read_text()
        for name in ("month_change_text", "month_withdrawn_text"):
            self.assertTrue(f"nw.{name}" in web, name)
            self.assertTrue(f"d.{name}" in mob, name)


if __name__ == "__main__":
    unittest.main()
