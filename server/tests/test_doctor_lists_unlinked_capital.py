"""Doctor lists the fronted movements whose charge is gone.

Before deletes took a fronted movement with its charge, the foreign key's
SET NULL kept the movement, unlinked, still counted on the capital account
for a cost the books no longer held. Those stay — some may be right — and
Doctor lists each one with a Remove for the owner. The door removes exactly
the movement asked for and only such a movement: one still linked to its
charge, or one entered by hand, is refused, and so is anyone but the owner.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config, TODAY


class DoctorUnlinkedCapital(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.email = f"unl-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={
            "email": cls.email, "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _conn(self):
        conn = tenancy.tenant_connect(self.tid)
        self.addCleanup(conn.close)
        return conn

    def _role(self, role):
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET role = %s WHERE email = %s",
                          (role, self.email))
        finally:
            admin.close()

    def _fronted(self, eid, amount, name):
        tid = add_txn(self._conn(), TODAY, amount, name)
        r = self.client.post(f"/api/business/entities/{eid}/reimburse",
                             json={"txn_id": tid, "mode": "contribute"})
        self.assertEqual(r.status_code, 200, r.text)
        return tid, r.json()["id"]

    def test_an_unlinked_contribution_is_listed_and_removable_alone(self):
        r = self.client.post("/api/business/entities",
                             json={"name": "Example Kiln Works",
                                   "structure": "sole_prop"})
        self.assertEqual(r.status_code, 200, r.text)
        eid = r.json()["id"]
        gone_txn, gone_mv = self._fronted(eid, 137.42, "GLAZE SUPPLY")
        _, linked_mv = self._fronted(eid, 58.19, "CLAY DEPOT")
        hand = self.client.post(f"/api/business/entities/{eid}/equity", json={
            "kind": "contribution", "amount": 911.07, "date": str(TODAY)})
        self.assertEqual(hand.status_code, 200, hand.text)
        hand_mv = hand.json()["id"]
        # the pre-fix delete: the row goes, the foreign key clears the link
        self._conn().execute("DELETE FROM transactions WHERE id = %s",
                             (gone_txn,))

        listed = self.client.get("/api/doctor/unlinked-movements").json()
        self.assertEqual(
            [(m["id"], m["entity_name"], m["amount"], m["date"], m["kind"])
             for m in listed["movements"]],
            [(gone_mv, "Example Kiln Works", 137.42, str(TODAY), "contribution")])
        row = [c for c in self.client.get("/api/doctor").json()["checks"]
               if c["name"] == "unlinked capital"]
        self.assertEqual(len(row), 1)
        self.assertFalse(row[0]["ok"])
        self.assertEqual(row[0]["severity"], "warn")

        base = "/api/doctor/unlinked-movements"
        # a movement still on its charge, and one entered by hand
        for mv in (linked_mv, hand_mv):
            self.assertEqual(self.client.delete(f"{base}/{mv}").status_code,
                             404)
        self.assertEqual(self.client.delete(f"{base}/not-a-uuid").status_code,
                         404)
        # anyone but the owner
        self._role("member")
        try:
            self.assertEqual(
                self.client.delete(f"{base}/{gone_mv}").status_code, 403)
        finally:
            self._role("owner")

        r = self.client.delete(f"{base}/{gone_mv}")
        self.assertEqual(r.status_code, 200, r.text)
        left = {m["id"] for m in self.client.get(
            f"/api/business/entities/{eid}/equity").json()["movements"]}
        self.assertEqual(left, {linked_mv, hand_mv})
        self.assertEqual(
            self.client.get(base).json()["movements"], [])
        self.assertFalse([c for c in self.client.get("/api/doctor")
                          .json()["checks"]
                          if c["name"] == "unlinked capital"])
        self.assertEqual(self.client.delete(f"{base}/{gone_mv}").status_code,
                         404)


if __name__ == "__main__":
    unittest.main()
