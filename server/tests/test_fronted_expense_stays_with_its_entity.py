"""A capitalized or reimbursable expense stays with the entity its movement
names.

Capitalizing (or marking reimbursable) a personally-paid business cost books
the row to one entity AND records the movement that explains it. Moving the
row afterwards through the per-transaction entity door — to another entity,
or back to personal — would leave the first entity's capital account counting
a contribution for a cost its books no longer carry. The door refuses that
move with a 409 until the movement is deleted; moves that keep the row with
the movement's entity, and rows with no movement, still work.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config, TODAY


class FrontedExpenseMoveTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"fx-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _entity(self, name):
        r = self.client.post("/api/business/entities", json={
            "name": f"{name} {uuid.uuid4().hex[:6]} LLC",
            "structure": "sole_prop"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def _txn(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return add_txn(conn, TODAY, 500.0, "OFFICE CHAIR", account="card")
        finally:
            conn.close()

    def _owner(self, t):
        conn = tenancy.tenant_connect(self.tid)
        try:
            r = conn.execute("SELECT entity_id FROM transactions WHERE id=%s",
                             (t,)).fetchone()
            return str(r["entity_id"]) if r["entity_id"] else None
        finally:
            conn.close()

    def _front(self, eid, t, mode):
        r = self.client.post(f"/api/business/entities/{eid}/reimburse",
                             json={"txn_id": t, "mode": mode})
        self.assertEqual(r.status_code, 200, r.text)

    def test_moving_a_fronted_expense_elsewhere_is_refused(self):
        for mode in ("contribute", "reimburse"):
            a, b = self._entity("Alpha"), self._entity("Beta")
            t = self._txn()
            self._front(a, t, mode)
            for target in (b, None):
                r = self.client.post(f"/api/transactions/{t}/entity",
                                     json={"entity_id": target})
                self.assertEqual(r.status_code, 409, f"{mode}: {r.text}")
                self.assertIn("delete that movement first", r.text)
                self.assertEqual(self._owner(t), a)

    def test_a_settled_hold_cannot_move_its_fronted_posted_charge(self):
        """A list can still show the pending hold after it posted. A move
        aimed at the hold lands on the posted charge, so the check must
        read the posted charge's movement too."""
        a, b = self._entity("Held"), self._entity("Elsewhere")
        conn = tenancy.tenant_connect(self.tid)
        try:
            hold = add_txn(conn, TODAY, 480.0, "OFFICE DESK", account="card",
                           pending=1)
            posted = add_txn(conn, TODAY, 500.0, "OFFICE DESK",
                             account="card")
            conn.execute("UPDATE transactions SET pending_transaction_id=%s"
                         " WHERE id=%s", (hold, posted))
            conn.execute("UPDATE transactions SET removed=1 WHERE id=%s",
                         (hold,))
        finally:
            conn.close()
        self._front(a, posted, "contribute")
        r = self.client.post(f"/api/transactions/{hold}/entity",
                             json={"entity_id": b})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._owner(posted), a)

    def test_reassigning_to_the_same_entity_still_works(self):
        a = self._entity("Same")
        t = self._txn()
        self._front(a, t, "contribute")
        r = self.client.post(f"/api/transactions/{t}/entity",
                             json={"entity_id": a})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_row_with_no_movement_moves_freely(self):
        a, b = self._entity("Free"), self._entity("Other")
        t = self._txn()
        for target in (a, b, None):
            r = self.client.post(f"/api/transactions/{t}/entity",
                                 json={"entity_id": target})
            self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self._owner(t))

    def test_after_deleting_the_movement_the_move_goes_through(self):
        a, b = self._entity("Undo"), self._entity("Next")
        t = self._txn()
        self._front(a, t, "contribute")
        mv = self.client.get(f"/api/business/entities/{a}/equity").json()
        mid = next(m["id"] for m in mv["movements"] if m["txn_id"] == t)
        self.assertEqual(self.client.delete(
            f"/api/business/entities/{a}/equity/{mid}").status_code, 200)
        r = self.client.post(f"/api/transactions/{t}/entity",
                             json={"entity_id": b})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._owner(t), b)


if __name__ == "__main__":
    unittest.main()
