"""The Net Worth investment panels count each real account once, and only
the household's.

A brokerage reached through two feeds and linked is one account; net
worth counts it once, and the P/L panels must too. A business account
stays out of the personal panels unless the combined view is on. Two
accounts that share an institution and a name are still two accounts,
each with its own basis.
"""
import unittest

from oikonome.engine import entities, links, reporting

from .test_reporting import add_account, add_holding, add_item
from .util import make_db, write_config


class InvestmentPanelTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_item(self.conn, "pa", "Fidelity")
        add_item(self.conn, "pb", "Fidelity Investments")

    def tearDown(self):
        self.conn.close()

    def _total(self):
        return reporting._retirement_combined(self.conn)["total"]

    def test_a_linked_brokerage_counts_once(self):
        for acc, item in (("b1", "pa"), ("b2", "pb")):
            add_account(self.conn, acc, "Brokerage", "investment",
                        "brokerage", 100000, item=item, mask="4321")
            add_holding(self.conn, acc, "FXAIX", 500, 200, 100000,
                        raw={"cost_basis": 80000})
        links.create(self.conn, ["b1", "b2"])
        links.set_shadow_scope(self.conn)
        self.assertEqual(self._total(), {"invested": 80000.0,
                                         "value": 100000.0,
                                         "pl": 20000.0, "pct": 25.0})

    def test_a_business_brokerage_stays_out(self):
        add_account(self.conn, "home", "Brokerage", "investment",
                    "brokerage", 1000, item="pa", mask="1111")
        add_holding(self.conn, "home", "VTI", 5, 200, 1000,
                    raw={"cost_basis": 800})
        add_account(self.conn, "biz", "Company Brokerage", "investment",
                    "brokerage", 5000, item="pa", mask="2222")
        add_holding(self.conn, "biz", "VTI", 25, 200, 5000,
                    raw={"cost_basis": 4000})
        ent = entities.create_entity(self.conn, name="Example Works LLC",
                                     structure="single_member_llc")
        entities.assign_account(self.conn, "biz", ent["id"])
        self.assertEqual(self._total()["value"], 1000.0)

    def test_same_named_accounts_keep_their_own_basis(self):
        write_config(self.conn, investment_basis_override={"1111": 40000})
        add_account(self.conn, "ra", "Roth IRA", "investment", "roth",
                    50000, item="pa", mask="1111")
        add_holding(self.conn, "ra", "VEA", 1000, 50, 50000,
                    raw={"cost_basis": 10})
        add_account(self.conn, "rb", "Roth IRA", "investment", "roth",
                    30000, item="pa", mask="2222")
        add_holding(self.conn, "rb", "VTI", 150, 200, 30000,
                    raw={"cost_basis": 25000})
        rows = [r for r in reporting._brokerage_pl(self.conn)
                if r["name"] != "Total"]
        self.assertEqual(sorted((r["basis"], r["value"]) for r in rows),
                         [(25000.0, 30000.0), (40000.0, 50000.0)])
        self.assertEqual(self._total()["invested"], 65000.0)


if __name__ == "__main__":
    unittest.main()
