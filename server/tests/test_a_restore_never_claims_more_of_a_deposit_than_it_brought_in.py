"""A restore never brings back links claiming more of a deposit than it
brought in (specs/restore-landers.md, "Payback past a charge").

The link door holds a deposit's links to its amount (`_deposit_left`: its
partial links and the charges fully paired to it), but an archive can
carry more: a $100 deposit partial-linked for $100 to each of two $100
charges. Restored as it was, both charges' spend reads $0 for $100 that
came in once. The restore cuts the partial links on such a deposit back to
its amount, the newest first, a link cut to nothing removed, so the
uncovered charge counts as spend again; it holds the charge side in the
same pass, and a second restore of the same archive changes nothing.

Synthetic numbers throughout.
"""

import unittest

from oikonome.sync import restore
from oikonome.web import data

from . import test_a_restore_never_brings_back_more_payback_than_its_charge \
    as charge_side
from .test_a_restore_never_brings_back_more_payback_than_its_charge import (
    _archive, _charge, _deposit, _link)
from .test_reimbursements_net_within_both_sides import in_and_out
from .util import make_db


class ARestoreNeverClaimsMoreOfADepositThanItBroughtIn(
        charge_side.ARestoreNeverBringsBackMorePaybackThanItsCharge):

    def _left(self, dep):
        row = self.conn.execute(
            "SELECT amount FROM transactions WHERE id = %s", (dep,)).fetchone()
        return data._deposit_left(self.conn, {"id": dep,
                                              "amount": float(row["amount"])})

    def test_the_newest_link_on_a_deposit_is_cut_and_its_charge_is_spend(self):
        z = _archive([_charge("e1", 100), _charge("e2", 100),
                      _deposit("d1", 100)],
                     [_link("e1", "d1", 100, 6), _link("e2", "d1", 100, 7)])
        out, again = self._restore_twice(z)
        self.assertEqual(self._links(), {("e1", "d1"): 100.0})
        self.assertEqual(self._spend("e1"), 0.0)
        self.assertEqual(self._spend("e2"), 100.0)   # spend again
        self.assertEqual(self._income("d1"), 0.0)
        self.assertEqual(self._left("d1"), 0.0)
        for o in (out, again):
            self.assertEqual(o.refused.get(("reimbursements", "over_cap")), 1)

    def test_a_deposit_partly_over_keeps_the_older_links_whole(self):
        z = _archive([_charge("e1", 70), _charge("e2", 50),
                      _deposit("d1", 90)],
                     [_link("e1", "d1", 70, 6), _link("e2", "d1", 50, 7)])
        out, _ = self._restore_twice(z)
        self.assertEqual(self._links(), {("e1", "d1"): 70.0,
                                         ("e2", "d1"): 20.0})
        self.assertEqual(self._spend("e2"), 30.0)
        self.assertEqual(out.repaired.get(
            ("reimbursements", "payback_trimmed")), 1)

    def test_a_full_pair_on_the_deposit_counts_against_it(self):
        z = _archive([_charge("e1", 80), _charge("e2", 40),
                      _deposit("d1", 100)],
                     [_link("e1", "d1", None, 6, partial=0),
                      _link("e2", "d1", 40, 7)])
        self._restore_twice(z)
        self.assertEqual(self._links(), {("e1", "d1"): None,
                                         ("e2", "d1"): 20.0})
        self.assertEqual(self._spend("e2"), 20.0)

    def test_both_limits_hold_where_a_link_sits_between_two(self):
        # e1 is over its cost and d1 over its amount; one pass, oldest
        # first: the old link takes what d1 has, the newer one what e1
        # has left, and a second restore leaves it so
        z = _archive([_charge("e1", 100), _deposit("d1", 60),
                      _deposit("d2", 50)],
                     [_link("e1", "d1", 100, 6), _link("e1", "d2", 50, 7)])
        self._restore_twice(z)
        self.assertEqual(self._links(), {("e1", "d1"): 60.0,
                                         ("e1", "d2"): 40.0})
        self.assertEqual(self._spend("e1"), 0.0)
        self.assertEqual(self._income("d2"), 10.0)

    def test_a_link_to_a_withdrawn_charge_is_neither_counted_nor_cut(self):
        withdrawn = _charge("e1", 100)
        withdrawn["removed"] = 1
        z = _archive([withdrawn, _charge("e2", 100), _deposit("d1", 100)],
                     [_link("e1", "d1", 100, 6), _link("e2", "d1", 100, 7)])
        out, _ = self._restore_twice(z)
        self.assertEqual(self._links(), {("e1", "d1"): 100.0,
                                         ("e2", "d1"): 100.0})
        self.assertEqual(self._spend("e2"), 0.0)

    def test_a_deposit_restored_as_linked_by_hand_reads_the_same(self):
        txns = [_charge("e1", 100), _charge("e2", 100), _deposit("d1", 100)]
        restore.restore_zip(self.conn, _archive(
            txns, [_link("e1", "d1", 100, 6), _link("e2", "d1", 100, 7)]))
        hand = make_db()
        try:
            restore.restore_zip(hand, _archive(txns, []))
            data.link_reimbursements(hand, "d1", ["e1"], partial=True)
            self.assertIn("already fully applied", " ".join(
                data.link_reimbursements(hand, "d1", ["e2"],
                                         partial=True)["errors"]))
            self.assertEqual(self._links(hand), self._links())
            self.assertEqual(in_and_out(hand), in_and_out(self.conn))
        finally:
            hand.close()


# the inherited charge-side cases run in their own module, not twice
for _n in [n for n in vars(
        charge_side.ARestoreNeverBringsBackMorePaybackThanItsCharge)
        if n.startswith("test_")]:
    setattr(ARestoreNeverClaimsMoreOfADepositThanItBroughtIn, _n, None)


if __name__ == "__main__":
    unittest.main()
