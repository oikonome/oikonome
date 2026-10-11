"""A weekly or daily bill's stored monthly figure is restated at the
mean-year rate once.

The per-bill monthly figure is the cycle's rate over a mean Gregorian year
(engine/bills.PER_YEAR). Bills saved before that change still hold the
52-week / 365-day figure until someone saves them again, and the budget's
expected income sums the stored column while the Bills headline recomputes
it, so the two disagree for every weekly or daily bill. A migration
restates the stored figure once: only rows still at the old rate to the
cent, so a re-run (or a bill already saved at the new rate, or a hand-set
figure) is left alone.
"""

import pathlib
import unittest

from oikonome.engine import bills

from .util import add_bill, make_db, write_config

MIGRATION = (pathlib.Path(bills.__file__).parent.parent / "db"
             / "migrations" / "183_bill_monthly_amount_mean_year.sql")

OLD_PER_YEAR = {"WEEKLY": 52.0, "DAILY": 365.0}


class StoredMonthlyFigureUsesTheMeanYear(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _bill(self, payee, amount, frequency, interval=1, *, income=False,
              old_rate=True, monthly=None):
        bid = add_bill(self.conn, payee, amount, frequency=frequency,
                       interval=interval, income=income,
                       next_due="2026-03-02")
        if old_rate or monthly is not None:
            signed = abs(amount) * (1 if income else -1)
            per_year = OLD_PER_YEAR.get(frequency, bills.PER_YEAR[frequency])
            value = (monthly if monthly is not None
                     else signed * per_year / interval / 12.0)
            self.conn.execute("UPDATE bills SET monthly_amount=%s WHERE id=%s",
                              (value, bid))
        return bid

    def _monthly(self, bid):
        return self.conn.execute(
            "SELECT monthly_amount FROM bills WHERE id=%s",
            (bid,)).fetchone()["monthly_amount"]

    def _migrate(self):
        self.conn.execute(MIGRATION.read_text())

    def test_old_rate_weekly_and_daily_figures_read_the_new_rate(self):
        pay = self._bill("Paycheck", 2000, "WEEKLY", 2, income=True)
        lawn = self._bill("Lawn service", 40, "WEEKLY")
        parking = self._bill("Parking", 10, "DAILY")
        self._migrate()
        self.assertAlmostEqual(self._monthly(pay),
                               bills._monthly(2000, "WEEKLY", 2), places=6)
        self.assertAlmostEqual(self._monthly(lawn),
                               bills._monthly(-40, "WEEKLY", 1), places=6)
        self.assertAlmostEqual(self._monthly(parking),
                               bills._monthly(-10, "DAILY", 1), places=6)

    def test_a_second_run_changes_nothing(self):
        lawn = self._bill("Lawn service", 40, "WEEKLY")
        self._migrate()
        once = self._monthly(lawn)
        self._migrate()
        self.assertEqual(self._monthly(lawn), once)

    def test_a_bill_already_at_the_new_rate_is_untouched(self):
        gym = self._bill("Gym", 15, "WEEKLY", old_rate=False)
        before = self._monthly(gym)
        self.assertAlmostEqual(before, bills._monthly(-15, "WEEKLY", 1),
                               places=6)
        self._migrate()
        self.assertEqual(self._monthly(gym), before)

    def test_monthly_yearly_envelope_and_odd_figures_are_untouched(self):
        rent = self._bill("Rent", 1500, "MONTHLY", old_rate=False)
        dues = self._bill("Dues", 120, "YEARLY", old_rate=False)
        food = add_bill(self.conn, "Groceries", 600, bill_type="envelope")
        # a figure that matches neither rate was put there by something
        # else; the migration does not guess what it meant
        odd = self._bill("Tutor", 50, "WEEKLY", monthly=-300.0)
        before = {b: self._monthly(b) for b in (rent, dues, food, odd)}
        self._migrate()
        self.assertEqual({b: self._monthly(b) for b in before}, before)


if __name__ == "__main__":
    unittest.main()


class RestatedMonthlyTests(unittest.TestCase):
    """A restore applies the same restatement to the bills an archive
    carries: an old-rate figure moves to the mean-year rate, anything else
    is left exactly as stored."""

    def test_an_old_rate_weekly_figure_is_restated(self):
        from oikonome.engine import bills
        raw = {"recurrence": {"frequency": "WEEKLY", "interval": 2}}
        self.assertAlmostEqual(
            bills.restated_monthly(2000.0, round(2000 * 52 / 2 / 12, 2), raw),
            round(2000 * (365.2425 / 14) / 12, 2), places=2)

    def test_a_figure_already_at_the_new_rate_is_unchanged(self):
        from oikonome.engine import bills
        raw = {"recurrence": {"frequency": "WEEKLY", "interval": 1}}
        new = round(100 * (365.2425 / 7) / 12, 2)
        self.assertEqual(bills.restated_monthly(100.0, new, raw), new)

    def test_a_monthly_bill_and_a_hand_set_figure_are_unchanged(self):
        from oikonome.engine import bills
        self.assertEqual(bills.restated_monthly(
            100.0, 100.0, {"recurrence": {"frequency": "MONTHLY"}}), 100.0)
        self.assertEqual(bills.restated_monthly(
            100.0, 123.45, {"recurrence": {"frequency": "WEEKLY"}}), 123.45)
        self.assertEqual(bills.restated_monthly(100.0, 433.33, None), 433.33)
