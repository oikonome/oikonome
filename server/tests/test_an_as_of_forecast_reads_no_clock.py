"""A forecast built as of a day reads that day, never the wall clock:
everything under forecast.build takes its `today` (the ledger's "present"
in coverage.window included), so a forecast made as of a past day is
exact. Figures and names are invented."""

import datetime as dt
import types
import unittest
from unittest import mock

from oikonome.engine import coverage, forecast

from .test_each_card_is_paid_its_own_way import _household
from .util import TODAY


class _NoClock(dt.date):
    @classmethod
    def today(cls):
        raise AssertionError("the wall clock was read")


class AnAsOfForecast(unittest.TestCase):
    def test_no_wall_clock_is_read_under_build(self):
        conn = _household(1)
        try:
            fake = types.SimpleNamespace(date=_NoClock,
                                         datetime=dt.datetime,
                                         timedelta=dt.timedelta)
            with mock.patch.object(coverage, "dt", fake):
                f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        self.assertTrue(f["pace_usual"]["series"])


if __name__ == "__main__":
    unittest.main()
