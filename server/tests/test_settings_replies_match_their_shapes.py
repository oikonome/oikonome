"""The settings doors' replies match the shape they document
(web/schemas/settings.py) for every reader, and no reader is sent a key
the exposure table keeps from them.

The settings read is role-dependent: web/settings_exposure.py decides key
by key what a viewer, a member and the owner get. The conformance hook
(tests/api_conformance.py) validates each reply against the one documented
shape; this module reads it as all three roles with a household that fills
it (a relay with a user, recipients, a schedule, a goal, a scenario), and
holds each reply to the table: every key a reader is sent is one the table
lets that role read.
"""

from __future__ import annotations

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import settings_exposure as se

from . import api_conformance
from .util import _ensure_db, seed_accounts, write_config

PW = "correct-horse-battery"


def _join(owner, role):
    inv = owner.post("/api/invites", json={"label": role, "role": role,
                                           "password": PW}).json()
    token = inv["url"].rsplit("token=", 1)[1]
    import oikonome.web.app as appmod
    c = TestClient(appmod.app)
    r = c.post("/api/invite/claim", json={
        "token": token, "email": f"{role}-{uuid.uuid4().hex[:8]}@example.dev",
        "password": "family-member-pass"})
    assert r.status_code == 200, r.text
    assert c.get("/api/me").json()["role"] == role
    return c


def allowed_for(role: str) -> set[str]:
    """Every key the exposure table lets `role` be sent."""
    rank = se._rank(role)
    out = set()
    for k, e in se.TABLE.items():
        if e.read == "never":
            continue
        if rank >= se._ROLE_RANK[e.read]:
            out.add(k)
        elif e.below == "set":
            out.add(f"{k}_set")
    return out


class SettingsShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.owner = TestClient(appmod.app)
        cls.email = f"settings-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.owner.post("/api/signup", data={"email": cls.email,
                                                "password": PW})
        assert r.status_code == 200, r.text
        tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(
                conn, smtp_host="smtp.relay.example", smtp_port=587,
                smtp_user="relay-user", smtp_from="budget@household.example",
                email_recipients=[cls.email],
                email_schedule={"daily": {"on": True, "hour": 7},
                                "monthly": {"on": True, "hour": 8}},
                savings_goals=[{"name": "Trip", "target": 1000,
                                "tokens": ["trip"], "monthly_plan": 100}],
                income_scenarios=[{"name": "Base", "take_home": 4000,
                                   "cadence": "monthly"}],
                custom_buckets=[{"name": "Pets", "parent": "other",
                                 "monthly": 80, "merchants": ["Chewy"]}])
        finally:
            conn.close()
        cls.member = _join(cls.owner, "member")
        cls.viewer = _join(cls.owner, "viewer")

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _ok(self, client, method, path, **kw):
        before = sum(api_conformance.validated().values())
        r = client.request(method, "/api" + path, **kw)
        self.assertEqual(r.status_code, 200, f"{method} {path}: {r.text}")
        self.assertGreater(sum(api_conformance.validated().values()), before,
                           f"{method} {path}: the reply was not validated")
        return r.json()

    def test_each_role_reads_a_valid_document_within_its_rows(self):
        for role, client in (("owner", self.owner), ("member", self.member),
                             ("viewer", self.viewer)):
            with self.subTest(role=role):
                view = self._ok(client, "GET", "/settings")
                beyond = set(view) - allowed_for(role)
                self.assertEqual(beyond, set(),
                                 f"a {role} is sent keys the exposure "
                                 f"table keeps from them: {sorted(beyond)}")
        # the household's setup is the owner's alone
        from .test_settings_exposure_table import OWNER_SETUP
        for role, client in (("member", self.member),
                             ("viewer", self.viewer)):
            with self.subTest(role=role):
                view = self._ok(client, "GET", "/settings")
                self.assertEqual(sorted(set(view) & OWNER_SETUP), [])
                self.assertIn("timezone", view)
        owner_view = self._ok(self.owner, "GET", "/settings")
        self.assertEqual(sorted(OWNER_SETUP - set(owner_view)), [])
        self.assertIn("smtp_user", self._ok(self.owner, "GET", "/settings"))
        self.assertNotIn("smtp_user",
                         self._ok(self.viewer, "GET", "/settings"))

    def test_a_save_answers_with_the_document(self):
        view = self._ok(self.owner, "GET", "/settings")
        saved = self._ok(self.owner, "POST", "/settings",
                         json={"food_monthly": 650,
                               "list_versions": view["list_versions"]})
        self.assertEqual(saved["food_monthly"], 650)
        mine = self._ok(self.member, "POST", "/settings",
                        json={"theme": "dark"})
        self.assertEqual(set(mine) - allowed_for("member"), set())
        self.assertNotIn("email_recipients", mine)


if __name__ == "__main__":
    unittest.main()
