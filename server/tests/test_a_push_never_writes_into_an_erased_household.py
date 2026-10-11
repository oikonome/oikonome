"""A collector push authenticated before an erasure must not write the
household's rows back after it. The domain tables have no FK to tenants,
so rows written under an erased tenant id are never swept again. A push
door asks `erasure.push_may_write` inside its write transaction: it holds
the push writer lock (which the erasure waits on) for that transaction, and
refuses once the erasure holds it, has flipped the tenant to
pending_delete, or has deleted the tenant row."""
import unittest
import uuid

from oikonome import erasure
from oikonome.db import tenancy

from .util import _ensure_db


class PushMayWrite(unittest.TestCase):

    def setUp(self):
        _ensure_db()
        self.admin = tenancy.admin_connect()
        self.addCleanup(self.admin.close)
        self.tid = tenancy.create_tenant(self.admin,
                                         f"push-{uuid.uuid4().hex[:8]}")
        self.addCleanup(self.admin.execute,
                        "DELETE FROM tenants WHERE id=%s", (self.tid,))

    def _ask(self):
        conn = tenancy.admin_connect()
        try:
            with conn.transaction():
                return erasure.push_may_write(conn, self.tid)
        finally:
            conn.close()

    def test_an_active_household_may_be_pushed_to(self):
        self.assertTrue(self._ask())

    def test_a_push_waits_out_an_erasure_holding_the_writers(self):
        erasure.hold_writers(self.admin, self.tid)
        try:
            self.assertFalse(self._ask())
        finally:
            erasure.release_writers(self.admin, self.tid)

    def test_a_push_after_the_status_flip_is_refused(self):
        self.admin.execute(
            "UPDATE tenants SET status='pending_delete' WHERE id=%s",
            (self.tid,))
        self.assertFalse(self._ask())

    def test_a_push_after_the_rows_are_gone_is_refused(self):
        self.admin.execute("DELETE FROM tenants WHERE id=%s", (self.tid,))
        self.assertFalse(self._ask())

    def test_the_erasure_waits_on_the_push_lock(self):
        self.assertIn(erasure.PUSH_LOCK, erasure.WRITER_LOCKS)


if __name__ == "__main__":
    unittest.main()
