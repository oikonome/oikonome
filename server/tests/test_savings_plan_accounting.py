"""Savings-plan money: what counts as posted, who owns a shared account,
and what the runway must reserve.

Measuring a goal's posted contribution from DESTINATION-side TRANSFER rows
alone reads posted=0 for a checking TRANSFER_OUT whose savings account
carries no rows (external / unsynced / checking-leg-only aggregator): the
forecast walk schedules the plan AGAIN and the month's cash understates by
the whole transfer. So posted_for_plan accepts the checking leg as evidence
(token-matched, or amount==plan for token-less goals), capped at the plan
and max'd with destination truth.

Two goals sharing one destination account with no tokens would each claim
the account's ENTIRE transfer net (MTD savings 2x, progress 2x). matched_net
partitions instead: tokened goals keep their carve-outs, the un-tokened
remainder goes to the first token-less goal in config order, and later
siblings read 0.

A bills-only due_total reads too rosy by exactly the residual the forecast
walk schedules inside the same window, so due_total carries
max(0, plan - posted) per goal — plus next month's plan when the horizon
crosses the 1st.
"""

import datetime as dt
import unittest

from oikonome.engine import forecast, savings

from .util import TODAY, add_txn, make_db, write_config


def _add_savings_account(conn):
    conn.execute(
        "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
        " VALUES ('sav','it1','Test Savings','depository','savings',0)"
        " ON CONFLICT (tenant_id, id) DO NOTHING")


class CheckingLegCountsAsPostedTests(unittest.TestCase):
    """Checking TRANSFER_OUT, empty savings account: the plan must not be
    scheduled a second time."""

    def setUp(self):
        self.conn = make_db()
        _add_savings_account(self.conn)
        self.goal = {"name": "Trip", "account_id": "sav",
                     "monthly_plan": 250.0, "tokens": ["trip"]}
        self.cfg = {"food_monthly": 1000, "other_monthly": 1000,
                    "dynamic_variable_budget": False,
                    "savings_goals": [self.goal]}
        write_config(self.conn, savings_goals=[self.goal])

    def tearDown(self):
        self.conn.close()

    def test_checking_leg_counts_as_posted(self):
        """Only the checking leg posted."""
        add_txn(self.conn, TODAY - dt.timedelta(days=5), 250.0,
                "Transfer to TRIP savings", account="chk",
                primary="TRANSFER_OUT")
        posted = savings.posted_for_plan(
            self.conn, self.cfg, self.goal, since=TODAY.replace(day=1))
        self.assertAlmostEqual(posted, 250.0, places=2)
        ev = forecast.build(self.conn, TODAY)["events"]
        self.assertFalse(
            [e for e in ev if "Trip (savings plan)"
             in e[2] and e[0][:7] == "2026-07"],
            f"plan re-scheduled despite posted checking leg: {ev}")

    def test_both_legs_count_once(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=5), 250.0,
                "Transfer to TRIP savings", account="chk",
                primary="TRANSFER_OUT")
        add_txn(self.conn, TODAY - dt.timedelta(days=5), -250.0,
                "TRIP transfer in", account="sav", primary="TRANSFER_IN")
        posted = savings.posted_for_plan(
            self.conn, self.cfg, self.goal, since=TODAY.replace(day=1))
        self.assertAlmostEqual(posted, 250.0, places=2)  # once, not 500

    def test_nothing_posted_still_owed(self):
        posted = savings.posted_for_plan(
            self.conn, self.cfg, self.goal, since=TODAY.replace(day=1))
        self.assertAlmostEqual(posted, 0.0, places=2)
        ev = forecast.build(self.conn, TODAY)["events"]
        self.assertTrue([e for e in ev if "Trip (savings plan)" in e[2]
                         and e[0][:7] == "2026-07"])

    def test_tokenless_goal_amount_matches_plan(self):
        goal = {"name": "Fund", "account_id": "sav", "monthly_plan": 250.0}
        cfg = {**self.cfg, "savings_goals": [goal]}
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 250.0,
                "Online transfer", account="chk", primary="TRANSFER_OUT")
        posted = savings.posted_for_plan(
            self.conn, cfg, goal, since=TODAY.replace(day=1))
        self.assertAlmostEqual(posted, 250.0, places=2)
        # a different-amount transfer is NOT evidence for a token-less goal
        goal2 = {"name": "Fund2", "account_id": "sav", "monthly_plan": 400.0}
        posted2 = savings.posted_for_plan(
            self.conn, {**self.cfg, "savings_goals": [goal2]}, goal2,
            since=TODAY.replace(day=1))
        self.assertAlmostEqual(posted2, 0.0, places=2)

    def test_checking_evidence_capped_at_plan(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=6), 400.0,
                "Transfer to TRIP savings", account="chk",
                primary="TRANSFER_OUT")
        posted = savings.posted_for_plan(
            self.conn, self.cfg, self.goal, since=TODAY.replace(day=1))
        self.assertAlmostEqual(posted, 250.0, places=2)


