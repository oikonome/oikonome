"""A mortgage or loan typed in on Net Worth is a debt the plan walks.

What breaks if these fail: the Debt page counts "3 debts" and quotes a
debt-free date while the household's largest debt, entered by hand on
Net Worth, is nowhere in the plan; or it is walked at a payment nobody
makes instead of the one the ledger shows (specs/debt-plan.md).
"""

import datetime as dt
import unittest

from oikonome.engine import debt, entities

from .util import TODAY, add_txn, make_db, write_config


def _assets(**over):
    row = {"name": "Northwind Mortgage", "kind": "mortgage",
           "value": -184_500.0, "as_of": "2026-01-01"}
    row.update(over)
    return [{"name": "Home", "kind": "property", "value": 310_000.0,
             "as_of": "2026-01-01"}, row]


class HandEnteredLoanTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.conn.execute("UPDATE accounts SET balance_current = 0"
                          " WHERE id = 'card'")

    def tearDown(self):
        self.conn.close()

    def _pay(self, amount=1_342.17, months=(3, 33, 63)):
        for back in months:
            add_txn(self.conn, TODAY - dt.timedelta(days=back), amount,
                    "NORTHWIND HOME LOANS PAYMENT", primary="LOAN_PAYMENTS",
                    account="chk")

    def test_the_loan_is_walked_at_the_ledgers_payment(self):
        write_config(self.conn, manual_assets=_assets())
        self._pay()
        r = debt.build(self.conn, TODAY)
        [d] = r["debts"]
        self.assertEqual((d["id"], d["kind"], d["origin"], d["balance"],
                          d["min_payment"], d["min_basis"]),
                         ("manual:northwind mortgage", "mortgage",
                          "hand_entered", 184_500.0, 1342.17, "ledger"))
        # no rate typed yet: the page asks for it
        self.assertTrue(d["rate_needed"])
        self.assertEqual(r["plan"]["starting"], 184_500.0)
        self.assertTrue(r["alt_same_path"])

    def test_the_rate_is_asked_once_and_saved_in_the_plan(self):
        write_config(self.conn, manual_assets=_assets(), debt_plan={
            "method": "avalanche", "extra_monthly": 0,
            "debts": {"manual:northwind mortgage": {"apr": 4.25}}})
        self._pay()
        [d] = debt.build(self.conn, TODAY)["debts"]
        self.assertEqual((d["apr"], d["apr_source"], d["rate_needed"]),
                         (4.25, "you", False))

    def test_no_matching_payment_falls_back_to_an_estimate(self):
        write_config(self.conn, manual_assets=_assets())
        self._pay(months=(3,))            # one month is not a pattern
        [d] = debt.build(self.conn, TODAY)["debts"]
        self.assertEqual(d["min_basis"], "estimate")

    def test_a_negative_other_item_is_a_loan(self):
        write_config(self.conn, manual_assets=_assets(
            name="Family loan", kind="other", value=-6_000.0))
        [d] = debt.build(self.conn, TODAY)["debts"]
        self.assertEqual((d["kind"], d["balance"]), ("loan", 6_000.0))

    def test_a_linked_mortgage_replaces_the_typed_one(self):
        write_config(self.conn, manual_assets=_assets())
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('mort','it1','Home Loan','loan',"
            "'mortgage',183000)")
        ids = [(d["id"], d["origin"])
               for d in debt.build(self.conn, TODAY)["debts"]]
        self.assertEqual(ids, [("mort", "account")])

    def test_only_a_mortgage_net_worth_counts_replaces_the_typed_one(self):
        # a business's linked mortgage and a closed one with no balance
        # stay off Net Worth, so the household's typed mortgage stays in
        # both Net Worth and the plan
        write_config(self.conn, manual_assets=_assets())
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('bizmort','it1','Shop Loan','loan',"
            "'mortgage',96000), ('oldmort','it1','Old Loan','loan',"
            "'mortgage',NULL)")
        ent = entities.create_entity(self.conn, name="Example Shop LLC",
                                     structure="sole_prop")
        entities.assign_account(self.conn, "bizmort", ent["id"])
        ids = [d["id"] for d in debt.build(self.conn, TODAY)["debts"]]
        self.assertIn("manual:northwind mortgage", ids)


if __name__ == "__main__":
    unittest.main()
