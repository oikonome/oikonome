"""A restore never cuts a reimbursement link the household holds now to
make room for an older one the archive brings back (specs/restore-landers.md,
"Payback past a charge").

The archive's links are always older than any the household made since
the export, so weighed oldest first across both they always won: a deposit
the person moved from one charge to another after the export had its
current link cut or removed, and the stale one reinstated. The links the
household already held are weighed first, oldest first among themselves;
the restored ones take only the room left over.

Synthetic numbers throughout.
"""

import unittest

from oikonome.sync import restore
from oikonome.web import data

from .test_a_restore_never_brings_back_more_payback_than_its_charge import (
    _archive, _charge, _deposit, _link)
from .util import _ensure_db, make_db


class ALiveLinkOutranksAnArchivedOne(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _links(self):
        return {(r["expense_id"], r["reimburse_id"]): round(float(r["amount"]), 2)
                for r in self.conn.execute(
                    "SELECT expense_id, reimburse_id, amount "
                    "FROM reimbursements").fetchall()}

    def _relink(self, old, new_charge, new_deposit):
        self.conn.execute("DELETE FROM reimbursements WHERE expense_id = %s "
                          "AND reimburse_id = %s", old)
        out = data.link_reimbursements(self.conn, new_deposit, [new_charge],
                                       partial=True)
        self.assertFalse(out.get("errors"), out)

    def test_a_deposit_moved_to_another_charge_keeps_its_current_link(self):
        z = _archive([_charge("e1", 100), _charge("e2", 100),
                      _deposit("d1", 100)], [_link("e1", "d1", 100, 6)])
        restore.restore_zip(self.conn, z)
        self._relink(("e1", "d1"), "e2", "d1")
        _r, out = restore.restore_outcomes(self.conn, z)
        self.assertEqual(self._links(), {("e2", "d1"): 100.0})
        self.assertEqual(out.refused.get(("reimbursements", "over_cap")), 1)

    def test_a_charge_repaid_by_another_deposit_keeps_its_current_link(self):
        z = _archive([_charge("e1", 100), _deposit("d1", 100),
                      _deposit("d2", 100)], [_link("e1", "d1", 100, 6)])
        restore.restore_zip(self.conn, z)
        self._relink(("e1", "d1"), "e1", "d2")
        _r, out = restore.restore_outcomes(self.conn, z)
        self.assertEqual(self._links(), {("e1", "d2"): 100.0})
        self.assertEqual(out.refused.get(("reimbursements", "over_cap")), 1)


if __name__ == "__main__":
    unittest.main()