class SharedSavingsAccountPartitionTests(unittest.TestCase):
    """Two token-less goals on one account partition instead of each
    claiming the full net."""

    def setUp(self):
        self.conn = make_db()
        _add_savings_account(self.conn)
        self.g1 = {"name": "Emergency", "account_id": "sav",
                   "monthly_plan": 100.0}
        self.g2 = {"name": "House", "account_id": "sav",
                   "monthly_plan": 100.0}
        self.cfg = {"savings_goals": [self.g1, self.g2]}
        add_txn(self.conn, TODAY - dt.timedelta(days=4), -500.0,
                "Deposit transfer", account="sav", primary="TRANSFER_IN")

    def tearDown(self):
        self.conn.close()

    def test_first_claim_partition(self):
        """500 in, two claimants -> 500 total, not 1000."""
        n1 = savings.matched_net(self.conn, self.cfg, self.g1)
        n2 = savings.matched_net(self.conn, self.cfg, self.g2)
        self.assertAlmostEqual(n1, 500.0, places=2)
        self.assertAlmostEqual(n2, 0.0, places=2)

    def test_tokened_sibling_carve_out(self):
        """A tokened sibling's rows must not also land in the token-less
        goal's remainder."""
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -200.0,
                "VACATION transfer", account="sav", primary="TRANSFER_IN")
        g_tok = {"name": "Vacation", "account_id": "sav",
                 "monthly_plan": 50.0, "tokens": ["vacation"]}
        cfg = {"savings_goals": [self.g1, g_tok]}
        self.assertAlmostEqual(
            savings.matched_net(self.conn, cfg, g_tok), 200.0, places=2)
        self.assertAlmostEqual(
            savings.matched_net(self.conn, cfg, self.g1), 500.0, places=2)

    def test_progress_uses_partition(self):
        rows = savings.progress(self.conn, self.cfg, TODAY)
        by_name = {p["name"]: p for p in rows}
        self.assertAlmostEqual(by_name["Emergency"]["saved"], 500.0,
                               places=2)
        self.assertAlmostEqual(by_name["House"]["saved"], 0.0, places=2)


class RunwayReservesTheSavingsPlanTests(unittest.TestCase):
    """due_total carries the unposted savings-plan residual."""

    def setUp(self):
        self.conn = make_db()
        _add_savings_account(self.conn)
        self.goal = {"name": "Trip", "account_id": "sav",
                     "monthly_plan": 250.0, "tokens": ["trip"]}
        write_config(self.conn, savings_goals=[self.goal])

    def tearDown(self):
        self.conn.close()

    def _due_total(self):
        from oikonome.web import report
        st = report.gather(self.conn, TODAY, live=True)
        return st["runway"]["due_total"]

    def test_unposted_plan_reserved(self):
        """No bills, nothing posted — headroom must still reserve the
        plan."""
        self.assertAlmostEqual(self._due_total(), 250.0, places=2)

    def test_posted_plan_not_reserved(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=5), 250.0,
                "Transfer to TRIP savings", account="chk",
                primary="TRANSFER_OUT")
        self.assertAlmostEqual(self._due_total(), 0.0, places=2)


if __name__ == "__main__":
    unittest.main()
