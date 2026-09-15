"""The default home page is a closed vocabulary: both clients redirect to
it blind at launch, so an arbitrary route would strand the app on a 404
every open. Today is stored as absence."""

import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db


class HomePageSettingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.c = TestClient(app)
        cls.c.post("/api/signup", data={
            "email": f"home-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def test_known_pages_round_trip_and_today_is_absence(self):
        r = self.c.post("/api/settings", json={"home_web": "/bills",
                                               "home_mobile": "accounts"})
        self.assertEqual(r.status_code, 200, r.text)
        v = self.c.get("/api/settings").json()
        self.assertEqual(v["home_web"], "/bills")
        self.assertEqual(v["home_mobile"], "accounts")
        r = self.c.post("/api/settings", json={"home_web": "/",
                                               "home_mobile": "index"})
        self.assertEqual(r.status_code, 200, r.text)
        v = self.c.get("/api/settings").json()
        self.assertFalse(v.get("home_web"))
        self.assertFalse(v.get("home_mobile"))

    def test_unknown_page_is_refused(self):
        for body in ({"home_web": "/admin"}, {"home_web": "javascript:x"},
                     {"home_mobile": "settings"}, {"home_web": [1]}):
            r = self.c.post("/api/settings", json=body)
            self.assertEqual(r.status_code, 400, (body, r.text))


if __name__ == "__main__":
    unittest.main()
