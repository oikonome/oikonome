"""A savings goal's saved figure is start_balance plus the ledger net of the
transfers it matches, and nothing inferred is added to it.

The checking leg of a transfer is an outflow of the right size: it cannot
say where the money went. It is evidence that a month's plan is no longer
owed, so the forecast and the sweep read it, but adding it into the total
counted unrelated outflows (rent shares, brokerage buys) and even a
withdrawal from the fund as money saved. Today's this-month row reads the
same plan evidence as the sweep line beside it, so the two always agree.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, savings
from oikonome.web import report

from .util import TODAY, add_txn, make_db, write_config


def _months_ago(n):
    d = TODAY.replace(day=10)
    for _ in range(n):
        d = (d.replace(day=1) - dt.timedelta(days=1)).replace(day=10)
    return d


class LedgerNetTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _saved(self, goal):
        write_config(self.conn, savings_goals=[goal])
        cfg = budget.load_config(self.conn)
        return next(p for p in savings.progress(self.conn, cfg, TODAY)
                    if p["name"] == goal["name"])["saved"]

    def test_a_withdrawal_only_month_lowers_the_goals_total(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sav','it1','Test Savings',"
            "'depository','savings',0)")
        goal = {"name": "Trip", "target": 3000.0, "monthly_plan": 250.0,
                "account_id": "sav", "tokens": ["trip"], "start_balance": 0}
        for n, amt in ((4, 400.0), (3, 300.0), (2, 300.0)):
            add_txn(self.conn, _months_ago(n), -amt, "Deposit - trip",
                    account="sav", primary="TRANSFER_IN")
        # the only row of last month: $800 taken out to pay for the trip
        add_txn(self.conn, _months_ago(1), 800.0, "Withdrawal - trip",
                account="sav", primary="TRANSFER_OUT")
        self.assertEqual(self._saved(goal), 200.0)

    def test_a_goal_created_today_reads_its_start_balance_only(self):
        """A new goal on an account with no rows yet, beside years of
        plan-sized outflows from checking, starts at what it was given."""
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('new-hysa','it1','New Savings',"
            "'depository','savings',0)")
        for n in range(1, 41):
            add_txn(self.conn, _months_ago(n), 500.0, "ONLINE TRANSFER",
                    account="chk", primary="TRANSFER_OUT")
        goal = {"name": "Holiday", "target": 6000.0, "monthly_plan": 500.0,
                "account_id": "new-hysa", "tokens": [],
                "start_balance": 750.0}
        self.assertEqual(self._saved(goal), 750.0)


class TodayRowMatchesSweepLineTests(unittest.TestCase):
    """A transfer that left checking yesterday and has not landed on the
    savings account yet: the sweep line counts the month as swept, and the
    money map's this-month row must read the same amount, not $0."""

    def setUp(self):
        self.conn = c = make_db()
        c.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                  "balance_current) VALUES ('sav','it1','Test Savings',"
                  "'depository','savings',0)")
        # the destination reports: it has an older row of its own
        add_txn(c, _months_ago(2), -500.0, "TRANSFER FROM CHECKING",
                account="sav", primary="TRANSFER_IN")
        add_txn(c, TODAY - dt.timedelta(days=1), 500.0, "ONLINE TRANSFER",
                account="chk", primary="TRANSFER_OUT")
        write_config(c, savings_goals=[
            {"name": "Savings", "monthly_plan": 500.0, "account_id": "sav",
             "tokens": [], "start_balance": 0, "mode": "sweep"}])

    def tearDown(self):
        self.conn.close()

    def test_this_months_row_and_the_sweep_line_agree(self):
        d = report.gather(self.conn, TODAY)
        g = next(g for g in d["savings_goals"] if g["name"] == "Savings")
        self.assertEqual(g["swept_this_month"], 500.0)
        self.assertEqual(g["saved_this_month"], g["swept_this_month"])


if __name__ == "__main__":
    unittest.main()
