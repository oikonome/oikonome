"""The "why it missed" door answers only for a horizon the grading keeps.

Grading records a fixed set of horizons; any other number in range used to
reach the engine and come back as a 404 that read "too old to explain",
which is not why. The door refuses it with a 422 that names the horizons it
does answer for, before opening a connection."""
import unittest
from unittest import mock

from fastapi import HTTPException

from oikonome.engine import forecast_score
from oikonome.web import api_today


class ForecastMissHorizonTest(unittest.TestCase):
    def test_a_horizon_grading_does_not_keep_is_refused_plainly(self):
        with mock.patch.object(api_today, "_conn") as conn:
            with self.assertRaises(HTTPException) as e:
                api_today.forecast_miss_api({"tenant_id": "x"},
                                            day="2026-03-04", horizon=3)
        self.assertEqual(e.exception.status_code, 422)
        for h in forecast_score.HORIZONS:
            self.assertIn(str(h), e.exception.detail)
        conn.assert_not_called()

    def test_every_kept_horizon_reaches_the_engine(self):
        for h in forecast_score.HORIZONS:
            with mock.patch.object(api_today, "_conn"), \
                 mock.patch.object(api_today, "_today"), \
                 mock.patch.object(forecast_score, "miss_kept",
                                   return_value={"ok": 1}) as kept:
                out = api_today.forecast_miss_api(
                    {"tenant_id": "x"}, day="2026-03-04", horizon=h)
            self.assertEqual(out, {"ok": 1})
            self.assertEqual(kept.call_args.args[2], h)


class TheEngineKeepsOnlyAMissThatCanExist(unittest.TestCase):
    """forecast_score.miss_kept answers None for a horizon grading does
    not keep, a day not yet past, or a night outside the inputs keep —
    before any query, and without keeping an entry for it."""

    def test_a_miss_that_cannot_exist_is_not_kept(self):
        import datetime as dt
        today = dt.date(2026, 7, 15)
        asks = [(dt.date(1900, 1, 1), 30), (dt.date(2026, 7, 1), 3),
                (dt.date(2026, 7, 1), 90), (dt.date(2026, 7, 15), 7),
                (dt.date(2100, 12, 31), 7),
                (today - dt.timedelta(days=200), 30)]
        conn = mock.MagicMock()
        with mock.patch.object(forecast_score, "_kept") as kept, \
             mock.patch.object(forecast_score, "miss") as miss:
            for day, h in asks:
                self.assertIsNone(
                    forecast_score.miss_kept(conn, day, h, today), (day, h))
        kept.assert_not_called()
        miss.assert_not_called()
        conn.execute.assert_not_called()

    def test_a_miss_that_can_exist_reaches_the_keep(self):
        import datetime as dt
        today = dt.date(2026, 7, 15)
        with mock.patch.object(forecast_score, "_kept",
                               return_value={"ok": 1}) as kept:
            self.assertEqual(forecast_score.miss_kept(
                mock.MagicMock(), dt.date(2026, 7, 1), 30, today), {"ok": 1})
        kept.assert_called_once()


if __name__ == "__main__":
    unittest.main()
