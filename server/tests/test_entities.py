"""Business entity model — CRUD, EIN encryption, assignment, tenant
isolation. The hard separation of the money math has its own tests; this
covers the model only."""
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import entities

from .util import _admin_dsn, _ensure_db, TEST_DB, seed_accounts, add_txn, TODAY


class EntityModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"ent-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def _make(self, **over):
        kw = dict(name="Acme LLC", structure="single_member_llc", state="ID",
                  # EIN is synthetic, not a real one (demo.py convention)
                  formation_date="2025-03-01", ein="00-0001234")
        kw.update(over)
        return entities.create_entity(self.conn, **kw)

    def test_create_and_ein_never_leaks(self):
        ent = self._make()
        self.assertEqual(ent["name"], "Acme LLC")
        self.assertEqual(ent["ein_last4"], "1234")
        self.assertEqual(ent["structure"], "single_member_llc")
        # the plaintext/ciphertext EIN is never in the returned dict
        self.assertNotIn("ein", ent)
        self.assertNotIn("ein_enc", ent)
        # ...but reveal_ein round-trips the full number
        self.assertEqual(entities.reveal_ein(self.conn, ent["id"]),
                         "000001234")

    def test_ein_is_encrypted_at_rest_when_keyed(self):
        import os
        from unittest import mock
        from cryptography.fernet import Fernet
        # with a master key configured, the stored column is ciphertext,
        # not the digits
        with mock.patch.dict(
                os.environ,
                {"OIKONOME_MASTER_KEY": Fernet.generate_key().decode()}):
            ent = self._make(name="Crypto LLC")
            raw = self.conn.execute(
                "SELECT ein_enc FROM business_entity WHERE id = %s",
                (ent["id"],)).fetchone()["ein_enc"]
            self.assertNotEqual(raw, "000001234")
            self.assertNotIn("000001234", raw or "")
            self.assertEqual(entities.reveal_ein(self.conn, ent["id"]),
                             "000001234")

    def test_validation(self):
        with self.assertRaises(ValueError):
            entities.create_entity(self.conn, name="", structure="sole_prop")
        with self.assertRaises(ValueError):
            entities.create_entity(self.conn, name="X", structure="bogus")
        with self.assertRaises(ValueError):        # bad EIN length
            entities.create_entity(self.conn, name="X",
                                   structure="sole_prop", ein="123")

    def test_list_get_update(self):
        e1 = self._make(name="One")
        self._make(name="Two", structure="sole_prop", ein=None)
        listed = entities.list_entities(self.conn)
        self.assertEqual({e["name"] for e in listed}, {"One", "Two"})
        got = entities.get_entity(self.conn, e1["id"])
        self.assertEqual(got["name"], "One")
        upd = entities.update_entity(self.conn, e1["id"],
                                     name="One Renamed", state="WA")
        self.assertEqual(upd["name"], "One Renamed")
        self.assertEqual(upd["state"], "WA")

    def test_archive_is_read_only(self):
        e = self._make()
        entities.set_status(self.conn, e["id"], "archived")
        self.assertEqual(entities.get_entity(self.conn, e["id"])["status"],
                         "archived")
        with self.assertRaises(ValueError):
            entities.update_entity(self.conn, e["id"], name="nope")

    def test_members(self):
        e = self._make()
        entities.add_member(self.conn, e["id"], member_name="Jordan Avery",
                            ownership_pct=100, is_manager=True)
        members = entities.list_members(self.conn, e["id"])
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["member_name"], "Jordan Avery")
        self.assertEqual(members[0]["ownership_pct"], 100.0)
        self.assertTrue(members[0]["is_manager"])

    def test_assign_account_and_transaction(self):
        seed_accounts(self.conn)
        t = add_txn(self.conn, TODAY, 50.0, "OFFICE DEPOT")
        e = self._make()
        # whole-account assignment
        self.assertTrue(entities.assign_account(self.conn, "chk", e["id"]))
        row = self.conn.execute(
            "SELECT entity_id FROM accounts WHERE id='chk'").fetchone()
        self.assertEqual(str(row["entity_id"]), e["id"])
        # per-transaction override
        self.assertTrue(entities.assign_transaction(self.conn, t, e["id"]))
        row = self.conn.execute(
            "SELECT entity_id FROM transactions WHERE id=%s", (t,)).fetchone()
        self.assertEqual(str(row["entity_id"]), e["id"])
        # clearing reverts to NULL (personal)
        entities.assign_transaction(self.conn, t, None)
        row = self.conn.execute(
            "SELECT entity_id FROM transactions WHERE id=%s", (t,)).fetchone()
        self.assertIsNone(row["entity_id"])

    def test_deleting_entity_reverts_money_to_personal(self):
        """ON DELETE SET NULL (entity_id): dropping an entity nulls the FK,
        never deletes the transaction, and never touches tenant_id."""
        seed_accounts(self.conn)
        t = add_txn(self.conn, TODAY, 20.0, "USPS")
        e = self._make()
        entities.assign_transaction(self.conn, t, e["id"])
        self.conn.execute("DELETE FROM business_entity WHERE id = %s",
                          (e["id"],))
        row = self.conn.execute(
            "SELECT entity_id, tenant_id FROM transactions WHERE id=%s",
            (t,)).fetchone()
        self.assertIsNone(row["entity_id"])          # reverted to personal
        self.assertEqual(str(row["tenant_id"]), self.tid)  # tenant intact

    def test_tenant_isolation(self):
        e = self._make(name="Tenant-A Co")
        other = str(tenancy.create_tenant(
            self.admin, f"ent-{uuid.uuid4().hex[:8]}"))
        oc = tenancy.tenant_connect(other)
        try:
            self.assertEqual(entities.list_entities(oc), [])
            self.assertIsNone(entities.get_entity(oc, e["id"]))
        finally:
            oc.close()


if __name__ == "__main__":
    unittest.main()
