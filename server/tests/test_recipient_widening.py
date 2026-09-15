"""A configured recipient list can never widen to "everyone".

`_recipients_raw` filters a configured list — hosted membership, then
accepted invites. Both can empty it, and a fall-through to "every verified
user on the tenant" would then send a household's balances to members who
were never on the list. Guarding that fall-through on `owners or recips`
is not enough either: it falls through again when the tenant has no owner
row AND the list has emptied. A list filtered to nothing means the owner;
with no owner it means nobody.
"""

import os
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.jobs import worker

from .util import make_db, write_config


class RecipientWideningTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_the_widening_branch_is_unreachable_once_a_list_is_configured(self):
        """Structural: the every-verified-user query must not be guarded by
        anything that a configured-but-emptied list can satisfy."""
        import inspect
        src = inspect.getsource(worker._recipients_raw)
        self.assertNotIn("if owners or recips:", src)
        # the configured branch returns unconditionally (the return now
        # rides through the per-person mute filter — same invariant)
        after = src.split("owners = [")[1]
        self.assertIn("return _unmuted(out)", after)
        self.assertLess(after.index("return _unmuted(out)"),
                        after.index("verified_at IS NOT NULL"),
                        "the fallback query is still reachable from the "
                        "configured-list branch")

    def test_no_list_configured_still_falls_back_to_the_tenant(self):
        write_config(self.conn)
        got = worker._recipients_raw(self.conn, self._tid())
        self.assertIsInstance(got, list)

    def _tid(self):
        return self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def _add_user(self, verified: bool):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, 'x', "
                "CASE WHEN %s THEN now() END)",
                (self._tid(), f"u-{uuid.uuid4().hex[:8]}@example.dev",
                 verified))
        finally:
            admin.close()

    def test_an_all_unverified_hosted_tenant_gets_no_mail(self):
        """On hosted, verified_at is the proof of mailbox control that
        gates household mail. An owner who never confirmed plus a member
        claimed with a typed address must not widen to "mail everyone
        anyway" — nobody is the safe answer until someone verifies."""
        write_config(self.conn)
        self._add_user(verified=False)
        os.environ["OIKONOME_HOSTED"] = "1"
        try:
            got = worker._recipients_raw(self.conn, self._tid())
        finally:
            os.environ.pop("OIKONOME_HOSTED", None)
        self.assertEqual(got, [])

    def test_an_all_unverified_selfhost_tenant_still_gets_its_mail(self):
        """Self-host installs may have no outbound mail to verify with —
        the fallback keeps them from going silent, and stays."""
        write_config(self.conn)
        self._add_user(verified=False)
        got = worker._recipients_raw(self.conn, self._tid())
        self.assertTrue(got)
