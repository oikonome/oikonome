"""A restore never brings back more payback than its charge cost
(specs/restore-landers.md, "Payback past a charge").

The link door holds a charge's partial links to what the charge cost, but
an archive can carry links past it: a $60 charge with $110 of links from a
$110 deposit. Restored as it was, the charge's spend floors at $0 and the
deposit's $50 that nothing repaid is gone from income. The restore cuts
the partial links on such a charge back to its cost — full pairs counted
as budget.REIMB_RECEIVED counts them, the newest link first, a link cut to
nothing removed, a link on a deposit the feed withdrew neither counted nor
cut — so the household reads as one linked by hand would, and a second
restore of the same archive changes nothing.

Synthetic numbers throughout.
"""

import unittest

from oikonome.engine import budget, reporting
from oikonome.sync import restore
from oikonome.web import data

from .test_reimbursements_net_within_both_sides import in_and_out
from .test_restore_v2 import _zip
from .util import _ensure_db, make_db

_TXN_COLS = ["id", "account_id", "date", "amount", "name",
             "category_primary", "removed"]
_LINK_COLS = ["expense_id", "reimburse_id", "partial", "amount",
              "created_at"]


def _charge(tid, amount):
    return {"id": tid, "account_id": "card", "date": "2026-07-01",
            "amount": amount, "name": "INVENTED HOTEL",
            "category_primary": "TRAVEL", "removed": 0}


def _deposit(tid, amount, removed=0):
    return {"id": tid, "account_id": "chk", "date": "2026-07-05",
            "amount": -amount, "name": "INVENTED PAYBACK",
            "category_primary": "INCOME", "removed": removed}


def _link(expense, deposit, amount, created, partial=1):
    return {"expense_id": expense, "reimburse_id": deposit,
            "partial": partial, "amount": amount if partial else None,
            "created_at": f"2026-07-{created:02d} 10:00:00"}


def _archive(txns, links):
    return _zip({"transactions": (_TXN_COLS, txns),
                 "reimbursements": (_LINK_COLS, links)})


