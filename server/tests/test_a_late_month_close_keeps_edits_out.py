"""A month frozen late is frozen only from the settings it ended under.

The month-close sweep freezes the month that just ended from the live
settings. When it runs late — the first hour of the new month was missed —
the owner may already have changed the budget for the new month, and that
edit must not reach back into the closed one. Asked to freeze only if the
settings are unchanged since the month ended, the freeze leaves the month's
earlier snapshot standing when they are not.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import make_db, write_config

OCT = dt.date(2025, 10, 1)


class LateMonthCloseTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=600)
        self.assertTrue(budget.snapshot_month(self.conn, OCT))

    def _now(self):
        return self.conn.execute("SELECT now() AS t").fetchone()["t"]

    def _frozen_food(self):
        return budget.snapshot_config(self.conn, 2025, 10)["food_monthly"]

    def test_an_edit_after_the_month_ended_is_not_frozen_into_it(self):
        ended = self._now()
        write_config(self.conn, food_monthly=900)        # the new month's
        self.assertFalse(budget.snapshot_month(
            self.conn, OCT, unchanged_since=ended))
        self.assertEqual(self._frozen_food(), 600)

    def test_settings_saved_before_the_month_ended_are_frozen(self):
        write_config(self.conn, food_monthly=650)        # the 31st, late
        ended = self._now()
        self.assertTrue(budget.snapshot_month(
            self.conn, OCT, unchanged_since=ended))
        self.assertEqual(self._frozen_food(), 650)

    def test_without_the_guard_the_live_settings_are_frozen(self):
        write_config(self.conn, food_monthly=900)
        self.assertTrue(budget.snapshot_month(self.conn, OCT))
        self.assertEqual(self._frozen_food(), 900)

    def test_a_naive_moment_is_refused(self):
        with self.assertRaises(ValueError):
            budget.snapshot_month(self.conn, OCT,
                                  unchanged_since=dt.datetime(2025, 11, 1))


if __name__ == "__main__":
    unittest.main()
