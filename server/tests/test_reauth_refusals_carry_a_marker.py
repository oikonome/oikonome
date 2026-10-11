"""A 401 that refuses a credential on a form carries a stable marker; a
401 for a session that is gone does not.

Both are 401. A client that treats the first kind as a lost session signs
the person out and wipes the screen holding the message they needed (a
wrong password, a spent one-time code). Clients used to tell the two apart
by wording, which matches anything that happens to contain the same words.
The marker is `"error": "reauth"` beside the unchanged `detail`.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db

PW = "correct-horse-battery"


class ReauthMarker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        self.email = f"reauth-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.client.post("/api/signup",
                             data={"email": self.email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_wrong_password_on_a_form_is_marked(self):
        r = self.client.post("/api/password/change", json={
            "current_password": "not-the-password",
            "new_password": "another-correct-horse-battery"})
        self.assertEqual(401, r.status_code, r.text)
        self.assertEqual("reauth", r.json().get("error"))
        self.assertEqual("wrong password", r.json()["detail"])

    def test_a_failed_sign_in_is_marked(self):
        r = TestClient(self.app).post("/api/login", data={
            "email": self.email, "password": "not-the-password"})
        self.assertEqual(401, r.status_code, r.text)
        self.assertEqual("reauth", r.json().get("error"))
        self.assertEqual("bad credentials", r.json()["detail"])

    def test_a_session_that_is_gone_is_not(self):
        r = TestClient(self.app).get("/api/me")
        self.assertEqual(401, r.status_code, r.text)
        self.assertNotIn("error", r.json())
        r = TestClient(self.app).get(
            "/api/me", headers={"Authorization": "Bearer nope-" + "x" * 30})
        self.assertEqual(401, r.status_code, r.text)
        self.assertNotIn("error", r.json())


if __name__ == "__main__":
    unittest.main()
