"""A bill can carry a TRANSACTION category that is stamped on the rows it
matches — the answer for a merchant that is not one thing (the monthly
storage charge at a truck-rental company is rent; the truck rentals are
transportation; a merchant rule cannot say both).

Invariants: only the bill's own matched rows change (the merchant rule is
not taught); a person's own correction on a row always wins; changing,
clearing, archiving or deleting the bill reverts the rows it stamped; the
pass is idempotent and reaches new charges via the nightly run."""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills

from .util import _ensure_db, add_txn, seed_accounts, write_config

TODAY = dt.date.today()


class BillTxnCategoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)

    def setUp(self):
        self.client.post("/api/signup", data={
            "email": f"btc-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            d = TODAY.replace(day=10)
            # the storage charge, monthly, and a truck rental at the same
            # merchant — one merchant, two kinds of spend
            self.storage = add_txn(conn, d, 120.00, "ZENITH STORAGE",
                                   account="chk", merchant="Zenith",
                                   primary="HOME_IMPROVEMENT")
            self.storage_prev = add_txn(conn, d - dt.timedelta(days=30), 120.00,
                                        "ZENITH STORAGE", account="chk",
                                        merchant="Zenith", primary="HOME_IMPROVEMENT")
            self.truck = add_txn(conn, d, 500.00, "ZENITH TRUCK RENTAL",
                                 account="chk", merchant="Zenith",
                                 primary="TRANSPORTATION")
        finally:
            conn.close()
        r = self.client.post("/api/bills/save", json={
            "payee": "Zenith", "amount": 120.00, "cadence": "MONTHLY:1",
            "next_due": (TODAY + dt.timedelta(days=20)).isoformat(),
            "txn_category": "RENT_AND_UTILITIES"})
        self.assertEqual(r.status_code, 200, r.text)

    def _cat(self, txn_id):
        conn = tenancy.tenant_connect(self.tid)
        try:
            r = conn.execute(
                "SELECT COALESCE(t.category_override, t.category_primary) c, "
                "       t.category_override o, m.bill_id "
                "FROM transactions t LEFT JOIN manual_categories m "
                "  ON m.transaction_id = t.id WHERE t.id=%s", (txn_id,)).fetchone()
        finally:
            conn.close()
        return r["c"], r["o"], r["bill_id"]

    def test_matched_rows_get_the_category_others_keep_theirs(self):
        self.assertEqual(self._cat(self.storage)[0], "RENT_AND_UTILITIES")
        self.assertEqual(self._cat(self.storage_prev)[0], "RENT_AND_UTILITIES")
        # the truck rental is outside the bill's amount tolerance: untouched
        self.assertEqual(self._cat(self.truck), ("TRANSPORTATION", None, None))
        # the mark says which bill did it — never mistaken for a person's edit
        self.assertEqual(self._cat(self.storage)[2], "rec:zenith")

    def test_get_reports_it_and_the_ledger_shows_it(self):
        g = self.client.get("/api/bills/bill", params={"payee": "Zenith"}).json()
        self.assertEqual(g["txn_category"], "RENT_AND_UTILITIES")
        rows = self.client.get("/api/transactions",
                               params={"q": "zenith"}).json()["rows"]
        by = {r["id"]: r for r in rows}
        self.assertEqual(by[self.storage]["category"], "RENT AND UTILITIES")
        self.assertEqual(by[self.storage]["recurring_bill"], "Zenith")
        self.assertEqual(by[self.truck]["category"], "TRANSPORTATION")

    def test_a_persons_correction_wins_and_survives_reruns(self):
        r = self.client.post(f"/api/transactions/{self.storage}/category",
                             json={"category": "GENERAL_SERVICES", "scope": "one"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._cat(self.storage), ("GENERAL_SERVICES",
                                                   "GENERAL_SERVICES", None))
        conn = tenancy.tenant_connect(self.tid)
        try:
            bills.apply_txn_categories(conn)
            bills.run(conn, TODAY)
        finally:
            conn.close()
        self.assertEqual(self._cat(self.storage)[0], "GENERAL_SERVICES")
        self.assertEqual(self._cat(self.storage_prev)[0], "RENT_AND_UTILITIES")

    def test_changing_the_category_restamps(self):
        self.client.post("/api/bills/save", json={
            "payee": "Zenith", "amount": 120.00, "cadence": "MONTHLY:1",
            "next_due": (TODAY + dt.timedelta(days=20)).isoformat(),
            "txn_category": "GENERAL_SERVICES"})
        self.assertEqual(self._cat(self.storage)[0], "GENERAL_SERVICES")
        self.assertEqual(self._cat(self.storage_prev)[0], "GENERAL_SERVICES")

    def test_clearing_reverts_to_the_merchant_rule(self):
        self.client.post("/api/bills/save", json={
            "payee": "Zenith", "amount": 120.00, "cadence": "MONTHLY:1",
            "next_due": (TODAY + dt.timedelta(days=20)).isoformat(),
            "txn_category": ""})
        self.assertEqual(self._cat(self.storage), ("HOME_IMPROVEMENT", None, None))
        g = self.client.get("/api/bills/bill", params={"payee": "Zenith"}).json()
        self.assertEqual(g["txn_category"], "")

    def test_plain_edit_keeps_it(self):
        # an edit that does not mention txn_category keeps the current one
        self.client.post("/api/bills/save", json={
            "payee": "Zenith", "amount": 120.00, "cadence": "MONTHLY:1",
            "next_due": (TODAY + dt.timedelta(days=25)).isoformat()})
        g = self.client.get("/api/bills/bill", params={"payee": "Zenith"}).json()
        self.assertEqual(g["txn_category"], "RENT_AND_UTILITIES")
        self.assertEqual(self._cat(self.storage)[0], "RENT_AND_UTILITIES")

    def test_archive_reverts_restore_restamps_delete_reverts(self):
        self.client.post("/api/bills/delete", json={"payee": "Zenith",
                                                     "mode": "archive"})
        self.assertEqual(self._cat(self.storage), ("HOME_IMPROVEMENT", None, None))
        self.client.post("/api/bills/restore", json={"payee": "Zenith"})
        self.assertEqual(self._cat(self.storage)[0], "RENT_AND_UTILITIES")
        self.client.post("/api/bills/delete", json={"payee": "Zenith",
                                                     "mode": "archive"})
        self.client.post("/api/bills/delete", json={"payee": "Zenith",
                                                     "mode": "purge"})
        self.assertEqual(self._cat(self.storage), ("HOME_IMPROVEMENT", None, None))

    def test_new_charge_is_stamped_by_the_nightly_pass(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            new = add_txn(conn, TODAY, 120.00, "ZENITH STORAGE", account="chk",
                          merchant="Zenith", primary="HOME_IMPROVEMENT")
            self.assertEqual(self._cat(new)[0], "HOME_IMPROVEMENT")
            stats = bills.run(conn, TODAY)
            self.assertGreaterEqual(stats["txn_category_applied"], 1)
            # idempotent: nothing more to do the second time
            self.assertEqual(bills.apply_txn_categories(conn)["applied"], 0)
        finally:
            conn.close()
        self.assertEqual(self._cat(new)[0], "RENT_AND_UTILITIES")

    def test_reset_to_source_on_a_bill_row_sticks(self):
        """'↺ reset to source category' on a bill-stamped row must not be
        undone by the next pass — it becomes the person's own answer."""
        r = self.client.post(f"/api/transactions/{self.storage}/category",
                             json={"category": "", "scope": "one"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._cat(self.storage)[0], "HOME_IMPROVEMENT")
        conn = tenancy.tenant_connect(self.tid)
        try:
            bills.apply_txn_categories(conn)
            bills.run(conn, TODAY)
        finally:
            conn.close()
        self.assertEqual(self._cat(self.storage)[0], "HOME_IMPROVEMENT")
        self.assertIsNone(self._cat(self.storage)[2])

    def test_reset_sticks_when_the_source_has_no_category_of_its_own(self):
        """A reset on a bill-stamped row must survive the next pass even
        when there is nothing underneath to fall back to.

        The pin the reset leaves behind is what the bill pass reads as
        "a person decided this". A row whose aggregator sent no category
        has no category to pin, so a reset that only pins a real category
        leaves nothing behind — and the row, indistinguishable from one
        nobody ever touched, is restamped within the hour."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            bare = add_txn(conn, TODAY, 120.00, "ZENITH STORAGE",
                           account="chk", merchant="Zenith", primary=None)
            bills.apply_txn_categories(conn)
        finally:
            conn.close()
        self.assertEqual(self._cat(bare)[0], "RENT_AND_UTILITIES")
        r = self.client.post(f"/api/transactions/{bare}/category",
                             json={"category": "", "scope": "one"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._cat(bare), (None, None, None))
        conn = tenancy.tenant_connect(self.tid)
        try:
            bills.apply_txn_categories(conn)
            bills.run(conn, TODAY)
            # a second reset (the ledger's undo path calls it on rows it
            # already reset) must not hand the row back to the bill either
            from oikonome.web import data
            data.clear_category(conn, bare)
            bills.apply_txn_categories(conn)
        finally:
            conn.close()
        self.assertEqual(self._cat(bare), (None, None, None))
        rows = self.client.get("/api/transactions",
                               params={"q": "zenith"}).json()["rows"]
        self.assertEqual({r["id"]: r for r in rows}[bare]["category"], "?")

    def test_a_correction_landing_mid_pass_is_not_clobbered(self):
        """The pass scans, then writes; a person's correction that commits
        between the two must survive the write (ownership is re-checked in
        the write itself, not only in the scan)."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            # simulate the interleaving: the row is a bill mark at scan time
            # (it is), then a person claims it before the write — the write
            # predicate `WHERE manual_categories.bill_id IS NOT NULL` is what
            # protects it, so exercise that predicate directly
            conn.execute(
                "UPDATE manual_categories SET bill_id=NULL, category='TRAVEL' "
                "WHERE transaction_id=%s", (self.storage,))
            conn.execute("UPDATE transactions SET category_override='TRAVEL' "
                         "WHERE id=%s", (self.storage,))
            n = conn.execute(
                """INSERT INTO manual_categories (transaction_id, category, set_at, bill_id)
                   VALUES (%s,'RENT_AND_UTILITIES',now(),'rec:zenith')
                   ON CONFLICT (tenant_id, transaction_id) DO UPDATE SET
                       category=EXCLUDED.category, bill_id=EXCLUDED.bill_id
                   WHERE manual_categories.bill_id IS NOT NULL""",
                (self.storage,)).rowcount
            self.assertEqual(n, 0)
            bills.apply_txn_categories(conn)
        finally:
            conn.close()
        self.assertEqual(self._cat(self.storage), ("TRAVEL", "TRAVEL", None))

    def test_two_bills_overlapping_the_closer_amount_wins(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            # a second Zenith bill at $100 with its own category; the $120.00
            # charge is inside both tolerances ($30) — closer wins, every run
            bills.save_bill(conn, payee="Zenith Locker", amount=100,
                            frequency="MONTHLY", interval=1,
                            next_due=(TODAY + dt.timedelta(days=5)).isoformat(),
                            merchant="zenith", txn_category="GENERAL_SERVICES")
            for _ in range(3):
                bills.apply_txn_categories(conn)
                self.assertEqual(self._cat(self.storage)[0], "RENT_AND_UTILITIES")
            odd = add_txn(conn, TODAY, 101.0, "ZENITH STORAGE", account="chk",
                          merchant="Zenith", primary="HOME_IMPROVEMENT")
            bills.apply_txn_categories(conn)
        finally:
            conn.close()
        self.assertEqual(self._cat(odd)[0], "GENERAL_SERVICES")

    def test_business_rows_are_never_stamped(self):
        from oikonome.engine import entities
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES ('bizchk','it1','Biz Checking',"
                "'depository','checking',5000)")
            ent = entities.create_entity(conn, name="Acme LLC",
                                         structure="sole_prop")
            entities.assign_account(conn, "bizchk", ent["id"])
            conn.commit()
        finally:
            conn.close()
        conn = tenancy.tenant_connect(self.tid)   # entity_account_ids set
        try:
            biz = add_txn(conn, TODAY, 120.00, "ZENITH STORAGE", account="bizchk",
                          merchant="Zenith", primary="HOME_IMPROVEMENT")
            bills.apply_txn_categories(conn)
        finally:
            conn.close()
        self.assertEqual(self._cat(biz), ("HOME_IMPROVEMENT", None, None))

    def test_flow_category_is_refused(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "Zenith", "amount": 120.00, "cadence": "MONTHLY:1",
            "next_due": (TODAY + dt.timedelta(days=20)).isoformat(),
            "txn_category": "TRANSFER_OUT"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._cat(self.storage)[0], "RENT_AND_UTILITIES")

    def test_category_rename_follows_into_the_bill(self):
        # a custom category on the bill; renaming it must move the bill's
        # setting too, or the next pass restamps the old name
        self.client.post("/api/bills/save", json={
            "payee": "Zenith", "amount": 120.00, "cadence": "MONTHLY:1",
            "next_due": (TODAY + dt.timedelta(days=20)).isoformat(),
            "txn_category": "Storage"})
        self.assertEqual(self._cat(self.storage)[0], "Storage")
        from oikonome.web import data
        conn = tenancy.tenant_connect(self.tid)
        try:
            data.rename_category(conn, "Storage", "Storage Unit")
            bills.apply_txn_categories(conn)
        finally:
            conn.close()
        g = self.client.get("/api/bills/bill", params={"payee": "Zenith"}).json()
        self.assertEqual(g["txn_category"], "Storage Unit")
        self.assertEqual(self._cat(self.storage)[0], "Storage Unit")

    def test_ledger_shows_the_stamped_category_prettified(self):
        rows = self.client.get("/api/transactions",
                               params={"q": "zenith"}).json()["rows"]
        by = {r["id"]: r for r in rows}
        self.assertEqual(by[self.storage]["category"], "RENT AND UTILITIES")
        self.assertEqual(by[self.truck]["category"], "TRANSPORTATION")

    def test_rename_keeps_the_marks(self):
        self.client.post("/api/bills/save", json={
            "payee": "Zenith Storage", "orig_payee": "Zenith",
            "amount": 120.00, "cadence": "MONTHLY:1",
            "next_due": (TODAY + dt.timedelta(days=20)).isoformat()})
        self.assertEqual(self._cat(self.storage)[2], "rec:zenith-storage")
        self.assertEqual(self._cat(self.storage)[0], "RENT_AND_UTILITIES")


if __name__ == "__main__":
    unittest.main()
