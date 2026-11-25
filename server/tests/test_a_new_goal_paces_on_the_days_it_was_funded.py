"""A savings goal's monthly rate divides its trailing-90-day net by the
months its account's ledger covers inside that window, not by a fixed
three. A goal funded for a month is not a third of its real rate: read
that way its arrival date is three times too far. The on/off-pace verdict
does not come from this rate (it is read against the goal's schedule), so
a goal with no plan stored yet has none."""

import calendar
import datetime as dt
import unittest

from oikonome.engine import savings

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 15)
GOAL = {"name": "cushion", "target": 6000.0, "monthly_plan": 500.0,
        "account_id": "sav", "tokens": [], "start_balance": 0.0,
        "target_date": (TODAY + dt.timedelta(days=365)).isoformat()}


class NewGoalPaceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sav','it1','Test Savings',"
            "'depository','savings',0) ON CONFLICT (tenant_id, id) DO NOTHING")

    def tearDown(self):
        self.conn.close()

    def _fund(self, days_ago):
        add_txn(self.conn, TODAY - dt.timedelta(days=days_ago), -500.0,
                "TO SAVINGS", account="sav", primary="TRANSFER_IN")

    def _progress(self):
        return savings.progress(self.conn, {"savings_goals": [dict(GOAL)]},
                                TODAY)[0]

    def test_a_month_old_goal_reads_its_whole_month(self):
        self._fund(30)
        p = self._progress()
        self.assertEqual(p["rate_90d"], 500.0)
        eta = dt.date.fromisoformat(p["eta"])
        self.assertLess((eta - TODAY).days, 365)       # ~11 months, not ~33

    def test_a_goal_with_no_plan_stored_gives_no_pace_verdict(self):
        self._fund(10)
        p = self._progress()
        self.assertIsNone(p["on_pace"])
        # one transfer is not stretched into a larger monthly rate
        self.assertEqual(p["rate_90d"], 500.0)

    def test_the_month_before_the_second_deposit_counts(self):
        # two monthly $500 transfers, 35 and 5 days ago: two months of
        # saving at $500, not $1,000 over the 35 days since the first
        self._fund(35)
        self._fund(5)
        goal = dict(GOAL, target=9000.0)               # needs ~$667/mo
        p = savings.progress(self.conn, {"savings_goals": [goal]}, TODAY)[0]
        self.assertAlmostEqual(p["rate_90d"], 500.0, delta=1)

    def test_a_full_window_still_divides_by_three(self):
        self._fund(100)                                 # ledger predates it
        self._fund(60)
        self._fund(30)
        self._fund(5)
        self.assertEqual(self._progress()["rate_90d"], round(1500 / 3, 2))

    def _rate_on(self, deposits, today):
        for d in deposits:
            add_txn(self.conn, d, -500.0, "TO SAVINGS", account="sav",
                    primary="TRANSFER_IN")
        goal = dict(GOAL, target_date=(today + dt.timedelta(
            days=365)).isoformat())
        return savings.progress(self.conn, {"savings_goals": [goal]},
                                today)[0]

    def test_a_thirty_day_month_is_still_two_months_of_saving(self):
        # April 1 to May 1 is 30 days: a day count reads it as one month
        # and doubles the rate on the day the second deposit lands
        D = dt.date
        p = self._rate_on([D(2026, 4, 1), D(2026, 5, 1)], D(2026, 5, 1))
        self.assertEqual(p["rate_90d"], 500.0)

    def test_february_is_still_two_months_of_saving(self):
        D = dt.date
        p = self._rate_on([D(2026, 2, 1), D(2026, 3, 1)], D(2026, 3, 3))
        self.assertEqual(p["rate_90d"], 500.0)

    def test_three_monthly_deposits_inside_sixty_days_are_three_months(self):
        D = dt.date
        p = self._rate_on([D(2026, 1, 1), D(2026, 2, 1), D(2026, 3, 1)],
                          D(2026, 3, 1))
        self.assertEqual(p["rate_90d"], 500.0)

    def test_a_full_window_counts_three_months_of_deposits_not_four(self):
        # Jan 1 to Apr 1 of a non-leap year is exactly 90 days: a window
        # reaching back 90 days inclusive holds four monthly deposits and
        # divides them by three
        D = dt.date
        p = self._rate_on([D(2026, m, 1) for m in (10, 11, 12)]
                          + [D(2027, m, 1) for m in (1, 2, 3, 4)],
                          D(2027, 4, 1))
        self.assertEqual(p["rate_90d"], 500.0)
        self.assertEqual(p["saved_90d"], 1500.0)

    def _month_ends(self, first, n):
        y, m = first.year, first.month
        out = []
        for _ in range(n):
            out.append(dt.date(y, m, calendar.monthrange(y, m)[1]))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out

    def test_a_month_end_deposit_read_on_a_month_end_counts_three_times(self):
        # a 30-day month (or February) read on its last day, with a 31-day
        # month three back: the window must not open on that month's last
        # day and hold a fourth month-end deposit
        D = dt.date
        for today in (D(2026, 6, 30), D(2026, 11, 30), D(2028, 2, 29),
                      D(2027, 2, 28), D(2026, 4, 30)):
            with self.subTest(today=today):
                self.conn.execute("DELETE FROM transactions")
                first = D(today.year - 1, today.month, 1)
                deps = [d for d in self._month_ends(first, 13) if d <= today]
                p = self._rate_on(deps, today)
                self.assertEqual(p["saved_90d"], 1500.0)
                self.assertEqual(p["rate_90d"], 500.0)

    def test_the_pace_window_mid_month_is_the_day_after_three_months_back(self):
        D = dt.date
        self.assertEqual(savings.pace_since(D(2026, 7, 15)), D(2026, 4, 16))
        self.assertEqual(savings.pace_since(D(2027, 4, 1)), D(2027, 1, 2))
        self.assertEqual(savings.pace_since(D(2026, 5, 30)), D(2026, 3, 1))
        self.assertEqual(savings.pace_since(D(2026, 6, 30)), D(2026, 4, 1))

    def test_interest_in_the_month_in_progress_is_not_a_month_of_saving(self):
        # one $500 transfer on Sep 15, a cents interest credit on Oct 1,
        # read on Oct 10 before October's deposit: still one month of
        # saving at $500, because interest is not a contribution and the
        # rate's numerator never counts it either
        D = dt.date
        add_txn(self.conn, D(2026, 10, 1), -0.41, "INTEREST PAID",
                account="sav", primary="INCOME")
        p = self._rate_on([D(2026, 9, 15)], D(2026, 10, 10))
        self.assertEqual(p["rate_90d"], 500.0)

    def test_a_fee_in_the_month_in_progress_is_not_a_month_of_saving(self):
        D = dt.date
        add_txn(self.conn, D(2026, 10, 2), 3.0, "MONTHLY FEE",
                account="sav", primary="BANK_FEES")
        p = self._rate_on([D(2026, 9, 15)], D(2026, 10, 10))
        self.assertEqual(p["rate_90d"], 500.0)

    def test_a_deposit_in_the_month_in_progress_still_counts_it(self):
        D = dt.date
        p = self._rate_on([D(2026, 9, 15), D(2026, 10, 1)], D(2026, 10, 10))
        self.assertEqual(p["rate_90d"], 500.0)


    def test_a_withdrawal_in_the_month_in_progress_is_not_a_month_of_saving(self):
        # $1,000 in on Sep 15, $200 moved back to checking on Oct 3, read
        # on Oct 16: one month of saving that netted $800. The withdrawal
        # lowers the numerator; it does not also lengthen the divisor.
        D = dt.date
        add_txn(self.conn, D(2026, 9, 15), -1000.0, "TO SAVINGS",
                account="sav", primary="TRANSFER_IN")
        add_txn(self.conn, D(2026, 10, 3), 200.0, "TO CHECKING",
                account="sav", primary="TRANSFER_OUT")
        goal = dict(GOAL, target_date=D(2027, 10, 16).isoformat())
        p = savings.progress(self.conn, {"savings_goals": [goal]},
                             D(2026, 10, 16))[0]
        self.assertEqual(p["rate_90d"], 800.0)

    def test_a_siblings_transfer_in_the_month_in_progress_is_not_this_goals_month(self):
        # an untokened goal and a tokened one share the account; only the
        # tokened goal was funded in October, so October is not a month of
        # saving for the untokened goal, whose net already excludes it
        D = dt.date
        add_txn(self.conn, D(2026, 8, 20), -600.0, "TO SAVINGS",
                account="sav", primary="TRANSFER_IN")
        add_txn(self.conn, D(2026, 9, 20), -600.0, "TO SAVINGS",
                account="sav", primary="TRANSFER_IN")
        add_txn(self.conn, D(2026, 10, 3), -200.0, "VACATION FUND",
                account="sav", primary="TRANSFER_IN")
        today = D(2026, 10, 16)
        cushion = dict(GOAL, target_date=D(2027, 10, 16).isoformat())
        trip = dict(GOAL, name="trip", tokens=["vacation"],
                    target_date=D(2027, 10, 16).isoformat())
        p = savings.progress(self.conn, {"savings_goals": [cushion, trip]},
                             today)[0]
        self.assertEqual(p["name"], "cushion")
        self.assertEqual(p["rate_90d"], 600.0)

    def test_an_untokened_goals_own_deposit_beside_a_tokened_sibling_opens_the_month(self):
        # beside a tokened sibling whose tokens match nothing, an untokened goal's own
        # October transfer is a contribution its net counts, so October is
        # a month of saving: three months of $600, not two of $900
        D = dt.date
        for d in (D(2026, 8, 20), D(2026, 9, 20), D(2026, 10, 3)):
            add_txn(self.conn, d, -600.0, "TO SAVINGS", account="sav",
                    primary="TRANSFER_IN")
        cushion = dict(GOAL, target_date=D(2027, 10, 16).isoformat())
        trip = dict(GOAL, name="trip", tokens=["vacation"],
                    target_date=D(2027, 10, 16).isoformat())
        p = savings.progress(self.conn, {"savings_goals": [cushion, trip]},
                             D(2026, 10, 16))[0]
        self.assertEqual(p["rate_90d"], 600.0)

    def test_a_goal_past_its_target_date_is_not_on_pace(self):
        # $5,900 of $6,000 saved at $1,000/mo, but the date passed a week
        # ago: the goal missed it, however little is left
        for days in (80, 50, 20):
            add_txn(self.conn, TODAY - dt.timedelta(days=days), -1000.0,
                    "TO SAVINGS", account="sav", primary="TRANSFER_IN")
        goal = dict(GOAL, start_balance=2900.0,
                    target_date=(TODAY - dt.timedelta(days=7)).isoformat())
        p = savings.progress(self.conn, {"savings_goals": [goal]}, TODAY)[0]
        self.assertGreater(p["rate_90d"], 0)
        self.assertIs(p["on_pace"], False)


if __name__ == "__main__":
    unittest.main()