class ARestoreNeverBringsBackMorePaybackThanItsCharge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _links(self, conn=None):
        return {(r["expense_id"], r["reimburse_id"]):
                None if r["amount"] is None else round(float(r["amount"]), 2)
                for r in (conn or self.conn).execute(
                    "SELECT expense_id, reimburse_id, amount "
                    "FROM reimbursements").fetchall()}

    def _spend(self, tid):
        return round(float(self.conn.execute(
            f"SELECT {budget.NET_AMOUNT} AS v FROM transactions t "
            "WHERE t.id = %s", (tid,)).fetchone()["v"]), 2)

    def _income(self, tid):
        return round(float(self.conn.execute(
            f"SELECT {reporting.INCOME_NET} AS v FROM transactions t "
            "WHERE t.id = %s", (tid,)).fetchone()["v"]), 2)

    def _restore_twice(self, z):
        """Restore `z`, then again; the second must change nothing."""
        _r, out = restore.restore_outcomes(self.conn, z)
        links, flows = self._links(), in_and_out(self.conn)
        _r2, again = restore.restore_outcomes(self.conn, z)
        self.assertEqual(self._links(), links, "a second restore cut again")
        self.assertEqual(in_and_out(self.conn), flows)
        self.assertEqual(again.written.get("reimbursements", 0), 0)
        self.assertNotIn(("reimbursements", "payback_trimmed"),
                         again.repaired)
        return out, again

    def test_links_past_the_charge_are_cut_and_the_rest_is_income(self):
        z = _archive([_charge("e1", 60), _deposit("d1", 110)],
                     [_link("e1", "d1", 110, 6)])
        out, _ = self._restore_twice(z)
        self.assertEqual(self._links(), {("e1", "d1"): 60.0})
        self.assertEqual(self._spend("e1"), 0.0)
        self.assertEqual(self._income("d1"), 50.0)    # the $50 comes back
        self.assertEqual(out.repaired.get(
            ("reimbursements", "payback_trimmed")), 1)
        self.assertEqual(out.written.get("reimbursements"), 1)
        fresh = make_db()
        try:
            notes = restore.restore_zip(fresh, z).get("_notes") or []
        finally:
            fresh.close()
        self.assertTrue(any("cut to it" in n for n in notes), notes)
        # the same household linked by hand reads the same
        hand = make_db()
        try:
            restore.restore_zip(hand, _archive(
                [_charge("e1", 60), _deposit("d1", 110)], []))
            data.link_reimbursements(hand, "e1", ["d1"], partial=True)
            self.assertEqual(self._links(hand), self._links())
            self.assertEqual(in_and_out(hand), in_and_out(self.conn))
        finally:
            hand.close()

    def test_the_newest_link_is_cut_first(self):
        z = _archive([_charge("e1", 60), _deposit("d1", 40),
                      _deposit("d2", 40)],
                     [_link("e1", "d1", 40, 6), _link("e1", "d2", 40, 7)])
        self._restore_twice(z)
        self.assertEqual(self._links(), {("e1", "d1"): 40.0,
                                         ("e1", "d2"): 20.0})
        self.assertEqual(self._spend("e1"), 0.0)
        self.assertEqual(self._income("d1"), 0.0)
        self.assertEqual(self._income("d2"), 20.0)

    def test_a_full_pair_counts_and_a_link_with_no_room_left_is_left_out(self):
        z = _archive([_charge("e1", 60), _deposit("d1", 60),
                      _deposit("d2", 10)],
                     [_link("e1", "d1", None, 6, partial=0),
                      _link("e1", "d2", 10, 7)])
        out, again = self._restore_twice(z)
        self.assertEqual(self._links(), {("e1", "d1"): None})
        self.assertEqual(self._income("d2"), 10.0)
        for o in (out, again):
            self.assertEqual(o.refused.get(("reimbursements", "over_cap")), 1)
            self.assertEqual(o.written.get("reimbursements", 0),
                             1 if o is out else 0)

    def test_a_link_on_a_withdrawn_deposit_is_neither_counted_nor_cut(self):
        z = _archive([_charge("e1", 60), _deposit("d1", 50, removed=1),
                      _deposit("d2", 60)],
                     [_link("e1", "d1", 50, 8), _link("e1", "d2", 60, 6)])
        out, _ = self._restore_twice(z)
        self.assertEqual(self._links(), {("e1", "d1"): 50.0,
                                         ("e1", "d2"): 60.0})
        self.assertEqual(self._spend("e1"), 0.0)
        self.assertNotIn(("reimbursements", "payback_trimmed"), out.repaired)

    def test_a_charge_with_no_amount_keeps_its_partial_links(self):
        # restored at 0 for want of an amount; its links wait for the
        # amount the person puts back, and its refund stays netted
        blank = _charge("e1", "")
        z = _archive([blank, _deposit("d1", 40)],
                     [_link("e1", "d1", 40, 6)])
        out, _ = self._restore_twice(z)
        self.assertEqual(self._links(), {("e1", "d1"): 40.0})
        self.assertEqual(self._income("d1"), 0.0)
        self.assertNotIn(("reimbursements", "over_cap"), out.refused)
        self.assertNotIn(("reimbursements", "payback_trimmed"), out.repaired)
        self.assertEqual(out.written.get("reimbursements"), 1)

    def test_a_malformed_link_ahead_does_not_hide_an_over_linked_charge(self):
        bad = _link("e1", "d1", 20, 5)
        bad["partial"] = "x"
        z = _archive([_charge("e1", 30), _deposit("d1", 20),
                      _charge("e3", 60), _deposit("d3", 110)],
                     [bad, _link("e3", "d3", 110, 6)])
        out, _ = self._restore_twice(z)
        self.assertEqual(self._links(), {("e3", "d3"): 60.0})
        self.assertEqual(self._income("d3"), 50.0)
        self.assertEqual(out.repaired.get(
            ("reimbursements", "payback_trimmed")), 1)
        self.assertEqual(out.refused.get(("reimbursements", "malformed")), 1)
        self.assertNotIn(("reimbursements", "over_cap"), out.refused)
        self.assertEqual(out.written.get("reimbursements"), 1)

    def test_a_charge_within_its_cost_is_left_as_archived(self):
        z = _archive([_charge("e1", 60), _deposit("d1", 110)],
                     [_link("e1", "d1", 45, 6)])
        out, _ = self._restore_twice(z)
        self.assertEqual(self._links(), {("e1", "d1"): 45.0})
        self.assertEqual(self._spend("e1"), 15.0)
        self.assertEqual(self._income("d1"), 65.0)
        self.assertEqual(out.repaired, {})


if __name__ == "__main__":
    unittest.main()
