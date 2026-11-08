"""A lone UTF-16 surrogate in a JSON string is the caller's bad input.

JSON can spell half a surrogate pair (a client that cuts text through an
emoji sends exactly this) and it parses to a Python string no UTF-8 encoder
accepts, so the database driver, the password hasher and the response
renderer all raise. Each text door must answer 400, as it does for a NUL. Any
other encoding failure is the server's own fault and stays a logged 500.
"""

import os
import unittest
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from .util import _ensure_db

PW = "correct-horse-battery"
LONE = "\ud83d"


class LoneSurrogateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app,
                                 raise_server_exceptions=False)
        r = self.client.post("/api/signup", data={
            "email": f"sur-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def _refused(self, client, path, body):
        # json= would refuse to encode the string; the escape is what a
        # real client puts on the wire
        import json
        r = client.post(path, content=json.dumps(body),
                        headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400, f"{path}: {r.status_code} {r.text}")
        self.assertIn("invalid character", r.json()["detail"])

    def test_a_signed_out_invite_claim_is_refused(self):
        self._refused(TestClient(self.appmod.app,
                                 raise_server_exceptions=False),
                      "/api/invite/claim", {"token": LONE, "password": PW})

    def test_a_text_field_bound_to_sql_is_refused(self):
        self._refused(self.client, "/api/bills/delete", {"payee": LONE})
        self._refused(self.client, "/api/bills/hint", {"payee": LONE})
        self._refused(self.client, "/api/accounts/rename",
                      {"account_id": LONE, "name": LONE})
        self._refused(self.client, "/api/import/rollback", {"batch_id": LONE})

    def test_a_password_field_is_refused(self):
        self._refused(self.client, "/api/tokens",
                      {"name": "x", "password": LONE})
        self._refused(self.client, "/api/continuity/email",
                      {"email": "new@example.dev", "password": LONE})

    def test_a_settings_list_field_is_refused(self):
        self._refused(self.client, "/api/settings",
                      {"custom_buckets": [{"name": LONE, "monthly": 1}]})
        self._refused(self.client, "/api/settings",
                      {"email_recipients": [LONE + "@example.dev"]})

    def test_a_good_string_with_an_emoji_still_saves(self):
        r = self.client.post("/api/devices", json={
            "device_name": "phone \U0001F600", "platform": "android"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_another_encoding_failure_stays_a_server_error(self):
        # the handler is on the real app, so borrow it for a door that fails
        # the way a server fault does: nothing the caller sent is at fault
        handlers = self.appmod.app.exception_handlers
        scratch = FastAPI()
        scratch.exception_handlers.update(handlers)

        @scratch.get("/api/ascii")
        def _ascii():
            "café".encode("ascii")

        @scratch.get("/api/idna")
        def _idna():
            "a..b".encode("idna")

        c = TestClient(scratch, raise_server_exceptions=False)
        self.assertEqual(c.get("/api/ascii").status_code, 500)
        self.assertEqual(c.get("/api/idna").status_code, 500)


if __name__ == "__main__":
    unittest.main()
