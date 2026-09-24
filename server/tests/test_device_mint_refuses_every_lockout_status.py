"""A totally locked-out household mints no 90-day device token.

The mint-ticket exchange authenticates by itself and never reaches
`current_user`, so the account-standing gate has to be applied inside it.
Spelling the frozen statuses out there is a second list, and a second
list drifts: a household frozen under a status added later could walk
away holding a durable credential while every cookie of its was being
refused. So the mint reads the same map the request gate reads.

A lockout that leaves doors open is deliberately still mintable: those
doors are the way out from inside the app, the phone holds no cookie, and
the token is confined to exactly those paths while the status holds.
"""

import os
import unittest
import unittest.mock
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.jobs import unverified

from .util import _ensure_db

PASSWORD = "correct-horse-battery"


def _set_status(email: str, status: str) -> None:
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE tenants SET status=%s WHERE id = "
            "(SELECT tenant_id FROM users WHERE email=%s)", (status, email))
    finally:
        admin.close()


class DeviceMintStandingTests(unittest.TestCase):
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
        self.email = f"mintlock-{uuid.uuid4().hex[:8]}@example.dev"
        TestClient(self.app).post(
            "/api/signup", data={"email": self.email, "password": PASSWORD})

    def tearDown(self):
        _set_status(self.email, "active")

    def _ticket(self) -> str:
        bare = TestClient(self.app)
        r = bare.post("/api/login", data={"email": self.email,
                                          "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        ticket = r.json().get("mint_ticket")
        self.assertTrue(ticket)
        return ticket

    def _mint(self, ticket):
        naked = TestClient(self.app)
        naked.cookies.clear()
        return naked.post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "phone",
            "platform": "android"})

    def test_a_total_lockout_added_later_refuses_the_mint_too(self):
        """The gate is the shared map, so a status nobody thought about
        when this door was written is refused by it — with the map's own
        code and wording, not a second opinion."""
        added = "quarantined"
        patched = dict(self.appmod.LOCKOUT_STATUSES)
        patched[added] = (403, "this account is quarantined", ())
        with unittest.mock.patch.object(self.appmod, "LOCKOUT_STATUSES",
                                        patched):
            ticket = self._ticket()
            _set_status(self.email, added)
            r = self._mint(ticket)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("quarantined", r.text)

    def test_every_total_lockout_refuses_the_mint(self):
        """A lockout with no doors open — the operator's suspension, a
        scheduled deletion — leaves nothing to hold a credential for."""
        total = [s for s, v in self.appmod.LOCKOUT_STATUSES.items()
                 if not v[2]]
        self.assertTrue(total)
        for status in total:
            with self.subTest(status=status):
                ticket = self._ticket()
                _set_status(self.email, status)
                r = self._mint(ticket)
                self.assertEqual(r.status_code,
                                 self.appmod.LOCKOUT_STATUSES[status][0],
                                 r.text)
                _set_status(self.email, "active")

    def test_a_lockout_with_a_way_out_still_mints_the_phone_its_token(self):
        """The freeze for a never-confirmed address leaves the resend door
        open, and the phone can only knock on it with a device token — a
        token `current_user` then confines to that door alone."""
        ticket = self._ticket()
        _set_status(self.email, unverified.STATUS)
        r = self._mint(ticket)
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        api = TestClient(self.app)
        api.cookies.clear()
        head = {"Authorization": f"Bearer {token}"}
        self.assertEqual(api.get("/api/me", headers=head).status_code, 403)
        self.assertEqual(
            api.post("/api/verify-email/resend", headers=head).status_code,
            200)

    def test_a_household_in_good_standing_still_mints(self):
        r = self._mint(self._ticket())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["token"])


if __name__ == "__main__":
    unittest.main()
