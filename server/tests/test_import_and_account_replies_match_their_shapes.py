"""The import and account doors the rest of the suite leaves unread match
the shapes they document (web/schemas/imports.py, accounts.py).

The conformance hook (tests/api_conformance.py) validates each reply; the
existing tests reach most of these doors, and this module reaches the
others: a W-2 staged, committed, listed, given an owner and removed; two
accounts linked, reordered and unlinked; a SimpleFIN claim and an MX pull,
with the providers stood in for; the accounts page and connections.
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from . import api_conformance
from .util import _ensure_db, seed_accounts, write_config

PW = "correct-horse-battery"
TODAY = dt.date.today()


class ImportAndAccountShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"impacct-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # the same checking account seen through a second connection
            conn.execute("INSERT INTO items (id, aggregator, "
                         "institution_name, access_token) VALUES "
                         "('it2', 'test', 'Test Bank', 'tok-two')")
            conn.execute("INSERT INTO accounts (id, item_id, name, type, "
                         "subtype, mask, balance_current) VALUES "
                         "('chk2', 'it2', 'Test Checking', 'depository', "
                         "'checking', '4417', 5000)")
            conn.execute("UPDATE accounts SET mask = '4417' WHERE id = 'chk'")
        finally:
            conn.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _ok(self, method, path, **kw):
        before = sum(api_conformance.validated().values())
        r = self.client.request(method, "/api" + path, **kw)
        self.assertEqual(r.status_code, 200, f"{method} {path}: {r.text}")
        self.assertGreater(sum(api_conformance.validated().values()), before,
                           f"{method} {path}: the reply was not validated")
        return r.json()

    def test_a_w2_staged_committed_owned_and_removed(self):
        from oikonome.engine import taxdocs
        conn = tenancy.tenant_connect(self.tid)
        try:
            plan = taxdocs.stage(conn, "w2", [{
                "year": TODAY.year - 1, "employer": "Acme Widgets",
                "ein": "12-3456789", "box1": 52000.0,
                "boxes": {"1": 52000.0, "2": 6100.0}}])
        finally:
            conn.close()
        rows = [dict(r, owner="primary") for r in plan["rows"]]
        self._ok("POST", "/import/taxdoc/commit",
                 json={"token": plan["token"], "rows": rows})
        w2s = self._ok("GET", "/import/taxdoc/w2s")["w2s"]
        self.assertEqual(len(w2s), 1)
        w = w2s[0]
        seen = {k: w[k] for k in ("year", "box1", "employer", "ein", "owner")}
        self._ok("POST", f"/import/taxdoc/w2s/{w['id']}/owner",
                 json={"owner": "spouse", "seen": seen})
        seen["owner"] = "spouse"
        self._ok("DELETE", f"/import/taxdoc/w2s/{w['id']}",
                 json={"seen": seen})

    def test_two_accounts_linked_reordered_and_unlinked(self):
        gid = self._ok("POST", "/accounts/links",
                       json={"account_ids": ["chk", "chk2"]})["group_id"]
        links = self._ok("GET", "/accounts/links")
        self.assertTrue(links["groups"])
        self._ok("POST", f"/accounts/links/{gid}/order",
                 json={"account_ids": ["chk2", "chk"]})
        accounts = self._ok("GET", "/accounts")["accounts"]
        self.assertTrue(any(a["link"] for a in accounts))
        self._ok("DELETE", f"/accounts/links/{gid}")

    def test_provider_pulls_with_the_providers_stood_in_for(self):
        from oikonome.sync import mx as mx_mod
        from oikonome.web import setup as _setup
        with mock.patch.object(_setup, "connect_simplefin",
                               return_value={"accounts": 2,
                                             "transactions": 14}):
            self._ok("POST", "/accounts/simplefin",
                     json={"token": "aGVsbG8="})
        with mock.patch.object(mx_mod, "sync", return_value={
                "members": 1, "accounts": 2, "transactions": 9,
                "removed": 0, "skipped_import_duplicates": 0}):
            self._ok("POST", "/accounts/mx/sync", json={})
        self._ok("GET", "/connections")


if __name__ == "__main__":
    unittest.main()
