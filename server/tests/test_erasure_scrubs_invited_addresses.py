"""Erasure must scrub the bounce state of every address the household
ever invited, not only the ones still on its recipient list.

Removing a bouncing address from Settings is the usual remedy for it;
the row in recipient_invites survives that, and so did its
email_delivery_state row — the provider's verbatim bounce text — after
the account was deleted.
"""

import unittest
import uuid

from oikonome.db import tenancy
from oikonome.notify import delivery, recipient_invites

from .util import _ensure_db


def _addr(tag: str) -> str:
    return f"era-{tag}-{uuid.uuid4().hex[:8]}@example.dev"


class InvitedAddressErasureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_a_removed_recipient_is_still_scrubbed(self):
        owner, gone = _addr("own"), _addr("gone")
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"era-{uuid.uuid4().hex[:8]}")
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (%s, %s, 'x')", (tid, owner))
            # invited once, then dropped from the live list (no
            # email_recipients in the config at erasure time)
            recipient_invites.invite(tid, gone)
            for e in (owner, gone):
                delivery.record_send_failure(
                    e, "550 5.1.1 recipient rejected: unknown user")
                self.assertIsNotNone(delivery.state_for(e))
            tenancy.delete_tenant_rows(admin, tid)
            self.assertIsNone(delivery.state_for(owner))
            self.assertIsNone(
                delivery.state_for(gone),
                "erasure left an invited-then-removed address's bounce row")
        finally:
            admin.close()


if __name__ == "__main__":
    unittest.main()
