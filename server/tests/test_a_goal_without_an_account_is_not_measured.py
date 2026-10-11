"""A savings goal with match words but no destination account is never
measured. The words match both legs of a transfer (the savings side and the
checking side), and with no destination there is no rule — sign, subtype or
pairing — that can say which leg is the fund. Summing them reports $0 for a
real contribution, a negative balance when only checking is linked, and
counts a withdrawal out of savings as evidence the plan was paid. So such a
goal reads as plan-only and flagged as needing an account: no saved figure,
no posted evidence, and the forecast keeps owing its whole plan."""
import datetime as dt
import unittest

from oikonome.engine import forecast, savings

from .util import TODAY, add_txn, make_db, write_config

GOAL = {"name": "Trip", "target": 1000, "tokens": ["trip"],
        "monthly_plan": 250, "start_balance": 0}


class GoalWithoutAccountIsNotMeasured(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sav','it1','Test Savings',"
            "'depository','savings',0) ON CONFLICT (tenant_id, id) DO NOTHING")
        self.cfg = {"savings_goals": [dict(GOAL)]}
        write_config(self.conn, savings_goals=[dict(GOAL)])
        self.day = TODAY - dt.timedelta(days=5)

    def tearDown(self):
        self.conn.close()

    def _checking_leg(self):
        add_txn(self.conn, self.day, 250.0, "Transfer to TRIP savings",
                account="chk", primary="TRANSFER_OUT")

    def _savings_leg(self):
        add_txn(self.conn, self.day, -250.0, "TRIP transfer in",
                account="sav", primary="TRANSFER_IN")

    def _progress(self):
        return savings.progress(self.conn, self.cfg, TODAY)[0]

    def test_both_legs_of_a_transfer_leave_the_goal_unmeasured(self):
        self._checking_leg()
        self._savings_leg()
        p = self._progress()
        self.assertIsNone(p["saved"])
        self.assertIsNone(p["pct"])
        self.assertTrue(p["plan_only"])
        self.assertTrue(p["needs_account"])

    def test_the_checking_leg_alone_never_reads_as_a_negative_balance(self):
        self._checking_leg()
        self.assertIsNone(self._progress()["saved"])

    def test_a_worded_goal_without_an_account_posts_no_plan_evidence(self):
        self._checking_leg()
        self._savings_leg()
        got = savings.posted_for_plan(self.conn, self.cfg, dict(GOAL),
                                      since=TODAY.replace(day=1))
        self.assertEqual(got, 0.0)

    def test_the_forecast_keeps_owing_the_whole_plan(self):
        self._checking_leg()
        self._savings_leg()
        f = forecast.build(self.conn, today=TODAY, days=30)
        got = [(d, a) for d, a, p in f["events"]
               if p == "Trip (savings plan)"]
        self.assertEqual(got[0], ("2026-07-16", -250.0))

    def test_a_goal_with_neither_words_nor_account_does_not_need_one(self):
        cfg = {"savings_goals": [{"name": "Savings", "monthly_plan": 250}]}
        p = savings.progress(self.conn, cfg, TODAY)[0]
        self.assertTrue(p["plan_only"])
        self.assertFalse(p["needs_account"])

    def test_a_goal_on_an_account_is_measured_and_needs_nothing(self):
        self._checking_leg()
        self._savings_leg()
        cfg = {"savings_goals": [dict(GOAL, account_id="sav")]}
        p = savings.progress(self.conn, cfg, TODAY)[0]
        self.assertEqual(p["saved"], 250.0)
        self.assertFalse(p["plan_only"])
        self.assertFalse(p["needs_account"])


if __name__ == "__main__":
    unittest.main()
