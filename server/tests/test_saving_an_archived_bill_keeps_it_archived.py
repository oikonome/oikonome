"""Saving a bill never brings an archived bill back to life.

Archiving is the person's decision to take a bill out of the plan, Today
and the daily email; restoring it is a separate, explicit act. An edit that
lands after the archive — a Save tapped while Archive is still in flight,
another member's stale edit form, or a machine cadence proposal approved
from a stale list — must keep the bill archived. Otherwise it reappears in
the plan with every request answering ok, and nobody sees why.
"""

import datetime as dt
import unittest

from oikonome.engine import bills

from .util import TODAY, _ensure_db, add_bill, make_db, write_config

NEXT = TODAY + dt.timedelta(days=10)


class SavingAnArchivedBill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _active(self, payee):
        return bills.get_bill(self.conn, payee)["active"]

    def test_an_edit_saved_after_the_archive_keeps_it_archived(self):
        add_bill(self.conn, "Harbor Gym", 40.0, next_due=NEXT)
        bills.archive_bill(self.conn, "Harbor Gym")
        bills.save_bill(self.conn, payee="Harbor Gym", amount=45.0,
                        frequency="MONTHLY", next_due=NEXT, source="manual",
                        manual_touch=True)
        self.assertEqual(self._active("Harbor Gym"), 0)
        # the edit itself still lands
        self.assertEqual(abs(bills.get_bill(self.conn,
                                            "Harbor Gym")["amount"]), 45.0)

    def test_an_explicit_revive_brings_it_back(self):
        add_bill(self.conn, "Harbor Gym", 40.0, next_due=NEXT)
        bills.archive_bill(self.conn, "Harbor Gym")
        bills.save_bill(self.conn, payee="Harbor Gym", amount=40.0,
                        frequency="MONTHLY", next_due=NEXT, revive=True)
        self.assertEqual(self._active("Harbor Gym"), 1)

    def test_a_cadence_proposal_for_an_archived_bill_is_refused(self):
        add_bill(self.conn, "Harbor Gym", 40.0, next_due=NEXT)
        for kind in ("frequency_change", "type_change"):
            with self.subTest(kind=kind):
                pid = f"prop:{kind}"
                bills._insert_proposal(
                    self.conn, pid=pid, kind=kind, payee="Harbor Gym",
                    amount=40.0, frequency="YEARLY", interval=1,
                    next_due=NEXT, evidence={})
                bills.archive_bill(self.conn, "Harbor Gym")
                out = bills.apply_proposal(self.conn, pid, "approve")
                self.assertIn("error", out)
                self.assertEqual(self._active("Harbor Gym"), 0)
                # and the proposal stays pending, not marked approved
                self.assertEqual(self.conn.execute(
                    "SELECT status FROM bill_proposals WHERE id=%s",
                    (pid,)).fetchone()["status"], "pending")

    def test_approving_an_add_for_an_archived_payee_restores_it(self):
        # an 'add' offer the person approves is an explicit ask for the bill
        add_bill(self.conn, "Harbor Gym", 40.0, next_due=NEXT)
        bills.archive_bill(self.conn, "Harbor Gym")
        bills._insert_proposal(
            self.conn, pid="prop:add", kind="add", payee="Harbor Gym",
            amount=40.0, frequency="MONTHLY", interval=1, next_due=NEXT,
            evidence={})
        out = bills.apply_proposal(self.conn, "prop:add", "approve")
        self.assertNotIn("error", out, out)
        self.assertEqual(self._active("Harbor Gym"), 1)


if __name__ == "__main__":
    unittest.main()
