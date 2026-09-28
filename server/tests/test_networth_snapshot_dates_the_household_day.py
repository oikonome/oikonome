"""The nightly net-worth snapshot is dated by the household's calendar.

The job runs on the instance clock (UTC in a typical container). A
household twelve hours behind that clock got a point dated tomorrow —
a day it had not lived yet — and its trend showed a date ahead of every
other figure in the app, which all key on the household's own day.
"""

import datetime as dt
import unittest

from oikonome import localtime
from oikonome.engine import budget, reporting

from .util import make_db, write_config


class SnapshotDateTests(unittest.TestCase):
    def test_row_date_is_the_household_day(self):
        conn = make_db()
        try:
            # the zone whose day differs from UTC's for the longest
            # stretch, so the assertion means something at most hours
            write_config(conn, timezone="Pacific/Kiritimati")
            expected = localtime.now_local(budget.load_config(conn)).date()
            reporting.snapshot_networth(conn)
            row = conn.execute(
                "SELECT date FROM networth_snapshot").fetchone()
            self.assertEqual(row["date"], expected)
            utc = dt.datetime.now(dt.timezone.utc).date()
            if expected != utc:
                self.assertNotEqual(row["date"], utc)
        finally:
            conn.close()
