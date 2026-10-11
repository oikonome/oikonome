"""A month closes under the budget that was live when it ended.

The month's budget snapshot is upserted by the nightly run, which fires at
one fixed hour of the instance's clock. For a household whose midnight is
not near that hour, the last snapshot a month got was taken before its
final local day was over: raise the food budget at 22:00 on the 31st, see
"on budget" on Today that evening, and the next morning the closed month's
report card, the Year lens and the monthly email judged it against the old
figure and said "over".

Pinned here: in the household's first local hour of a month, the hourly
sweep re-freezes the month that just closed from the config live at that
moment. Once that has happened no later hour touches it, so an edit made
later on the 1st never leaks back into the closed month. When the first
hour was missed (worker down, the sweep's lock still held, a zone whose
clocks skip midnight), the closed month still carries a snapshot taken
before it ended, and the next sweep catches it up — but only from
settings nobody has saved since the month ended: an edit made in the new
month is the new month's, and a late close must not freeze it into the
one before.
"""

import datetime as dt
import unittest
import zoneinfo
from unittest import mock

from oikonome.engine import budget
from oikonome.jobs import worker

from .util import make_db, write_config

ZONE = zoneinfo.ZoneInfo("America/Los_Angeles")


class MonthCloseSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(self.conn, food_monthly=600)
        # the nightly froze October at 21:17 on the 31st, local
        budget.snapshot_month(self.conn, dt.date(2026, 10, 31))
        self._captured(dt.datetime(2026, 10, 31, 21, 17, tzinfo=ZONE))
        cfg = budget.load_config(self.conn)
        cfg["food_monthly"] = 800                 # raised at 22:00 that day
        budget.save_config(self.conn, cfg)
        self._saved(dt.datetime(2026, 10, 31, 22, 0, tzinfo=ZONE))

    def _saved(self, at):
        # the settings' save stamp, moved onto the household's clock the
        # same way
        self.conn.execute("UPDATE tenant_settings SET updated_at=%s", (at,))

    def tearDown(self):
        self.conn.close()

    def _captured(self, at):
        # the database stamps captured_at with its own clock; these tests
        # move the household's clock, so the stamp is set to match
        self.conn.execute("UPDATE budget_snapshots SET captured_at=%s "
                          "WHERE year=2026 AND month=10", (at,))

    def _october_food(self):
        row = self.conn.execute(
            "SELECT config FROM budget_snapshots WHERE year=2026 AND month=10"
        ).fetchone()
        return row["config"]["food_monthly"]

    def _hourly_at(self, local):
        with mock.patch("oikonome.localtime.now_local", return_value=local), \
             mock.patch.object(worker, "emails_due", return_value=[]):
            worker._email_if_due(self.tid)

    def test_the_first_hour_of_the_month_refreezes_the_closed_month(self):
        self._hourly_at(dt.datetime(2026, 11, 1, 0, 5, tzinfo=ZONE))
        self.assertEqual(self._october_food(), 800)

    def test_later_hours_never_reach_back_into_the_closed_month(self):
        self._hourly_at(dt.datetime(2026, 11, 1, 0, 5, tzinfo=ZONE))
        self._captured(dt.datetime(2026, 11, 1, 0, 5, tzinfo=ZONE))
        self.assertEqual(self._october_food(), 800)
        cfg = budget.load_config(self.conn)
        cfg["food_monthly"] = 950                 # November's own edit
        budget.save_config(self.conn, cfg)
        for local in (dt.datetime(2026, 11, 1, 9, 5, tzinfo=ZONE),
                      dt.datetime(2026, 11, 15, 0, 5, tzinfo=ZONE)):
            self._hourly_at(local)
        self.assertEqual(self._october_food(), 800)

    def test_a_missed_first_hour_is_caught_up_on_the_next_sweep(self):
        # 00:05 never ran (worker down); the next sweep finds October's
        # snapshot older than October's end and re-freezes it
        self._hourly_at(dt.datetime(2026, 11, 1, 3, 5, tzinfo=ZONE))
        self.assertEqual(self._october_food(), 800)

    def test_a_zone_that_skips_midnight_on_the_1st_is_caught_up(self):
        # where the clocks jump 00:00 -> 01:00 on the 1st there is no
        # local hour 0; the first sweep that month runs at 01:05
        cl = zoneinfo.ZoneInfo("America/Santiago")
        self.conn.execute("DELETE FROM budget_snapshots")
        write_config(self.conn, food_monthly=600)
        budget.snapshot_month(self.conn, dt.date(2026, 10, 31))
        self._captured(dt.datetime(2026, 10, 31, 21, 17, tzinfo=cl))
        cfg = budget.load_config(self.conn)
        cfg["food_monthly"] = 800
        budget.save_config(self.conn, cfg)
        self._saved(dt.datetime(2026, 10, 31, 22, 0, tzinfo=cl))
        self._hourly_at(dt.datetime(2026, 11, 1, 1, 5, tzinfo=cl))
        self.assertEqual(self._october_food(), 800)

    def test_a_late_close_leaves_out_an_edit_made_after_the_month_ended(self):
        # the worker was down until 02:00; at 01:30 the owner raised food
        # for November. October ended under $800 and must keep the
        # nightly's verdict rather than take November's figure.
        cfg = budget.load_config(self.conn)
        cfg["food_monthly"] = 900
        budget.save_config(self.conn, cfg)
        self._saved(dt.datetime(2026, 11, 1, 1, 30, tzinfo=ZONE))
        self._hourly_at(dt.datetime(2026, 11, 1, 2, 5, tzinfo=ZONE))
        self.assertEqual(self._october_food(), 600)

    def test_the_first_hour_also_leaves_out_an_edit_made_since_midnight(self):
        cfg = budget.load_config(self.conn)
        cfg["food_monthly"] = 900
        budget.save_config(self.conn, cfg)
        self._saved(dt.datetime(2026, 11, 1, 0, 2, tzinfo=ZONE))
        self._hourly_at(dt.datetime(2026, 11, 1, 0, 5, tzinfo=ZONE))
        self.assertEqual(self._october_food(), 600)

    def test_a_month_with_no_snapshot_is_not_judged_after_the_fact(self):
        self.conn.execute("DELETE FROM budget_snapshots")
        self._hourly_at(dt.datetime(2026, 11, 2, 9, 5, tzinfo=ZONE))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM budget_snapshots WHERE year=2026 AND month=10"
        ).fetchone())


if __name__ == "__main__":
    unittest.main()
