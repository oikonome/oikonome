"""Family members: invite mint/claim lifecycle, single-use + expiry,
viewer read-everything/write-nothing enforcement at the current_user choke
point, own-account allowlist, member removal."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config
from .export_ticket import export_get


class FamilyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app
        cls.owner = TestClient(app)
        cls.owner.post("/api/signup", data={
            "email": f"own-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _mint(self, label="spouse"):
        r = self.owner.post("/api/invites", json={"label": label, "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_owner_role_and_defaults(self):
        me = self.owner.get("/api/me").json()
        self.assertEqual(me["role"], "owner")

    def test_invite_claim_and_viewer_enforcement(self):
        inv = self._mint()
        token = inv["url"].rsplit("token=", 1)[1]
        # peek greets with the role
        peek = self.owner.get(f"/api/invite/peek?token={token}")
        self.assertEqual(peek.json()["role"], "viewer")

        viewer = TestClient(self.app)
        email = f"fam-{uuid.uuid4().hex[:8]}@example.dev"
        r = viewer.post("/api/invite/claim", json={
            "token": token, "email": email,
            "password": "family-member-pass"})
        self.assertEqual(r.status_code, 200)
        me = viewer.get("/api/me").json()
        self.assertEqual((me["role"], me["email"]), ("viewer", email))

        # single use: the same link never works twice
        r = viewer.post("/api/invite/claim", json={
            "token": token, "email": "x@example.dev",
            "password": "family-member-pass"})
        self.assertEqual(r.status_code, 400)

        # sees everything…
        self.assertEqual(viewer.get("/api/today").status_code, 200)
        self.assertEqual(viewer.get("/api/settings").status_code, 200)
        self.assertEqual(
            viewer.get("/api/users").status_code, 200)
        # …changes nothing
        self.assertEqual(viewer.post(
            "/api/settings", json={"food_monthly": 1}).status_code, 403)
        self.assertEqual(viewer.post(
            "/api/bills/detect").status_code, 403)
        self.assertEqual(viewer.post(
            "/api/invites", json={"password": "correct-horse-battery"}).status_code, 403)
        self.assertEqual(viewer.delete(
            f"/api/users/{uuid.uuid4()}").status_code, 403)
        # own-account actions stay open (wrong password ≠ forbidden)
        r = viewer.post("/api/password/change", json={
            "current_password": "wrong", "new_password": "irrelevant-pw-123"})
        self.assertNotEqual(r.status_code, 403)
        # …and so does feedback (zip flavor — no SMTP in tests)
        self.assertEqual(viewer.post(
            "/api/testing/feedback", data={"message": "viewer feedback"}
        ).status_code, 200)

        # roster shows both; owner removes the viewer → session dies
        users = self.owner.get("/api/users").json()["users"]
        vid = next(u["id"] for u in users if u["email"] == email)
        self.assertEqual(
            self.owner.request("DELETE", f"/api/users/{vid}",
                json={"password": "correct-horse-battery"}).status_code, 200)
        self.assertEqual(viewer.get("/api/me").status_code, 401)

    def test_viewer_cannot_complete_a_plaid_link_session(self):
        """The POST that starts a Plaid link session is owner-only via
        the global write gate (non-owners cannot POST); the completion
        routes are GETs, which that gate waves through — yet the status
        poll is where the public-token exchange, first sync and
        item-status writes happen. The same role rule must hold at the
        GET, or a view-only member could drive a link session the owner
        left in flight."""
        import time as _time

        from oikonome.web import pages
        inv = self._mint("plaid-viewer")
        token = inv["url"].rsplit("token=", 1)[1]
        viewer = TestClient(self.app)
        r = viewer.post("/api/invite/claim", json={
            "token": token,
            "email": f"pv-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        self.assertEqual(r.status_code, 200)
        lt = f"link-test-{uuid.uuid4().hex}"
        pages._PLAID_LINK[lt] = {
            "tenant_id": self.tid, "item_id": None, "kind": "add",
            "manage_accounts": False, "institution": None,
            "at": _time.time(), "url": "https://cdn.plaid.com/link/x",
            "done": False}
        try:
            r = viewer.get(f"/accounts/plaid/link/{lt}/status")
            self.assertEqual(r.status_code, 403)
            r = viewer.get(f"/accounts/plaid/link/{lt}",
                           follow_redirects=False)
            self.assertEqual(r.status_code, 403)
            # the owner still reaches their session (the waiting page
            # needs no Plaid credentials, so it proves the gate is
            # role-based, not broken-for-everyone)
            r = self.owner.get(f"/accounts/plaid/link/{lt}",
                               follow_redirects=False)
            self.assertEqual(r.status_code, 200)
        finally:
            pages._PLAID_LINK.pop(lt, None)

    def test_invite_expiry_and_revoke(self):
        inv = self._mint("expired person")
        token = inv["url"].rsplit("token=", 1)[1]
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE invites SET expires_at = now() - "
                          "interval '1 hour' WHERE tenant_id = %s "
                          "AND label = 'expired person'", (self.tid,))
        finally:
            admin.close()
        self.assertEqual(
            self.owner.get(f"/api/invite/peek?token={token}").status_code, 404)
        r = TestClient(self.app).post("/api/invite/claim", json={
            "token": token, "email": "late@example.dev",
            "password": "family-member-pass"})
        self.assertEqual(r.status_code, 400)

        inv2 = self._mint("changed my mind")
        pend = self.owner.get("/api/invites").json()["invites"]
        h = next(i["token_hash"] for i in pend
                 if i["label"] == "changed my mind")
        self.assertEqual(
            self.owner.delete(f"/api/invites/{h}").status_code, 200)
        token2 = inv2["url"].rsplit("token=", 1)[1]
        self.assertEqual(
            self.owner.get(f"/api/invite/peek?token={token2}").status_code,
            404)

    def test_owner_cannot_remove_self(self):
        me = self.owner.get("/api/me").json()
        users = self.owner.get("/api/users").json()["users"]
        my_id = next(u["id"] for u in users if u["me"])
        r = self.owner.request("DELETE", f"/api/users/{my_id}",
            json={"password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.owner.get("/api/me").json()["email"],
                         me["email"])


if __name__ == "__main__":
    unittest.main()


class DumpExportTests(unittest.TestCase):
    """The database-dump download is owner-only; content checks run only
    where pg_dump exists (the container image ships it)."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app
        cls.owner = TestClient(app)
        cls.owner.post("/api/signup", data={
            "email": f"dump-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def test_viewer_gets_403(self):
        r = self.owner.post("/api/invites", json={"label": "v", "password": "correct-horse-battery"})
        token = r.json()["url"].rsplit("token=", 1)[1]
        viewer = TestClient(self.app)
        viewer.post("/api/invite/claim", json={
            "token": token, "email": f"dv-{uuid.uuid4().hex[:6]}@example.dev",
            "password": "family-member-pass"})
        self.assertEqual(viewer.get("/export/dump").status_code, 403)

    def test_dump_streams_gzip_sql(self):
        import shutil
        if shutil.which("pg_dump") is None:
            self.skipTest("pg_dump not on this host (container image has it)")
        import gzip
        from unittest import mock

        from oikonome.web import pages
        # the dump refuses on a multi-tenant instance, and the
        # shared test DB is inherently multi-tenant — model the real
        # single-tenant self-host box this 200 branch exists for
        # (the multi-tenant refusal has its own test in test_export_dump_tenant_gate)
        with mock.patch.object(pages, "_tenant_count", return_value=1):
            r = export_get(self.owner, "/export/dump")
        self.assertEqual(r.status_code, 200)
        self.assertIn("oikonome-", r.headers["content-disposition"])
        body = gzip.decompress(r.content).decode(errors="replace")
        self.assertIn("PostgreSQL database dump", body)
        self.assertIn("CREATE TABLE", body)
