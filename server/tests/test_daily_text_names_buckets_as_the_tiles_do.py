"""The daily SMS and push name the over-pace bucket by the label the
Today tiles use ("Everything else"), never by month_status's internal key
("other") — the same message already names the bucket's allowance by its
label, and two names for one bucket on a lock screen read as two."""

import datetime as dt
import unittest

from oikonome.notify import render
from oikonome.web import report

from .util import TODAY, add_ledger_history, add_txn, make_db, write_config


class DailyTextBucketLabelTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_ledger_history(self.conn, TODAY)
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 1500.0,
                "HARDWARE BARN")

    def tearDown(self):
        self.conn.close()

    def test_the_driver_is_named_by_its_tile_label(self):
        d = report.gather(self.conn, TODAY)
        self.assertEqual(d["verdict"], "OVER BUDGET")
        self.assertEqual([r["bucket"] for r in d["reasons"]], ["other"])
        for text in (render.short_daily(d), render.push_daily(d)):
            self.assertIn("Everything else +$", text)
            self.assertNotRegex(text, r"(^|\n|· )other ")


if __name__ == "__main__":
    unittest.main()
