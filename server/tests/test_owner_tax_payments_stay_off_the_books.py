"""An owner's income and self-employment tax paid out of the business
account is not a Schedule C expense.

Booked as "Taxes and licenses" it lowers the profit the tax is figured on,
so every quarterly payment shrank the next quarter's estimate and the
year-end package understated taxable profit by every dollar of tax paid.
The payments are kept off the P&L (like an owner draw) and reported as
tax already paid toward the year, which is what a tax set-aside still has
to cover the estimate with. Figures are invented."""
import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import books, entities, selfemploy

from .util import _admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts

TAX = "GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT"


class OwnerTaxPaymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"otx-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Fixture Studio", structure="sole_prop",
            business_start_date="2030-01-01")
        self.eid = self.ent["id"]
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('biz','it1','Biz Checking',"
            "'depository','checking',1000)")
        entities.assign_account(self.conn, "biz", self.eid)
        add_txn(self.conn, dt.date(2031, 3, 2), -60000.0, "CLIENT PAYMENT",
                account="biz", primary="INCOME", txn_id="rev")
        add_txn(self.conn, dt.date(2031, 3, 9), 500.0, "PAPER SUPPLY CO",
                account="biz", txn_id="sup")
        for i, d in enumerate(("2031-04-14", "2031-06-13", "2031-09-12")):
            add_txn(self.conn, d, 4000.0, "IRS USATAXPYMT", account="biz",
                    primary="GOVERNMENT_AND_NON_PROFIT", detailed=TAX,
                    txn_id=f"es{i}")

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def test_estimated_payments_do_not_lower_profit_or_the_estimate(self):
        p = books.pnl(self.conn, self.eid, 2031)
        self.assertEqual(p["operating_expenses"], 500.0)
        self.assertEqual(p["net_operating"], 59500.0)
        self.assertNotIn("Taxes and licenses", p["operating_by_line"])
        self.assertEqual(p["estimated_tax_paid"], 12000.0)
        est = selfemploy.estimated_tax(self.conn, self.eid, 2031)
        self.assertEqual(est["net_profit"], 59500.0)

    def test_a_tax_payment_given_a_schedule_c_line_is_a_business_cost(self):
        # the employer's payroll share carries the same aggregator label;
        # the user's own line choice makes it deductible again
        books.classify(self.conn, "es0", "operating",
                       sched_c_line="Taxes and licenses")
        p = books.pnl(self.conn, self.eid, 2031)
        self.assertEqual(p["operating_by_line"]["Taxes and licenses"], 4000.0)
        self.assertEqual(p["net_operating"], 55500.0)
        self.assertEqual(p["estimated_tax_paid"], 8000.0)

    def test_a_tax_refund_is_neither_revenue_nor_a_lower_cost(self):
        add_txn(self.conn, dt.date(2031, 5, 1), -900.0, "IRS TREAS 310",
                account="biz", primary="INCOME", detailed="INCOME_TAX_REFUND",
                txn_id="ref")
        add_txn(self.conn, dt.date(2031, 5, 2), -300.0, "STATE TAX REFUND",
                account="biz", primary="GOVERNMENT_AND_NON_PROFIT",
                detailed=TAX, txn_id="ref2")
        p = books.pnl(self.conn, self.eid, 2031)
        self.assertEqual(p["revenue"], 60000.0)
        self.assertEqual(p["operating_expenses"], 500.0)

    def test_a_january_payment_counts_toward_the_year_before(self):
        add_txn(self.conn, dt.date(2032, 1, 14), 4000.0, "IRS USATAXPYMT",
                account="biz", primary="GOVERNMENT_AND_NON_PROFIT",
                detailed=TAX, txn_id="es3")
        self.assertEqual(books.pnl(self.conn, self.eid, 2031)
                         ["estimated_tax_paid"], 16000.0)
        self.assertEqual(books.pnl(self.conn, self.eid, 2032)
                         ["estimated_tax_paid"], 0.0)

    def test_tax_payments_stay_out_of_the_review_queue(self):
        self.assertEqual(books.unclassified_count(self.conn, self.eid), 1)
        ids = {s["txn_id"] for s in books.suggest_lines(self.conn, self.eid)}
        self.assertEqual(ids, {"sup"})

    def test_the_worksheet_gives_a_tax_payment_no_bucket(self):
        rows = {r["id"]: r for r in
                books.entity_transactions(self.conn, self.eid, 2031)}
        self.assertIsNone(rows["es0"]["effective_bucket"])
        self.assertEqual(rows["sup"]["effective_bucket"], "operating")


if __name__ == "__main__":
    unittest.main()
