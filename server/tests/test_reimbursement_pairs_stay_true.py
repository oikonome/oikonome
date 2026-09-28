"""Reimbursement pairs stay true to the rows they name.

A pair's full-or-partial choice and a partial link's amount are made from
the amounts and links there are when it is made. Whatever changes those
afterwards — another link on the same deposit removed, a pending row that
posts for a different amount, a paired row deleted by an import undo or an
account removal — must leave the pairs, the transfer categories the links
wrote and the income and spend figures saying what really happened. The
unlink and flag doors report only what they did, and a check anchoring
many charges costs a bounded number of queries per charge.
"""

import unittest
from unittest import mock

from oikonome.engine import reporting
from oikonome.sync import base, batches
from oikonome.web import data as d

from .util import TODAY, add_txn, make_db, write_config


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _cat(self, tid):
        return self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_override"]

    def _flows(self):
        return reporting.month_flows(self.conn, TODAY.year, TODAY.month)

    def _pair(self, e, r):
        return self.conn.execute(
            "SELECT partial, amount FROM reimbursements "
            "WHERE expense_id=%s AND reimburse_id=%s", (e, r)).fetchone()


class SharedCheckUnlinkTests(_Base):
    def test_unlinking_one_charge_returns_its_share_of_the_check_to_income(self):
        """One check repays two charges linked from their own pages: the
        first becomes a partial link (the check is bigger), the second a
        full pair. Removing the first must leave the check counting as
        income in the part no charge claims — not a whole-check transfer
        that hides that share from income for good."""
        check = add_txn(self.conn, TODAY, -1000.0, "INSURANCE CLAIM",
                        account="chk", primary="INCOME")
        a = add_txn(self.conn, TODAY, 400.0, "CLINIC VISIT", primary="MEDICAL")
        b = add_txn(self.conn, TODAY, 600.0, "HOSPITAL BILL", primary="MEDICAL")
        self.assertTrue(d.link_reimbursement(self.conn, a, check)["partial"])
        self.assertNotIn("partial", d.link_reimbursement(self.conn, b, check))
        self.assertAlmostEqual(self._flows()["in"], 0.0, places=2)
        d.unlink_reimbursement(self.conn, a, check)
        flows = self._flows()
        self.assertAlmostEqual(flows["in"], 400.0, places=2)
        self.assertAlmostEqual(flows["out"], 400.0, places=2)
        self.assertNotEqual(self._cat(check), "TRANSFER_IN")
        # the hospital bill is still repaid in full, now as what it is
        pair = self._pair(b, check)
        self.assertEqual((pair["partial"], pair["amount"]), (1, 600.0))
        self.assertNotEqual(self._cat(b), "TRANSFER_OUT")


