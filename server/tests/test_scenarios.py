"""named income scenarios (legacy pair migrates) + ledger-driven
savings goals — progress from matched transfers, fixed-bill plan surplus
semantics, pace, and edge-triggered milestones."""

import datetime as dt
import unittest

from oikonome.engine import budget, savings

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 15)


def _add_savings_account(conn):
    conn.execute(
        "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
        " VALUES ('sav','it1','Test Savings','depository','savings',0)"
        " ON CONFLICT (tenant_id, id) DO NOTHING")


def _xfer(conn, date, amount, name, txn_id=None):
    # destination-side transfer: negative = money INTO savings
    return add_txn(conn, date, amount, name, primary="TRANSFER_IN",
                   account="sav", txn_id=txn_id)


GOAL = {"name": "trip fund", "target": 3000.0, "monthly_plan": 250.0,
        "account_id": "sav", "tokens": [], "start_balance": 0.0}


class ScenarioTests(unittest.TestCase):
    def test_legacy_pair_migrates_and_behaves_identically(self):
        cfg = {"income_biweekly_saving": 2400.0,
               "income_biweekly_not_saving": 2900.0,
               "retirement_saving": True}
        scen, active = budget.income_scenarios(cfg)
        self.assertEqual([s["name"] for s in scen], ["saving", "not saving"])
        self.assertEqual(active, "saving")
        self.assertEqual(budget.active_monthly_income(cfg), 4800.0)
        cfg["retirement_saving"] = False
        self.assertEqual(budget.active_monthly_income(cfg), 5800.0)

    def test_named_list_with_cadences(self):
        cfg = {"income_scenarios": [
                   {"name": "full", "take_home": 3000, "cadence": "monthly"},
                   {"name": "weekly gig", "take_home": 500,
                    "cadence": "weekly"}],
               "active_scenario": "weekly gig"}
        self.assertEqual(budget.active_monthly_income(cfg), 2000.0)
        cfg["active_scenario"] = "full"
        self.assertEqual(budget.active_monthly_income(cfg), 3000.0)
        # unknown active falls back to the first entry, never crashes
        cfg["active_scenario"] = "gone"
        self.assertEqual(budget.active_monthly_income(cfg), 3000.0)

    def test_no_scenarios_falls_back_to_static(self):
        self.assertEqual(budget.active_monthly_income(
            {"budgeted_income_monthly": 5100}), 5100)


class GoalProgressTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _add_savings_account(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_matched_net_and_pace(self):
        # 3 monthly $300 deposits + one $100 withdrawal, all inside 90d
        for i, d in enumerate((TODAY - dt.timedelta(days=75),
                               TODAY - dt.timedelta(days=45),
                               TODAY - dt.timedelta(days=15))):
            _xfer(self.conn, d, -300.0, "TRANSFER TO SAVINGS")
        _xfer(self.conn, TODAY - dt.timedelta(days=10), 100.0,
              "TRANSFER FROM SAVINGS")
        cfg = {"savings_goals": [dict(GOAL, start_balance=1000.0)]}
        p = savings.progress(self.conn, cfg, TODAY)[0]
        self.assertEqual(p["saved"], 1800.0)        # 1000 + 900 − 100
        self.assertEqual(p["rate_90d"], round(800 / 3, 2))
        self.assertEqual(p["pct"], 60.0)
        self.assertIsNotNone(p["eta"])              # rate > 0 → projected
        # dated goal: needs 1200 in ~2 months at ~267/mo → off pace
        cfg["savings_goals"][0]["target_date"] = (
            TODAY + dt.timedelta(days=61)).isoformat()
        p = savings.progress(self.conn, cfg, TODAY)[0]
        self.assertFalse(p["on_pace"])
        # a year out → comfortably on pace
        cfg["savings_goals"][0]["target_date"] = (
            TODAY + dt.timedelta(days=365)).isoformat()
        p = savings.progress(self.conn, cfg, TODAY)[0]
        self.assertTrue(p["on_pace"])

    def test_tokens_carve_one_account(self):
        _xfer(self.conn, TODAY - dt.timedelta(days=5), -200.0,
              "TRANSFER TRIP FUND")
        _xfer(self.conn, TODAY - dt.timedelta(days=5), -500.0,
              "TRANSFER EMERGENCY")
        cfg = {"savings_goals": [dict(GOAL, tokens=["trip"])]}
        p = savings.progress(self.conn, cfg, TODAY)[0]
        self.assertEqual(p["saved"], 200.0)

    def test_non_transfer_rows_do_not_count(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=5), -12.0,
                "INTEREST PAYMENT", primary="INCOME", account="sav")
        p = savings.progress(self.conn, {"savings_goals": [GOAL]}, TODAY)[0]
        self.assertEqual(p["saved"], 0.0)

    def test_plan_reduces_plan_surplus_not_verdict(self):
        write_config(self.conn, budgeted_income_monthly=6000,
                     savings_goals=[GOAL])
        st = budget.month_status(self.conn, TODAY)
        # compare against the same status without the goal
        write_config(self.conn, budgeted_income_monthly=6000,
                     savings_goals=[])
        st2 = budget.month_status(self.conn, TODAY)
        self.assertEqual(round(st2["plan_surplus"] - st["plan_surplus"], 2),
                         250.0)
        self.assertEqual(st["verdict"], st2["verdict"])   # verdict untouched


class MilestoneTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _add_savings_account(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_edges_fire_once(self):
        _xfer(self.conn, TODAY - dt.timedelta(days=20), -1600.0,
              "TRANSFER TO SAVINGS")
        cfg = {"savings_goals": [dict(GOAL)]}
        msgs, state = savings.check_milestones(self.conn, cfg, TODAY)
        self.assertEqual(len(msgs), 1)
        self.assertIn("50%", msgs[0])
        # same state again → silent
        cfg["savings_goal_state"] = state
        msgs, state = savings.check_milestones(self.conn, cfg, TODAY)
        self.assertEqual(msgs, [])
        # crossing 100% fires once
        _xfer(self.conn, TODAY - dt.timedelta(days=1), -1500.0,
              "TRANSFER TO SAVINGS", txn_id="xfer-final")
        cfg["savings_goal_state"] = state
        msgs, _ = savings.check_milestones(self.conn, cfg, TODAY)
        self.assertEqual(len(msgs), 1)
        self.assertIn("100%", msgs[0])

    def test_off_pace_flip_fires_once(self):
        # slow saver, near-term date → off pace after an initial on-pace state
        _xfer(self.conn, TODAY - dt.timedelta(days=80), -50.0,
              "TRANSFER TO SAVINGS")
        cfg = {"savings_goals": [dict(
            GOAL, target_date=(TODAY + dt.timedelta(days=45)).isoformat())],
            "savings_goal_state": {"trip fund": {"milestone": 25,
                                                 "on_pace": True}}}
        msgs, state = savings.check_milestones(self.conn, cfg, TODAY)
        self.assertTrue(any("off pace" in m for m in msgs))
        cfg["savings_goal_state"] = state
        msgs, _ = savings.check_milestones(self.conn, cfg, TODAY)
        self.assertFalse(any("off pace" in m for m in msgs))


if __name__ == "__main__":
    unittest.main()
