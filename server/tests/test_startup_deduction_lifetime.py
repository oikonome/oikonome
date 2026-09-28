"""The startup/organizational deduction is a lifetime allowance, not annual.

Section 195/248 grants a ONE-TIME allowance over the life of the business.
Computing it from a single year's costs hands a business that books
pre-opening costs across two years the $5,000 twice — and the $50,000
phase-out never triggers when no year alone exceeds it.
"""

import datetime as dt
import pathlib
import unittest
import uuid

import oikonome
from oikonome.db import tenancy
from oikonome.engine import books, entities, selfemploy

from .util import _admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent / rel).read_text()


class StartupDeductionIsLifetimeTests(unittest.TestCase):

    def test_the_rule_itself_is_unchanged(self):
        self.assertEqual(books.startup_deduction(4000.0)["immediate"], 4000.0)
        self.assertEqual(books.startup_deduction(20000.0)["immediate"], 5000.0)
        # phase-out: every dollar over 50k removes a dollar of the 5k
        self.assertEqual(books.startup_deduction(52000.0)["immediate"], 3000.0)
        self.assertEqual(books.startup_deduction(56000.0)["immediate"], 0.0)

    def test_the_total_fed_to_it_is_lifetime_not_yearly(self):
        src = _src("engine/books.py")
        self.assertIn("def _lifetime_startup(", src)
        self.assertIn("_lifetime_startup(conn, entity_id, \"organizational\"",
                      src)
        self.assertIn("_lifetime_startup(conn, entity_id, \"startup_195\"",
                      src)

    def test_an_all_time_call_does_not_re_query(self):
        """year=None already IS the lifetime figure."""
        src = _src("engine/books.py")
        block = src[src.index("def _lifetime_startup("):]
        self.assertIn("if year is None:", block[:900])
        self.assertIn("return this_year", block[:900])


class DeductibleNowBelongsToTheYearTheBusinessBegan(unittest.TestCase):
    """The immediate part of the allowance is taken once, in the year the
    business begins; every other year shows none of it. Showing the
    lifetime immediate figure in each year that books a pre-opening cost
    reads as claiming it once per year."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"sd-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Example Works LLC", structure="sole_prop",
            business_start_date="2026-01-01")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('biz','it1','Biz Checking',"
            "'depository','checking',1000)")
        entities.assign_account(self.conn, "biz", self.ent["id"])
        # $3,000 of pre-opening cost in 2024 and $3,000 more in 2025
        add_txn(self.conn, dt.date(2024, 5, 1), 3000.0, "CONSULTANT",
                account="biz", txn_id="sd24")
        add_txn(self.conn, dt.date(2025, 5, 1), 3000.0, "CONSULTANT",
                account="biz", txn_id="sd25")

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def test_immediate_only_in_the_year_the_business_began(self):
        eid = self.ent["id"]
        y24 = books.pnl(self.conn, eid, 2024)["startup_deduction"]
        y25 = books.pnl(self.conn, eid, 2025)["startup_deduction"]
        y26 = books.pnl(self.conn, eid, 2026)["startup_deduction"]
        self.assertEqual(y24["total"], 6000.0)          # lifetime allowance
        self.assertEqual(y24["immediate"], 0.0)
        self.assertEqual(y25["immediate"], 0.0)
        self.assertEqual(y26["immediate"], 5000.0)
        # the $1,000 remainder amortizes from Jan 2026: 12 months that year
        self.assertEqual(y26["amortization_this_year"],
                         round(round(1000 / 180, 2) * 12, 2))
        self.assertEqual(y25["deductible_this_year"], 0.0)

    def test_all_years_view_keeps_the_lifetime_figure(self):
        p = books.pnl(self.conn, self.ent["id"])
        self.assertEqual(p["startup_deduction"]["immediate"], 5000.0)

    def test_estimated_tax_subtracts_the_years_startup_deduction(self):
        """_pnl_core keeps start-up cost out of net_operating, so the
        estimate must take the year's share off itself or it taxes profit
        the page calls deductible."""
        add_txn(self.conn, dt.date(2026, 3, 1), -20000.0, "CLIENT",
                account="biz", primary="INCOME", txn_id="sdrev")
        est = selfemploy.estimated_tax(self.conn, self.ent["id"], 2026)
        share = books.pnl(self.conn, self.ent["id"], 2026)[
            "startup_deduction"]["deductible_this_year"]
        self.assertGreater(share, 5000.0)
        self.assertEqual(est["startup_deduction"], share)
        self.assertEqual(est["taxable_profit"], round(20000.0 - share, 2))
