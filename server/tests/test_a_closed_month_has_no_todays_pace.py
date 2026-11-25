"""A closed month's hero bar does not say "today's pace".

The label under the hero bar's tick was printed on every month, so a
month that ended weeks ago read "today's pace" under its budget line.
The server now says whether there is a pace to label.
"""
import datetime as dt
import unittest

from oikonome.web import lenses

from .util import add_txn, freeze_month, make_db, write_config

TODAY = dt.date(2026, 7, 15)


class PaceLabelTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=800, other_monthly=1000)
        for m in (6, 7):
            add_txn(self.conn, dt.date(2026, m, 5), 2100.0, "HARBOR GROCER",
                    primary="FOOD_AND_DRINK")
        freeze_month(self.conn, 2026, 6)

    def _label(self, m):
        return lenses.month_summary(self.conn, 2026, m,
                                    today=TODAY)["pace_label"]

    def test_the_month_in_progress_labels_todays_pace(self):
        self.assertEqual(self._label(7), "today's pace")

    def test_a_closed_month_has_no_pace_label(self):
        self.assertIsNone(self._label(6))

    def test_a_future_month_has_no_pace_label(self):
        self.assertIsNone(self._label(9))


if __name__ == "__main__":
    unittest.main()
