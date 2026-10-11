"""A household the billing add-on froze stays frozen while the add-on is
not loaded.

The add-on freezes households under statuses only it names. When a hosted
instance's add-on fails to load, the product runs under a fail-closed
stand-in that does not name them either, so every standing check would
read such a status as "not locked out" and hand the household its whole
ledger back for as long as the add-on is down — and let its members mint
90-day device tokens that outlive the outage. Every check asks one helper,
which falls back to the stand-in's judgement: such a status is locked.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome import ext
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"
ADDON_ONLY = "addon_frozen_example"


class AddOnFrozenHouseholdsStayFrozen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        self.email = f"addonfrozen-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.client.post("/api/signup", data={"email": self.email,
                                                  "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.ticket = r.json().get("mint_ticket")
        admin = tenancy.admin_connect()
        try:
            self.tid = admin.execute(
                "SELECT tenant_id FROM users WHERE email=%s",
                (self.email,)).fetchone()["tenant_id"]
            admin.execute("UPDATE tenants SET status=%s WHERE id=%s",
                          (ADDON_ONLY, self.tid))
        finally:
            admin.close()
        self.addCleanup(ext.set_gate, None)
        ext.set_gate(ext._FailClosedGate())

    def test_reads_are_refused(self):
        r = self.client.get("/api/me")
        self.assertEqual(r.status_code, 503, r.text)

    def test_sign_out_stays_open(self):
        self.assertEqual(self.client.post("/api/logout").status_code, 200)

    def test_login_page_does_not_wave_the_session_into_the_app(self):
        r = self.client.get("/login", follow_redirects=False)
        self.assertEqual(r.status_code, 200)

    def test_no_device_token_is_minted(self):
        r = TestClient(self.app).post("/api/devices", json={
            "mint_ticket": self.ticket, "device_name": "phone",
            "platform": "android"})
        self.assertEqual(r.status_code, 503, r.text)

    def test_the_write_refusal_for_emailed_actions(self):
        refusal = self.appmod.write_refusal({
            "tenant_id": self.tid, "tenant_status": ADDON_ONLY,
            "has_totp": True, "second_factor_waived": True})
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal[0], 503)

    def test_a_status_the_product_names_is_judged_as_before(self):
        self.assertEqual(self.appmod.lockout_for("suspended")[0], 403)
        self.assertIsNone(self.appmod.lockout_for("active"))

    def test_with_the_add_on_loaded_an_unnamed_status_passes(self):
        ext.set_gate(ext.Gate())
        self.assertIsNone(self.appmod.lockout_for(ADDON_ONLY))


if __name__ == "__main__":
    unittest.main()
