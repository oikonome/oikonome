"""A person's "not this charge" follows the charge when it changes id.

Taking a receipt off a charge is remembered by the charge's id, so the
matcher never pairs the two again. A charge does not keep one id for life:
a hold settles into a posted row under a new id, and a retired row is
re-anchored onto its live twin. The memory has to move with it — otherwise
the matcher, running in the very sync that delivers the posted row, sees a
fresh exact-cents charge and pairs back the receipt the person rejected.
"""
import unittest

from oikonome.engine import receipt_match
from oikonome.sync import base as sync_base, reanchor

from .test_a_receipt_snapped_early_meets_its_charge import (
    DAY, _attached, _enable_llm, _post, _snap)
from .util import add_txn, make_db, write_config


def _memory(conn, rid):
    return list(conn.execute(
        "SELECT unmatched_txn_ids FROM receipts WHERE id = %s",
        (rid,)).fetchone()["unmatched_txn_ids"])


class UnmatchFollowsTheChargeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rejected_on_hold(self, hold):
        rid = _snap(self.conn)
        _post(self.conn, hold, 42.17, "MAPLE LEAF BISTRO", pending=True,
              date=DAY)
        self.assertEqual(_attached(self.conn, rid), (hold, "auto"))
        self.assertEqual(receipt_match.unmatch(self.conn, rid), hold)
        return rid

    def test_an_unmatch_on_a_hold_survives_the_hold_settling(self):
        rid = self._rejected_on_hold("hold-u1")
        _post(self.conn, "post-u1", 42.17, "MAPLE LEAF BISTRO",
              raw={"pending_transaction_id": "hold-u1"})
        self.assertEqual(_attached(self.conn, rid), (None, None))
        self.assertEqual(_memory(self.conn, rid), ["post-u1"])
        w = receipt_match.waiting(self.conn)["waiting"][0]
        self.assertNotIn("post-u1", [c["txn_id"] for c in w["candidates"]])
        # the retired hold is not left looking like it has work to carry
        self.assertEqual(reanchor.stranded(self.conn, ["hold-u1"]), [])

    def test_a_refusal_alone_does_not_send_the_re_anchor_guessing(self):
        rid = self._rejected_on_hold("hold-u2")
        # a same-cents charge is already on the books under another id; the
        # re-anchor would have to GUESS it is the hold's twin, and a guess
        # can land on a different purchase — so a refusal is not, on its
        # own, a reason to re-anchor the retired hold
        add_txn(self.conn, DAY, 42.17, "MAPLE LEAF BISTRO", txn_id="twin-u2")
        sync_base.mark_removed(self.conn, ["hold-u2"])
        self.assertEqual(_memory(self.conn, rid), ["hold-u2"])
        self.assertEqual(reanchor.stranded(self.conn, ["hold-u2"]), [])
        # the matcher still keeps the retired hold's look-alike off
        receipt_match.match_waiting(self.conn)
        self.assertEqual(_attached(self.conn, rid), (None, None))

    def test_the_nightly_sweep_leaves_a_refusal_only_row_alone(self):
        rid = self._rejected_on_hold("hold-u3")
        add_txn(self.conn, DAY, 42.17, "MAPLE LEAF BISTRO", txn_id="twin-u3")
        self.conn.execute(
            "UPDATE transactions SET removed = 1 WHERE id = 'hold-u3'")
        out = reanchor.reanchor_stranded(self.conn)
        self.assertEqual(out["moved"], 0)
        self.assertEqual(out.get("ambiguous", 0), 0)
        self.assertEqual(_memory(self.conn, rid), ["hold-u3"])

    def test_a_row_re_anchored_for_its_own_reasons_takes_the_refusal(self):
        rid = self._rejected_on_hold("hold-u6")
        # a person's category on the hold is its own reason to re-anchor
        self.conn.execute(
            "UPDATE transactions SET category_override = 'FOOD_AND_DRINK'"
            " WHERE id = 'hold-u6'")
        add_txn(self.conn, DAY, 42.17, "MAPLE LEAF BISTRO", txn_id="twin-u6")
        self.conn.execute(
            "UPDATE transactions SET removed = 1 WHERE id = 'hold-u6'")
        self.assertEqual(reanchor.reanchor_stranded(self.conn)["moved"], 1)
        self.assertEqual(_memory(self.conn, rid), ["twin-u6"])

    def test_a_refusal_of_the_twin_itself_is_not_doubled(self):
        rid = self._rejected_on_hold("hold-u4")
        self.conn.execute(
            "UPDATE receipts SET unmatched_txn_ids = unmatched_txn_ids"
            " || 'post-u4'::text WHERE id = %s", (rid,))
        _post(self.conn, "post-u4", 42.17, "MAPLE LEAF BISTRO",
              raw={"pending_transaction_id": "hold-u4"})
        self.assertEqual(_memory(self.conn, rid), ["post-u4"])

    def test_a_hold_that_is_not_retired_keeps_its_own_refusal(self):
        # a successor naming a row that was never a hold does not retire
        # it, so the refusal stays on the row that is still live
        rid = _snap(self.conn)
        add_txn(self.conn, DAY, 42.17, "MAPLE LEAF BISTRO", txn_id="live-u5")
        receipt_match.match_waiting(self.conn)
        self.assertEqual(receipt_match.unmatch(self.conn, rid), "live-u5")
        _post(self.conn, "post-u5", 42.17, "MAPLE LEAF BISTRO",
              raw={"pending_transaction_id": "live-u5"})
        self.assertIn("live-u5", _memory(self.conn, rid))


if __name__ == "__main__":
    unittest.main()
