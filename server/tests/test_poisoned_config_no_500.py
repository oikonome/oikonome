"""A restored/hand-edited config must never 500 a read endpoint.

The Settings-save path validates every value it stores (ss_estimates keys as
ints in [62,70], manual_assets values through _num()). The restore config
merge does NOT — a backup ZIP's tenant_settings.config is merged in largely
as-is. So a hand-edited or corrupted backup can plant a non-integer
ss_estimates key or a non-numeric manual_assets value, and the next page load
would crash the endpoint for the whole household with no UI path to clear the
bad key. Reads must tolerate a poisoned config: drop the bad entry, render the
rest.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db, seed_accounts, write_config


def _client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


class PoisonedConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _client()
        cls.client.post("/api/signup", data={
            "email": f"poison-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # what a poisoned restore leaves behind — written straight into
            # config, exactly as restore's merge would, bypassing the
            # Settings-save validators
            with budget.config_txn(conn) as cfg:
                cfg["birthdate"] = "1975-03-04"
                cfg["ss_estimates"] = {"abc": 1000, "67": 3000}
                cfg["manual_assets"] = [
                    {"name": "House", "kind": "real_estate", "value": "abc"},
                    {"name": "Car", "kind": "vehicle", "value": 12000},
                    "not-even-a-dict",
                ]
        finally:
            conn.close()

    def test_retirement_survives_a_non_integer_ss_estimates_key(self):
        r = self.client.get("/api/retirement")
        self.assertEqual(r.status_code, 200, r.text)

    def test_networth_survives_a_non_numeric_manual_asset(self):
        r = self.client.get("/api/reports/networth")
        self.assertEqual(r.status_code, 200, r.text)
        # the good asset still counts; the poisoned ones are dropped, not
        # allowed to zero out or crash the whole report
        names = [p.get("name") for p in r.json().get("property_items", [])]
        self.assertIn("Car", names)
        self.assertNotIn("House", names)


if __name__ == "__main__":
    unittest.main()
