"""A savings goal's monthly rate divides its trailing-90-day net by the
months its account's ledger covers inside that window, not by a fixed
three. A goal funded for a month is not a third of its real rate: read
that way it shows "off pace" and an arrival three times too far. Under
about a month of ledger there is no rate to judge a pace by, so the goal
gives no on/off-pace verdict at all."""

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
        self.assertTrue(p["on_pace"])
        eta = dt.date.fromisoformat(p["eta"])
        self.assertLess((eta - TODAY).days, 365)       # ~11 months, not ~33

    def test_under_a_month_of_ledger_gives_no_pace_verdict(self):
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
        self.assertFalse(p["on_pace"])

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


if __name__ == "__main__":
    unittest.main()
