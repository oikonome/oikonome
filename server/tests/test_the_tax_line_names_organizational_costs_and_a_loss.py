"""The estimate's line names what its deduction is — a §248 organizational
fee is not a "start-up" cost — and a year that ends in a loss says whose
return the loss lands on rather than reading "$0 taxable" alone."""
import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import books, entities, selfemploy

from .util import _admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts


class TaxLineWordsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"taxw-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Example Works LLC",
            structure="single_member_llc",
            business_start_date="2026-03-01")["id"]
        entities.assign_account(self.conn, "chk", self.ent)

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def test_an_organizational_fee_and_a_loss_are_named(self):
        fee = add_txn(self.conn, dt.date(2026, 2, 10), 90.0,
                      "Example Filing Office", account="chk",
                      primary="GOVERNMENT_AND_NON_PROFIT")
        books.classify(self.conn, fee, "organizational")
        add_txn(self.conn, dt.date(2026, 5, 1), 400.0, "Example Hosting",
                account="chk", primary="GENERAL_SERVICES")
        est = selfemploy.estimated_tax(self.conn, self.ent, 2026)
        self.assertEqual(est["startup_label"], "organizational costs")
        self.assertEqual(est["taxable_profit"], 0.0)
        self.assertIn("Schedule C of your own return", est["loss_note"])

    def test_a_profitable_year_has_no_loss_note(self):
        add_txn(self.conn, dt.date(2026, 5, 1), -900.0, "Example Client",
                account="chk", primary="INCOME")
        est = selfemploy.estimated_tax(self.conn, self.ent, 2026)
        self.assertIsNone(est["loss_note"])


if __name__ == "__main__":
    unittest.main()
