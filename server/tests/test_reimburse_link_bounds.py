"""Bulk reimbursement linking is bounded and throttled.

One POST /api/reimburse/link fans out into per-id DB work on a single held
connection, so the id list must be capped the way the sibling bulk endpoint
caps its selection, the route must carry the same per-IP rate limit, and a
junk element in the list must fail that one element — never the request or
the connection.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import security

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config


class ReimburseLinkBoundsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"rl-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.exp = add_txn(conn, TODAY, 60.0, "CLINIC COPAY",
                              account="chk", primary="MEDICAL")
            cls.dep = add_txn(conn, TODAY, -60.0, "INSURANCE CO",
                              account="chk", primary="INCOME")
        finally:
            conn.close()

    def setUp(self):
        # each test starts with a clean sliding window so the throttle test
        # cannot starve the others
        security._limiter._hits.clear()

    def test_an_absurd_id_list_is_refused(self):
        """The list length is client-controlled; past the cap the request is
        refused up front instead of looping tens of thousands of SELECTs on
        one held connection."""
        r = self.client.post("/api/reimburse/link",
                             json={"txn_id": self.dep,
                                   "other_ids": ["x"] * 1001})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("1000", r.json()["detail"])

    def test_a_list_at_the_cap_still_works(self):
        """The cap refuses the abuse case, not legitimate bulk linking."""
        r = self.client.post("/api/reimburse/link",
                             json={"txn_id": self.dep,
                                   "other_ids": ["nope"] * 1000})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["linked"], 0)

    def test_non_list_other_ids_is_refused(self):
        """A JSON object (or any non-list) is a malformed request, not
        something to iterate."""
        r = self.client.post("/api/reimburse/link",
                             json={"txn_id": self.dep,
                                   "other_ids": {"a": 1}})
        self.assertEqual(r.status_code, 400, r.text)

    def test_junk_elements_fail_per_item_not_the_request(self):
        """A nested list/dict/number element must never reach the SQL adapter
        as a raw parameter (a 500) — it just fails to match a transaction."""
        r = self.client.post("/api/reimburse/link",
                             json={"txn_id": self.dep,
                                   "other_ids": [["nested"], {"d": 1}, 7,
                                                 self.exp]})
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["linked"], 1)
        self.assertEqual(len(out["errors"]), 3)

    def test_route_declares_a_rate_limit(self):
        """Structural, like the step-up doors: the route carries the limiter
        dependency, so a rewrite cannot quietly drop it."""
        from .util import all_routes
        match = [r for r in all_routes(self.appmod.app)
                 if r.path.endswith("/reimburse/link")
                 and "POST" in r.methods]
        self.assertTrue(match, "route missing: /reimburse/link")
        dep = getattr(match[0], "dependant", None)
        names = [getattr(d.call, "__qualname__", "")
                 for d in (dep.dependencies if dep else [])]
        self.assertTrue(any("limit" in n for n in names),
                        f"/reimburse/link has no rate limit: {names}")

    def test_hammering_the_endpoint_trips_the_throttle(self):
        """Behavioural twin: repeated posts from one IP eventually 429."""
        codes = [self.client.post("/api/reimburse/link",
                                  json={"txn_id": self.dep,
                                        "other_ids": ["missing"]}).status_code
                 for _ in range(61)]
        self.assertIn(429, codes, f"never throttled: {set(codes)}")


if __name__ == "__main__":
    unittest.main()
