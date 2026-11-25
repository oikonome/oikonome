"""The accounts reply says which accounts can be excluded from the budget,
and it says so from the list the spend predicate itself is built from: any
account whose rows can count as spending is excludable, a loan is not
(specs/counted-figures.md). Decided on the client by type, the switch was
hidden on an investment account whose card charges the budget counts, and
un-ticking an excluded one made the switch disappear for good.
"""
import datetime as dt
import unittest

from oikonome.engine import budget, forecast, reporting

from .util import add_txn, make_db, write_config


class ExcludableWhereRowsCount(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('hsa','it1','Invented HSA','investment','hsa',900),"
            "('oth','it1','Invented Other','other',NULL,50),"
            "('mtg','it1','Home Loan','loan','mortgage',200000)")

    def tearDown(self):
        self.conn.close()

    def _spend_accounts(self):
        """The accounts a charge on which the spend predicate counts."""
        for acct in ("chk", "card", "hsa", "oth", "mtg"):
            add_txn(self.conn, dt.date(2026, 10, 5), 40.0, f"Charge {acct}",
                    account=acct, primary="MEDICAL")
        got = set()
        for where in (f"t.removed = 0 {budget.SPEND_ONLY_SQL}",
                      reporting.SPEND_WHERE):
            rows = self.conn.execute(
                f"SELECT DISTINCT t.account_id AS a FROM transactions t "
                f"WHERE {where}").fetchall()
            got.add(frozenset(r["a"] for r in rows))
        self.assertEqual(len(got), 1, "the two spend predicates disagree")
        return set(next(iter(got)))

    def test_every_type_whose_rows_count_is_excludable_and_a_loan_is_not(self):
        counted = self._spend_accounts()
        self.assertEqual(counted, {"chk", "card", "hsa", "oth"})
        types = {r["id"]: r["type"] for r in self.conn.execute(
            "SELECT id, type FROM accounts")}
        for acct, typ in types.items():
            self.assertEqual(budget.account_counts_as_spending(typ),
                             acct in counted, acct)

    def test_the_predicate_and_the_flag_read_one_list(self):
        for typ in budget.NON_SPEND_ACCOUNT_TYPES:
            self.assertIn(f"'{typ}'", budget.SPEND_ONLY_SQL)
            self.assertIn(f"'{typ}'", reporting.SPEND_WHERE)
            self.assertFalse(budget.account_counts_as_spending(typ))
        self.assertIn(budget.NOT_A_NON_SPEND_ACCOUNT_SQL,
                      budget.SPEND_ONLY_SQL)
        self.assertIn(budget.NOT_A_NON_SPEND_ACCOUNT_SQL,
                      reporting.SPEND_WHERE)

    def test_the_pin_is_offered_on_the_type_the_forecast_picks_among(self):
        self.assertTrue(forecast.pin_offered("depository"))
        for typ in ("credit", "investment", "loan", "other", None):
            self.assertFalse(forecast.pin_offered(typ), typ)

    def test_the_accounts_reply_carries_both_flags(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "oikonome" / "web" / "api_accounts.py").read_text()
        self.assertIn('r["can_exclude"] = '
                      '_budget_rules.account_counts_as_spending(typ)', src)
        self.assertIn('r["can_pin"] = _forecast_rules.pin_offered(typ)', src)


if __name__ == "__main__":
    unittest.main()
