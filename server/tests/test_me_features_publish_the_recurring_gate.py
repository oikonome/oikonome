"""/api/me publishes whether the bank's recurring-streams cross-check is
allowed on this household's plan.

The nightly pass asks the gate before calling that billed product, and a
household whose plan does not include it never gets its proposals. The
Settings toggle for it must then be hidden, or it is a switch that does
nothing; the clients can only hide it if /api/me says so, under the same
feature name the worker asks the gate about.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome import ext

from .util import _ensure_db


class MeFeaturesPublishTheRecurringGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"feat-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text

    def _features(self, allowed):
        g = ext.gate._resolve()
        with mock.patch.object(g, "billing_enabled", return_value=True), \
                mock.patch.object(g, "effective_tier", return_value="basic"), \
                mock.patch.object(g, "tier_allows",
                                  side_effect=lambda conn, f: f in allowed):
            return self.client.get("/api/me").json()["features"]

    def test_a_plan_without_it_says_so(self):
        self.assertIs(self._features({"enrich"})["plaid_recurring"], False)

    def test_a_plan_with_it_says_so(self):
        self.assertIs(self._features({"plaid_recurring"})["plaid_recurring"],
                      True)


if __name__ == "__main__":
    unittest.main()