class SettlementRecheckTests(_Base):
    def _post(self, pending_id, posted_id, amount, account, name, primary):
        base.upsert_transactions(self.conn, [base.Transaction(
            id=posted_id, account_id=account, date=TODAY, amount=amount,
            name=name, category_primary=primary,
            raw={"pending_transaction_id": pending_id})])

    def test_charge_posting_with_a_tip_keeps_the_tip_as_spend(self):
        """A $50 pending charge fully paired to a $50 repayment posts at
        $60: the repayment covers $50 of it, and the other $10 is spend."""
        dep = add_txn(self.conn, TODAY, -50.0, "FRIEND PAYBACK",
                      account="chk", primary="INCOME")
        pend = add_txn(self.conn, TODAY, 50.0, "DINER", primary="FOOD",
                       pending=1, txn_id="pend-diner")
        self.assertNotIn("partial", d.link_reimbursement(self.conn, pend, dep))
        self._post("pend-diner", "posted-diner", 60.0, "card", "DINER", "FOOD")
        pair = self._pair("posted-diner", dep)
        self.assertEqual((pair["partial"], pair["amount"]), (1, 50.0))
        self.assertNotEqual(self._cat("posted-diner"), "TRANSFER_OUT")
        self.assertEqual(self._cat(dep), "TRANSFER_IN")
        self.assertAlmostEqual(self._flows()["out"], 10.0, places=2)
        self.assertAlmostEqual(self._flows()["in"], 0.0, places=2)

    def test_charge_posting_for_less_leaves_the_rest_of_the_deposit_income(self):
        """Posted $10 under the pending amount, the charge is still fully
        repaid, and the $10 of the deposit it no longer uses is income."""
        dep = add_txn(self.conn, TODAY, -60.0, "FRIEND PAYBACK",
                      account="chk", primary="INCOME")
        pend = add_txn(self.conn, TODAY, 60.0, "DINER", primary="FOOD",
                       pending=1, txn_id="pend-diner2")
        d.link_reimbursement(self.conn, pend, dep)
        self._post("pend-diner2", "posted-diner2", 50.0, "card", "DINER",
                   "FOOD")
        self.assertAlmostEqual(self._flows()["in"], 10.0, places=2)
        self.assertAlmostEqual(self._flows()["out"], 0.0, places=2)
        self.assertNotEqual(self._cat(dep), "TRANSFER_IN")

    def test_deposit_posting_for_less_leaves_the_rest_of_the_charge_spend(self):
        """A pending repayment posts at $40 against a $50 charge fully
        paired to it: $40 came back, $10 is still spend."""
        charge = add_txn(self.conn, TODAY, 50.0, "BOOKSHOP")
        add_txn(self.conn, TODAY, -50.0, "EMPLOYER REIMB", account="chk",
                primary="INCOME", pending=1, txn_id="pend-pay")
        d.link_reimbursement(self.conn, charge, "pend-pay")
        self._post("pend-pay", "posted-pay", -40.0, "chk", "EMPLOYER REIMB",
                   "INCOME")
        pair = self._pair(charge, "posted-pay")
        self.assertEqual((pair["partial"], pair["amount"]), (1, 40.0))
        self.assertAlmostEqual(self._flows()["out"], 10.0, places=2)
        self.assertAlmostEqual(self._flows()["in"], 0.0, places=2)

    def test_an_unchanged_amount_leaves_a_full_pair_alone(self):
        dep = add_txn(self.conn, TODAY, -50.0, "FRIEND PAYBACK",
                      account="chk", primary="INCOME")
        add_txn(self.conn, TODAY, 50.0, "DINER", primary="FOOD", pending=1,
                txn_id="pend-same")
        d.link_reimbursement(self.conn, "pend-same", dep)
        self._post("pend-same", "posted-same", 50.0, "card", "DINER", "FOOD")
        self.assertEqual(self._pair("posted-same", dep)["partial"], 0)
        self.assertEqual(self._cat("posted-same"), "TRANSFER_OUT")

    def test_business_flag_follows_the_charge_when_it_posts(self):
        """A charge marked business while pending is still marked once the
        bank posts it under a new id."""
        add_txn(self.conn, TODAY, 80.0, "PRINT SHOP", pending=1,
                txn_id="pend-biz")
        d.flag_business(self.conn, "pend-biz")
        self._post("pend-biz", "posted-biz", 80.0, "card", "PRINT SHOP",
                   "GENERAL_MERCHANDISE")
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM business_flags WHERE txn_id='posted-biz'"
        ).fetchone())


