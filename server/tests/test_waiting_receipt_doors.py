"""The doors for a receipt snapped before its transaction.

Snap one with no transaction, list what is waiting (with the charges each
could be), pair one by hand, take it off again, and delete one — through the
real routes. Snapping answers to the same rules as attaching a receipt to a
transaction: a viewer may do neither, a demo household may do neither, and
the size cap is the receipt cap.
"""
import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"
PNG = ("r.png", b"\x89PNG...", "image/png")


def _app():
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return app


def _signup(app, prefix):
    client = TestClient(app)
    r = client.post("/api/signup", data={
        "email": f"{prefix}-{uuid.uuid4().hex[:8]}@example.dev",
        "password": PW})
    assert r.status_code == 200, r.text
    tid = client.get("/api/me").json()["tenant_id"]
    conn = tenancy.tenant_connect(tid)
    try:
        seed_accounts(conn)
        write_config(conn)
    finally:
        conn.close()
    return client, tid


def _claim(app, owner, role):
    r = owner.post("/api/invites", json={"label": role, "role": role,
                                         "password": PW})
    assert r.status_code == 200, r.text
    token = r.json()["url"].rsplit("token=", 1)[1]
    client = TestClient(app)
    got = client.post("/api/invite/claim", json={
        "token": token,
        "email": f"{role}-{uuid.uuid4().hex[:8]}@example.dev",
        "password": "family-member-pass"})
    assert got.status_code == 200, got.text
    assert client.get("/api/me").json()["role"] == role
    return client


class WaitingReceiptDoorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        cls.owner, cls.tid = _signup(cls.app, "wr")
        conn = tenancy.tenant_connect(cls.tid)
        try:
            cls.txn = add_txn(conn, dt.date.today(), 18.40,
                              "QUILL AND INK STATIONERS", account="chk")
        finally:
            conn.close()

    def _db(self, sql, args=()):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(sql, args).fetchone()
        finally:
            conn.close()

    def test_snap_list_match_unmatch_delete(self):
        r = self.owner.post("/api/receipts", files={"file": PNG})
        self.assertEqual(r.status_code, 200, r.text)
        rid = r.json()["id"]
        self.assertIn(rid, [w["id"] for w in r.json()["waiting"]])
        # no AI on this household: it waits unparsed, offered to nobody
        listed = self.owner.get("/api/receipts/waiting").json()
        mine = next(w for w in listed["waiting"] if w["id"] == rid)
        self.assertEqual(mine["status"], "uploaded")
        self.assertEqual(mine["candidates"], [])
        self.assertIsNone(self._db("SELECT txn_id FROM receipts WHERE id=%s",
                                   (rid,))["txn_id"])
        # the image serves like any receipt's
        self.assertEqual(
            self.owner.get(f"/api/receipts/{rid}/image").status_code, 200)

        m = self.owner.post(f"/api/receipts/{rid}/match",
                            json={"txn_id": self.txn})
        self.assertEqual(m.status_code, 200, m.text)
        self.assertNotIn(rid, [w["id"] for w in m.json()["waiting"]])
        on_txn = self.owner.get(
            f"/api/transactions/{self.txn}/receipts").json()["receipts"]
        self.assertEqual([x["match_method"] for x in on_txn
                          if x["id"] == rid], ["manual"])
        # a second hand match of an attached receipt is refused
        self.assertEqual(self.owner.post(
            f"/api/receipts/{rid}/match",
            json={"txn_id": self.txn}).status_code, 404)

        u = self.owner.post(f"/api/receipts/{rid}/unmatch")
        self.assertEqual(u.status_code, 200, u.text)
        self.assertEqual(u.json()["txn_id"], self.txn)
        self.assertIn(rid, [w["id"] for w in u.json()["waiting"]])
        self.assertEqual(self.owner.post(
            f"/api/receipts/{rid}/unmatch").status_code, 404)

        d = self.owner.delete(f"/api/receipts/{rid}")
        self.assertEqual(d.status_code, 200, d.text)
        self.assertIsNone(self._db("SELECT 1 FROM receipts WHERE id=%s",
                                   (rid,)))
        # every hand act is on the household's activity log
        acts = [a["action"] for a in self._acts()]
        for act in ("snapped", "matched", "unmatched", "removed"):
            self.assertIn(act, acts)

    def _acts(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT action FROM activity_log WHERE kind='receipt'"
            ).fetchall()
        finally:
            conn.close()

    def test_match_to_an_unknown_transaction_is_404(self):
        rid = self.owner.post("/api/receipts",
                              files={"file": PNG}).json()["id"]
        r = self.owner.post(f"/api/receipts/{rid}/match",
                            json={"txn_id": "no-such-txn"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.owner.post(
            f"/api/receipts/{rid}/match", json={}).status_code, 400)

    def test_snap_refuses_what_attach_refuses(self):
        self.assertEqual(self.owner.post(
            "/api/receipts",
            files={"file": ("x.html", b"<p>", "text/html")}).status_code, 400)
        big = self.owner.post(
            "/api/receipts",
            files={"file": ("big.png", b"\x89" + b"x" * (5 * 1024 * 1024),
                            "image/png")})
        self.assertEqual(big.status_code, 413)


class WhoMaySnapTests(unittest.TestCase):
    """A member attaches and snaps; a viewer does neither."""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        cls.owner, cls.tid = _signup(cls.app, "wrr")
        conn = tenancy.tenant_connect(cls.tid)
        try:
            cls.txn = add_txn(conn, dt.date.today(), 9.10, "HARBOR COFFEE",
                              account="chk")
        finally:
            conn.close()
        cls.viewer = _claim(cls.app, cls.owner, "viewer")
        cls.member = _claim(cls.app, cls.owner, "member")

    def test_a_viewer_can_neither_attach_nor_snap(self):
        self.assertEqual(self.viewer.post(
            f"/api/transactions/{self.txn}/receipt",
            files={"file": PNG}).status_code, 403)
        self.assertEqual(self.viewer.post(
            "/api/receipts", files={"file": PNG}).status_code, 403)
        rid = self.owner.post("/api/receipts",
                              files={"file": PNG}).json()["id"]
        self.assertEqual(self.viewer.post(
            f"/api/receipts/{rid}/match",
            json={"txn_id": self.txn}).status_code, 403)
        self.assertEqual(self.viewer.post(
            f"/api/receipts/{rid}/unmatch").status_code, 403)
        # but reads the waiting list like everything else
        got = self.viewer.get("/api/receipts/waiting")
        self.assertEqual(got.status_code, 200)
        self.assertIn(rid, [w["id"] for w in got.json()["waiting"]])

    def test_a_member_snaps_and_matches(self):
        r = self.member.post("/api/receipts", files={"file": PNG})
        self.assertEqual(r.status_code, 200, r.text)
        m = self.member.post(f"/api/receipts/{r.json()['id']}/match",
                             json={"txn_id": self.txn})
        self.assertEqual(m.status_code, 200, m.text)


class DemoRefusesSnapTests(unittest.TestCase):
    def test_a_demo_household_cannot_snap(self):
        app = _app()
        client, tid = _signup(app, "wrd")
        from oikonome.engine import budget
        from oikonome.web import demoguard
        conn = tenancy.tenant_connect(tid)
        try:
            cfg = budget.load_config(conn)
            cfg["demo_mode"] = True
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        demoguard._cache.clear()
        try:
            r = client.post("/api/receipts", files={"file": PNG})
            self.assertEqual(r.status_code, 403, r.text)
        finally:
            demoguard._cache.clear()


if __name__ == "__main__":
    unittest.main()
