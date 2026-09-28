"""The integrations summary, the Prometheus gauges and the integrations
net-worth door show today's figures only. compute_networth(trend=False)
must give them those figures without walking the holdings history —
the walk is most of the report's cost — and with the series keys
present and empty, so a reader of the full shape never sees a missing
key."""

import unittest
from unittest import mock

from oikonome.engine import reporting

from .util import make_db


class NetWorthWithoutHistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_todays_figures_come_without_the_holdings_walk(self):
        with mock.patch.object(reporting, "_reconstruct_trend",
                               side_effect=AssertionError("walked")), \
             mock.patch.object(reporting, "_cumulative_savings",
                               side_effect=AssertionError("walked")):
            nw = reporting.compute_networth(self.conn, trend=False)
        for k in ("current_total", "full_total", "by_institution",
                  "by_asset_class", "property_net"):
            self.assertIn(k, nw)
        self.assertEqual(nw["trend"], [])
        self.assertEqual(nw["snapshot_trend"], [])
        self.assertEqual(nw["savings_trend"], [])
        self.assertEqual(nw["trend_estimated_until"], 0)

    def test_the_full_report_still_walks(self):
        with mock.patch.object(reporting, "_reconstruct_trend",
                               return_value=[["2026-01", 1.0]]) as walk:
            nw = reporting.compute_networth(self.conn)
        self.assertTrue(walk.called)
        self.assertEqual(nw["trend"], [["2026-01", 1.0]])


if __name__ == "__main__":
    unittest.main()
