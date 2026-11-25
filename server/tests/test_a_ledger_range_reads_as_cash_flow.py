"""A Transactions search states money out / in / net by Cash Flow's rules,
and its range buttons cover Cash Flow's windows.

The search headline used to be the raw sum of every matched row: a card
payment counted on both sides, a transfer between the household's own
accounts as money made or spent, so "this month" by date said one thing,
the month tiles another and Cash Flow a third. The server now states the
matched rows' money out / in / net by reporting.flows — the predicates
Cash Flow totals — and every range button's dates come from Cash Flow's
own window (reporting.ledger_ranges): complete months, "This month" to
today, "All" unbounded. specs/ledger-figures.md.

Every amount and payee is invented.
"""

import datetime as dt
import unittest

from oikonome.engine import reporting
from oikonome.web import api  # noqa: F401 (mounts the routers in order)
from oikonome.web import api_common, api_ledger

from .util import add_txn, make_db, write_config


def _month_start(d: dt.date, back: int) -> dt.date:
    d = d.replace(day=1)
    for _ in range(back):
        d = (d - dt.timedelta(days=1)).replace(day=1)
    return d


class ALedgerRangeReadsAsCashFlow(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.today = api_common._today(self.conn)
        # fourteen months of an ordinary household, this month included
        for back in range(14, -1, -1):
            day = _month_start(self.today, back) + dt.timedelta(days=1)
            if day > self.today:
                day = self.today
            add_txn(self.conn, day, -2400.00, "Acme Payroll",
                    primary="INCOME", account="chk")
            add_txn(self.conn, day, 180.25, "Corner Grocer")
            add_txn(self.conn, day, 61.10, "Lamplight Books")
            # money moved, never made or spent
            add_txn(self.conn, day, 500.00, "Card autopay", account="chk",
                    primary="LOAN_PAYMENTS",
                    detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
            add_txn(self.conn, day, -500.00, "Payment thank you",
                    primary="LOAN_PAYMENTS",
                    detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
            add_txn(self.conn, day, 300.00, "To savings", account="chk",
                    primary="TRANSFER_OUT")

    def _page(self, **kw) -> dict:
        return api_ledger._transactions_payload(self.conn, **kw)

    def test_a_ranges_figures_are_cash_flows_for_the_same_window(self):
        for key in ("cur", "1m", "3m", "1y"):
            with self.subTest(key=key):
                r = self._page()["ranges"][key]
                page = self._page(date_from=r["start"], date_to=r["end"])
                spend = reporting.spending_window(self.conn, self.today, key)
                inc = reporting.income_window(self.conn, self.today, key)
                self.assertEqual((r["start"], r["end"]),
                                 (spend["from"], spend["to"]))
                self.assertEqual(page["flow"]["money_out"], spend["total"])
                self.assertEqual(page["flow"]["money_in"], inc["total"])
                self.assertEqual(page["flow"]["net"],
                                 round(inc["total"] - spend["total"], 2))

    def test_card_payments_and_transfers_are_on_neither_side(self):
        r = self._page()["ranges"]["1m"]
        flow = self._page(date_from=r["start"], date_to=r["end"])["flow"]
        self.assertEqual(flow, {"money_out": 241.35, "money_in": 2400.0,
                                "net": round(2400.0 - 241.35, 2)})

    def test_a_merchant_search_counts_only_the_rows_it_matched(self):
        flow = self._page(q="corner grocer")["flow"]
        self.assertEqual(flow["money_in"], 0.0)
        self.assertEqual(flow["money_out"], round(15 * 180.25, 2))

    def test_the_month_ranges_are_complete_months(self):
        ranges = self._page()["ranges"]
        last = self.today.replace(day=1) - dt.timedelta(days=1)
        self.assertEqual(ranges["3m"], {
            "start": _month_start(self.today, 3).isoformat(),
            "end": last.isoformat()})
        self.assertEqual(ranges["1y"]["start"],
                         _month_start(self.today, 12).isoformat())
        self.assertEqual(ranges["cur"], {
            "start": self.today.replace(day=1).isoformat(),
            "end": self.today.isoformat()})
        self.assertEqual(ranges["all"], {"start": "", "end": ""})

    def test_a_month_browse_carries_the_same_ranges(self):
        self.assertEqual(self._page()["ranges"],
                         self._page(q="grocer")["ranges"])


if __name__ == "__main__":
    unittest.main()
