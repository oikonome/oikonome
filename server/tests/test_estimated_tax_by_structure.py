"""The estimated-tax card must follow the entity's structure and the calendar.

* An S-corporation's owner pays no self-employment tax on the company's
  profit: their Social Security and Medicare go through payroll on wages,
  which the books already carry as an expense. Charging 15.3% on the
  profit tells them to set aside money they do not owe.
* A multi-member LLC's profit is split among its members, and each member's
  self-employment tax is capped at their own Social Security wage base.
  Applying one cap to the whole partnership's profit understates the tax
  when members' shares together pass the cap. The figure covers every
  member, and says so.
* Mid-year, the profit so far is not the year's profit. An installment
  taken as a quarter of the tax on profit to date under-reserves by the
  share of the year still to come, so the per-quarter figure is taken from
  the year-to-date profit projected over the whole year, while the
  "so far" total stays what is owed on what has been earned.
"""

import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import entities, selfemploy

from .util import _admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts


def _se(base, year):
    if base < 400:
        return 0.0
    return round(min(base, selfemploy.ss_wage_base(year)) * 0.124
                 + base * 0.029, 2)


class EstimatedTaxByStructureTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(self.admin.close)
        tid = str(tenancy.create_tenant(
            self.admin, f"est-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(tid)
        self.addCleanup(self.conn.close)
        seed_accounts(self.conn)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('biz','it1','Biz Checking',"
            "'depository','checking',0)")

    def _entity(self, structure, profit, on=dt.date(2025, 3, 1), rate=None):
        ent = entities.create_entity(self.conn, name="Example Co",
                                     structure=structure)
        entities.assign_account(self.conn, "biz", ent["id"])
        add_txn(self.conn, on, -profit, "CLIENT", account="biz",
                primary="INCOME", txn_id=f"rev-{uuid.uuid4().hex[:6]}")
        if rate is not None:
            entities.update_entity(self.conn, ent["id"], income_tax_rate=rate)
        return ent["id"]

    def test_an_s_corp_owes_no_self_employment_tax_on_its_profit(self):
        eid = self._entity("s_corp", 100_000.0, rate=22)
        est = selfemploy.estimated_tax(self.conn, eid, 2025)
        self.assertEqual(est["se_tax"], 0.0)
        self.assertFalse(est["se_tax_applies"])
        self.assertEqual(est["income_tax"], 22_000.0,
                         "with no SE tax there is no half-SE deduction")
        self.assertEqual(est["total_estimated"], 22_000.0)

    def test_a_sole_proprietor_still_owes_it(self):
        eid = self._entity("sole_prop", 100_000.0)
        est = selfemploy.estimated_tax(self.conn, eid, 2025)
        self.assertEqual(est["se_tax"], _se(92_350.0, 2025))
        self.assertTrue(est["se_tax_applies"])
        self.assertEqual(est["covers"], "owner")

    def test_each_partner_gets_their_own_wage_base(self):
        eid = self._entity("multi_member_llc", 400_000.0)
        entities.add_member(self.conn, eid, member_name="Member A",
                            ownership_pct=50)
        entities.add_member(self.conn, eid, member_name="Member B",
                            ownership_pct=50)
        est = selfemploy.estimated_tax(self.conn, eid, 2025)
        each = _se(round(200_000 * selfemploy.SE_BASE_FACTOR, 2), 2025)
        self.assertAlmostEqual(est["se_tax"], 2 * each, delta=0.02)
        self.assertGreater(est["se_tax"],
                           _se(round(400_000 * selfemploy.SE_BASE_FACTOR, 2),
                               2025),
                           "one cap across two partners understates the tax")
        self.assertEqual(est["covers"], "all_members")
        self.assertEqual([m["ownership_pct"] for m in est["members"]],
                         [50.0, 50.0])
        self.assertEqual(sorted(m["share_of_profit"] for m in est["members"]),
                         [200_000.0, 200_000.0])

    def test_two_decimal_thirds_are_complete_shares(self):
        """33.33% three times is 100% to the cent; floating-point summing
        must not reject it and tax three partners on one wage base."""
        eid = self._entity("multi_member_llc", 300_000.0)
        for name in ("Member A", "Member B", "Member C"):
            entities.add_member(self.conn, eid, member_name=name,
                                ownership_pct=33.33)
        est = selfemploy.estimated_tax(self.conn, eid, 2025)
        self.assertEqual(len(est["members"]), 3)
        self.assertGreater(est["se_tax"],
                           _se(round(300_000 * selfemploy.SE_BASE_FACTOR, 2),
                               2025),
                           "three partners were taxed on one wage base")

    def test_a_partnership_without_shares_says_it_covers_everyone(self):
        eid = self._entity("multi_member_llc", 100_000.0)
        est = selfemploy.estimated_tax(self.conn, eid, 2025)
        self.assertEqual(est["covers"], "all_members")
        self.assertEqual(est["members"], [])
        self.assertEqual(est["se_tax"], _se(92_350.0, 2025))

    def test_mid_year_quarterly_is_projected_from_the_year_so_far(self):
        today = dt.date(2026, 10, 1)
        eid = self._entity("sole_prop", 90_000.0, on=dt.date(2026, 3, 1))
        est = selfemploy.estimated_tax(self.conn, eid, 2026, today=today)
        # the so-far figure is unchanged: tax on profit earned to date
        self.assertEqual(est["se_tax"],
                         _se(round(90_000 * selfemploy.SE_BASE_FACTOR, 2),
                             2026))
        self.assertTrue(est["annualized"])
        elapsed = (today - dt.date(2026, 1, 1)).days + 1
        projected = round(90_000 * 365 / elapsed, 2)
        self.assertAlmostEqual(est["annualized_taxable_profit"], projected,
                               delta=0.02)
        self.assertEqual(est["annualized_total"],
                         _se(round(projected * selfemploy.SE_BASE_FACTOR, 2),
                             2026))
        self.assertEqual(est["quarterly"],
                         round(est["annualized_total"] / 4, 2))
        self.assertGreater(est["quarterly"], est["total_estimated"] / 4)

    def test_a_finished_year_is_not_projected(self):
        eid = self._entity("sole_prop", 90_000.0)
        est = selfemploy.estimated_tax(self.conn, eid, 2025,
                                       today=dt.date(2026, 10, 1))
        self.assertFalse(est["annualized"])
        self.assertEqual(est["annualized_total"], est["total_estimated"])
        self.assertEqual(est["quarterly"],
                         round(est["total_estimated"] / 4, 2))


if __name__ == "__main__":
    unittest.main()
