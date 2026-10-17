"""The net-worth page's headline change is measured on the base of the
Total net worth figure it sits beside.

`trend` covers financial accounts only. A mortgage payment takes the
whole payment out of cash while the loan balance falls by its principal,
so a change read off `trend` shows the full payment as a loss beside a
total that fell only by the interest. `full_trend` adds the property layer
month by month — recorded by the nightly snapshot where it exists, else
the live loan walked back through the rows posted to it — and ends at
full_total.

A loan ledger of whole payments alone does not say how much of each was
principal; crediting the whole payment to the balance would cancel the
cash leaving and read the month as costing nothing, and holding the loan
flat would read it as the whole payment lost. Neither is known, so a month
whose property value is only that guess carries no headline change at all.
"""
import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import reporting
from oikonome.engine.compat import jsonb

from .util import add_txn, make_db

TODAY = dt.date(2026, 10, 15)


def _record_september(conn):
    """September's financial point is a recorded month-end, so the cash
    the payment took is in the trend and the loan walk is measured beside
    it (a rebuilt month holds both layers; see
    test_networth_change_never_counts_principal_without_its_cash)."""
    conn.execute("INSERT INTO networth_recorded (month, total) "
                 "VALUES ('2026-09', 5750.0)")


class FullTrendTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('mtg','it1','Home Loan','loan','mortgage',200000)")
        # the October payment: cash fell by 1000 (trend below), the
        # loan by its 800 principal
        self.pay = add_txn(self.conn, "2026-10-05", -800.0,
                           "PRINCIPAL PAYMENT", account="mtg")
        self.trend = [["2026-09", 5750.0], ["2026-10", 4750.0]]
        _record_september(self.conn)

    def tearDown(self):
        self.conn.close()

    def _nw(self, **kw):
        with mock.patch.object(reporting, "_reconstruct_trend",
                               return_value=[list(p) for p in self.trend]):
            return reporting.compute_networth(self.conn, today=TODAY, **kw)

    def test_payment_reads_as_the_interest_on_the_full_line(self):
        nw = self._nw()
        self.assertEqual(nw["trend"], self.trend)
        ft = nw["full_trend"]
        self.assertEqual([m for m, _ in ft], ["2026-09", "2026-10"])
        self.assertEqual(ft[-1][1], nw["full_total"])
        self.assertAlmostEqual(ft[-1][1] - ft[-2][1], -200.0)
        # the financial-only change still shows the whole payment
        self.assertAlmostEqual(nw["trend"][-1][1] - nw["trend"][-2][1],
                               -1000.0)

    def test_a_recorded_snapshot_month_wins(self):
        self.conn.execute(
            "INSERT INTO networth_snapshot (date,total,by_class) "
            "VALUES (%s,%s,%s)",
            (dt.date(2026, 9, 30), 5750.0,
             jsonb({"Cash": 5750.0,
                    reporting.SNAPSHOT_PROPERTY_KEY: -201000.0})))
        ft = self._nw()["full_trend"]
        self.assertAlmostEqual(ft[0][1], 5750.0 - 201000.0)

    def test_the_snapshot_records_the_property_net(self):
        reporting.snapshot_networth(self.conn)
        row = self.conn.execute(
            "SELECT by_class FROM networth_snapshot "
            "ORDER BY date DESC LIMIT 1").fetchone()
        by_class = row["by_class"]
        if isinstance(by_class, str):
            import json
            by_class = json.loads(by_class)
        self.assertEqual(by_class[reporting.SNAPSHOT_PROPERTY_KEY],
                         -200000.0)

    def test_without_history_the_series_is_empty(self):
        self.assertEqual(
            reporting.compute_networth(self.conn, trend=False)["full_trend"],
            [])

    def test_owner_lens_has_no_full_trend(self):
        self.assertEqual(self._nw(owner="someone")["full_trend"], [])


