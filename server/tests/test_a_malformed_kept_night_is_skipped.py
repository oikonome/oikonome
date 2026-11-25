"""A kept forecast night is read whatever JSON shape it carries
(forecast_score: grading, the panel, the drill): a restored row whose
`usual` walk is a list, whose balances are strings, or whose inputs are
not the shape the drill wrote is no forecast — it grades nothing and
crashes nothing — and a night dated at the start of the calendar does not
walk every day since. Figures are invented."""

import datetime as dt
import json
import time
import unittest

from oikonome.engine import forecast_score as fs

D = dt.date
H = fs.HORIZONS


def _night(day, start, walks):
    return {"as_of": day, "model": "w3", "checking_id": "chk",
            "start_checking": float(start), "fresh": True,
            "restored": False, "walks": walks}


def _good(day, start):
    at = [round(start * 100)] * len(H)
    return _night(day, start, {k: {"at": at, "cards_out": [0] * 5}
                               for k in ("usual", "now", "due", "low")})


BAD_WALKS = (
    {"usual": [1, 2, 3]},
    {"usual": {"at": ["1200", "x", None, {}, []]}},
    {"usual": {"at": "120000"}},
    {"usual": None, "low": 7},
    {"usual": {"at": [True, "1", None, None, None]}},
)


class AMalformedNight(unittest.TestCase):
    def test_grading_skips_it(self):
        d0 = D(2026, 5, 1)
        for walks in BAD_WALKS:
            nights = [_night(d0, 1000, walks)] + [
                _good(d0 + dt.timedelta(days=k), 1000) for k in range(1, 70)]
            pairs, _sk = fs.graded_pairs(nights, D(2026, 7, 15))
            self.assertFalse([p for p in pairs if p.as_of == d0], walks)
            self.assertTrue(pairs)

    def test_a_night_at_the_start_of_the_calendar_is_bounded(self):
        nights = [_good(D(1800, 1, 2), 1000), _good(D(2026, 5, 1), 1000)]
        t0 = time.monotonic()
        fs.graded_pairs(nights, D(2026, 7, 15))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_the_panel_and_the_drill_read_it(self):
        from .util import make_db, write_config
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=600)
            today = D(2026, 7, 15)
            for k in range(60):
                d = today - dt.timedelta(days=60 - k)
                walks = (BAD_WALKS[k % len(BAD_WALKS)] if k % 3 == 0
                         else _good(d, 1000)["walks"])
                inputs = ({"events": [[3, "x"], "y", [1, 100, "Pay", "bill",
                                                      None],
                                      [3, -5000, "Rent", "bill", None, "x"],
                                      [2, -700, "Water", "bill", 4]]}
                          if k % 2 else ["not", "an", "object"])
                conn.execute(
                    "INSERT INTO forecast_nights (as_of, model, checking_id,"
                    " start_checking, fresh, walks, pace, inputs) VALUES "
                    "(%s, 'w3', 'chk', 1000, true, %s::jsonb, '{}', "
                    "%s::jsonb)", (d, json.dumps(walks), json.dumps(inputs)))
            # a restored night dated at the start of the calendar
            conn.execute(
                "INSERT INTO forecast_nights (as_of, model, checking_id,"
                " start_checking, fresh, walks, pace) VALUES "
                "('0001-01-01', 'w3', 'chk', 1000, true, %s::jsonb, '{}')",
                (json.dumps(_good(today, 1000)["walks"]),))
            fs.panel(conn, today)
            fs.compute_score(conn, today)
            for k in range(60):
                d = today - dt.timedelta(days=60 - k)
                for h in H:
                    fs.miss(conn, d + dt.timedelta(days=h), h, today)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
