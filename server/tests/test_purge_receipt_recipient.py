"""The deletion receipt goes to the OWNER, and never to an address the
provider already bounces.

Picking the tenant's earliest user is not picking the owner — the owner
may have been re-invited after a member joined. And mailing after the
erasure has scrubbed the delivery table re-mails a hard-bounced owner
address and regenerates its bounce row for a household that no longer
exists.
"""

import unittest
import uuid
from unittest import mock

from oikonome.db import tenancy
from oikonome.notify import delivery

from .util import _ensure_db

_RELEASED = {"plaid_items": 0, "released": "none", "mx_user": "none"}


def _seed(owner_first: bool):
    admin = tenancy.admin_connect()
    try:
        tag = uuid.uuid4().hex[:8]
        owner, member = f"pr-own-{tag}@x.dev", f"pr-mem-{tag}@x.dev"
        tid = tenancy.create_tenant(admin, owner)
        rows = [(owner, "owner", "2 days"), (member, "member", "5 days")]
        if owner_first:
            rows = [(owner, "owner", "5 days"), (member, "member", "2 days")]
        for email, role, age in rows:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at, created_at) VALUES (%s, %s, 'x', %s, now(), "
                "now() - %s::interval)", (tid, email, role, age))
        admin.execute(
            "UPDATE tenants SET status='pending_delete', "
            "delete_after = now() - interval '1 minute' WHERE id=%s", (tid,))
    finally:
        admin.close()
    return tid, owner


class PurgeReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _purge(self):
        from oikonome.jobs import worker
        with mock.patch("oikonome.erasure.release_external",
                        return_value=_RELEASED), \
             mock.patch("oikonome.notify.account_mail.account_deleted",
                        return_value=True) as deliver:
            worker.purge_scheduled_deletions()
        return deliver

    def test_receipt_goes_to_the_owner_not_the_earliest_user(self):
        tid, owner = _seed(owner_first=False)
        deliver = self._purge()
        sent = [c.args[0] for c in deliver.call_args_list]
        self.assertIn(owner, sent)

    def test_a_bounced_owner_is_not_mailed_after_erasure(self):
        tid, owner = _seed(owner_first=True)
        delivery.record_send_failure(
            owner, "550 5.1.1 recipient rejected: unknown user")
        deliver = self._purge()
        self.assertEqual([c for c in deliver.call_args_list
                          if c.args[0] == owner], [])
        # and the erasure took the bounce row with it — nothing regrew it
        self.assertIsNone(delivery.state_for(owner))


if __name__ == "__main__":
    unittest.main()
