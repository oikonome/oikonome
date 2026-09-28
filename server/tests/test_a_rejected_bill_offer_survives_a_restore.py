"""Saying no to a detected bill is a decision, and a restore keeps it.

Detection offers a run of look-alike charges as a recurring bill. The offer's
id is derived from the evidence — the payee group, the cycle, the rounded
amount — so the row that records the answer is the only thing standing
between the household and the same offer tomorrow night: the detector's
insert is ON CONFLICT DO NOTHING, and it re-derives the same id from the same
ledger.

So a restore that carried the ledger and left the answers behind would hand
the person back every candidate they have already turned down, on the first
night on the new instance. It has to carry the answer with the charges that
produced it.
"""

import datetime as dt
import unittest

from oikonome.engine import bills
from oikonome.sync import export, restore

from .util import TODAY, add_txn, make_db, write_config

PAYEE = "HARBOR LIGHTS CLUB DUES"


def _monthly_run(conn, months: int = 6) -> None:
    """A steady monthly charge — what detection is built to notice."""
    for i in range(months):
        m = TODAY.month - 1 - i                     # months back from this one
        day = dt.date(TODAY.year + m // 12, m % 12 + 1, 8)
        add_txn(conn, day, 42.0, PAYEE, primary="GENERAL_SERVICES",
                account="chk", txn_id=f"dues-{i}")


class RejectedBillOfferSurvivesARestoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def _offers(conn, status=None):
        sql = "SELECT id, payee, status FROM bill_proposals WHERE kind='add'"
        args: tuple = ()
        if status:
            sql += " AND status=%s"
            args = (status,)
        return conn.execute(sql, args).fetchall()

    def _rejected_offer(self):
        """The source household: a detected candidate, turned down."""
        _monthly_run(self.conn)
        bills.run(self.conn, TODAY)
        offers = [o for o in self._offers(self.conn, "pending")
                  if o["payee"] == PAYEE]
        self.assertEqual(len(offers), 1, f"detection offered {offers}")
        pid = offers[0]["id"]
        self.assertIn("rejected", bills.apply_proposal(self.conn, pid,
                                                       "reject"))
        return pid

    def test_the_answer_travels_with_the_charges_that_produced_it(self):
        pid = self._rejected_offer()
        data = export.build_zip(self.conn)
        dest = make_db()
        try:
            counts = restore.restore_zip(dest, data)
            self.assertTrue(counts.get("bill_proposals"))
            row = dest.execute("SELECT status, decided_at FROM bill_proposals "
                               "WHERE id=%s", (pid,)).fetchone()
            self.assertEqual(row["status"], "rejected")
            self.assertIsNotNone(row["decided_at"])
            # …and the night after the restore does not ask again
            bills.run(dest, TODAY)
            self.assertEqual(
                [o["payee"] for o in self._offers(dest, "pending")], [])
            self.assertEqual(
                dest.execute("SELECT status FROM bill_proposals WHERE id=%s",
                             (pid,)).fetchone()["status"], "rejected")
        finally:
            dest.close()

    def test_without_the_answer_the_same_ledger_offers_it_again(self):
        """The other half of the proof: the offer is not being suppressed by
        anything else the restore carries — the ledger alone brings it
        straight back."""
        self._rejected_offer()
        data = export.build_zip(self.conn)
        dest = make_db()
        try:
            restore.restore_zip(dest, data)
            dest.execute("DELETE FROM bill_proposals")
            bills.run(dest, TODAY)
            self.assertEqual(
                [o["payee"] for o in self._offers(dest, "pending")], [PAYEE])
        finally:
            dest.close()

    def test_a_status_the_app_never_writes_lands_as_an_unanswered_offer(self):
        """A hand-edited CSV is the one way a value outside the set exists.
        Read back as-is it would sit in the table invisible to every query;
        read back as an offer, the person decides it."""
        pid = self._rejected_offer()
        self.conn.execute("UPDATE bill_proposals SET status='whatever' "
                          "WHERE id=%s", (pid,))
        data = export.build_zip(self.conn)
        dest = make_db()
        try:
            restore.restore_zip(dest, data)
            self.assertEqual(
                dest.execute("SELECT status FROM bill_proposals WHERE id=%s",
                             (pid,)).fetchone()["status"], "pending")
        finally:
            dest.close()

    def test_a_restore_into_a_household_that_answered_already_keeps_its_answer(self):
        """Re-restoring the same archive is a no-op, and a decision made
        HERE is never overwritten by the archive's copy of it."""
        pid = self._rejected_offer()
        data = export.build_zip(self.conn)
        dest = make_db()
        try:
            restore.restore_zip(dest, data)
            dest.execute("UPDATE bill_proposals SET status='approved' "
                         "WHERE id=%s", (pid,))
            counts = restore.restore_zip(dest, data)
            self.assertFalse(counts.get("bill_proposals"))
            self.assertEqual(
                dest.execute("SELECT status FROM bill_proposals WHERE id=%s",
                             (pid,)).fetchone()["status"], "approved")
        finally:
            dest.close()


if __name__ == "__main__":
    unittest.main()
