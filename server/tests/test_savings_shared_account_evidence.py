"""One real transfer is evidence for ONE goal — never for each sibling
sharing the destination account.

Several goals may share one savings account (that is the documented
shape: carve them apart with tokens). The destination-side partition in
`matched_net` already gives the account's un-tokened net to the FIRST
token-less goal so the same dollars are not counted N times. The
checking-side fallback is the other half of that rule: a token-less goal
has only "an outflow equal to my plan, to the cent" to infer from, and
that signal cannot tell siblings apart — so one $500 checking→savings
move must not read as $1,000 swept, and a goal that received nothing
must not look funded.

The failure modes these protect against:

- the Today page and the daily email announce "swept $1,000 to savings
  this month" when $500 moved;
- a monthly-mode goal that got $0 counts as posted, so its real future
  outflow silently vanishes from headroom and the cash forecast reads
  more optimistic than reality;
- one over-funded goal's excess masks an unfunded sibling, because the
  sweep total sums evidence with no per-goal cap;
- a goal token containing `%` or `_` is passed to LIKE unescaped and
  over-matches unrelated transfers, inflating the same evidence.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, savings
from oikonome.web import report
from oikonome.web.todayview import build_context

from .util import TODAY, add_txn, make_db, write_config

PLAN = 500.0


def _goal(name, **over):
    g = {"name": name, "target": 0, "monthly_plan": PLAN,
         "account_id": "sav", "tokens": [], "start_balance": 0,
         "mode": "sweep"}
    g.update(over)
    return g


def _add_savings_account(conn):
    conn.execute(
        "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
        " VALUES ('sav','it1','Test Savings','depository','savings',0)"
        " ON CONFLICT (tenant_id, id) DO NOTHING")


def _one_real_transfer(conn, amount=PLAN, when=None):
    """Both legs of a single move: money leaves checking (positive =
    money out) and lands in savings (negative on the destination side)."""
    when = when or TODAY - dt.timedelta(days=3)
    add_txn(conn, when, amount, "TRANSFER TO SAVINGS",
            primary="TRANSFER_OUT", account="chk")
    add_txn(conn, when, -amount, "TRANSFER TO SAVINGS",
            primary="TRANSFER_IN", account="sav")


class SiblingGoalsCountOneTransferOnceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        _add_savings_account(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_one_transfer_is_not_evidence_for_every_tokenless_sibling(self):
        """Two token-less sweep goals on one account, one $500 move: the
        posted total is $500 and only the first-claim goal reads as
        swept. Counting the checking leg again for the sibling reported
        $1,000 swept from a single $500 transfer."""
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=[_goal("Emergency"), _goal("Vacation")])
        _one_real_transfer(self.conn)
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["sweep_plan"], 2 * PLAN)
        self.assertEqual(st["sweep_posted"], PLAN)

        cfg = budget.load_config(self.conn)
        first, second = savings.progress(self.conn, cfg, TODAY)
        self.assertEqual(first["swept_this_month"], PLAN)
        self.assertTrue(first["sweep_satisfied"])
        self.assertEqual(second["swept_this_month"], 0.0)
        self.assertFalse(second["sweep_satisfied"])

    def test_today_line_never_reports_more_swept_than_moved(self):
        """The Today page and its email mirror quote the posted total, so
        a double-counted sibling shows up as a sentence claiming money
        that never left."""
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=[_goal("Emergency"), _goal("Vacation")])
        _one_real_transfer(self.conn)
        d = report.gather(self.conn, TODAY)
        line = build_context(d)["day"]["sweep_line"]
        self.assertNotIn("swept $1,000", line)
        self.assertIn("sweep up to $1,000", line)
        _, plain, html = report.build(d)
        self.assertIn(line, plain)
        self.assertIn(line, html)

    def test_unfunded_monthly_sibling_still_reserved_in_headroom(self):
        """Monthly-mode plans are commitments: headroom subtracts each
        goal's UNPOSTED plan. A sibling credited with a transfer it never
        received looks funded, and its real outflow disappears from the
        cash picture."""
        _one_real_transfer(self.conn)
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=[_goal("Emergency", mode="monthly")])
        alone = report.gather(self.conn, TODAY)["runway"]["due_total"]
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=[_goal("Emergency", mode="monthly"),
                                    _goal("Vacation", mode="monthly")])
        both = report.gather(self.conn, TODAY)["runway"]["due_total"]
        self.assertEqual(round(both - alone, 2), PLAN)

    def test_over_funding_one_goal_does_not_cover_an_unfunded_sibling(self):
        """Sweep evidence is capped at each goal's own plan before it is
        summed. Without the cap, $1,000 into goal A read as the whole
        $1,000 sweep plan being satisfied while goal B had $0."""
        goals = [_goal("Alpha", tokens=["alpha"]),
                 _goal("Beta", tokens=["beta"])]
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=goals)
        for i in range(2):
            add_txn(self.conn, TODAY - dt.timedelta(days=3 + i), -PLAN,
                    "ALPHA TRANSFER", primary="TRANSFER_IN", account="sav")
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["sweep_plan"], 2 * PLAN)
        self.assertEqual(st["sweep_posted"], PLAN)
        d = report.gather(self.conn, TODAY)
        self.assertIn("sweep up to $1,000",
                      build_context(d)["day"]["sweep_line"])


class GoalTokenWildcardTests(unittest.TestCase):
    """A goal token is a literal, not a pattern. `%` and `_` reaching LIKE
    unescaped let a token like `100%` match any transfer whose text merely
    starts with `100`, inflating both the destination-side net and the
    checking-side inference the sweep math runs on."""

    def setUp(self):
        self.conn = make_db()
        _add_savings_account(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_percent_in_a_token_matches_only_a_literal_percent(self):
        g = _goal("Bonus", tokens=["100%"])
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=[g])
        cfg = budget.load_config(self.conn)
        add_txn(self.conn, TODAY - dt.timedelta(days=2), -200.0,
                "BONUS 100% SAVE", primary="TRANSFER_IN", account="sav")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), -500.0,
                "PAYROLL 1005 SAVE", primary="TRANSFER_IN", account="sav")
        self.assertEqual(savings.matched_net(self.conn, cfg, g), 200.0)

    def test_underscore_in_a_token_matches_only_a_literal_underscore(self):
        g = _goal("Fund", tokens=["a_b"])
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=[g])
        cfg = budget.load_config(self.conn)
        add_txn(self.conn, TODAY - dt.timedelta(days=2), -300.0,
                "XFER a_b FUND", primary="TRANSFER_IN", account="sav")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), -700.0,
                "XFER axb FUND", primary="TRANSFER_IN", account="sav")
        self.assertEqual(savings.matched_net(self.conn, cfg, g), 300.0)

    def test_checking_side_inference_honours_the_literal_token(self):
        """The fallback leg matches on the same tokens; an unescaped `%`
        there credits a goal with an unrelated outflow."""
        g = _goal("Bonus", tokens=["100%"])
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=[g])
        cfg = budget.load_config(self.conn)
        add_txn(self.conn, TODAY - dt.timedelta(days=2), PLAN,
                "XFER 1005 OUT", primary="TRANSFER_OUT", account="chk")
        self.assertEqual(
            savings.posted_for_plan(self.conn, cfg, g,
                                    since=TODAY.replace(day=1), until=TODAY),
            0.0)


if __name__ == "__main__":
    unittest.main()
