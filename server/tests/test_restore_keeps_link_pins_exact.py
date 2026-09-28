"""What a reimbursement link remembers about a row's category survives a
restore exactly, so an unlink on the restored copy does what it would
have done on the source.

Two things are at stake. A link that replaced a person's explicit "no
category" pin (the empty-string pin) remembers it as the empty string,
which is not the same as remembering nothing: unlink puts the empty pin
back, where nothing-remembered deletes the pin and lets the bill and store
passes recategorise a row the person deliberately cleared. And an archive
written before pins carried the link mark is classified by date — but only
the pins that archive brought in; a pin already in the household keeps the
mark it has, or a person's own category set after a link is re-read as
the link's and deleted by the next unlink."""

import unittest

from oikonome.sync import restore
from oikonome.web import data

from .test_restore_v2 import _zip
from .util import TODAY, _ensure_db, add_txn, make_db

_TXNS = (["id", "account_id", "date", "amount", "name", "category_primary",
          "category_override", "override_source"], [
    {"id": "e1", "account_id": "card", "date": "2026-07-01", "amount": 200,
     "name": "HOTEL", "category_primary": "TRAVEL",
     "category_override": "TRANSFER_OUT", "override_source": "user"},
    {"id": "r1", "account_id": "chk", "date": "2026-07-05", "amount": -200,
     "name": "EMPLOYER", "category_primary": "INCOME",
     "category_override": "TRANSFER_IN", "override_source": "user"}])
_PAIR = (["expense_id", "reimburse_id", "partial", "amount", "created_at"], [
    {"expense_id": "e1", "reimburse_id": "r1", "partial": 0,
     "created_at": "2026-07-06 10:00:00"}])


class RestoreKeepsLinkPinsExactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _pin(self, tid):
        r = self.conn.execute(
            "SELECT category FROM manual_categories WHERE transaction_id=%s",
            (tid,)).fetchone()
        return None if r is None else r["category"]

    def test_cleared_pin_a_link_replaced_comes_back_after_restore(self):
        """The archive marks that the remembered pin is the empty one."""
        cols = ["transaction_id", "category", "set_at", "link_made",
                "link_prior", "link_prior_set"]
        z = _zip({"transactions": _TXNS,
                  "manual_categories": (cols, [
                      {"transaction_id": "e1", "category": "TRANSFER_OUT",
                       "set_at": "2026-07-06 10:00:00", "link_made": "t",
                       "link_prior": "", "link_prior_set": "t"},
                      {"transaction_id": "r1", "category": "TRANSFER_IN",
                       "set_at": "2026-07-06 10:00:00", "link_made": "t",
                       "link_prior": "", "link_prior_set": "f"}]),
                  "reimbursements": _PAIR})
        restore.restore_zip(self.conn, z)
        data.unlink_reimbursement(self.conn, "e1", "r1")
        self.assertEqual(self._pin("e1"), "")      # the person's cleared pin
        self.assertIsNone(self._pin("r1"))         # nothing to give back

    def test_archive_without_the_marker_reads_an_empty_cell_as_none(self):
        """Archives written before the marker keep their old meaning."""
        cols = ["transaction_id", "category", "set_at", "link_made",
                "link_prior"]
        z = _zip({"transactions": _TXNS,
                  "manual_categories": (cols, [
                      {"transaction_id": "e1", "category": "TRANSFER_OUT",
                       "set_at": "2026-07-06 10:00:00", "link_made": "t",
                       "link_prior": ""}]),
                  "reimbursements": _PAIR})
        restore.restore_zip(self.conn, z)
        prior = self.conn.execute(
            "SELECT link_prior FROM manual_categories WHERE transaction_id"
            "='e1'").fetchone()["link_prior"]
        self.assertIsNone(prior)

    def test_old_archive_leaves_the_households_own_pins_alone(self):
        """A person's category chosen after linking, already in the
        household, survives merge-restoring an archive that predates the
        link mark, and survives the unlink after it."""
        exp = add_txn(self.conn, TODAY, 200.0, "OFFICE SUPPLY CO")
        dep = add_txn(self.conn, TODAY, -200.0, "EMPLOYER REIMB",
                      account="chk", primary="INCOME")
        data.link_reimbursement(self.conn, exp, dep)
        data.set_category(self.conn, exp, "TRANSFER_OUT")     # by hand
        z = _zip({"transactions": _TXNS,
                  "manual_categories": (["transaction_id", "category",
                                         "set_at"], [
                      {"transaction_id": "e1", "category": "TRANSFER_OUT",
                       "set_at": "2026-07-06 10:00:00"}]),
                  "reimbursements": _PAIR})
        restore.restore_zip(self.conn, z)
        data.unlink_reimbursement(self.conn, exp, dep)
        self.assertEqual(self._pin(exp), "TRANSFER_OUT")
        # the archive's own pin is still classified as the link's
        data.unlink_reimbursement(self.conn, "e1", "r1")
        self.assertIsNone(self._pin("e1"))


if __name__ == "__main__":
    unittest.main()
