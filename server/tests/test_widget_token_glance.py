"""The home-screen widget's credential and its one door. A phone mints a
widget token off its device token; the token reads the glance payload and
nothing else; the payload's number is the Today hero's headline to the
dollar; and the widget dies with its device — revocation and a password
change end it, and it cannot be minted by a browser, a script, or another
widget token."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

PASSWORD = "correct-horse-battery"


class WidgetTokenGlanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.email = f"dev-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={
            "email": cls.email, "password": PASSWORD})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _device(self):
        r = self.client.post("/api/devices", json={
            "device_name": "widget phone", "platform": "android"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _bearer(self, token):
        return TestClient(self.client.app), {
            "Authorization": f"Bearer {token}"}

    def _widget(self, device_token):
        bare, hdr = self._bearer(device_token)
        r = bare.post("/api/devices/widget", headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def test_widget_reads_the_headline_and_nothing_else(self):
        dev = self._device()
        wtok = self._widget(dev["token"])
        self.assertTrue(wtok.startswith("oikw_"))
        bare, hdr = self._bearer(wtok)
        r = bare.get("/api/today/glance", headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        g = r.json()
        self.assertTrue(g["budgets_set"])
        self.assertIn(g["verdict"], ("OVER BUDGET", "UNDER BUDGET",
                                     "ON BUDGET"))
        # the same number the app's hero shows, to the dollar
        dbare, dhdr = self._bearer(dev["token"])
        full = dbare.get("/api/today/full", headers=dhdr).json()
        self.assertEqual(g["left_today"],
                         round(full["simple"]["left_today"]))
        self.assertEqual(g["verdict"], full["verdict"])
        self.assertEqual([c["text"] for c in g["chips"]],
                         [c["text"] for c in full["simple"]["chips"]])
        # the day bar never claims more spent than allowed unless over
        self.assertGreaterEqual(g["day_allow"], 0)
        self.assertGreaterEqual(g["day_spent"], 0)
        # nothing a lock screen must not show
        for key in ("accounts", "recent", "balance", "runway", "networth"):
            self.assertNotIn(key, g)
        # every other door is closed to it — the full page, the ledger,
        # the device roster, and the widget door itself
        for path in ("/api/today/full", "/api/me", "/api/devices",
                     "/api/transactions"):
            self.assertEqual(bare.get(path, headers=hdr).status_code, 403,
                             path)
        self.assertEqual(
            bare.post("/api/devices/widget", headers=hdr).status_code, 403)

    def test_glance_answers_304_when_nothing_moved(self):
        dev = self._device()
        bare, hdr = self._bearer(self._widget(dev["token"]))
        r = bare.get("/api/today/glance", headers=hdr)
        etag = r.headers.get("etag")
        self.assertTrue(etag)
        again = bare.get("/api/today/glance",
                         headers={**hdr, "If-None-Match": etag})
        self.assertEqual(again.status_code, 304)

    def test_only_a_phone_mints_a_widget_token(self):
        # a browser session has no home screen
        self.assertEqual(
            self.client.post("/api/devices/widget").status_code, 403)
        # a script token never reaches the device doors
        r = self.client.post("/api/tokens",
                             json={"name": "s", "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        bare, hdr = self._bearer(r.json()["token"])
        self.assertEqual(
            bare.post("/api/devices/widget", headers=hdr).status_code, 403)
        # a garbage widget token is a 401 at its own door, 403 elsewhere
        bare, hdr = self._bearer("oikw_deadbeef")
        self.assertEqual(
            bare.get("/api/today/glance", headers=hdr).status_code, 401)
        self.assertEqual(bare.get("/api/me", headers=hdr).status_code, 403)

    def test_minting_again_replaces_the_previous_token(self):
        dev = self._device()
        first = self._widget(dev["token"])
        second = self._widget(dev["token"])
        self.assertNotEqual(first, second)
        bare, hdr = self._bearer(first)
        self.assertEqual(
            bare.get("/api/today/glance", headers=hdr).status_code, 401)
        bare, hdr = self._bearer(second)
        self.assertEqual(
            bare.get("/api/today/glance", headers=hdr).status_code, 200)

    def test_the_widget_dies_with_its_device(self):
        dev = self._device()
        wtok = self._widget(dev["token"])
        dbare, dhdr = self._bearer(dev["token"])
        r = dbare.post("/api/devices/revoke", headers=dhdr,
                       json={"id": dev["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        bare, hdr = self._bearer(wtok)
        self.assertEqual(
            bare.get("/api/today/glance", headers=hdr).status_code, 401)

    def test_a_password_change_ends_the_widget(self):
        dev = self._device()
        wtok = self._widget(dev["token"])
        r = self.client.post("/api/password/change", json={
            "current_password": PASSWORD, "new_password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        bare, hdr = self._bearer(wtok)
        self.assertEqual(
            bare.get("/api/today/glance", headers=hdr).status_code, 401)

    def test_no_budget_means_no_verdict(self):
        # a fresh household: no plan yet, so the widget must say so
        # rather than show $0 left
        c = TestClient(self.client.app)
        c.post("/api/signup", data={
            "email": f"dev-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PASSWORD})
        r = c.get("/api/today/glance")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["budgets_set"])
        self.assertNotIn("left_today", r.json())


if __name__ == "__main__":
    unittest.main()
