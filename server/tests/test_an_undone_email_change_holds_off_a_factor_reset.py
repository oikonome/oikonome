"""An account whose login address was just put back by the undo link does
not take a factor-clearing reset request for a while.

The undo says the account was in someone else's hands a moment ago. The
undo itself deliberately leaves every factor in place, so reading the
restored inbox is not enough to own the account; the cooling-off reset is
the one road where inbox control plus a quiet week clears them all, and
asked for in the days right after a hijack it would hand back exactly what
the undo kept. So the undo stamps `users.reverted_at`, and while that is
inside the hold the request is refused with a reply that names the window.

Pinned here: the undo writes the stamp; a request inside the hold is
refused, starts no clock and tells no channel; one past the hold goes
through; an account never undone is untouched.
"""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import email_revert, factor_reset, posture
from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class AnUndoneEmailChangeHoldsOffAFactorReset(unittest.TestCase):
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
        self.client = TestClient(self.appmod.app)
        admin = tenancy.admin_connect()
        admin.autocommit = True
        self.addCleanup(admin.close)
        self.admin = admin
        self.tid = tenancy.create_tenant(admin,
                                         f"undo-{uuid.uuid4().hex[:8]}")
        self.old = f"owner-{uuid.uuid4().hex[:6]}@example.dev"
        self.moved = f"elsewhere-{uuid.uuid4().hex[:6]}@example.dev"
        from oikonome.auth import passwords
        self.uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, totp_secret) "
            "VALUES (%s,%s,%s,'JBSWY3DPEHPK3PXP') RETURNING id",
            (self.tid, self.moved,
             passwords.hash_password(PW))).fetchone()["id"]
        for name in ("_deliver_factor_reset_notice", "_deliver_reset",
                     "_deliver_email_reverted_notice"):
            p = mock.patch.object(self.appmod, name)
            setattr(self, name.strip("_"), p.start())
            self.addCleanup(p.stop)

    def _undo(self):
        token = email_revert.create(self.admin, self.uid, self.old,
                                    self.moved)
        r = self.client.post("/account/email-revert", data={"token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._row()["email"], self.old)

    def _row(self):
        return self.admin.execute(
            "SELECT email, reverted_at FROM users WHERE id=%s",
            (self.uid,)).fetchone()

    def _ask(self):
        return self.client.post(
            "/recover", data={"token": reset_mod.create_reset(self.admin,
                                                              self.uid)})

    def test_the_undo_records_when_it_happened(self):
        self.assertIsNone(self._row()["reverted_at"])
        self._undo()
        stamp = self._row()["reverted_at"]
        self.assertIsNotNone(stamp)
        self.assertLess(dt.datetime.now(dt.timezone.utc) - stamp,
                        dt.timedelta(minutes=1))

    def test_a_request_inside_the_hold_is_refused_and_names_the_window(self):
        self._undo()
        r = self._ask()
        self.assertEqual(r.status_code, 403, r.text)
        # the person asked from a page, so the page carries the reason
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn(f"{posture.REVERT_HOLD.days} days", r.text)
        # nothing started, nobody told
        self.assertIsNone(factor_reset.pending(self.admin, self.uid))
        self.deliver_factor_reset_notice.assert_not_called()

    def test_the_refusal_holds_on_the_module_door_too(self):
        # whatever door asks, the clock refuses: not only the page route
        self._undo()
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            factor_reset.request(self.admin, self.uid)
        self.assertEqual(cm.exception.detail["error"], "recently_reverted")
        self.assertIsNone(factor_reset.pending(self.admin, self.uid))

    def test_a_request_past_the_hold_goes_through(self):
        self._undo()
        self.admin.execute(
            "UPDATE users SET reverted_at = now() - %s WHERE id=%s",
            (posture.REVERT_HOLD + dt.timedelta(days=1), self.uid))
        r = self._ask()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Recovery requested", r.text)
        self.assertIsNotNone(factor_reset.pending(self.admin, self.uid))
        self.deliver_factor_reset_notice.assert_called_once()

    def test_an_account_never_undone_asks_as_before(self):
        self.admin.execute("UPDATE users SET email=%s WHERE id=%s",
                           (self.old, self.uid))
        self.assertIsNone(self._row()["reverted_at"])
        r = self._ask()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Recovery requested", r.text)
        self.assertIsNotNone(factor_reset.pending(self.admin, self.uid))


if __name__ == "__main__":
    unittest.main()
