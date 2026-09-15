"""The public "how do I get an account here" answer: the native app's
sign-in screen reads it before any login, so it must be reachable
unauthenticated, carry no secrets, and never point at a closed door."""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from oikonome.web.app import app

from .util import _ensure_db


class AccessDoorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        cls.client = TestClient(app)

    def setUp(self):
        # DEV_MODE opens the intake door unconditionally, and other suites flip
        # it on without flipping it back — pin it off so these door-closed
        # assertions don't depend on test order
        self._dev = patch("oikonome.web.app.DEV_MODE", False)
        self._dev.start()

    def tearDown(self):
        self._dev.stop()

    def test_self_host_has_no_door(self):
        with patch.dict(os.environ, {"OIKONOME_HOSTED": "", "OIKONOME_OPEN_SIGNUP": ""}):
            j = self.client.get("/api/access").json()
        self.assertFalse(j["hosted"])
        self.assertIsNone(j["signup_path"])
        self.assertIsNone(j["request_access_path"])
        self.assertIsNone(j["site_url"])

    def test_hosted_closed_points_at_the_site(self):
        with patch.dict(os.environ, {"OIKONOME_HOSTED": "1", "OIKONOME_OPEN_SIGNUP": "",
                                     "OIKONOME_SITE_URL": "https://example.org"}):
            j = self.client.get("/api/access").json()
        self.assertTrue(j["hosted"])
        self.assertIsNone(j["signup_path"])
        self.assertIsNone(j["request_access_path"])
        self.assertEqual(j["site_url"], "https://example.org")

    def test_hosted_closed_without_a_site_offers_no_door(self):
        with patch.dict(os.environ, {"OIKONOME_HOSTED": "1", "OIKONOME_OPEN_SIGNUP": "",
                                     "OIKONOME_SITE_URL": ""}):
            j = self.client.get("/api/access").json()
            page = self.client.get("/login").text
        self.assertIsNone(j["site_url"])
        self.assertNotIn("No account?", page)


    def test_hosted_open_signup_offers_signup(self):
        with patch.dict(os.environ, {"OIKONOME_HOSTED": "1", "OIKONOME_OPEN_SIGNUP": "1"}):
            j = self.client.get("/api/access").json()
            page = self.client.get("/login").text
        self.assertEqual(j["signup_path"], "/signup")
        self.assertIn("No account? Create one", page)
        self.assertNotIn("Request access", page)

    def test_a_demo_instance_never_advertises_signup(self):
        """The demo box 404s every account-creating door; advertising one
        would send the native sign-in's "Create one" at nothing."""
        with patch.dict(os.environ, {"OIKONOME_HOSTED": "1", "OIKONOME_OPEN_SIGNUP": "1",
                                     "OIKONOME_DEMO": "1"}):
            j = self.client.get("/api/access").json()
        self.assertFalse(j["signup_open"])
        self.assertIsNone(j["signup_path"])
        self.assertIsNone(j["request_access_path"])

    def test_hosted_closed_login_page_points_at_the_site(self):
        # both doors shut: the web login page must still offer the same
        # "no account" answer /api/access gives the native app — the site
        with patch.dict(os.environ, {"OIKONOME_HOSTED": "1", "OIKONOME_OPEN_SIGNUP": "",
                                     "OIKONOME_SITE_URL": "https://example.org"}):
            page = self.client.get("/login").text
        self.assertIn('href="https://example.org">No account? example.org', page)
        self.assertNotIn("Request access", page)
        self.assertNotIn("Create one", page)

    def test_self_host_login_page_has_no_door(self):
        with patch.dict(os.environ, {"OIKONOME_HOSTED": "", "OIKONOME_OPEN_SIGNUP": ""}):
            page = self.client.get("/login").text
        self.assertNotIn("No account?", page)

