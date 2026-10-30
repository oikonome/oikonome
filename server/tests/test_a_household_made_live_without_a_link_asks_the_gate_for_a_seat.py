"""Two roads make a household live without a confirmation link, and both
ask the installed gate whether it may hold a seat.

* The reclaim click that REPLACES an empty owner household creates a fresh,
  already-confirmed account. When the shell it erased had been left
  unconfirmed past its seat hold, it held no seat, so the replacement is
  the same case as a confirmation clicked late: an add-on with an intake
  ceiling must be able to put it on its waiting list, or anyone can walk
  past a full cap by signing up again.
* Reopening a household frozen for never confirming makes it 'active'
  after the confirmation has already been judged (the household was still
  frozen then), so the question is asked again in the reopen's own
  transaction, after the household is live.

The member move-out is not on this list: it already answers the cap
itself, with a refusal that keeps the link usable. A household running for
itself has no ceiling, so the default gate parks nobody.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome import ext
from oikonome.db import tenancy
from oikonome.jobs import unverified

from .util import _ensure_db

PW = "correct-horse-battery"


def _admin():
    return tenancy.admin_connect()


class _Asked:
    """A gate answer that records each question and parks the household
    under a status the product already locks out."""

    def __init__(self, park: bool):
        self.park = park
        self.asked: list[tuple[str, str]] = []

    def __call__(self, admin, tenant_id):
        status = admin.execute("SELECT status FROM tenants WHERE id=%s",
                               (tenant_id,)).fetchone()["status"]
        self.asked.append((str(tenant_id), status))
        if not self.park:
            return None
        admin.execute("UPDATE tenants SET status='suspended' WHERE id=%s",
                      (tenant_id,))
        return "suspended"


class ReclaimAsksForASeatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        for p in (mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                               "OIKONOME_OPEN_SIGNUP": "1"}),
                  mock.patch.object(self.appmod, "DEV_MODE", False),
                  mock.patch.object(self.appmod, "_deliver_verification")):
            p.start()
            self.addCleanup(p.stop)
        self.sent: list[tuple[str, str]] = []
        p = mock.patch.object(
            self.appmod, "_deliver_reclaim",
            side_effect=lambda email, token: self.sent.append((email, token)))
        p.start()
        self.addCleanup(p.stop)
        from oikonome.web import security
        security._limiter._hits.clear()

    def _signup(self, email):
        return TestClient(self.appmod.app).post(
            "/api/signup", data={"email": email,
                                 "password": f"{PW}-{uuid.uuid4().hex[:8]}"})

    def _finish(self, token):
        return TestClient(self.appmod.app).post(
            "/signup/reclaim",
            data={"token": token, "password": "clicker-chosen-password"},
            follow_redirects=False)

    def _tenant(self, email):
        admin = _admin()
        try:
            return admin.execute(
                "SELECT t.id, t.status FROM users u JOIN tenants t "
                "ON t.id = u.tenant_id WHERE u.email=%s", (email,)).fetchone()
        finally:
            admin.close()

    def test_the_replace_road_asks_once_the_new_household_is_live(self):
        email = f"own-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(email).status_code, 200)
        old = self._tenant(email)["id"]
        self.assertEqual(self._signup(email).status_code, 409)
        asked = _Asked(park=True)
        with mock.patch.object(ext.gate, "wait_for_seat", side_effect=asked):
            r = self._finish(self.sent[-1][1])
        self.assertEqual(r.status_code, 303, r.text)
        new = self._tenant(email)
        self.assertNotEqual(new["id"], old)
        self.assertEqual(asked.asked, [(str(new["id"]), "active")])
        # the gate's answer stands: the household waits, it is not live
        self.assertEqual(new["status"], "suspended")

    def test_the_member_move_out_is_not_asked(self):
        host = f"host-{uuid.uuid4().hex[:8]}@x.dev"
        member = f"mem-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(host).status_code, 200)
        admin = _admin()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member')",
                (self._tenant(host)["id"], member))
        finally:
            admin.close()
        self.assertEqual(self._signup(member).status_code, 409)
        asked = _Asked(park=True)
        with mock.patch.object(ext.gate, "wait_for_seat", side_effect=asked):
            r = self._finish(self.sent[-1][1])
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(asked.asked, [])

    def test_the_default_gate_parks_nobody(self):
        self.assertIsNone(ext.Gate().wait_for_seat(None, "any"))


class ReopenAsksForASeatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _frozen(self, prior="active"):
        admin = _admin()
        try:
            tid = tenancy.create_tenant(admin, f"fz-{uuid.uuid4().hex[:8]}")
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'x','owner',now()) RETURNING id",
                (tid, f"fz-{uuid.uuid4().hex[:8]}@x.dev")).fetchone()["id"]
            admin.execute(
                "UPDATE tenants SET status=%s, status_before_delete=%s, "
                "delete_after = now() + interval '7 days' WHERE id=%s",
                (unverified.STATUS, prior, tid))
        finally:
            admin.close()
        return str(tid), uid

    def _status(self, tid):
        admin = _admin()
        try:
            return admin.execute("SELECT status FROM tenants WHERE id=%s",
                                 (tid,)).fetchone()["status"]
        finally:
            admin.close()

    def _reopen(self, uid, asked, reactivate=None):
        admin = _admin()
        try:
            with mock.patch.object(ext.gate, "wait_for_seat",
                                   side_effect=asked), \
                    mock.patch.object(ext.gate, "reactivate",
                                      side_effect=reactivate or
                                      ext.Gate().reactivate):
                return unverified.reopen(admin, uid)
        finally:
            admin.close()

    def test_a_reopened_household_is_asked_after_it_is_live(self):
        tid, uid = self._frozen()
        asked = _Asked(park=True)
        self.assertEqual(self._reopen(uid, asked), tid)
        self.assertEqual(asked.asked, [(tid, "active")])
        self.assertEqual(self._status(tid), "suspended")

    def test_a_gate_with_room_leaves_it_active(self):
        tid, uid = self._frozen()
        asked = _Asked(park=False)
        self.assertEqual(self._reopen(uid, asked), tid)
        self.assertEqual(asked.asked, [(tid, "active")])
        self.assertEqual(self._status(tid), "active")

    def test_a_household_the_gate_did_not_make_active_is_not_asked(self):
        tid, uid = self._frozen()

        def lockout(admin, tenant_id):
            admin.execute("UPDATE tenants SET status='suspended' "
                          "WHERE id=%s", (tenant_id,))
            return "suspended"
        asked = _Asked(park=True)
        self.assertEqual(self._reopen(uid, asked, lockout), tid)
        self.assertEqual(asked.asked, [])


if __name__ == "__main__":
    unittest.main()