class DeletedRowReleasesItsPartnerTests(_Base):
    def test_removing_the_charges_account_frees_the_deposit(self):
        """Deleting an account's rows removes their pairs by cascade; the
        deposit left behind must not stay a transfer for a pair that no
        longer exists."""
        dep = add_txn(self.conn, TODAY, -75.0, "EMPLOYER REIMB",
                      account="chk", primary="INCOME")
        charge = add_txn(self.conn, TODAY, 75.0, "TRAIN FARE")
        d.link_reimbursement(self.conn, charge, dep)
        self.assertEqual(self._cat(dep), "TRANSFER_IN")
        base.purge_account_data(self.conn, "card")
        self.assertIsNone(self._cat(dep))
        self.assertAlmostEqual(self._flows()["in"], 75.0, places=2)

    def test_undoing_an_import_frees_the_charge_its_deposit_repaid(self):
        charge = add_txn(self.conn, TODAY, 75.0, "TRAIN FARE")
        dep = add_txn(self.conn, TODAY, -75.0, "EMPLOYER REIMB",
                      account="chk", primary="INCOME")
        self.conn.execute(
            "UPDATE transactions SET raw = %s::jsonb WHERE id = %s",
            ('{"_batches": ["batch-x"], "_batch": "batch-x"}', dep))
        d.link_reimbursement(self.conn, charge, dep)
        self.assertEqual(self._cat(charge), "TRANSFER_OUT")
        self.assertEqual(int(batches.rollback(self.conn, "batch-x")), 1)
        self.assertIsNone(self._cat(charge))
        self.assertAlmostEqual(self._flows()["out"], 75.0, places=2)


class DoorsReportWhatTheyDidTests(_Base):
    def test_unlink_says_whether_a_pair_was_removed(self):
        dep = add_txn(self.conn, TODAY, -20.0, "REFUND", account="chk",
                      primary="INCOME")
        charge = add_txn(self.conn, TODAY, 20.0, "CAFE")
        d.link_reimbursement(self.conn, charge, dep)
        self.assertTrue(d.unlink_reimbursement(self.conn, charge, dep))
        self.assertFalse(d.unlink_reimbursement(self.conn, charge, dep))

    def test_a_repaid_charge_cannot_be_flagged_as_awaiting_again(self):
        """A flag sent from a list shown before the match was made must not
        put a fully repaid charge back on the awaiting list."""
        dep = add_txn(self.conn, TODAY, -20.0, "REFUND", account="chk",
                      primary="INCOME")
        charge = add_txn(self.conn, TODAY, 20.0, "CAFE")
        d.flag_reimbursement(self.conn, charge)
        d.link_reimbursement(self.conn, charge, dep)
        self.assertTrue(d.flag_reimbursement(self.conn, charge, partial=True))
        self.assertEqual(d.bulk_apply(self.conn, [charge], "reimb_flag")
                         ["applied"], 0)
        self.assertEqual(d.pending_reimbursements(self.conn), [])

    def test_a_bulk_flag_keeps_the_expectation_already_recorded(self):
        charge = add_txn(self.conn, TODAY, 90.0, "OPTICIAN")
        d.flag_reimbursement(self.conn, charge, partial=True, expected=60.0)
        d.bulk_apply(self.conn, [charge], "reimb_flag")
        (row,) = d.pending_reimbursements(self.conn)
        self.assertEqual((row["partial"], row["expected"]), (1, 60.0))


class CheckAnchoringManyChargesTests(_Base):
    def test_charge_rooms_are_read_a_bounded_number_of_times(self):
        """A small check anchoring many charges is used up by the first;
        every later charge is refused. Weighing each one against every
        other selected charge from the database would be N² queries under
        the tenant lock."""
        check = add_txn(self.conn, TODAY, -10.0, "SMALL REFUND",
                        account="chk", primary="INCOME")
        charges = [add_txn(self.conn, TODAY, 10.0 + i, f"CHARGE {i}")
                   for i in range(40)]
        real = d._charge_room
        calls = []

        def counting(conn, expense):
            calls.append(expense["id"])
            return real(conn, expense)

        with mock.patch.object(d, "_charge_room", counting):
            out = d.link_reimbursements(self.conn, check, charges)
        self.assertEqual(out["linked"], 1)
        self.assertEqual(len(out["errors"]), 39)
        self.assertLessEqual(len(calls), 4 * len(charges))


if __name__ == "__main__":
    unittest.main()
