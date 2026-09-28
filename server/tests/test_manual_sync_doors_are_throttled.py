"""The hand-pressed sync doors are throttled.

Each press is a live aggregator pull — for Plaid, /transactions/sync plus
the liabilities and holdings products that are billed per request — and on
hosted every household shares one Plaid client's rate limit. The advisory
lock only stops two presses overlapping, so without a throttle a loop of
presses one after another spends that shared budget and a web pool slot
per press, and never earns an auto-ban strike. A person pressing sync on
each of their connections must never meet the limit; a loop must, whether
it comes from one address or rotates through many (the household-wide
budget), and on every manual door: the JSON door, the form door and MX.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db

PW = "correct-horse-battery"


class ManualSyncThrottle(unittest.TestCase):
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
        self.c = TestClient(self.app)
        r = self.c.post("/api/signup", data={
            "email": f"thr-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.c.get("/api/me").json()["tenant_id"]

    def _press(self):
        # an item this household does not have: the handler answers 404
        # without calling any aggregator, so the test spends no network
        return self.c.post("/api/accounts/sync",
                           json={"item_id": f"none-{uuid.uuid4().hex}"})

    def _forget_the_address(self):
        """What a loop rotating its source address gets: the per-address
        windows start over, the household's does not."""
        from oikonome.web import security
        for k in list(security._limiter._hits):
            if k[0] == "account-sync" and k[1] != self.tid:
                del security._limiter._hits[k]

    def test_a_person_syncing_every_connection_is_not_throttled(self):
        for _ in range(12):
            self.assertEqual(self._press().status_code, 404)

    def test_a_loop_from_one_address_is_refused_on_every_manual_door(self):
        from oikonome.web.pages import SYNC_PER_IP
        for _ in range(SYNC_PER_IP[0]):
            self.assertEqual(self._press().status_code, 404)
        self.assertEqual(self._press().status_code, 429)
        r = self.c.post("/accounts/sync", data={"item_id": "x"},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(self.c.post("/api/accounts/mx/sync").status_code, 429)

    def test_a_loop_rotating_addresses_meets_the_household_budget(self):
        from oikonome.web.pages import SYNC_PER_IP, SYNC_PER_TENANT
        pressed = 0
        while pressed < SYNC_PER_TENANT[0]:
            for _ in range(min(SYNC_PER_IP[0] - 1,
                               SYNC_PER_TENANT[0] - pressed)):
                self.assertEqual(self._press().status_code, 404)
                pressed += 1
            self._forget_the_address()
        r = self._press()
        self.assertEqual(r.status_code, 429, r.text)
        self.assertIn("Too many syncs", r.json()["detail"])
        r = self.c.post("/accounts/sync", data={"item_id": "x"},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Too%20many%20syncs", r.headers["location"])
        self.assertEqual(self.c.post("/api/accounts/mx/sync").status_code, 429)


if __name__ == "__main__":
    unittest.main()
