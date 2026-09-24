"""Self-employed tax pack — mileage, home office, estimated tax,
balance sheet, 1099 vendors."""
import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import entities, selfemploy

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts,
                   TODAY)


class PureCalcTests(unittest.TestCase):
    def test_home_office_simplified(self):
        self.assertEqual(selfemploy.home_office_deduction(200), 1000.0)
        self.assertEqual(selfemploy.home_office_deduction(400), 1500.0)  # 300 cap
        self.assertEqual(selfemploy.home_office_deduction(0), 0.0)
        self.assertEqual(selfemploy.home_office_deduction(None), 0.0)

    def test_mileage_rate_follows_its_effective_date(self):
        """The IRS can change the rate mid-year (2026: 72.5¢ → 76¢ on Jul 1),
        so a trip is priced by its date, not its tax year; a whole year
        resolves to the rate in force at its end, and a date past the
        table takes the latest known rate."""
        self.assertEqual(selfemploy.mileage_rate(2025), 0.70)
        self.assertEqual(selfemploy.mileage_rate(dt.date(2026, 6, 30)), 0.725)
        self.assertEqual(selfemploy.mileage_rate(dt.date(2026, 7, 1)), 0.76)
        self.assertEqual(selfemploy.mileage_rate(2026), 0.76)
        self.assertEqual(selfemploy.mileage_rate(2099), 0.76)      # latest known
        self.assertEqual(selfemploy.mileage_rate(dt.date(2099, 1, 1)), 0.76)

    def test_social_security_wage_base_covers_the_current_year(self):
        self.assertEqual(selfemploy.ss_wage_base(2025), 176_100)
        self.assertEqual(selfemploy.ss_wage_base(2026), 184_500)
        self.assertEqual(selfemploy.ss_wage_base(2099), 184_500)  # latest known

    def test_quarterly_deadlines(self):
        d = selfemploy.quarterly_deadlines(2026)
        self.assertEqual(d, [dt.date(2026, 4, 15), dt.date(2026, 6, 15),
                             dt.date(2026, 9, 15), dt.date(2027, 1, 15)])


class SelfEmployTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"se-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Acme LLC", structure="sole_prop")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('biz','it1','Biz Checking','depository','checking',4000)")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('bizcard','it1','Biz Card','credit','credit card',600)")
        entities.assign_account(self.conn, "biz", self.ent["id"])
        entities.assign_account(self.conn, "bizcard", self.ent["id"])

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def test_mileage_deduction(self):
        selfemploy.add_trip(self.conn, self.ent["id"], date=TODAY, miles=100,
                            purpose="client visit")
        selfemploy.add_trip(self.conn, self.ent["id"], date=TODAY, miles=50)
        d = selfemploy.mileage_deduction(self.conn, self.ent["id"],
                                         TODAY.year)
        self.assertEqual(d["miles"], 150.0)
        self.assertEqual(d["deduction"],
                         round(150 * selfemploy.mileage_rate(TODAY.year), 2))

    def test_mileage_deduction_prices_each_trip_at_its_own_rate(self):
        """One tax year, two IRS rates: a Jun 30 trip at 72.5¢ and a Jul 1
        trip at 76¢ must each carry their own rate. One year rate over the
        year's total misprices every trip on the other side of the change."""
        selfemploy.add_trip(self.conn, self.ent["id"],
                            date=dt.date(2026, 6, 30), miles=100)
        selfemploy.add_trip(self.conn, self.ent["id"],
                            date=dt.date(2026, 7, 1), miles=100)
        d = selfemploy.mileage_deduction(self.conn, self.ent["id"], 2026)
        self.assertEqual(d["miles"], 200.0)
        self.assertEqual(d["deduction"], 72.50 + 76.00)
        self.assertEqual(d["rates"], [{"rate": 0.725, "miles": 100.0},
                                      {"rate": 0.76, "miles": 100.0}])
        self.assertEqual(d["rate"], round(148.5 / 200, 4))   # effective

    def test_social_security_tax_uses_the_years_own_wage_base(self):
        """Self-employment earnings between last year's cap and this year's
        are still Social-Security-taxed at 12.4%; a stale cap stops the tax
        $8,400 early."""
        profit = round(180_000 / selfemploy.SE_BASE_FACTOR, 2)
        add_txn(self.conn, dt.date(2026, 3, 1), -profit, "CLIENT",
                account="biz", primary="INCOME", txn_id="rev26")
        est = selfemploy.estimated_tax(self.conn, self.ent["id"], 2026)
        se_base = round(profit * selfemploy.SE_BASE_FACTOR, 2)
        self.assertLess(se_base, 184_500)
        self.assertGreater(se_base, 176_100)
        expected = round(se_base * 0.124 + se_base * 0.029, 2)  # no cap yet
        self.assertEqual(est["se_tax"], expected)

    def test_balance_sheet(self):
        bs = selfemploy.balance_sheet(self.conn, self.ent["id"])
        self.assertEqual(bs["total_assets"], 4000.0)       # biz checking
        self.assertEqual(bs["total_liabilities"], 600.0)   # biz card
        self.assertEqual(bs["net"], 3400.0)

    def test_estimated_tax_se_only_without_rate(self):
        # $10,000 net profit, no income-tax rate set → SE tax only
        add_txn(self.conn, TODAY, -10000.0, "CLIENT", account="biz",
                primary="INCOME", txn_id="rev")
        est = selfemploy.estimated_tax(self.conn, self.ent["id"], TODAY.year)
        self.assertEqual(est["net_profit"], 10000.0)
        # under the SS wage base, SE tax ≈ 15.3% of the .9235 base (SS 12.4% +
        # Medicare 2.9%); allow a cent of split-vs-flat rounding
        self.assertAlmostEqual(est["se_tax"], 10000 * 0.9235 * 0.153, delta=0.02)
        self.assertIsNone(est["income_tax"])
        self.assertEqual(est["quarterly"], round(est["total_estimated"] / 4, 2))

    def test_estimated_tax_honors_the_400_dollar_floor(self):
        """IRC §1402(b)(2): no SE tax when net earnings from
        self-employment are under $400. A $400 profit has se_base $369.40,
        below the floor, so the estimate carries no SE tax."""
        add_txn(self.conn, TODAY, -400.0, "TINY CLIENT", account="biz",
                primary="INCOME", txn_id="tiny")
        est = selfemploy.estimated_tax(self.conn, self.ent["id"], TODAY.year)
        self.assertEqual(est["net_profit"], 400.0)
        self.assertEqual(est["se_tax"], 0.0,
                         "SE tax charged below the §1402(b)(2) floor")
        self.assertEqual(est["total_estimated"], 0.0)

    def test_estimated_tax_caps_social_security_at_wage_base(self):
        # a big net profit exceeds the SS wage base → SS portion is capped, so
        # SE tax is LESS than a naive flat 15.3% of the whole base
        add_txn(self.conn, TODAY, -400000.0, "BIG CLIENT", account="biz",
                primary="INCOME", txn_id="big")
        est = selfemploy.estimated_tax(self.conn, self.ent["id"], TODAY.year)
        se_base = round(400000 * 0.9235, 2)
        wage_base = selfemploy.ss_wage_base(TODAY.year)
        self.assertGreater(se_base, wage_base)
        expected = round(wage_base * 0.124 + se_base * 0.029, 2)
        self.assertEqual(est["se_tax"], expected)
        self.assertLess(est["se_tax"], round(se_base * 0.153, 2))  # capped

    def test_estimated_tax_with_rate_and_deductions(self):
        add_txn(self.conn, TODAY, -10000.0, "CLIENT", account="biz",
                primary="INCOME", txn_id="rev")
        selfemploy.add_trip(self.conn, self.ent["id"], date=TODAY, miles=100)
        entities.update_entity(self.conn, self.ent["id"],
                               income_tax_rate=22, home_office_sqft=100)
        est = selfemploy.estimated_tax(self.conn, self.ent["id"], TODAY.year)
        # deductions reduce taxable profit below net profit
        self.assertLess(est["taxable_profit"], est["net_profit"])
        self.assertEqual(est["home_office_deduction"], 500.0)
        self.assertIsNotNone(est["income_tax"])
        self.assertEqual(est["income_tax_rate"], 22.0)

    def test_1099_vendor_tracking(self):
        add_txn(self.conn, TODAY, 800.0, "JANE CONTRACTOR", account="biz",
                txn_id="c1")
        add_txn(self.conn, TODAY, 200.0, "SMALL VENDOR", account="biz",
                txn_id="c2")
        # before marking: reportable false
        vt = {v["merchant"]: v for v in
              selfemploy.vendor_totals(self.conn, self.ent["id"], TODAY.year)}
        self.assertEqual(vt["JANE CONTRACTOR"]["paid"], 800.0)
        self.assertFalse(vt["JANE CONTRACTOR"]["needs_1099"])
        # mark Jane a contractor → over $600 → needs a 1099
        selfemploy.mark_vendor(self.conn, self.ent["id"], "JANE CONTRACTOR")
        selfemploy.mark_vendor(self.conn, self.ent["id"], "SMALL VENDOR")
        vt = {v["merchant"]: v for v in
              selfemploy.vendor_totals(self.conn, self.ent["id"], TODAY.year)}
        self.assertTrue(vt["JANE CONTRACTOR"]["needs_1099"])
        self.assertFalse(vt["SMALL VENDOR"]["needs_1099"])   # under $600


    def test_1099_vendor_total_is_net_of_partial_reimbursement(self):
        """A vendor charge partly paid back counts only the part the business
        kept paying — gross, $700 less $200 repaid would cross the $600
        line on money that came back."""
        add_txn(self.conn, TODAY, 700.0, "JANE CONTRACTOR", account="biz",
                txn_id="c9")
        add_txn(self.conn, TODAY, -200.0, "CLIENT REPAYS", account="biz",
                primary="INCOME", txn_id="d9")
        self.conn.execute(
            "INSERT INTO reimbursements (expense_id, reimburse_id, partial, "
            "amount) VALUES ('c9','d9',1,200)")
        selfemploy.mark_vendor(self.conn, self.ent["id"], "JANE CONTRACTOR")
        vt = {v["merchant"]: v for v in
              selfemploy.vendor_totals(self.conn, self.ent["id"], TODAY.year)}
        self.assertEqual(vt["JANE CONTRACTOR"]["paid"], 500.0)
        self.assertFalse(vt["JANE CONTRACTOR"]["needs_1099"])

if __name__ == "__main__":
    unittest.main()
