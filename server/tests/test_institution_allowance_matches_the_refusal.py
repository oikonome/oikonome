"""The institution allowance a client shows is counted the way the ADD
door counts.

The cap holds a place for a platform Item released within the hold window
(the aggregator billed it for that month whatever happened after), so link,
disconnect, link again cannot mint a fresh billed Item every pass. A
`/api/me` that still counted live Items alone would show a slot free that
the door then refuses, after the person had signed in at their bank.
"""
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import base as sync_base

from .util import _ensure_db


class AllowanceMatchesTheRefusal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={
            "email": f"allow-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        sfx = uuid.uuid4().hex[:8]
        conn = tenancy.tenant_connect(self.tid)
        try:
            # one bank released two days ago, its Item still billed
            conn.execute(
                """INSERT INTO plaid_item_ledger
                       (item_id, credential_source, tenant, created_at,
                        removed_at)
                   VALUES (%s, 'platform', current_setting('app.tenant_id'),
                           now() - interval '3 days',
                           now() - interval '2 days')""",
                (f"p-gone-{sfx}",))
            conn.commit()
        finally:
            conn.close()

    def test_a_released_item_still_counts_against_the_allowance(self):
        with mock.patch.object(sync_base, "institution_cap", return_value=1):
            features = self.client.get("/api/me").json()["features"]
        self.assertEqual(1, features["institution_cap"])
        self.assertEqual(1, features["institutions_used"])
        conn = tenancy.tenant_connect(self.tid)
        try:
            with mock.patch.object(sync_base, "institution_cap",
                                   return_value=1), \
                    self.assertRaises(ValueError):
                sync_base.check_institution_cap(conn)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
