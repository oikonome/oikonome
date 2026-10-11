"""Every /api reply tells HTTP caches not to keep it, except the polls
that answer conditional requests by ETag.

A phone's HTTP stack writes cacheable GET replies to disk, where a
household's figures would outlive sign-out. So an API reply carries
`Cache-Control: no-store` unless its route sets a policy of its own. The
polls answering 304 to a matching ETag keep `private, no-cache` (store,
but always ask again) on the 200 and on the 304 alike — a 304 that read
no-store would make the client drop the copy the 304 points it to.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class ApiRepliesAreNotCached(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.app = appmod.app
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"nostore-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_an_ordinary_api_read_is_no_store(self):
        r = self.client.get("/api/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_an_api_error_is_no_store_too(self):
        r = TestClient(self.app).get("/api/me")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_an_etag_poll_keeps_no_cache_on_200_and_304(self):
        r = self.client.get("/api/today/glance")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("cache-control"), "private, no-cache")
        again = self.client.get("/api/today/glance",
                                headers={"If-None-Match": r.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.headers.get("cache-control"),
                         "private, no-cache")

    def test_help_keeps_its_own_conditional_policy(self):
        r = self.client.get("/api/help")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "private, no-cache")


if __name__ == "__main__":
    unittest.main()
