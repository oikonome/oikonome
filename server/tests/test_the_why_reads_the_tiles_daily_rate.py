"""The Why's "$X/day keeps it green", its "(…/day)" and the OVER-budget
recovery cap name the same daily the spend-at-most tile shows, over the
same days. Two figures for one allowance side by side on the Today page,
the email HTML or the plain text is the drift the shared rate removes:
once today has spend the tile spreads the rest over the days after today,
and the Why must not keep dividing by a count that includes today.
"""

import datetime as dt
import re
import unittest

from oikonome.engine import budget
from oikonome.web import todayview

from .util import (TODAY, add_ledger_history, add_txn, make_db,
                   write_config)


class WhyRateMatchesTheTileTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        add_ledger_history(self.conn, TODAY)
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _status(self):
        st = budget.month_status(self.conn, TODAY)
        # as the page context sets them before it renders the tiles
        st["today"], st["days_in_month"] = TODAY, 31
        allow, _kids, tiles = todayview.allowances(st)
        return st, dict(tiles)

    def _assert_parity(self, st, tiles):
        entries = {e["name"]: e for e in st["why"]["entries"]}
        for label in ("Food", "Everything else"):
            if label not in entries:
                continue
            self.assertAlmostEqual(entries[label]["rate"],
                                   tiles[label]["rate"], 1, label)
            self.assertEqual(entries[label]["rate_days"],
                             tiles[label]["rate_days"], label)

    def test_no_spend_today(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 400.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        self._assert_parity(*self._status())

    def test_spend_today_under_and_over_the_allowance(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 400.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 5.0, "COFFEE", primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 150.0, "TARGET")
        st, tiles = self._status()
        self.assertEqual(tiles["Food"]["rate_days"], 16)
        self._assert_parity(st, tiles)

    def test_the_recovery_cap_is_the_tiles_floored_rate_over_its_days(self):
        # far over pace in both buckets, with spend today
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 800.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 800.0, "TARGET")
        add_txn(self.conn, TODAY, 20.0, "COFFEE", primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 20.0, "TARGET")
        st, tiles = self._status()
        self.assertEqual(st["verdict"], "OVER BUDGET")
        rec = st["why"]["recovery"]
        self.assertIsNotNone(rec)
        days = min(tiles["Food"]["rate_days"],
                   tiles["Everything else"]["rate_days"])
        self.assertIn(f"over the last {days} days", rec)
        cap = int(tiles["Food"]["rate"])
        self.assertIn(f"${cap:,d}/day on Food", rec)
        self._assert_parity(st, tiles)
        # and the "keeps it green"/"…/day" figures are whole-dollar
        # roundings of the same rate wherever they appear
        for e in st["why"]["entries"]:
            for m in re.findall(r"\$([\d,]+)/day keeps it green", e["text"]):
                self.assertEqual(int(m.replace(",", "")),
                                 round(tiles[e["name"]]["rate"]))


if __name__ == "__main__":
    unittest.main()
