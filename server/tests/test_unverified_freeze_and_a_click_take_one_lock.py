"""The nightly freeze and a click on a verification link are ordered.

The freeze re-asks "has anyone here confirmed?" under a lock before it
freezes a household. That lock has to be one the click also takes, or a
click landing between the question and the freeze is told "Email
confirmed" while its household is frozen anyway — and, being verified now,
is never revisited by the sweep before the purge erases it. The click
writes the user row and never the tenant row, so the freeze locks the
household's USER rows, and the click takes its own user row first: a click
in flight waits for the freeze, then finds the household frozen and is
offered the reopen button.
"""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

import psycopg

from oikonome.auth import email_verify
from oikonome.db import tenancy
from oikonome.jobs import unverified

from .util import _ensure_db


class FreezeAndClickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "OIKONOME_HOSTED": "1",
            "OIKONOME_BASE_URL": "https://app.example.test",
            "OIKONOME_UNVERIFIED_REAP_DAYS": "30"})
        self._env.start()
        self.addCleanup(self._env.stop)
        p = mock.patch.object(unverified.account_mail, "send",
                              return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_a_click_during_the_freeze_waits_and_lands_on_a_frozen_household(
            self):
        admin = tenancy.admin_connect()
        self.addCleanup(admin.close)
        tid = tenancy.create_tenant(admin, f"fz-{uuid.uuid4().hex[:8]}")
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)
        uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role, "
            "created_at) VALUES (%s,%s,'x','owner',%s) RETURNING id",
            (tid, f"fz-{uuid.uuid4().hex[:8]}@x.dev", since)
        ).fetchone()["id"]
        token = email_verify.create(admin, uid)
        row = email_verify.lookup(admin, token)

        clicker = tenancy.admin_connect()
        self.addCleanup(clicker.close)
        clicker.execute("SET lock_timeout = '300ms'")
        seen = {}

        def click_mid_freeze(_admin, _tid):
            # the freeze has asked its question and holds its locks: a
            # click now must wait for it rather than slip in underneath
            try:
                seen["ok"] = unverified.confirm(clicker, row["id"], uid)
            except psycopg.errors.LockNotAvailable:
                seen["waited"] = True
            return False

        with mock.patch.object(unverified, "_skip", click_mid_freeze):
            word = unverified._freeze(admin, str(tid),
                                      dt.datetime.now(dt.timezone.utc))
        self.assertEqual(seen, {"waited": True},
                         "the click must not commit under a freeze")
        self.assertEqual(word, "scheduled")
        # once the freeze has committed, the same click goes through and
        # finds the household frozen, which is what offers the reopen
        clicker.execute("SET lock_timeout = 0")
        self.assertTrue(unverified.confirm(clicker, row["id"], uid))
        self.assertIsNotNone(unverified.frozen_household(clicker, uid))


if __name__ == "__main__":
    unittest.main()
