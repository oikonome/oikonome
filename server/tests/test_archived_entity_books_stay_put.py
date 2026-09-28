"""An archived business entity's books are read-only in every direction.

Archiving keeps the equity ledger, mileage log, compliance calendar and the
transactions the entity owns as retention records; only the owner-only,
typed-name delete-forever may remove them. So a write that would take rows
OUT of an archived entity (reassigning its account or a transaction, the
legacy-flag import, a record DELETE) is refused exactly like a write into
one. Also covered: assigning the pinned primary checking into a business
drops the pin, and the business write doors answer malformed numbers and
text with a 400 rather than a 500.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget, entities

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts,
                   write_config, TODAY)


class _EngineBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"ro-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        write_config(self.conn)
        self.old = entities.create_entity(
            self.conn, name="Old Venture LLC", structure="sole_prop")["id"]
        self.new = entities.create_entity(
            self.conn, name="New Venture LLC", structure="sole_prop")["id"]

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def _archive(self, eid):
        self.conn.execute(
            "UPDATE business_entity SET status='archived' WHERE id=%s", (eid,))


class MovingRowsOutOfAnArchivedEntity(_EngineBase):

    def test_account_cannot_leave_an_archived_entity(self):
        entities.assign_account(self.conn, "card", self.old)
        self._archive(self.old)
        for target in (None, self.new):
            with self.assertRaises(ValueError):
                entities.assign_account(self.conn, "card", target)
        row = self.conn.execute(
            "SELECT entity_id FROM accounts WHERE id='card'").fetchone()
        self.assertEqual(str(row["entity_id"]), self.old)

    def test_transaction_cannot_leave_an_archived_entity(self):
        entities.assign_account(self.conn, "card", self.old)
        t = add_txn(self.conn, TODAY, 30.0, "PAPER CO", account="card")
        self._archive(self.old)
        # an override to another entity pulls it out of the archived books
        with self.assertRaises(ValueError):
            entities.assign_transaction(self.conn, t, self.new)

    def test_clearing_an_override_cannot_drop_a_row_into_an_archived_entity(self):
        entities.assign_account(self.conn, "card", self.old)
        t = add_txn(self.conn, TODAY, 30.0, "PAPER CO", account="card")
        entities.assign_transaction(self.conn, t, self.new)
        self._archive(self.old)
        with self.assertRaises(ValueError):
            entities.assign_transaction(self.conn, t, None)

    def test_moves_between_active_entities_still_work(self):
        entities.assign_account(self.conn, "card", self.old)
        self.assertTrue(entities.assign_account(self.conn, "card", self.new))
        t = add_txn(self.conn, TODAY, 30.0, "PAPER CO", account="card")
        self.assertTrue(entities.assign_transaction(self.conn, t, None))

    def test_flag_import_leaves_rows_owned_through_their_account(self):
        """The legacy-flag import may only claim rows no entity owns — a
        row on an account assigned to an entity already belongs to it."""
        entities.assign_account(self.conn, "card", self.old)
        owned = add_txn(self.conn, TODAY, 30.0, "PAPER CO", account="card")
        free = add_txn(self.conn, TODAY, 20.0, "INK CO", account="chk")
        for t in (owned, free):
            self.conn.execute(
                "INSERT INTO business_flags (txn_id) VALUES (%s)", (t,))
        self._archive(self.old)
        self.assertEqual(entities.count_flagged(self.conn), 1)
        self.assertEqual(
            entities.import_flagged_transactions(self.conn, self.new), 1)
        row = self.conn.execute(
            "SELECT entity_id FROM transactions WHERE id=%s",
            (owned,)).fetchone()
        self.assertIsNone(row["entity_id"])


class PrimaryCheckingPin(_EngineBase):

    def _pin(self):
        return budget.load_config(self.conn).get("checking_account_id")

    def test_assigning_the_pinned_checking_to_a_business_drops_the_pin(self):
        """The forecast ignores a business account, and a pin left on one
        makes every later edit of the account re-send a pin the server
        refuses."""
        with budget.config_txn(self.conn) as cfg:
            cfg["checking_account_id"] = "chk"
        entities.assign_account(self.conn, "chk", self.new)
        self.assertIsNone(self._pin())

    def test_combined_household_keeps_the_pin(self):
        with budget.config_txn(self.conn) as cfg:
            cfg["checking_account_id"] = "chk"
            cfg["combine_entities"] = True
        entities.assign_account(self.conn, "chk", self.new)
        self.assertEqual(self._pin(), "chk")


class ArchivedEntityHttpDoors(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"ro-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _entity(self, name):
        r = self.client.post("/api/business/entities",
                             json={"name": name, "structure": "sole_prop"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_record_deletes_on_an_archived_entity_are_refused(self):
        eid = self._entity(f"Keepsake {uuid.uuid4().hex[:6]} LLC")
        base = f"/api/business/entities/{eid}"
        mv = self.client.post(f"{base}/equity", json={
            "kind": "contribution", "amount": 100,
            "date": TODAY.isoformat()}).json()["id"]
        trip = self.client.post(f"{base}/mileage", json={
            "date": TODAY.isoformat(), "miles": 12}).json()["id"]
        ob = self.client.post(f"{base}/compliance", json={
            "title": "Annual report",
            "due_date": TODAY.isoformat()}).json()["id"]
        self.assertEqual(self.client.delete(base).json()["status"],
                         "archived")
        for path in (f"equity/{mv}", f"mileage/{trip}", f"compliance/{ob}"):
            r = self.client.delete(f"{base}/{path}")
            self.assertEqual(r.status_code, 409, f"{path}: {r.text}")
        self.assertEqual(
            len(self.client.get(f"{base}/equity").json()["movements"]), 1)

    def test_moving_an_account_out_of_an_archived_entity_is_409(self):
        eid = self._entity(f"Closed {uuid.uuid4().hex[:6]} LLC")
        r = self.client.post("/api/accounts/card/entity",
                             json={"entity_id": eid})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.delete(f"/api/business/entities/{eid}")
        r = self.client.post("/api/accounts/card/entity",
                             json={"entity_id": None})
        self.assertEqual(r.status_code, 409, r.text)

    def test_malformed_numbers_and_text_are_400_not_500(self):
        eid = self._entity(f"Inputs {uuid.uuid4().hex[:6]} LLC")
        base = f"/api/business/entities/{eid}"
        day = TODAY.isoformat()
        cases = [
            ("post", "equity", {"kind": "contribution", "amount": "NaN",
                                "date": day}),
            ("post", "equity", {"kind": "contribution", "amount": 10 ** 400,
                                "date": day}),
            ("post", "mileage", {"date": day, "miles": "NaN"}),
            ("post", "mileage", {"date": day, "miles": 5,
                                 "purpose": {"a": 1}}),
            ("post", "compliance", {"title": "Fee", "due_date": day,
                                    "fee": "NaN"}),
            ("post", "compliance", {"title": {"a": 1}, "due_date": day}),
            ("post", "vendors", {"merchant": "Acme", "tin_last4": "n/a"}),
            ("post", "vendors", {"merchant": {"a": 1}}),
            ("post", "vendors", {"merchant": ["x"]}),
            ("post", "vendors", {"merchant": "Acme", "tin_last4": {"a": 1}}),
        ]
        for method, path, body in cases:
            r = getattr(self.client, method)(f"{base}/{path}", json=body)
            self.assertEqual(r.status_code, 400, f"{path} {body}: {r.text}")
        # nothing non-finite was stored, so the reads still answer
        self.assertEqual(self.client.get(f"{base}/equity").status_code, 200)
        # home_office_sqft: 1e999 parses to float inf
        r = self.client.patch(base, content='{"home_office_sqft": 1e999}',
                              headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
