"""Open signup: invites still count, and a squatted address is taken
back through the mailbox, never through signup.

An invite presented under OIKONOME_OPEN_SIGNUP is honoured: the switch
decides whether signing up WITHOUT one is allowed, not whether an
invite's own options count. A taken address answers 409 whether or not it
is verified — any immediate reclaim on signup is a takeover of whatever
that account holds by anyone who knows the address, before any mailbox
proof (the wipe variant would unlink an existing household's banks). An unverified taken address parks the signup behind a mailed
link instead — the click is the proof, and only then does the reclaim
run (test_signup_reclaim_needs_the_mailbox_first).
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from .util import _ensure_db

PW = "correct-horse-battery"


class OpenSignupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                                 "OIKONOME_OPEN_SIGNUP": "1"})
        self._env.start()
        self._dev = mock.patch.object(self.appmod, "DEV_MODE", False)
        self._dev.start()
        self._mail = mock.patch.object(self.appmod, "_deliver_verification")
        self._mail.start()
        from oikonome.web import security
        security._limiter._hits.clear()

    def tearDown(self):
        self._mail.stop(); self._dev.stop(); self._env.stop()

    def _signup(self, email, invite=""):
        return TestClient(self.appmod.app).post(
            "/api/signup", data={"email": email, "password": PW,
                                 "invite": invite})


    def test_no_ladder_configured_never_gates_a_signup(self):
        """A self-hosted install must not inherit a hosted-only ceiling,
        and an operator who never configured the ladder has no cap."""
        r = self._signup(f"nocap-{uuid.uuid4().hex[:8]}@x.dev")
        self.assertEqual(r.status_code, 200, r.text)
