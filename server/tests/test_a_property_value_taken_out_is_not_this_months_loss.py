"""A hand-entered property value typed in, taken out or re-valued since
last month's end is not money lost this month (specs/counted-figures.md,
"The headline's change this month follows the same rule"). Measured on the
total, a household whose property list came back empty read a large loss
"property −$N" beside a property tile of $0 and an empty Property section.

And an archive whose hand-entered items are dated by month alone — the
shape an older build stored and the net-worth report reads — restores
with every item, not none.
"""
import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import reporting
from oikonome.engine.compat import jsonb
from oikonome.sync import restore

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)


class APropertyTakenOutThisMonth(unittest.TestCase):
    """October closed with a 100,000 house recorded beside the accounts;
    the house is no longer on the list today."""

    def setUp(self):
        self.conn = make_db()
        self.october = {"Cash": 4950.0, "Card debt": -200.0,
                        reporting.SNAPSHOT_PROPERTY_KEY: 100000.0}
        self.conn.execute(
            "INSERT INTO networth_snapshot (date,total,by_class) "
            "VALUES (%s,%s,%s)",
            (dt.date(2026, 10, 31), 4750.0, jsonb(self.october)))

    def tearDown(self):
        self.conn.close()

    def _nw(self):
        trend = [["2026-10", 4750.0], ["2026-11", 4750.0]]
        with mock.patch.object(reporting, "_reconstruct_trend",
                               return_value=trend):
            return reporting.compute_networth(self.conn, today=TODAY)

    def test_the_headline_does_not_count_the_removed_value_as_a_loss(self):
        nw = self._nw()
        self.assertEqual(nw["property_net"], 0.0)
        self.assertEqual(nw["month_change"], nw["financial_month_change"])
        # the value taken out is not the tile's change either
        self.assertEqual(nw["property_month_change"], 0.0)
        self.assertNotIn("property", nw["month_change_text"] or "")

    def test_a_house_still_listed_keeps_its_month_change_on_the_total(self):
        write_config(self.conn, manual_assets=[
            {"name": "House", "kind": "real_estate", "value": 100000,
             "as_of": "2026-06-01", "acquired_on": "2026-06-01"}])
        nw = self._nw()
        self.assertEqual(nw["property_net"], 100000.0)
        self.assertEqual(nw["property_month_change"], 0.0)
        self.assertEqual(nw["month_change"], 0.0)


class AHouseAddedBesideALoanPaidDown(unittest.TestCase):
    """A linked mortgage paid 800 of principal this month, and a house was
    typed in this month: only the house's value comes off the headline;
    the paydown is still this month's property change."""

    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('mtg','it1','Home Loan','loan','mortgage',200000)")
        add_txn(self.conn, "2026-09-05", -800.0, "PRINCIPAL PAYMENT",
                account="mtg")
        add_txn(self.conn, "2026-11-05", -800.0, "PRINCIPAL PAYMENT",
                account="mtg")
        write_config(self.conn, manual_assets=[
            {"name": "House", "kind": "real_estate", "value": 300000,
             "as_of": "2026-11-02", "acquired_on": "2026-11-02"}])
        # October closed recorded: the loan alone, 800 more owed then
        self.conn.execute(
            "INSERT INTO networth_snapshot (date,total,by_class) "
            "VALUES (%s,%s,%s)",
            (dt.date(2026, 10, 31), 4750.0,
             jsonb({"Cash": 4950.0, "Card debt": -200.0,
                    reporting.SNAPSHOT_PROPERTY_KEY: -200800.0})))

    def tearDown(self):
        self.conn.close()

    def test_only_the_typed_in_value_comes_off(self):
        trend = [["2026-09", 4750.0], ["2026-10", 4750.0],
                 ["2026-11", 4750.0]]
        with mock.patch.object(reporting, "_reconstruct_trend",
                               return_value=trend):
            nw = reporting.compute_networth(self.conn, today=TODAY)
        self.assertEqual(nw["financial_month_change"], 0.0)
        self.assertEqual(nw["property_month_change"], 800.0)
        self.assertEqual(nw["month_change"], 800.0)


class AMonthDatedItemRestores(unittest.TestCase):

    def test_an_item_dated_by_month_alone_is_kept(self):
        cfg = {"manual_assets": [
            {"name": "House", "kind": "real_estate", "value": 300000,
             "as_of": "2026-07"},
            {"name": "Car", "kind": "vehicle", "value": 12000,
             "as_of": "2026-07"}]}
        notes = restore.clean_manual_assets(cfg)
        self.assertEqual(notes, [])
        got = cfg.get("manual_assets") or []
        self.assertEqual([a["name"] for a in got], ["House", "Car"])
        # the month the report reads off it is the month it said
        self.assertEqual({a["acquired_on"][:7] for a in got}, {"2026-07"})
        self.assertEqual({a["as_of"][:7] for a in got}, {"2026-07"})


if __name__ == "__main__":
    unittest.main()
