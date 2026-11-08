"""Taking the ledger out as a file is the owner's act, whatever the door.

The full-data export has always been owner-only: a copy that has left the
instance cannot be recalled. The ledger page's CSV / XLSX download (up to
fifty thousand rows), the business list's Schedule C CSV, an entity's P&L
CSV and its year-end package are the same act at a smaller size — the
household's transactions, written to a file somebody can carry away. A
member or a view-only login still reads every one of those rows on the
page; what they cannot do is take them with them.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

PW = "correct-horse-battery"
OTHER_PW = "family-member-pass"


class LedgerDownloadsAreOwnerOnlyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app
        cls.owner = TestClient(app)
        cls.owner.post("/api/signup", data={
            "email": f"dl-own-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        cls.eid = cls.owner.post("/api/business/entities", json={
            "name": "Ledger LLC", "structure": "sole_prop"}).json()["id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, pending, removed, entity_id)
                   VALUES ('dl-t','chk','2026-07-11',42.0,'Supplies',0,0,%s)""",
                (cls.eid,))
        finally:
            conn.close()
        cls.member = cls._claim(cls, "member")
        cls.viewer = cls._claim(cls, "viewer")

    def _claim(self, role):
        r = self.owner.post("/api/invites",
                            json={"label": role, "role": role,
                                  "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        client = TestClient(self.app)
        got = client.post("/api/invite/claim", json={
            "token": token,
            "email": f"dl-{role}-{uuid.uuid4().hex[:8]}@example.dev",
            "password": OTHER_PW})
        assert got.status_code == 200, got.text
        return client

    def _doors(self):
        return {
            "ledger csv": "/api/transactions/export?format=csv",
            "ledger xlsx": "/api/transactions/export?format=xlsx",
            "schedule c csv": "/api/business/export.csv",
            "expense csv": "/api/receipts/report?tag=travel&y=2026&m=1&format=csv",
            "entity p&l csv": f"/api/business/entities/{self.eid}/pnl.csv",
            "entity year-end package":
                f"/api/business/entities/{self.eid}/package.zip",
        }

    def test_the_owner_downloads_every_ledger_file(self):
        for name, path in self._doors().items():
            with self.subTest(door=name):
                r = self.owner.get(path)
                self.assertEqual(r.status_code, 200, r.text[:200])

    def test_a_member_cannot_download_the_ledger(self):
        for name, path in self._doors().items():
            with self.subTest(door=name):
                r = self.member.get(path)
                self.assertEqual(r.status_code, 403, r.text[:200])
                self.assertIn("detail", r.json())

    def test_a_view_only_login_cannot_download_the_ledger(self):
        for name, path in self._doors().items():
            with self.subTest(door=name):
                r = self.viewer.get(path)
                self.assertEqual(r.status_code, 403, r.text[:200])
                self.assertIn("detail", r.json())

    def test_a_member_still_reads_the_same_rows_on_the_page(self):
        """The refusal is about the file, not the data: the page that the
        download mirrors still answers a member."""
        self.assertEqual(self.member.get("/api/transactions").status_code, 200)
        self.assertEqual(self.viewer.get("/api/transactions").status_code, 200)


if __name__ == "__main__":
    unittest.main()
