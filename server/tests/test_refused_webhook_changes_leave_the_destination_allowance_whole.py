"""A webhook create, URL change or re-enable that the step-up refuses
must not spend the account's hourly allowance of new destinations.

The clients post once, get "elevation required", show the step-up sheet
and post again; counted before the step-up, every successful change cost
two units and an owner who dismissed the sheet a few times was told "too
many webhook changes" for an hour without having added anything. Only a
request that has passed the step-up is counted. Each request comes from a
fresh client address, so the per-address limit never decides the result.
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"
RUNS = 25          # more than the hourly allowance of 20


class RefusedWebhookChangesLeaveTheAllowanceWhole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.auth import passwords
        from oikonome.web import netguard, security
        security._limiter._hits.clear()
        netguard._DNS_CACHE.clear()
        self.email = f"hookbudget-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, self.email)
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at, role) VALUES (%s, %s, %s, now(), 'owner')",
                (tid, self.email, passwords.hash_password(PW)))
        finally:
            admin.close()
        self.client = TestClient(self.app)
        r = self.client.post("/login", data={"email": self.email,
                                             "password": PW},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        resolve = mock.patch("oikonome.web.netguard._getaddrinfo",
                             lambda h: ["93.184.216.34"])
        resolve.start()
        self.addCleanup(resolve.stop)

    def _post(self, i: int, path: str, body: dict):
        from oikonome.web import security
        with mock.patch.object(security, "client_ip",
                               lambda req, i=i: f"203.0.113.{i % 250}"):
            return self.client.post(path, json=body)

    def _hook(self) -> dict:
        r = self._post(251, "/api/webhooks", {
            "url": "https://first.hooks.example/in", "events": ["*"],
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_creates_refused_at_the_step_up_spend_nothing(self):
        codes = {self._post(i, "/api/webhooks", {
            "url": f"https://h{i}.hooks.example/in",
            "events": ["*"]}).status_code for i in range(RUNS)}
        self.assertEqual(codes, {403})
        self._hook()

    def test_url_changes_and_re_enables_refused_at_the_step_up_spend_nothing(
            self):
        hook = self._hook()
        path = f"/api/webhooks/{hook['id']}"
        codes = set()
        for i in range(RUNS):
            codes.add(self._post(i, path, {
                "url": f"https://h{i}.hooks.example/in"}).status_code)
            codes.add(self._post(i, path, {"enabled": True}).status_code)
        self.assertEqual(codes, {403})
        r = self._post(252, path, {"url": "https://second.hooks.example/in",
                                   "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_allowance_still_stops_changes_that_passed_the_step_up(self):
        hook = self._hook()
        codes = [self._post(i, f"/api/webhooks/{hook['id']}", {
            "url": f"https://h{i}.hooks.example/in",
            "password": PW}).status_code for i in range(RUNS)]
        self.assertEqual(codes[0], 200)
        self.assertIn(429, codes)


if __name__ == "__main__":
    unittest.main()
