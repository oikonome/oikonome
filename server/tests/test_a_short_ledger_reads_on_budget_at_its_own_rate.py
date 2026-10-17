"""A household whose ledger starts inside the current month, spending
steadily, reads on budget against the budget its own history gives it.

The seed and the wizard's suggestion take the daily rate over the
finished days the ledger covers and scale it to a month, and the live
verdict paces the month from the ledger's first day over the same
finished days — one window, budget.pace_window. When one of the three
drifts from the others, a new household reads OVER BUDGET for most of
its first month on exactly the spending its budget came from, and a
saved partial-month suggestion goes OVER again the next month.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 22)            # day 22 of a 31-day July
FOOD_PER_DAY = 40.0
OTHER_PER_DAY = 60.0


def _spend(conn, start: dt.date, end: dt.date) -> None:
    d = start
    while d <= end:
        add_txn(conn, d, FOOD_PER_DAY, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(conn, d, OTHER_PER_DAY, "HARDWARE BARN")
        d += dt.timedelta(days=1)


class ShortLedgerVerdictTests(unittest.TestCase):
    # 3 and 7 days, 20 days, and a ledger that starts on the 1st
    SPANS = (3, 7, 20, TODAY.day)

    def _ledger(self, days: int):
        conn = make_db()
        _spend(conn, TODAY - dt.timedelta(days=days - 1), TODAY)
        return conn

    def _assert_not_over(self, conn, today, food, other, label):
        write_config(conn, food_monthly=food, other_monthly=other)
        st = budget.month_status(conn, today)
        self.assertNotEqual(
            st["verdict"], "OVER BUDGET",
            f"{label}: actual {st['variable_actual']:.2f} vs expected "
            f"{st['variable_expected']:.2f} on budgets {food}/{other}")
        return st

    def test_the_seed_reads_on_budget_on_the_spending_it_came_from(self):
        for days in self.SPANS:
            with self.subTest(days=days):
                conn = self._ledger(days)
                try:
                    seed = budget._seed_config(conn, today=TODAY)
                    self._assert_not_over(conn, TODAY, seed["food_monthly"],
                                          seed["other_monthly"],
                                          f"seed, {days} days")
                finally:
                    conn.close()

    def test_an_accepted_suggestion_reads_on_budget(self):
        for days in self.SPANS:
            with self.subTest(days=days):
                conn = self._ledger(days)
                try:
                    write_config(conn)
                    sug = budget.suggest_budgets(conn, TODAY)["suggestions"]
                    st = self._assert_not_over(
                        conn, TODAY, sug["food_monthly"],
                        sug["other_monthly"], f"suggestion, {days} days")
                    # the suggestion is the ledger's own rate: on budget
                    # exactly, however few days stand behind it
                    self.assertEqual(st["verdict"], "ON BUDGET")
                finally:
                    conn.close()

    def test_an_accepted_suggestion_reads_on_budget_next_month(self):
        """The suggestion is a monthly figure, so the same steady spending
        is on budget through the following month too."""
        nxt = dt.date(2026, 8, 16)
        for days in (3, budget.MIN_RATE_DAYS, 20, TODAY.day):
            with self.subTest(days=days):
                conn = self._ledger(days)
                try:
                    write_config(conn)
                    sug = budget.suggest_budgets(conn, TODAY)["suggestions"]
                    _spend(conn, TODAY + dt.timedelta(days=1), nxt)
                    st = self._assert_not_over(
                        conn, nxt, sug["food_monthly"],
                        sug["other_monthly"], f"next month, {days} days")
                    self.assertEqual(st["verdict"], "ON BUDGET")
                finally:
                    conn.close()

    def test_a_young_ledger_still_reads_over_when_it_blows_the_month(self):
        """A young ledger is never called UNDER, but that is no pass:
        spending the whole month's budget in two days is OVER BUDGET."""
        conn = make_db()
        try:
            add_txn(conn, TODAY - dt.timedelta(days=1), 900.0,
                    "HARDWARE BARN")
            add_txn(conn, TODAY, 900.0, "HARDWARE BARN")
            write_config(conn)
            self.assertEqual(budget.month_status(conn, TODAY)["verdict"],
                             "OVER BUDGET")
        finally:
            conn.close()

    def test_the_running_rate_counts_only_the_days_the_ledger_saw(self):
        """"Running $X/day" divides by the days of this month the ledger
        covers, not by the calendar day."""
        conn = make_db()
        try:
            _spend(conn, TODAY - dt.timedelta(days=9), TODAY)
            write_config(conn, food_monthly=200, other_monthly=200)
            st = budget.month_status(conn, TODAY)
            self.assertEqual(st["verdict"], "OVER BUDGET")
            food = next(r for r in st["reasons"] if r["bucket"] == "food")
            self.assertIn(f"running ${FOOD_PER_DAY:,.0f}/day", food["pace"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
