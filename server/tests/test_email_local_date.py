"""A live gather is live, whatever this process's own clock says.

The email sweep gathers on the HOUSEHOLD's local date, which near a month
boundary can legitimately sit in a different month than the instance's
clock (a household west of UTC at local month-end, or east of it just
after local midnight). gather() used to re-decide liveness by comparing
that date to dt.date.today() — the instance zone — and a mismatch flipped
the send "historical", which silently drops the whole fixed-bill schedule
from a live daily email. Liveness belongs to the caller: live=True means
`today` IS the caller's present; only a live=False past-day view may be
historical.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.web import report

from .util import make_db, write_config


def _prev_month_day() -> dt.date:
    return dt.date.today().replace(day=1) - dt.timedelta(days=1)


class LiveGatherAcrossZones(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _historical_flag(self, today: dt.date, *, live: bool) -> bool:
        # gather() calls month_status once directly and downstream code may
        # call it again — the FIRST call is gather's own liveness decision
        seen: list[bool] = []
        real = budget.month_status

        def spy(conn, day, *, historical=False, **kw):
            seen.append(historical)
            return real(conn, day, historical=historical, **kw)

        with mock.patch.object(budget, "month_status", spy):
            report.gather(self.conn, today, live=live)
        return seen[0]

    def test_a_live_send_in_another_month_is_not_historical(self):
        # the household's local date is in a month this process's clock
        # has left (or not reached) — the send is still LIVE
        self.assertFalse(self._historical_flag(_prev_month_day(), live=True))

    def test_a_past_day_view_in_another_month_is_historical(self):
        self.assertTrue(self._historical_flag(_prev_month_day(), live=False))

    def test_a_past_day_in_the_current_month_is_not_historical(self):
        # same-month back-nav keeps the live schedule (unchanged behaviour)
        self.assertFalse(self._historical_flag(
            dt.date.today().replace(day=1), live=False))


if __name__ == "__main__":
    unittest.main()
