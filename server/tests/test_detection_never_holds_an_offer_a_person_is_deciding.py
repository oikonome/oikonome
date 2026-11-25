"""Detection refreshing a standing offer never makes a person's decision
on it wait, nor waits on one.

The pass re-finds every pending offer to add a series and refreshes its
next date and evidence. It runs in one long transaction, so a refresh that
locked the row would hold Approve or "Not a bill" on that offer until the
whole pass committed. An offer found again unchanged is not touched (no
lock), and one a person is deciding right now is left for the next pass.
"""
import datetime as dt
import unittest


from oikonome.db import tenancy
from oikonome.engine import bills

from .util import make_db, write_config

ARGS = dict(pid="prop:add:lantern", kind="add", payee="LANTERN STREAMING",
            amount=-12.0, frequency="MONTHLY", interval=1,
            evidence={"hits": 4, "cycle_days": 30})


class DetectionRefreshLocks(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.assertTrue(bills._insert_proposal(
            self.conn, next_due=dt.date(2026, 12, 1), **ARGS))
        self.other = tenancy.tenant_connect(self.tid)
        self.other.execute("SET lock_timeout = '300ms'")

    def tearDown(self):
        # pooled: the timeout must not ride back into the pool
        self.other.execute("RESET lock_timeout")
        self.other.close()
        self.conn.execute("RESET lock_timeout")
        self.conn.close()

    def _next_due(self):
        return self.conn.execute(
            "SELECT next_due FROM bill_proposals WHERE id = %s",
            (ARGS["pid"],)).fetchone()["next_due"]

    def test_an_offer_found_again_unchanged_is_not_locked(self):
        with self.conn.transaction():
            self.assertFalse(bills._insert_proposal(
                self.conn, next_due=dt.date(2026, 12, 1), **ARGS))
            # a person's decision on it goes ahead while the pass runs on
            self.other.execute(
                "SELECT id FROM bill_proposals WHERE id = %s FOR UPDATE",
                (ARGS["pid"],)).fetchone()
            self.other.rollback()

    def test_an_offer_being_decided_is_left_for_the_next_pass(self):
        with self.other.transaction():
            self.other.execute(
                "SELECT id FROM bill_proposals WHERE id = %s FOR UPDATE",
                (ARGS["pid"],)).fetchone()
            self.conn.execute("SET lock_timeout = '300ms'")
            self.assertFalse(bills._insert_proposal(
                self.conn, next_due=dt.date(2027, 1, 1), **ARGS))
        self.assertEqual(self._next_due(), dt.date(2026, 12, 1))

    def test_a_changed_offer_still_takes_its_new_date(self):
        self.assertFalse(bills._insert_proposal(
            self.conn, next_due=dt.date(2027, 1, 1), **ARGS))
        self.assertEqual(self._next_due(), dt.date(2027, 1, 1))


if __name__ == "__main__":
    unittest.main()
