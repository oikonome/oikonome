"""Per-person opt-out from the scheduled email (`email_muted`): the owner
can stop their own copy while the rest of the household keeps receiving —
"owner always receives" was all-or-nothing for the one person who sets the
schedule. Muting is explicit per-address state, filtered case-insensitively
in every recipient path (configured list, owner union, no-list fallback)."""

import unittest
import uuid

from oikonome.db import tenancy
from oikonome.jobs import worker

from .util import TEST_DB, _admin_dsn, accept_recipient_invite, make_db, \
    write_config


class EmailMutedTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.owner = f"owner-{uuid.uuid4().hex[:6]}@x.dev"
        self.member = f"member-{uuid.uuid4().hex[:6]}@x.dev"
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'h','owner',now()), "
                "(%s,%s,'h','viewer',now())",
                (self.tid, self.owner, self.tid, self.member))
        finally:
            admin.close()

    def tearDown(self):
        self.conn.close()

    def test_no_list_fallback_honours_the_mute(self):
        write_config(self.conn, email_muted=[self.member])
        got = worker._recipients_raw(self.conn, self.tid)
        self.assertIn(self.owner, got)
        self.assertNotIn(self.member, got)

    def test_the_owner_can_mute_themselves(self):
        guest = f"guest-{uuid.uuid4().hex[:6]}@x.dev"
        accept_recipient_invite(self.tid, guest)
        # stored lowercased by the API; the filter must not care about case
        write_config(self.conn, email_recipients=[guest],
                     email_muted=[self.owner.upper().lower()])
        got = worker._recipients_raw(self.conn, self.tid)
        self.assertNotIn(self.owner, got,
                         "a muted owner still received the scheduled mail")
        self.assertIn(guest, got,
                      "muting the owner must not touch the rest of the list")

    def test_muting_an_extra_leaves_the_owner(self):
        guest = f"guest-{uuid.uuid4().hex[:6]}@x.dev"
        accept_recipient_invite(self.tid, guest)
        write_config(self.conn, email_recipients=[guest],
                     email_muted=[guest])
        got = worker._recipients_raw(self.conn, self.tid)
        self.assertIn(self.owner, got)
        self.assertNotIn(guest, got)

    def test_unmuted_is_unchanged(self):
        write_config(self.conn)
        got = worker._recipients_raw(self.conn, self.tid)
        self.assertIn(self.owner, got)
        self.assertIn(self.member, got)


class EmailMutedApiCapTests(unittest.TestCase):
    """The mute list takes the SAME cap-and-refuse as email_recipients.
    Un-capped, one authenticated request could park a ~1MB address list in
    tenant config that every later load_config — and every row-locked
    settings write — then pays to deserialize."""

    @classmethod
    def setUpClass(cls):
        import os
        import uuid

        from fastapi.testclient import TestClient
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"mutecap-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def test_oversized_mute_list_is_refused(self):
        from oikonome.web import mailguard
        big = [f"a{i}@x.dev" for i in range(mailguard.MAX_RECIPIENTS + 1)]
        r = self.client.post("/api/settings", json={"email_muted": big})
        self.assertEqual(r.status_code, 400)
        self.assertIn("limit", r.json()["detail"])

    def test_reasonable_mute_list_saves(self):
        r = self.client.post("/api/settings",
                             json={"email_muted": ["one@x.dev",
                                                   "Two@X.dev"]})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get("/api/settings")
        self.assertEqual(r.json()["email_muted"], ["one@x.dev",
                                                   "two@x.dev"])


class NonListMutedNeverCrashes(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_worker_recipients_survive_scalar_muted(self):
        write_config(self.conn, email_muted=1)   # a hand-edited/crafted ZIP
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        got = worker._recipients_raw(self.conn, tid)   # must not TypeError
        self.assertIsInstance(got, list)

    def test_recipient_status_survives_scalar_muted(self):
        from oikonome.web.api import _recipient_status
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        rows = _recipient_status(tid, [], 1)     # must not TypeError
        self.assertIsInstance(rows, list)

    def test_restore_scrub_normalizes_muted(self):
        from oikonome.sync.restore import scrub_config
        self.assertNotIn("email_muted",
                         scrub_config({"email_muted": True}))
        self.assertEqual(scrub_config({"email_muted": ["A@x.dev", " "]}),
                         {"email_muted": ["a@x.dev"]})
