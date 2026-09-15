"""The compute-heavy read endpoints are bounded, and the retirement cache
is not defeated by a parameter nobody meant to change.

GET /api/retirement and the four /api/reports/* endpoints recompute from
scratch on every request and run in the shared threadpool, so an unbounded
one is a shared-CPU denial of service on every OTHER tenant of the same
worker: one signed-in account can hold every worker thread with ordinary
GETs. Two invariants keep that shut.

1. Each of them refuses past a per-IP request budget far above what the
   pages themselves ask for.
2. Requests that differ only below the grid the page's own controls step on
   answer from ONE cached projection. The projection is memoized on its
   exact parameter tuple, so walking `spend` by a dollar used to miss the
   cache — and run ~10k simulations — on every single request.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import retirement
from oikonome.web import security

from .util import _ensure_db, seed_accounts, write_config


def _client(prefix: str) -> tuple[TestClient, str]:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    client = TestClient(appmod.app)
    r = client.post("/api/signup", data={
        "email": f"{prefix}-{uuid.uuid4().hex[:8]}@example.dev",
        "password": "correct-horse-battery"})
    assert r.status_code == 200, r.text
    tid = client.get("/api/me").json()["tenant_id"]
    conn = tenancy.tenant_connect(tid)
    try:
        seed_accounts(conn)
        write_config(conn)
    finally:
        conn.close()
    return client, tid


class TheRetirementProjectionIsBounded(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _client("retlimit")

    def setUp(self):
        # the limiter is process-global and keyed per client IP; every test
        # here starts from a clean budget and leaves one behind
        security._limiter._hits.clear()

    tearDown = setUp

    def test_a_flood_of_projections_is_refused(self):
        codes = [self.client.get("/api/retirement").status_code
                 for _ in range(31)]
        self.assertEqual(set(codes[:30]), {200})
        self.assertEqual(codes[30], 429)

    def test_a_dollar_of_spend_does_not_buy_a_fresh_simulation(self):
        """Five requests inside one $1,000 step must cost ONE projection.

        This is the cache-busting hole itself: the memo key is the full
        parameter tuple, so an attacker (or a jittery client) that walks a
        parameter by 1 gets a full recompute per request while also
        evicting every other tenant's entry from the shared LRU."""
        retirement._project_cached.cache_clear()
        before = retirement._project_cached.cache_info().misses
        for spend in (52_001, 52_002, 52_003, 52_004, 52_005):
            r = self.client.get(f"/api/retirement?spend={spend}")
            self.assertEqual(r.status_code, 200, r.text)
            # the answer says which numbers produced it
            self.assertEqual(r.json()["inputs"]["spend"], 52_000)
        after = retirement._project_cached.cache_info().misses
        self.assertEqual(after - before, 1)

    def test_the_other_continuous_inputs_snap_to_the_pages_own_steps(self):
        r = self.client.get("/api/retirement?employer_mo=1020&"
                            "taxable_mo=74&ret=7.04&infl=2.97")
        self.assertEqual(r.status_code, 200, r.text)
        inp = r.json()["inputs"]
        self.assertEqual(inp["employer_mo"], 1000)
        self.assertEqual(inp["taxable_mo"], 50)
        self.assertEqual(inp["ret"], 7.0)
        self.assertEqual(inp["infl"], 3.0)


class TheAnalyticsReportsAreBounded(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client, cls.tid = _client("replimit")

    def setUp(self):
        security._limiter._hits.clear()

    tearDown = setUp

    def test_a_flood_of_report_builds_is_refused(self):
        codes = [self.client.get("/api/reports/networth").status_code
                 for _ in range(61)]
        self.assertEqual(set(codes[:60]), {200})
        self.assertEqual(codes[60], 429)

    def test_a_flood_of_merchant_histories_is_refused(self):
        """A merchant history is two ledger scans and a fuzzy match per
        surviving row, on a payee the caller names — the same class of
        expense as the reports, and reachable from every merchant name both
        clients render. Unbounded, one signed-in account can hold the
        worker with ordinary GETs."""
        codes = [self.client.get("/api/bills/history?payee=x").status_code
                 for _ in range(61)]
        self.assertEqual(set(codes[:60]), {200})
        self.assertEqual(codes[60], 429)

    def test_the_four_reports_share_one_budget(self):
        """Spreading the same flood across the four routes must not buy
        four times the work — they cost the same worker the same scan."""
        for _ in range(60):
            self.client.get("/api/reports/spending")
        for path in ("networth", "spending", "cashflow", "fees"):
            self.assertEqual(
                self.client.get(f"/api/reports/{path}").status_code, 429)


if __name__ == "__main__":
    unittest.main()
