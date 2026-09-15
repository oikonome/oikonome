"""The demo lockdown's remaining /api data doors.

A demo tenant (config `demo_mode: true`) stays FULLY EDITABLE — budgets,
buckets, goals, bills, matching all save — but every DATA DOOR (in or
out: aggregator credentials, import rollback, the sealed connections
bundle, live pulls, mail sending) and credential wipe must 403. The DOORS
table below enumerates every such door.

/settings is the subtle one: it is both the demo's main editing surface
and the place SMTP/LLM/recipient door keys live, so a partial refusal —
door keys 403, budget keys save — is not enough. The demo is READ-ONLY
wholesale: its credentials are shared and printed publicly, so any write is
one visitor reconfiguring the instance for every visitor after them. Every
/settings write 403s.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db

PW = "correct-horse-battery"


def _fresh_client(app, demo: bool):
    """Signed-in client; optionally demo-locked."""
    client = TestClient(app)
    email = f"dd-{uuid.uuid4().hex[:10]}@example.dev"
    r = client.post("/api/signup", data={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    tid = client.get("/api/me").json()["tenant_id"]
    if demo:
        from oikonome.db import tenancy
        from oikonome.engine import budget
        from oikonome.web import demoguard
        conn = tenancy.tenant_connect(tid)
        try:
            cfg = budget.load_config(conn)
            cfg["demo_mode"] = True
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        demoguard._cache.clear()
    return client, tid


# Every data door, with a minimal valid-enough body. deny() sits at
# the top of each handler, so bogus ids/keys still exercise the guard
# (403 must beat the handler's own 400/404).
DOORS = [
    ("POST", "/api/import/rollback", {"batch_id": "x"}),
    ("POST", "/api/accounts/coinbase/link",
     {"label": "cb", "key_name": "k", "private_key": "p"}),
    ("POST", "/api/connections/export", {"passphrase": "hunter2hunter2"}),
    ("POST", "/api/jobs/sync", None),
    ("POST", "/api/jobs/sync/start", None),
    ("POST", "/api/jobs/email", None),
    ("POST", "/api/connections/nope/disconnect", None),
    ("DELETE", "/api/accounts/plaid/keys", None),
    ("DELETE", "/api/accounts/mx/keys", None),
]


class DemoDataDoorTests(unittest.TestCase):

    def setUp(self):
        os.environ["OIKONOME_DEV"] = "1"
        os.environ.pop("OIKONOME_HOSTED", None)
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        self.app = appmod.app

    def test_doors_403_on_demo(self):
        client, _ = _fresh_client(self.app, demo=True)
        for method, path, body in DOORS:
            r = (client.request(method, path, json=body)
                 if body is not None else client.request(method, path))
            self.assertEqual(r.status_code, 403,
                             f"{method} {path}: {r.status_code} {r.text}")
            self.assertIn("demo", r.json()["detail"].lower(), path)

    def test_settings_door_keys_403_on_demo(self):
        """Door keys are still refused — now as part of the blanket rule."""
        client, _ = _fresh_client(self.app, demo=True)
        for body in ({"smtp_host": "mail.example.com", "smtp_port": 587},
                     {"llm_url": "http://127.0.0.1:11434/v1"},
                     {"email_recipients": "a@example.dev"},
                     {"food_monthly": 555, "smtp_password": "s3cret"}):
            r = client.post("/api/settings", json=body)
            self.assertEqual(r.status_code, 403,
                             f"{body}: {r.status_code} {r.text}")
            self.assertIn("demo", r.json()["detail"].lower())

    def test_settings_is_read_only_on_demo(self):
        """The demo is READ-ONLY, not 'editable except the door keys':
        even the plainest budget key is refused, and nothing is
        written."""
        client, _ = _fresh_client(self.app, demo=True)
        before = client.get("/api/settings").json()
        for body in ({"food_monthly": 900}, {"theme": "dark"},
                     {"other_monthly": 1200},
                     {"feedback_enabled": True}):
            r = client.post("/api/settings", json=body)
            self.assertEqual(r.status_code, 403, f"{body}: {r.text}")
        after = client.get("/api/settings").json()
        self.assertEqual(after["food_monthly"], before["food_monthly"])
        self.assertEqual(after["theme"], before["theme"])

    def test_demo_hides_sessions_and_forces_feedback_off(self):
        """The demo login is shared, so the session list is a roster of
        strangers (hidden), and feedback is off and unswitchable."""
        client, _ = _fresh_client(self.app, demo=True)
        s = client.get("/api/sessions")
        self.assertEqual(s.status_code, 200, s.text)
        self.assertEqual(s.json()["sessions"], [])
        self.assertTrue(s.json().get("hidden"))
        got = client.get("/api/settings").json()
        self.assertIs(got["feedback_enabled"], False)
        self.assertTrue(got.get("demo_read_only"))
        self.assertFalse(client.get("/api/testing").json()["enabled"])
        # and it cannot be switched back on
        self.assertEqual(
            client.post("/api/settings",
                        json={"feedback_enabled": True}).status_code, 403)

    def test_non_demo_keeps_sessions_and_feedback(self):
        """Positive control: an ordinary tenant is unaffected."""
        client, _ = _fresh_client(self.app, demo=False)
        s = client.get("/api/sessions").json()
        self.assertGreaterEqual(len(s["sessions"]), 1)
        self.assertNotIn("hidden", s)
        got = client.get("/api/settings").json()
        self.assertNotEqual(got.get("feedback_enabled"), False)
        self.assertIsNone(got.get("demo_read_only"))
        self.assertEqual(
            client.post("/api/settings",
                        json={"food_monthly": 777}).status_code, 200)

    def test_non_demo_doors_still_work(self):
        """Positive controls: the same routes on an ordinary tenant run
        their handlers (2xx, or the handler's own 404 — never the demo
        403)."""
        client, _ = _fresh_client(self.app, demo=False)
        r = client.delete("/api/accounts/plaid/keys")
        self.assertEqual(r.status_code, 200, r.text)
        r = client.delete("/api/accounts/mx/keys")
        self.assertEqual(r.status_code, 200, r.text)
        r = client.post("/api/import/rollback", json={"batch_id": "nope"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["deleted"], 0)
        # handler runs past the guard and 404s on the bogus id
        r = client.post("/api/connections/nope/disconnect")
        self.assertEqual(r.status_code, 404, r.text)
        # settings door keys save on a normal self-hosted instance
        r = client.post("/api/settings",
                        json={"email_recipients": "me@example.dev"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(client.get("/api/settings").json()
                         .get("email_recipients"), ["me@example.dev"])


class JinjaDataDoorsTests(unittest.TestCase):
    """The legacy Jinja twins of the /api data doors carry the same demo
    guard. Without it a demo session cookie can curl POST /import and land
    real rows in the synthetic ledger."""

    def setUp(self):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        self.app = appmod.app

    def test_data_doors_403_on_demo(self):
        client, _ = _fresh_client(self.app, demo=True)
        doors = [
            ("/import", {"account_id": "x", "amount_sign": "bank"},
             {"file": ("t.csv", b"date,amount\n2026-01-01,1\n",
                       "text/csv")}),
            ("/import/mapped", {"token": "x", "date_col": "a",
                                "amount_col": "b", "name_col": "c"}, None),
            ("/import/rollback", {"batch_id": "x"}, None),
            ("/accounts/simplefin", {"simplefin_token": "x"}, None),
            ("/accounts/plaid/link", {"item_id": ""}, None),
            ("/accounts/sync", {"item_id": "x"}, None),
        ]
        for path, data, files in doors:
            r = client.post(path, data=data, files=files,
                            follow_redirects=False)
            self.assertEqual(r.status_code, 403, f"{path}: {r.status_code}")
            self.assertIn("demo", r.text.lower(), path)

    def test_legacy_settings_post_blocked_on_demo(self):
        """Legacy POST /settings is a write door too: the demo lockdown is
        broader than the aggregator doors."""
        client, _ = _fresh_client(self.app, demo=True)
        r = client.post("/settings", data={"food_monthly": "900",
                                           "other_monthly": "1500"},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertIn("demo", r.text.lower())


if __name__ == "__main__":
    unittest.main()
