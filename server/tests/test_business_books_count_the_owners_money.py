"""The owner's money on the business books (specs/business-books.md): a
business cost paid on a personal card counts in the capital account as
money the owner put in, owner transfers leave the expense list and are
recorded as contributions or draws, the balance sheet says why capital and
cash differ, and no ledger row or P&L line prints a raw bank category."""
import datetime as dt
import io
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import books, entities, equity, selfemploy

from .util import _admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts

DAY = dt.date(2026, 8, 3)


class OwnerMoneyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"own-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)            # chk + card are the household's
        for aid, name, bal in (("bchk", "Ops Checking", 970.0),
                               ("bsav", "Ops Savings", 0.0)):
            self.conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES (%s,'it1',%s,'depository',"
                "'checking',%s)", (aid, name, bal))
        self.ent = entities.create_entity(
            self.conn, name="Example Works LLC",
            structure="single_member_llc", business_start_date="2026-01-01")["id"]
        entities.assign_account(self.conn, "bchk", self.ent)
        entities.assign_account(self.conn, "bsav", self.ent)

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def _row(self, tid):
        return next(t for t in books.entity_transactions(self.conn, self.ent)
                    if t["id"] == tid)

    def test_a_cost_paid_on_a_personal_card_is_money_the_owner_put_in(self):
        fee = add_txn(self.conn, DAY, 30.0, "Example Filing Office",
                      account="card", primary="GOVERNMENT_AND_NON_PROFIT")
        entities.assign_transaction(self.conn, fee, self.ent)
        cap = equity.capital_summary(self.conn, self.ent)
        self.assertEqual(cap["paid_personally"], 30.0)
        self.assertEqual(cap["contributions"], 30.0)
        self.assertTrue(self._row(fee)["paid_personally"])
        # marked reimbursable instead: the business owes it back, so it is
        # no longer the owner's contribution
        equity.reimburse_expense(self.conn, self.ent, fee)
        cap = equity.capital_summary(self.conn, self.ent)
        self.assertEqual((cap["paid_personally"], cap["contributions"]),
                         (0.0, 0.0))

    def test_owner_transfers_leave_the_expense_list_and_record_as_equity(self):
        # $1,000 from the household into the business: the personal leg
        # booked to the business by hand, and the business's own leg
        out_leg = add_txn(self.conn, DAY, 1000.0, "Transfer to Ops",
                          account="chk", primary="TRANSFER_OUT")
        entities.assign_transaction(self.conn, out_leg, self.ent)
        in_leg = add_txn(self.conn, DAY, -1000.0, "Transfer from Household",
                         account="bchk", primary="TRANSFER_IN")
        # a move between the business's own accounts
        to_sav = add_txn(self.conn, DAY, 25.0, "To savings", account="bchk",
                         primary="TRANSFER_OUT")
        at_sav = add_txn(self.conn, DAY + dt.timedelta(days=1), -25.0,
                         "From checking", account="bsav",
                         primary="TRANSFER_IN")
        cost = add_txn(self.conn, DAY, 30.0, "Example Hosting",
                       account="bchk", primary="GENERAL_SERVICES")
        roles = {t["id"]: t["role"]
                 for t in books.entity_transactions(self.conn, self.ent)}
        self.assertEqual(roles, {out_leg: "owner_side", in_leg: "owner_in",
                                 to_sav: "internal", at_sav: "internal",
                                 cost: "cost"})
        # nothing that moved is in a bucket
        self.assertIsNone(self._row(out_leg)["effective_bucket"])

        sheet = selfemploy.balance_sheet(self.conn, self.ent)
        # cash $970 against capital of -$30 (the cost): $1,000 unrecorded
        self.assertEqual(sheet["capital_gap"], 1000.0)
        self.assertTrue(any("$1,000.00 is owner money moved in" in line
                            for line in sheet["reconcile"]),
                        sheet["reconcile"])
        mv = equity.record_owner_transfer(self.conn, self.ent, in_leg)
        self.assertEqual((mv["kind"], mv["amount"]), ("contribution", 1000.0))
        # a second tap is the same movement
        self.assertEqual(
            equity.record_owner_transfer(self.conn, self.ent, in_leg)["id"],
            mv["id"])
        sheet = selfemploy.balance_sheet(self.conn, self.ent)
        self.assertEqual(sheet["capital_gap"], 0.0)
        self.assertEqual(sheet["reconcile"], [])
        self.assertEqual(self._row(in_leg)["ledger_line"],
                         "Owner contribution")
        with self.assertRaises(ValueError):
            equity.record_owner_transfer(self.conn, self.ent, cost)
        with self.assertRaises(ValueError):
            equity.record_owner_transfer(self.conn, self.ent, out_leg)

    def test_no_pnl_line_or_ledger_row_is_a_raw_category(self):
        unlined = add_txn(self.conn, DAY, 42.0, "Example Services",
                          account="bchk", primary="GENERAL_SERVICES")
        filing = add_txn(self.conn, DAY, 100.0, "Example Filing Office",
                         account="bchk", primary="GOVERNMENT_AND_NON_PROFIT")
        books.classify(self.conn, filing, "organizational",
                       sched_c_line="Taxes and licenses")
        p = books.pnl(self.conn, self.ent, 2026)
        self.assertEqual(p["operating_by_line"],
                         {"Uncategorized — needs a line": 42.0})
        self.assertEqual(self._row(unlined)["ledger_line"],
                         "Uncategorized — needs a line")
        # a capitalized cost is not printed on a deductible line as well
        self.assertEqual(self._row(filing)["ledger_line"],
                         "Organizational cost (§248)")


class OwnerTransferDoorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        from oikonome.web.app import app
        security._limiter._hits.clear()
        self.client = TestClient(app)
        r = self.client.post("/api/signup", data={
            "email": f"otd-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            self.ent = entities.create_entity(
                conn, name="Example Studio", structure="sole_prop")["id"]
            entities.assign_account(conn, "chk", self.ent)
            self.in_leg = add_txn(conn, DAY, -500.0, "Owner deposit",
                                  account="chk", primary="TRANSFER_IN")
            self.cost = add_txn(conn, DAY, 12.0, "Example Ads",
                                account="chk", primary="GENERAL_SERVICES")
            conn.commit()
        finally:
            conn.close()

    def test_the_door_records_a_transfer_and_refuses_a_cost(self):
        base = f"/api/business/entities/{self.ent}"
        r = self.client.post(f"{base}/owner-transfer",
                             json={"txn_id": self.cost})
        self.assertEqual(400, r.status_code, r.text)
        r = self.client.post(f"{base}/owner-transfer",
                             json={"txn_id": self.in_leg})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(r.json()["kind"], "contribution")
        rows = self.client.get(f"{base}/transactions").json()["transactions"]
        self.assertEqual({t["id"]: t["role"] for t in rows},
                         {self.in_leg: "owner_in", self.cost: "cost"})
        z = zipfile.ZipFile(io.BytesIO(
            self.client.get(f"{base}/package.zip").content))
        ledger = z.read("ledger.csv").decode()
        self.assertIn("Owner contribution", ledger)
        self.assertNotIn("Unclassified", ledger)


if __name__ == "__main__":
    unittest.main()
