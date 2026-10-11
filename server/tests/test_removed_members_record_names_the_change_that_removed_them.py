"""The banner record that names the members a credential change removed
says which change it was.

A password change, email change or second-factor change removes members
the owner's recent invites created, just as a recovery does. When that
change armed no other pause, the record is created to name them, and it
must say "rotation": labelled "recovery", the banner would tell the owner
their account was recovered when they only changed their password. A
record already armed keeps the reason it was armed with."""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.notify import outbound_pause as op

from .util import _ensure_db, write_config

MEMBER = {"email": "member@example.dev", "role": "member",
          "joined": "2026-10-01T00:00:00+00:00"}


class RemovedMembersRecordReason(unittest.TestCase):
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
        client = TestClient(self.app)
        email = f"rmr-{uuid.uuid4().hex[:10]}@example.dev"
        r = client.post("/api/signup", data={
            "email": email, "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = client.get("/api/me").json()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            self.uid = admin.execute(
                "SELECT id FROM users WHERE lower(email)=lower(%s)",
                (email,)).fetchone()["id"]
        finally:
            admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn)
        finally:
            conn.close()

    def _note(self, **kw):
        admin = tenancy.admin_connect()
        try:
            op.note_removed_members(admin, self.uid, [dict(MEMBER)], **kw)
        finally:
            admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            return op.paused(budget.load_config(conn))
        finally:
            conn.close()

    def test_a_rotation_that_only_removed_members_reads_as_a_rotation(self):
        p = self._note(reason=op.ROTATION)
        self.assertEqual(p["reason"], op.ROTATION)
        self.assertEqual([m["email"] for m in p["removed_members"]],
                         [MEMBER["email"]])

    def test_a_recovery_is_still_labelled_a_recovery_by_default(self):
        self.assertEqual(self._note()["reason"], op.RECOVERY)

    def test_an_armed_record_keeps_the_reason_it_was_armed_with(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg[op.KEY] = {"at": "2026-10-01T00:00:00+00:00",
                               "reason": op.RECOVERY, "smtp": True,
                               "recipients": [], "llm": False}
        finally:
            conn.close()
        self.assertEqual(self._note(reason=op.ROTATION)["reason"],
                         op.RECOVERY)


if __name__ == "__main__":
    unittest.main()