class WholePaymentTests(unittest.TestCase):
    """The loan ledger holds the full cash payment (1000), of which 800
    was principal and 200 interest."""

    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('mtg','it1','Home Loan','loan','mortgage',200000)")
        add_txn(self.conn, "2026-10-05", -1000.0, "MORTGAGE PAYMENT",
                account="mtg", primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_MORTGAGE_PAYMENT")
        self.trend = [["2026-09", 5750.0], ["2026-10", 4750.0]]
        _record_september(self.conn)

    def tearDown(self):
        self.conn.close()

    def _change(self):
        """The hero's change as the clients compute it: None when the
        series has no earlier month to measure from."""
        with mock.patch.object(reporting, "_reconstruct_trend",
                               return_value=[list(p) for p in self.trend]):
            ft = reporting.compute_networth(self.conn,
                                            today=TODAY)["full_trend"]
        for _, v in ft:
            self.assertIsInstance(v, float)     # never a null to subtract
        if len(ft) < 2:
            return None
        return round(ft[-1][1] - ft[-2][1], 2)

    def _ytd(self, principal):
        self.conn.execute(
            "INSERT INTO liabilities (account_id, as_of, raw) "
            "VALUES (%s,%s,%s)",
            ("mtg", dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc),
             jsonb({"ytd_principal_paid": principal})))

    def test_whole_payment_alone_shows_no_change(self):
        # neither 0 (the payment credited to principal wholesale) nor
        # −1000 (the loan held flat): the split is unknown
        self.assertIsNone(self._change())

    def test_a_late_fee_is_not_a_statement(self):
        # one fee on the loan says nothing about how the payments split,
        # so the whole payment is not credited to principal again
        add_txn(self.conn, "2026-08-16", 15.0, "LATE FEE", account="mtg")
        self.assertIsNone(self._change())

    def test_a_late_fee_beside_a_split_payment_still_splits(self):
        add_txn(self.conn, "2025-12-05", -1000.0, "MORTGAGE PAYMENT",
                account="mtg")
        add_txn(self.conn, "2026-08-16", 15.0, "LATE FEE", account="mtg")
        self._ytd(800.0)
        self.assertEqual(self._change(), -200.0)

    def test_a_ledger_starting_mid_year_does_not_split(self):
        # the year's principal (7200) is far more than the one payment the
        # ledger holds this year: the share would be capped to the whole
        # payment and read the month as costing nothing
        self._ytd(7200.0)
        self.assertIsNone(self._change())

    def test_a_share_above_the_payments_does_not_split(self):
        add_txn(self.conn, "2025-12-05", -1000.0, "MORTGAGE PAYMENT",
                account="mtg")
        self._ytd(7200.0)
        self.assertIsNone(self._change())

    def test_months_after_the_last_unsplit_payment_are_still_measured(self):
        # the only payment posted before last month ended: nothing moved
        # the loan since, so the change is the financial accounts' alone
        self.conn.execute("UPDATE transactions SET date = '2026-08-05'"
                          " WHERE account_id = 'mtg'")
        self.assertEqual(self._change(), -1000.0)

    def test_a_recorded_month_is_measured_past_an_unsplit_payment(self):
        self.conn.execute(
            "INSERT INTO networth_snapshot (date,total,by_class) "
            "VALUES (%s,%s,%s)",
            (dt.date(2026, 9, 30), 5750.0,
             jsonb({reporting.SNAPSHOT_PROPERTY_KEY: -200800.0})))
        self.assertEqual(self._change(), -200.0)

    def test_liabilities_principal_paid_splits_the_payment(self):
        # the ledger reaches back past the start of the year, so this
        # year's payments are all there to split
        add_txn(self.conn, "2025-12-05", -1000.0, "MORTGAGE PAYMENT",
                account="mtg")
        self.conn.execute(
            "INSERT INTO liabilities (account_id, as_of, raw) "
            "VALUES (%s,%s,%s)",
            ("mtg", dt.datetime(2026, 10, 10, 12, tzinfo=dt.timezone.utc),
             jsonb({"ytd_principal_paid": 800.0,
                    "ytd_interest_paid": 200.0})))
        self.assertEqual(self._change(), -200.0)

    def test_a_ledger_with_charges_sums_to_the_balance_change(self):
        add_txn(self.conn, "2026-10-01", 200.0, "INTEREST", account="mtg")
        self.assertEqual(self._change(), -200.0)

    def test_a_bad_recorded_property_net_does_not_break_the_trend(self):
        for i, raw in enumerate(('{"property_net": "x"}',
                                 '{"property_net": true}',
                                 '{"property_net": {}}',
                                 '{"property_net": 1e999}',
                                 '{"property_net": "NaN"}',
                                 '{"property_net": "Infinity"}',
                                 '[1, 2]', '7')):
            with self.subTest(raw=raw):
                self.conn.execute("DELETE FROM networth_snapshot")
                self.conn.execute(
                    "INSERT INTO networth_snapshot (date,total,by_class) "
                    "VALUES (%s,%s,%s::jsonb)",
                    (dt.date(2026, 9, 30), 5750.0, raw))
                self.assertIsNone(self._change())


if __name__ == "__main__":
    unittest.main()
