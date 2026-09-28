"""A refused password change must leave the password unchanged.

The rotation is one act: the new hash, and the eviction of everything the
old password could still reach — other sessions, passkeys, script tokens,
device tokens, push subscriptions, unspent reset links, pending invites.
The passkey eviction can still REFUSE at its own lock, because a key named
by the caller's proof may be deleted between the pre-flight check and that
lock, and refusing is right: keeping a key that no longer exists would
delete the account's last factor and call it success.

The whole rotation is one transaction, so the answer is true either way.
On an autocommit connection that refusal would land AFTER the password had
already changed: the caller told 403 while their password was in fact new,
their other sessions gone, and their tokens, devices and pending invites
still live — a rotation reported as a failure that had already begun,
leaving exactly the persistence the rotation exists to kill.
"""

import unittest
import uuid
from unittest import mock

from fastapi.exceptions import HTTPException
from fastapi.testclient import TestClient

from .util import _ensure_db

PW = "correct-horse-battery"
NEW_PW = "a-brand-new-passphrase"


class RotationIsAtomicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.email = f"rot-{uuid.uuid4().hex[:8]}@example.dev"
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/api/signup",
                             data={"email": self.email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def _signs_in_with(self, password: str) -> bool:
        c = TestClient(self.appmod.app)
        return c.post("/api/login", data={"email": self.email,
                                          "password": password}
                      ).status_code == 200

    def test_a_refused_eviction_leaves_the_old_password_working(self):
        """The eviction's in-lock refusal is the one failure that can
        happen after the password write is issued — it must take the write
        with it."""
        other = TestClient(self.appmod.app)
        self.assertEqual(
            other.post("/api/login",
                       data={"email": self.email, "password": PW}
                       ).status_code, 200)

        def refuse(*a, **kw):
            raise HTTPException(403, detail={"error": "elevation_required",
                                             "fresh": False})

        with mock.patch.object(self.appmod, "_evict_passkeys", refuse):
            r = self.client.post("/api/password/change", json={
                "current_password": PW, "new_password": NEW_PW})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(self._signs_in_with(NEW_PW),
                         "a refused rotation must not have changed the "
                         "password it says it refused to change")
        self.assertTrue(self._signs_in_with(PW))
        self.assertEqual(other.get("/api/me").status_code, 200,
                         "the other session was revoked by a rotation that "
                         "then reported failure")

    def test_the_rotation_still_completes_when_nothing_refuses(self):
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW_PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self._signs_in_with(NEW_PW))
        self.assertFalse(self._signs_in_with(PW))


if __name__ == "__main__":
    unittest.main()
