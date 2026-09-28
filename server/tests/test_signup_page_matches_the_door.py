"""The signup PAGE must describe the door it stands in front of.

`/api/access` and the rendered page read the same switch. With
OIKONOME_OPEN_SIGNUP set the page must not claim to be invite-only or
demand a code; without it, it must say exactly "Signups are invite-only
on this instance." and ask for the code. A door that is open while its
sign says closed is only visible in a browser, because only a browser
renders the page — so both directions are pinned here."""

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from .util import _ensure_db


class SignupPageMatchesTheDoorTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _page(self, **env):
        base = {"OIKONOME_HOSTED": "1"}
        base.update(env)
        with mock.patch.dict(os.environ, base, clear=False):
            if "OIKONOME_OPEN_SIGNUP" not in env:
                os.environ.pop("OIKONOME_OPEN_SIGNUP", None)
            from oikonome.web.app import app
            return TestClient(app).get("/signup")

    def test_open_signup_does_not_ask_for_an_invite(self):
        r = self._page(OIKONOME_OPEN_SIGNUP="1")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("invite-only", r.text)
        self.assertNotIn('name="invite"', r.text)
        # the form is still a form
        self.assertIn('name="email"', r.text)
        self.assertIn('name="password"', r.text)

    def test_open_signup_still_carries_an_invite_that_was_supplied(self):
        """A link minted before signup opened must not stop working."""
        with mock.patch.dict(os.environ,
                             {"OIKONOME_HOSTED": "1",
                              "OIKONOME_OPEN_SIGNUP": "1"}, clear=False):
            from oikonome.web.app import app
            r = TestClient(app).get("/signup?invite=abc123")
        self.assertIn('name="invite"', r.text)
        self.assertIn("abc123", r.text)

    def test_closed_signup_asks_for_an_invite(self):
        r = self._page()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Signups are invite-only on this instance.", r.text)
        self.assertIn('name="invite"', r.text)
