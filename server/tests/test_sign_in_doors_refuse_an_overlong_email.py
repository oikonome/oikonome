"""A mailbox address is at most 254 characters. Every door that takes one
from an anonymous client refuses a longer field before it reaches a
limiter key, a query or a log line, so a client cannot choose how much
each failed attempt costs to remember."""
import unittest

from fastapi.testclient import TestClient

from oikonome.web import app as app_mod

from .util import make_db

LONG = "a" * 300 + "@example.test"


class OverlongEmailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_db().close()
        cls.client = TestClient(app_mod.app)

    def test_each_door_refuses_it(self):
        for path, data in (
                ("/api/login", {"email": LONG, "password": "x"}),
                ("/login", {"email": LONG, "password": "x"}),
                ("/forgot", {"email": LONG}),
                ("/unlock", {"email": LONG}),
                ("/api/signup", {"email": LONG, "password": "x" * 12})):
            r = self.client.post(path, data=data, follow_redirects=False)
            self.assertIn(r.status_code, (400, 422), (path, r.status_code))

    def test_an_ordinary_address_still_reaches_the_check(self):
        r = self.client.post("/api/login", data={
            "email": "nobody@example.test", "password": "wrong"},
            follow_redirects=False)
        self.assertNotIn(r.status_code, (422,))


if __name__ == "__main__":
    unittest.main()
