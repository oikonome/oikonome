"""A native app turns a completed login into a device token without ever
re-sending a cookie.

A cookie-only exchange authenticates and then fails to mint on iOS: the
platform exposes no Set-Cookie to the app, so its jar does not carry the
session to the very next request. The login response therefore also carries
a one-shot ticket, and what these tests protect is that the ticket works
exactly once, only for the account that earned it, only inside its window —
and that the cookie path still stands.
"""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import device_tokens
from oikonome.db import tenancy

from .util import _ensure_db

PASSWORD = "correct-horse-battery"


class MintTicketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app
        cls.email = f"mint-{uuid.uuid4().hex[:8]}@example.dev"
        c = TestClient(app)
        c.post("/api/signup", data={"email": cls.email,
                                    "password": PASSWORD})

    def _login(self):
        """A cookie-less client, the way the phone actually behaves."""
        bare = TestClient(self.app)
        r = bare.post("/api/login", data={"email": self.email,
                                          "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_login_hands_back_a_ticket_the_phone_can_spend(self):
        r = self._login()
        ticket = r.json().get("mint_ticket")
        self.assertTrue(ticket, "login response carried no mint ticket")
        # a brand-new client: no cookie jar, nothing but the ticket
        naked = TestClient(self.app)
        naked.cookies.clear()
        m = naked.post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "iPad", "platform": "ios"})
        self.assertEqual(m.status_code, 200, m.text)
        self.assertTrue(m.json()["token"].startswith(device_tokens.PREFIX))

    def test_the_ticket_is_single_use(self):
        ticket = self._login().json()["mint_ticket"]
        naked = TestClient(self.app)
        naked.cookies.clear()
        first = naked.post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "iPad", "platform": "ios"})
        self.assertEqual(first.status_code, 200, first.text)
        again = naked.post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "iPad again",
            "platform": "ios"})
        self.assertEqual(again.status_code, 401, again.text)

    def test_an_expired_ticket_is_refused(self):
        ticket = self._login().json()["mint_ticket"]
        conn = tenancy.control_connect()
        try:
            conn.execute(
                "UPDATE webauthn_challenges SET expires_at = now() - %s "
                "WHERE purpose = 'device-mint-ticket'",
                (dt.timedelta(minutes=1),))
            conn.commit()
        finally:
            conn.close()
        naked = TestClient(self.app)
        naked.cookies.clear()
        r = naked.post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "late", "platform": "ios"})
        self.assertEqual(r.status_code, 401, r.text)

    def test_garbage_and_foreign_tickets_are_refused(self):
        naked = TestClient(self.app)
        naked.cookies.clear()
        for bad in ("", "oikm-not-a-real-ticket", "pkstep-wrong-kind",
                    "x" * 64):
            r = naked.post("/api/devices", json={
                "mint_ticket": bad, "device_name": "nope", "platform": "ios"})
            self.assertIn(r.status_code, (401, 403), f"{bad!r}: {r.text}")

    def test_the_ticket_is_hashed_at_rest(self):
        """It is a bearer credential: a DB read inside the window must not
        yield something redeemable."""
        ticket = self._login().json()["mint_ticket"]
        conn = tenancy.control_connect()
        try:
            rows = conn.execute(
                "SELECT challenge FROM webauthn_challenges "
                "WHERE purpose = 'device-mint-ticket'").fetchall()
        finally:
            conn.close()
        stored = {r["challenge"] for r in rows}
        self.assertTrue(stored)
        self.assertNotIn(ticket, stored)

    def test_the_session_cookie_path_still_mints(self):
        """Browsers and every shipped client keep working unchanged."""
        c = TestClient(self.app)
        r = c.post("/api/login", data={"email": self.email,
                                       "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        m = c.post("/api/devices", json={"device_name": "cookie phone",
                                         "platform": "android"})
        self.assertEqual(m.status_code, 200, m.text)
        self.assertTrue(m.json()["token"].startswith(device_tokens.PREFIX))


class MintTicketRespectsStandingTests(unittest.TestCase):
    """A ticket authenticates the request by itself, so the gates every
    other write meets in current_user have to be met here instead — a
    device token is 90-day full-access persistence, exactly what an
    account in bad standing must not be able to plant."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app

    def _account(self):
        email = f"standing-{uuid.uuid4().hex[:8]}@example.dev"
        c = TestClient(self.app)
        c.post("/api/signup", data={"email": email, "password": PASSWORD})
        tid = c.get("/api/me").json()["tenant_id"]
        return email, tid

    def _ticket(self, email):
        bare = TestClient(self.app)
        r = bare.post("/api/login", data={"email": email,
                                          "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["mint_ticket"]

    def _set_status(self, tenant_id, status):
        # tenants is an operator table — the app role cannot write it
        conn = tenancy.admin_connect()
        try:
            conn.execute("UPDATE tenants SET status=%s WHERE id=%s",
                         (status, tenant_id))
            conn.commit()
        finally:
            conn.close()

    def _mint(self, ticket):
        naked = TestClient(self.app)
        naked.cookies.clear()
        return naked.post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "iPad",
            "platform": "ios"})

    def test_a_suspended_account_cannot_plant_a_device(self):
        email, tid = self._account()
        ticket = self._ticket(email)
        self._set_status(tid, "suspended")
        r = self._mint(ticket)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("suspended", r.text)

    def test_an_account_scheduled_for_deletion_cannot(self):
        email, tid = self._account()
        ticket = self._ticket(email)
        self._set_status(tid, "pending_delete")
        self.assertEqual(self._mint(ticket).status_code, 403)

    def test_a_refused_mint_leaves_the_ticket_spendable(self):
        """The redemption rides the same transaction as the mint, so a
        rejected attempt must not cost the phone its one ticket."""
        email, tid = self._account()
        ticket = self._ticket(email)
        self._set_status(tid, "suspended")
        self.assertEqual(self._mint(ticket).status_code, 403)
        self._set_status(tid, "active")
        good = self._mint(ticket)
        self.assertEqual(good.status_code, 200, good.text)
        self.assertTrue(good.json()["token"].startswith(device_tokens.PREFIX))

    def test_expired_tickets_are_swept_as_they_accumulate(self):
        """Only a phone ever spends one, so unspent rows must not pile up
        on an instance whose users all sign in from a browser."""
        email, _ = self._account()
        self._ticket(email)
        conn = tenancy.control_connect()
        try:
            conn.execute(
                "UPDATE webauthn_challenges SET expires_at = now() - %s "
                "WHERE purpose = 'device-mint-ticket'",
                (dt.timedelta(hours=1),))
            conn.commit()
            before = conn.execute(
                "SELECT count(*) AS n FROM webauthn_challenges "
                "WHERE purpose='device-mint-ticket'").fetchone()["n"]
        finally:
            conn.close()
        self.assertGreaterEqual(before, 1)
        self._ticket(email)          # the next login sweeps the dead rows
        conn = tenancy.control_connect()
        try:
            left = conn.execute(
                "SELECT count(*) AS n FROM webauthn_challenges "
                "WHERE purpose='device-mint-ticket' "
                "AND expires_at < now()").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(left, 0)


if __name__ == "__main__":
    unittest.main()
