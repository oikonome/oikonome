"""The savings-goal account pickers list the accounts a goal can measure.

What breaks if this fails: the picker lists every account the household
ever had — closed imports, cards, loans, a hidden account, the backup
copy of a linked one, a holdings-only plan whose ledger never shows a
transfer — and a goal pointed at one of them never moves.
"""

import unittest

import oikonome.web.app  # noqa: F401  (the routers import in app order)
from oikonome.web.api_accounts import goal_destination


def _acct(kind, **kw):
    row = {"kind": kind, "balance_current": 100.0, "user_removed_at": None,
           "link": None, "txns": 12}
    row.update(kw)
    return row


class GoalDestinationTests(unittest.TestCase):
    def test_open_cash_and_investment_accounts_are_listed(self):
        self.assertTrue(goal_destination(_acct("depository/checking")))
        self.assertTrue(goal_destination(_acct("depository/savings")))
        self.assertTrue(goal_destination(_acct("investment/brokerage")))

    def test_debts_are_not_destinations(self):
        self.assertFalse(goal_destination(_acct("credit/credit card")))
        self.assertFalse(goal_destination(_acct("loan/mortgage")))

    def test_closed_or_hidden_accounts_are_not_listed(self):
        self.assertFalse(goal_destination(
            _acct("depository/checking", balance_current=None)))
        self.assertFalse(goal_destination(
            _acct("depository/savings", user_removed_at="2026-05-01")))

    def test_the_backup_copy_of_a_linked_account_is_not_listed(self):
        self.assertFalse(goal_destination(_acct(
            "depository/savings", link={"primary": False})))
        self.assertTrue(goal_destination(_acct(
            "depository/savings", link={"primary": True})))

    def test_an_account_whose_ledger_never_moves_is_not_listed(self):
        self.assertFalse(goal_destination(_acct("investment/401a", txns=0)))


if __name__ == "__main__":
    unittest.main()
