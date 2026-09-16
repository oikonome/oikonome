"""A merchant's history page lists its transactions over a chosen window.

The list covers the chosen window from the site-wide timeframe vocabulary,
"all" included, so a long-time merchant's rows are all reachable; the
lifetime aggregate above the list stays lifetime whatever the window.
"""

import datetime as dt
import unittest

from oikonome.engine import bills

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 6, 15)
NAME = "Harbor Hardware"


class MerchantHistoryWindowTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.recent = add_txn(self.conn, TODAY - dt.timedelta(days=10), 30.0,
                              "HARBOR HARDWARE", merchant=NAME,
                              primary="HOME_IMPROVEMENT")
        self.old = add_txn(self.conn, TODAY - dt.timedelta(days=1000), 45.0,
                           "HARBOR HARDWARE", merchant=NAME,
                           primary="HOME_IMPROVEMENT")

    def tearDown(self):
        self.conn.close()

    def _ids(self, **kw):
        h = bills.merchant_history(self.conn, NAME, today=TODAY, **kw)
        return {t["txn_id"] for t in h["txns"]}, h

    def test_the_default_window_is_still_two_years(self):
        ids, _ = self._ids()
        self.assertEqual(ids, {self.recent})

    def test_all_lists_every_charge_the_lifetime_total_counts(self):
        ids, h = self._ids(months=None)
        self.assertEqual(ids, {self.recent, self.old})
        self.assertEqual(h["lifetime"]["count"], 2)

    def test_a_short_window_keeps_the_lifetime_aggregate(self):
        ids, h = self._ids(months=12)
        self.assertEqual(ids, {self.recent})
        self.assertEqual(h["lifetime"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
