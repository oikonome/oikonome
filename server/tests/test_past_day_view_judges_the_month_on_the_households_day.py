"""A past day the person steps back to is in a CLOSED month only when its
month has ended where the household lives. The process clock is the
container's (UTC on a hosted instance), and near a month boundary it sits in
a different month than the household: deciding on it would drop the bill
schedule from an earlier day of the household's current month, or project
today's live schedule onto a month that has already closed there."""

import datetime as dt
import unittest
from unittest import mock

from oikonome import localtime
from oikonome.web import report

from .util import TODAY, add_bill, make_db, write_config


def _household_clock(day: dt.date):
    """The household's wall clock pinned to `day` in the evening, whatever
    the process's own date is (the fixture month is never the real one)."""
    def now_local(cfg):
        return dt.datetime.combine(day, dt.time(18, 0),
                                   tzinfo=localtime.tenant_tz(cfg))
    return mock.patch.object(localtime, "now_local", now_local)


class PastDayHistoricalOnHouseholdDay(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=1000, other_monthly=1000)
        add_bill(self.conn, "Rent", 2000.0, next_due=dt.date(2026, 7, 1),
                 last_seen=dt.date(2026, 6, 1))

    def tearDown(self):
        self.conn.close()

    def test_an_earlier_day_of_the_households_month_keeps_its_bills(self):
        with _household_clock(TODAY):
            st = report.gather(self.conn, TODAY - dt.timedelta(days=1),
                               live=False)
        self.assertEqual(st["buckets"]["fixed"]["month_budget"], 2000.0)

    def test_a_day_of_a_month_closed_for_the_household_has_no_schedule(self):
        # the household is already on the 1st of the next month; the
        # viewed day belongs to a month that has closed for it
        with _household_clock(dt.date(2026, 8, 1)):
            st = report.gather(self.conn, TODAY, live=False)
        self.assertEqual(st["buckets"]["fixed"]["month_budget"], 0.0)


if __name__ == "__main__":
    unittest.main()
