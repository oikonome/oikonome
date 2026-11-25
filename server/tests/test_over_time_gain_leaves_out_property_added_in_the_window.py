"""Net Worth's Over-time gain does not count a typed-in property arriving
as money gained.

A hand-entered item enters the trend on the month it was recorded, so the
total steps up there. A window whose first point is before that month and
whose last is on or after it measured the step as gain: a household whose
accounts fell read a large rise. Such a window's gain and percent are
measured on the financial accounts alone, like for like, and the window
carries the server's note saying what was added and when. The line still
steps up, and a window that does not cross the month is unchanged."""

import datetime as dt
import pathlib
import unittest
from unittest import mock

from oikonome.engine import reporting
from oikonome.engine.compat import jsonb

from .util import make_db, write_config

TODAY = dt.date(2026, 11, 17)
# thirteen month-ends, every figure invented: the accounts lose 1,000 a
# month, from 50,000 down to 38,000
MONTHS = ([f"2025-{m:02d}" for m in (11, 12)]
          + [f"2026-{m:02d}" for m in range(1, 12)])
FIN = [[m, 50000.0 - 1000.0 * i] for i, m in enumerate(MONTHS)]
HOME = 200000.0


class GainAcrossAnAddedProperty(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self._assets("2026-07-01")
        for m in MONTHS[:-1]:
            self.conn.execute("INSERT INTO networth_recorded (month, total) "
                              "VALUES (%s, 1)", (m,))

    def _assets(self, since, *more):
        write_config(self.conn, manual_assets=[
            {"name": "Home", "kind": "property", "value": HOME,
             "as_of": since, "acquired_on": since}, *more])

    def _nw(self):
        with mock.patch.object(reporting, "_reconstruct_trend",
                               return_value=[list(p) for p in FIN]):
            return reporting.compute_networth(self.conn, today=TODAY)

    def test_a_window_across_the_month_measures_the_accounts_alone(self):
        nw = self._nw()
        full = dict(nw["full_trend"])
        # the line still steps up on the month the home was recorded
        self.assertEqual(full["2026-06"], 43000.0)
        self.assertEqual(full["2026-07"], 42000.0 + HOME)
        for key, base in (("1y", 50000.0), ("6m", 44000.0), ("all", 50000.0)):
            w = nw["trend_windows"][key]
            self.assertEqual(w["gain"], 38000.0 - base, key)
            self.assertEqual(w["pct"],
                             round(100 * (38000.0 - base) / base, 2), key)
            self.assertEqual(
                w["note"], "Home added 07/26 — the change is measured on "
                           "the financial accounts", key)
            # the points drawn are the same ones
            self.assertEqual(nw["full_trend"][w["end"]][0], "2026-11")

    def test_a_window_that_starts_after_the_month_is_unchanged(self):
        nw = self._nw()
        w = nw["trend_windows"]["3m"]        # Aug → Nov, the home in both
        self.assertEqual(w["gain"], -3000.0)
        self.assertEqual(w["pct"], round(100 * -3000.0 / (41000.0 + HOME), 2))
        self.assertIsNone(w["note"])
        w = nw["trend_windows"]["cur"]
        self.assertEqual(w["gain"], -1000.0)
        self.assertIsNone(w["note"])

    def test_a_window_whose_first_point_is_the_month_is_unchanged(self):
        self._assets("2026-08-01")           # 3m's baseline is August
        w = self._nw()["trend_windows"]["3m"]
        self.assertEqual(w["gain"], -3000.0)
        self.assertIsNone(w["note"])

    def test_several_items_are_named_generically(self):
        self._assets("2026-07-01",
                     {"name": "Hatchback", "kind": "vehicle", "value": 9000.0,
                      "as_of": "2026-09-01", "acquired_on": "2026-09-01"})
        w = self._nw()["trend_windows"]["1y"]
        self.assertEqual(w["gain"], -12000.0)
        self.assertEqual(
            w["note"], "property added 07/26, 09/26 — the change is "
                       "measured on the financial accounts")

    def test_a_mortgage_typed_in_with_its_home_is_not_what_the_note_names(self):
        self._assets("2026-07-01",
                     {"name": "Home loan", "kind": "mortgage",
                      "value": -150000.0, "as_of": "2026-07-01",
                      "acquired_on": "2026-07-01"})
        w = self._nw()["trend_windows"]["1y"]
        self.assertEqual(w["gain"], -12000.0)
        self.assertTrue(w["note"].startswith("Home added 07/26"))

    def test_with_no_property_every_window_has_no_note(self):
        write_config(self.conn, manual_assets=[])
        nw = self._nw()
        for key, w in nw["trend_windows"].items():
            self.assertIsNone(w["note"], key)
        self.assertEqual(nw["trend_windows"]["1y"]["gain"], -12000.0)


class AHomeTypedInLaterThanItWasOwned(unittest.TestCase):
    """Snapshots recorded no property; later the owner types in a home
    owned long before. A recorded month keeps what was recorded, so the
    total steps up in the month it was typed in, and that is the month a
    window must not count as gain, whatever "owned since" says."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, manual_assets=[
            {"name": "Home", "kind": "property", "value": HOME,
             "as_of": "2026-09-10", "acquired_on": "1990-01-01"}])
        for m in MONTHS[:-1]:
            self.conn.execute("INSERT INTO networth_recorded (month, total) "
                              "VALUES (%s, 1)", (m,))
        for m in MONTHS[4:-1]:                    # 2026-03 .. 2026-10
            self.conn.execute(
                "INSERT INTO networth_snapshot (date, total, by_class) "
                "VALUES (%s, 1, %s)",
                (f"{m}-28", jsonb({"property_net":
                                   HOME if m >= "2026-09" else 0})))

    def _nw(self):
        with mock.patch.object(reporting, "_reconstruct_trend",
                               return_value=[list(p) for p in FIN]):
            return reporting.compute_networth(self.conn, today=TODAY)

    def test_the_step_is_where_the_value_first_appears_in_the_line(self):
        nw = self._nw()
        full = dict(nw["full_trend"])
        self.assertEqual(full["2026-08"], 41000.0)
        self.assertEqual(full["2026-09"], 40000.0 + HOME)
        for key, base in (("3m", 41000.0), ("6m", 44000.0)):
            w = nw["trend_windows"][key]
            self.assertEqual(w["gain"], 38000.0 - base, key)
            self.assertEqual(
                w["note"], "Home added 09/26 — the change is measured on "
                           "the financial accounts", key)

    def test_a_window_after_the_step_is_unchanged(self):
        w = self._nw()["trend_windows"]["cur"]
        self.assertEqual(w["gain"], -1000.0)
        self.assertIsNone(w["note"])

    def test_a_value_that_leaves_and_returns_is_never_counted(self):
        """The months before the snapshots are rebuilt with the home (it
        was owned), the recorded ones hold none: the line drops in March
        and rises in September. A window across either measures the
        accounts, and says the property value changed."""
        nw = self._nw()
        full = dict(nw["full_trend"])
        self.assertEqual(full["2026-02"], 47000.0 + HOME)
        self.assertEqual(full["2026-03"], 46000.0)
        w = nw["trend_windows"]["1y"]
        self.assertEqual(w["gain"], -12000.0)
        self.assertEqual(
            w["note"], "property value changed 03/26, 09/26 — the change "
                       "is measured on the financial accounts")

    def test_a_revalued_home_is_not_a_gain_either(self):
        self.conn.execute(
            "UPDATE networth_snapshot SET by_class = %s "
            "WHERE date = '2026-10-28'", (jsonb({"property_net": HOME + 25000}),))
        nw = self._nw()
        w = nw["trend_windows"]["cur"]      # Oct (recorded higher) -> today
        self.assertEqual(w["gain"], -1000.0)
        self.assertEqual(
            w["note"], "property value changed 11/26 — the change is "
                       "measured on the financial accounts")


class BothClientsPrintTheNote(unittest.TestCase):
    """The window's words are the server's on the web and on the phone;
    neither decides when a note applies."""

    def test_the_note_is_printed_as_sent(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        for rel in ("webapp/src/pages/NetWorth.tsx",
                    "mobile/src/app/networth.tsx"):
            self.assertIn("{w.note}", (root / rel).read_text(), rel)


if __name__ == "__main__":
    unittest.main()
