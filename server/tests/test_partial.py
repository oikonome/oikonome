"""Partial reimbursement: netting semantics (received nets off the
expense row, remainder stays real spend), multi-deposit accumulation, the
awaiting badge's expected threshold, and unlink asymmetry."""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import data

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 14)
SINCE, UNTIL = dt.date(2026, 7, 1), dt.date(2026, 8, 1)


def _row(conn, txn_id):
    rows = budget._spend_rows(conn, SINCE, UNTIL)
    return next((r for r in rows if r["txn_id"] == txn_id), None)


def _cat(conn, txn_id):
    return conn.execute(
        "SELECT category_override FROM transactions WHERE id=%s",
        (txn_id,)).fetchone()["category_override"]


class PartialReimbTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.bill = add_txn(self.conn, TODAY, 500.0, "CEDAR FAMILY CLINIC",
                            primary="MEDICAL", account="chk")
        self.check = add_txn(self.conn, TODAY + dt.timedelta(days=1), -150.0,
                             "NORTHWIND INSURANCE", primary="INCOME",
                             account="chk")

    def tearDown(self):
        self.conn.close()

    def test_partial_nets_received_remainder_stays_spend(self):
        out = data.link_reimbursement(self.conn, self.bill, self.check,
                                      partial=True)
        self.assertEqual(out.get("received"), 150.0)
        row = _row(self.conn, self.bill)
        self.assertEqual(row["amount"], 350.0)          # 500 − 150
        self.assertIsNone(_cat(self.conn, self.bill))   # stays real spend
        self.assertEqual(_cat(self.conn, self.check), "TRANSFER_IN")

    def test_full_pair_unchanged(self):
        check = add_txn(self.conn, TODAY + dt.timedelta(days=1), -500.0,
                        "NORTHWIND INSURANCE", primary="INCOME", account="chk")
        data.link_reimbursement(self.conn, self.bill, check)
        self.assertEqual(_cat(self.conn, self.bill), "TRANSFER_OUT")
        self.assertEqual(_cat(self.conn, check), "TRANSFER_IN")
        self.assertIsNone(_row(self.conn, self.bill))   # excluded entirely

    def test_full_link_of_a_smaller_check_records_what_came_back(self):
        """A $150 check cannot repay a $500 bill in full: the link is
        recorded as $150 back and the other $350 stays spend."""
        r = data.link_reimbursement(self.conn, self.bill, self.check)
        self.assertEqual((r.get("partial"), r.get("received")), (True, 150.0))
        self.assertTrue(r.get("note"))
        self.assertIsNone(_cat(self.conn, self.bill))
        self.assertEqual(_row(self.conn, self.bill)["amount"], 350.0)

    def test_multiple_partials_accumulate(self):
        check2 = add_txn(self.conn, TODAY + dt.timedelta(days=9), -350.0,
                         "NORTHWIND INSURANCE", primary="INCOME", account="chk")
        data.link_reimbursement(self.conn, self.bill, self.check, partial=True)
        data.link_reimbursement(self.conn, self.bill, check2, partial=True)
        self.assertEqual(_row(self.conn, self.bill)["amount"], 0.0)

    def test_awaiting_clears_only_when_expected_covered(self):
        data.flag_reimbursement(self.conn, self.bill, partial=True,
                                expected=500.0)
        data.link_reimbursement(self.conn, self.bill, self.check, partial=True)
        pend = data.pending_reimbursements(self.conn)
        self.assertEqual(len(pend), 1)                  # 150 < 500: still waiting
        self.assertEqual(pend[0]["received"], 150.0)
        self.assertEqual(pend[0]["expected"], 500.0)
        check2 = add_txn(self.conn, TODAY + dt.timedelta(days=9), -350.0,
                         "NORTHWIND INSURANCE", primary="INCOME", account="chk")
        data.link_reimbursement(self.conn, self.bill, check2, partial=True)
        self.assertEqual(data.pending_reimbursements(self.conn), [])

    def test_awaiting_with_low_expected_clears_at_first_check(self):
        data.flag_reimbursement(self.conn, self.bill, partial=True,
                                expected=150.0)
        data.link_reimbursement(self.conn, self.bill, self.check, partial=True)
        self.assertEqual(data.pending_reimbursements(self.conn), [])

    def test_unlink_partial_restores_deposit_only(self):
        self.conn.execute(
            "UPDATE transactions SET category_override='MEDICAL' WHERE id=%s",
            (self.bill,))
        data.link_reimbursement(self.conn, self.bill, self.check, partial=True)
        data.unlink_reimbursement(self.conn, self.bill, self.check)
        self.assertEqual(_cat(self.conn, self.bill), "MEDICAL")  # untouched
        self.assertIsNone(_cat(self.conn, self.check))           # fell back
        self.assertEqual(_row(self.conn, self.bill)["amount"], 500.0)

    def test_pairs_expose_partial_and_received(self):
        data.link_reimbursement(self.conn, self.bill, self.check, partial=True)
        pairs = data.reimbursement_pairs(self.conn, self.bill)
        self.assertEqual((pairs[0]["partial"], pairs[0]["received"]),
                         (1, 150.0))


if __name__ == "__main__":
    unittest.main()
