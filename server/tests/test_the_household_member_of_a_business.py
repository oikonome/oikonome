"""One member of a business is the household; its share is the figure.

A multi-member LLC's estimate used to cover every member and say so,
because nothing recorded which member this household is. Marking a
member as the household makes the estimate's headline that member's
share of the tax (their SE tax on their share of the profit, the rate on
that share) with the business total beside it, and the capital account
reports the household's share by ownership percentage. At most one
member is the household; marking another clears the first."""

import unittest

from oikonome.engine import entities, equity, selfemploy

from .util import TODAY, add_txn, make_db, write_config


class HouseholdMemberTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        e = entities.create_entity(self.conn, name="Two Owners LLC",
                                   structure="multi_member_llc",
                                   business_start_date="2025-01-01")
        self.eid = e["id"]
        entities.assign_account(self.conn, "chk", self.eid)
        # one year of profit on the books
        add_txn(self.conn, TODAY.replace(year=2025, month=3, day=1), -100_000.0,
                "CLIENT PAYMENT", primary="INCOME", account="chk")
        self.a = entities.add_member(self.conn, self.eid, member_name="Member A",
                                     ownership_pct=60, is_household=True)
        self.b = entities.add_member(self.conn, self.eid, member_name="Member B",
                                     ownership_pct=40)

    def tearDown(self):
        self.conn.close()

    def test_the_estimate_is_the_households_share_with_the_business_beside(self):
        est = selfemploy.estimated_tax(self.conn, self.eid, 2025)
        self.assertEqual(est["covers"], "household")
        self.assertEqual(est["household"]["member_name"], "Member A")
        self.assertEqual(est["household"]["ownership_pct"], 60.0)
        a = next(m for m in est["members"] if m["member_name"] == "Member A")
        self.assertEqual(est["se_tax"], a["se_tax"])
        self.assertEqual(est["total_estimated"], est["household"]["total"])
        self.assertGreater(est["business_total"], est["total_estimated"])
        self.assertAlmostEqual(est["business_total"],
                               sum(m["se_tax"] for m in est["members"]), delta=0.02)

    def test_without_a_household_member_the_estimate_covers_everyone(self):
        entities.update_member(self.conn, self.eid, self.a["id"],
                               is_household=False)
        est = selfemploy.estimated_tax(self.conn, self.eid, 2025)
        self.assertEqual(est["covers"], "all_members")
        self.assertIsNone(est["household"])
        self.assertIsNone(est["business_total"])

    def test_only_one_member_is_the_household(self):
        entities.update_member(self.conn, self.eid, self.b["id"],
                               is_household=True)
        flags = {m["member_name"]: m["is_household"]
                 for m in entities.list_members(self.conn, self.eid)}
        self.assertEqual(flags, {"Member A": False, "Member B": True})
        entities.add_member(self.conn, self.eid, member_name="Member C",
                            ownership_pct=0, is_household=True)
        flags = {m["member_name"]: m["is_household"]
                 for m in entities.list_members(self.conn, self.eid)}
        self.assertEqual([k for k, v in flags.items() if v], ["Member C"])

    def test_the_capital_account_reports_the_households_share(self):
        equity.record_movement(self.conn, self.eid, kind="contribution",
                               amount=10_000.0, date="2025-01-02")
        cap = equity.capital_summary(self.conn, self.eid)
        self.assertEqual(cap["household_share_pct"], 60.0)
        self.assertAlmostEqual(cap["household_capital"],
                               round(cap["capital_balance"] * 0.6, 2))
        bs = selfemploy.balance_sheet(self.conn, self.eid)
        self.assertEqual(bs["household_share_pct"], 60.0)
        self.assertEqual(bs["household_capital"], cap["household_capital"])

    def test_a_household_member_without_a_share_gives_no_share(self):
        entities.update_member(self.conn, self.eid, self.a["id"],
                               ownership_pct=None)
        cap = equity.capital_summary(self.conn, self.eid)
        self.assertIsNone(cap["household_share_pct"])
        self.assertIsNone(cap["household_capital"])
        # shares no longer complete: the estimate covers everyone again
        est = selfemploy.estimated_tax(self.conn, self.eid, 2025)
        self.assertEqual(est["covers"], "all_members")

    def test_the_flag_is_a_boolean(self):
        with self.assertRaises(ValueError):
            entities.update_member(self.conn, self.eid, self.a["id"],
                                   is_household="yes")


if __name__ == "__main__":
    unittest.main()
