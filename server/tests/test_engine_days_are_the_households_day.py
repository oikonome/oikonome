"""What day it is, is a question about the household — not the container.

A hosted instance runs on UTC, so `date.today()` inside a job or an engine
pass rolls over at 5pm Pacific. Everything whose answer is a CALENDAR DAY
is wrong for those hours: the forecast walk starts on tomorrow, a bill
hand-edited this evening is stamped with tomorrow's date and comes out of
its grace window a day early, a savings pace counts from the wrong month.

The household's zone is in its settings; the instance's is the fallback.
These tests pin a zone whose current date is NOT the process's, and ask the
engine what day it thinks it is.
"""

import datetime as dt
import unittest
import zoneinfo

from oikonome.engine import bills, budget, forecast
from oikonome.engine.compat import as_dict

from .util import make_db, write_config


def a_zone_on_another_date() -> tuple[str, dt.date]:
    """An IANA zone whose date right now differs from the process's, and
    that date. At every instant the world spans more than one calendar
    date, so one of these always qualifies — which makes the test
    independent of when in the day it runs."""
    process_day = dt.date.today()
    for offset in (-12, -11, 12, 13, 14, 11, -10, 10):
        # Etc/GMT signs are inverted: Etc/GMT-14 is UTC+14
        name = f"Etc/GMT{-offset:+d}"
        day = dt.datetime.now(zoneinfo.ZoneInfo(name)).date()
        if day != process_day:
            return name, day
    raise AssertionError("no zone differs from the process day")


class TheHouseholdsOwnDay(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.zone, self.day = a_zone_on_another_date()
        write_config(self.conn, timezone=self.zone)
        self.assertNotEqual(self.day, dt.date.today())   # the premise

    def tearDown(self):
        self.conn.close()

    def test_the_forecast_walk_starts_on_the_households_day(self):
        """Day zero of the cash walk is the day the household is living,
        or every dated point on the chart — the trough, the first negative
        day — is labelled with a day that has not arrived there yet."""
        fc = forecast.build(self.conn)
        self.assertEqual(fc["plan"]["series"][0][0], self.day.isoformat())

    def test_a_hand_edited_amount_is_stamped_with_the_households_day(self):
        """The stamp starts the grace window that keeps drift off a number
        the person just typed. Stamped a day ahead, the window closes a day
        late; a day behind, it opens already spent."""
        bills.save_bill(self.conn, payee="Zenith Fibre", amount=80,
                        frequency="MONTHLY", source="manual")
        raw = as_dict(self.conn.execute(
            "SELECT raw FROM bills WHERE payee='Zenith Fibre'"
        ).fetchone()["raw"])
        self.assertEqual(raw["manual_edited_at"], self.day.isoformat())
        self.assertEqual(raw["amount_set_on"], self.day.isoformat())

    def test_an_explicit_today_still_wins(self):
        """Every one of these takes `today` from its caller when the caller
        has one — the zone resolution is the DEFAULT, not an override."""
        pinned = dt.date(2026, 7, 15)
        fc = forecast.build(self.conn, today=pinned)
        self.assertEqual(fc["plan"]["series"][0][0], pinned.isoformat())

    def test_the_instance_zone_answers_for_a_household_with_none(self):
        """A self-hosted box in somebody's closet has one clock and never
        sets a household zone; it must still not be asked the container's."""
        with budget.config_txn(self.conn) as cfg:
            cfg.pop("timezone", None)
        from oikonome import localtime
        self.assertEqual(localtime.household_day(self.conn),
                         localtime.now_local(None).date())


if __name__ == "__main__":
    unittest.main()
