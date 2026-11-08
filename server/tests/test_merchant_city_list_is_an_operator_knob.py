"""The merchant city list is an operator knob, not a household setting.

No client renders it, so publishing it on the settings view and accepting
it on the save only offered a door nobody could see: a raw POST could
rewrite how every merchant name in the household is trimmed. It is set
where the operator works (the stored tenant settings), the settings door
neither shows nor takes it, and the nightly merchant pass still obeys a
value that is already stored.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget, merchant_dedup

from .util import _ensure_db, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"
KEYS = ("merchant_strip_cities", "merchant_cities_seen")


class MerchantCityListIsAnOperatorKnobTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.owner = TestClient(app)
        cls.owner.post("/api/signup", data={
            "email": f"cities-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        cls.owner.post("/api/auth/elevate", json={"password": PW})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cfg = budget.load_config(conn)
            cfg["merchant_strip_cities"] = ["Elmhaven"]
            cfg["merchant_cities_seen"] = ["Stone Creek"]
            budget.save_config(conn, cfg)
        finally:
            conn.close()

    def _stored(self) -> dict:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def test_the_settings_view_does_not_carry_the_city_list(self):
        view = self.owner.get("/api/settings").json()
        for k in KEYS:
            self.assertNotIn(k, view)

    def test_a_settings_save_does_not_store_the_city_list(self):
        r = self.owner.post("/api/settings", json={
            "merchant_strip_cities": ["Riverbend", "Oakfield"]})
        self.assertEqual(r.status_code, 200, r.text)
        for k in KEYS:
            self.assertNotIn(k, r.json())
        self.assertEqual(self._stored().get("merchant_strip_cities"),
                         ["Elmhaven"])
        # an empty value used to clear it; it must not either
        r = self.owner.post("/api/settings",
                            json={"merchant_strip_cities": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored().get("merchant_strip_cities"),
                         ["Elmhaven"])

    def test_the_nightly_pass_still_honours_a_stored_city_list(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertIn("Elmhaven", merchant_dedup.cities_for(conn))
            add_txn(conn, "2025-07-03", 30, "QUILLO GRILL ELMHAVEN",
                    account="card")
            merchant_dedup.apply(conn)
            row = conn.execute(
                "SELECT canonical FROM merchant_canonical "
                "WHERE raw_merchant=%s",
                ("QUILLO GRILL ELMHAVEN",)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertNotIn("Elmhaven", row["canonical"])


if __name__ == "__main__":
    unittest.main()
