"""A refund the bank feed withdraws repays nothing.

A charge linked to a refund leaves spend (a full pair) or is netted down
by it (a partial link). When the feed later takes the refund back, the row
is retired in place and the pair can outlive it while the removal path
looks for a re-numbered twin. Every reader must then see the charge as
unrepaid spend: the verdict's month spend, the reports' spend, and what
has come back against it for the awaiting list. The removal path itself
never releases the pair: a re-numbered twin can arrive on a later page,
sync or re-link, and must find the pair there to carry it. Once the
reconcile window has passed and the nightly sweep still finds no twin, the
sweep releases the pair so the charge loses the transfer category the full
link gave it. The mirror case holds too: a deposit whose charge is
withdrawn is income again.
"""
import unittest

from oikonome.engine import budget, reporting
from oikonome.web import data as d

from .util import TODAY, add_txn, make_db, write_config


def _withdraw(conn, txn_id):
    """What sync.base.mark_removed does to a row the feed withdraws."""
    conn.execute(
        "UPDATE transactions SET removed = 1, raw = COALESCE(raw, '{}'::jsonb)"
        " || jsonb_build_object(%s::text, now()) WHERE id = %s",
        (budget.FEED_REMOVED_KEY, txn_id))


class WithdrawnRefundTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _cat(self, tid):
        return self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_override"]

    def _status_spend(self):
        return round(budget.month_status(self.conn, TODAY)
                     ["variable_actual"], 2)

    def _report_spend(self):
        ym = TODAY
        return reporting.month_flows(self.conn, ym.year, ym.month)["out"]

    def _received(self, charge):
        return float(self.conn.execute(
            f"SELECT {budget.REIMB_RECEIVED} AS got FROM transactions t "
            f"WHERE t.id = %s", (charge,)).fetchone()["got"])

    def test_partial_link_on_a_withdrawn_refund_nets_nothing(self):
        charge = add_txn(self.conn, TODAY, 120.0, "GARDEN CENTER")
        refund = add_txn(self.conn, TODAY, -40.0, "GARDEN CENTER REFUND",
                         account="chk", primary="INCOME")
        r = d.link_reimbursement(self.conn, charge, refund, partial=True)
        self.assertEqual(r.get("received"), 40.0)
        self.assertEqual(self._status_spend(), 80.0)
        self.assertEqual(self._report_spend(), 80.0)
        _withdraw(self.conn, refund)
        self.assertEqual(self._status_spend(), 120.0)
        self.assertEqual(self._report_spend(), 120.0)
        self.assertEqual(self._received(charge), 0.0)
        # nothing came back, so the charge can wait on a reimbursement again
        self.assertIsNone(d.flag_reimbursement(self.conn, charge))
        pend = d.pending_reimbursements(self.conn)
        self.assertEqual([p["id"] if "id" in p else p["txn_id"]
                          for p in pend], [charge])
        self.assertEqual(float(pend[0]["received"]), 0.0)

    def test_full_pair_on_a_withdrawn_refund_is_spend_again(self):
        charge = add_txn(self.conn, TODAY, 120.0, "GARDEN CENTER")
        refund = add_txn(self.conn, TODAY, -120.0, "GARDEN CENTER REFUND",
                         account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, charge, refund)
        self.assertEqual(self._cat(charge), "TRANSFER_OUT")
        self.assertEqual(self._report_spend(), 0.0)
        _withdraw(self.conn, refund)
        # before the pair is released, nothing reads as having come back
        self.assertEqual(self._received(charge), 0.0)
        self.assertEqual(budget.release_withdrawn_pairs(
            self.conn, [refund]), 1)
        self.assertIsNone(self._cat(charge))
        self.assertEqual(d.reimbursement_pairs(self.conn, charge), [])
        self.assertEqual(self._status_spend(), 120.0)
        self.assertEqual(self._report_spend(), 120.0)
        self.assertIsNone(d.flag_reimbursement(self.conn, charge))
        self.assertEqual(float(d.pending_reimbursements(
            self.conn)[0]["received"]), 0.0)

    def _age_withdrawal(self, txn_id, days):
        self.conn.execute(
            "UPDATE transactions SET raw = raw || jsonb_build_object("
            "%s::text, now() - make_interval(days => %s)) WHERE id = %s",
            (budget.FEED_REMOVED_KEY, days, txn_id))

    def _full_pair_withdrawn_by_the_feed(self):
        from oikonome.sync import base
        charge = add_txn(self.conn, TODAY, 120.0, "GARDEN CENTER")
        refund = add_txn(self.conn, TODAY, -120.0, "GARDEN CENTER REFUND",
                         account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, charge, refund)
        self.assertEqual(self._cat(charge), "TRANSFER_OUT")
        self.assertEqual(base.mark_removed(self.conn, [refund]), 1)
        return charge, refund

    def test_the_removal_delta_keeps_the_pair_for_a_twin_to_carry(self):
        """The feed's own removal path: the pair stays (a twin may still be
        on its way), and nothing reads as having come back meanwhile."""
        charge, refund = self._full_pair_withdrawn_by_the_feed()
        self.assertEqual(len(d.reimbursement_pairs(self.conn, charge)), 1)
        self.assertEqual(self._received(charge), 0.0)

    def test_a_twin_arriving_later_carries_the_pair(self):
        from oikonome.sync import adopt, reanchor
        charge, refund = self._full_pair_withdrawn_by_the_feed()
        self._age_withdrawal(refund, 2)
        twin = add_txn(self.conn, TODAY, -120.0, "GARDEN CENTER REFUND",
                       account="chk", primary="INCOME")
        out = reanchor.reanchor_stranded(
            self.conn, release_after_days=adopt.RECONCILE_DAYS)
        self.assertEqual(out["moved"], 1)
        self.assertEqual(out["released"], 0)
        pairs = d.reimbursement_pairs(self.conn, charge)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(self._cat(charge), "TRANSFER_OUT")
        self.assertEqual(self._received(charge), 120.0)
        self.assertNotEqual(twin, refund)

    def test_the_sweep_waits_the_window_before_releasing(self):
        from oikonome.sync import adopt, reanchor
        charge, refund = self._full_pair_withdrawn_by_the_feed()
        out = reanchor.reanchor_stranded(
            self.conn, release_after_days=adopt.RECONCILE_DAYS)
        self.assertEqual(out["released"], 0)
        self.assertEqual(self._cat(charge), "TRANSFER_OUT")
        self._age_withdrawal(refund, adopt.RECONCILE_DAYS + 1)
        out = reanchor.reanchor_stranded(
            self.conn, release_after_days=adopt.RECONCILE_DAYS)
        self.assertEqual(out["released"], 1)
        self.assertIsNone(self._cat(charge))
        self.assertEqual(d.reimbursement_pairs(self.conn, charge), [])
        self.assertEqual(self._report_spend(), 120.0)

    def test_an_unreadable_withdrawal_stamp_holds_back_only_its_own_row(self):
        """A restored row can carry a stamp that is not a time. That row is
        skipped (never old enough) and named in the log; every other row's
        release still happens."""
        from oikonome.sync import adopt
        charge, refund = self._full_pair_withdrawn_by_the_feed()
        self._age_withdrawal(refund, adopt.RECONCILE_DAYS + 1)
        other = add_txn(self.conn, TODAY, 60.0, "SEED SHOP")
        back = add_txn(self.conn, TODAY, -60.0, "SEED SHOP REFUND",
                       account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, other, back)
        self.conn.execute(
            "UPDATE transactions SET removed = 1, raw = COALESCE(raw, "
            "'{}'::jsonb) || jsonb_build_object(%s::text, 'yes') "
            "WHERE id = %s", (budget.FEED_REMOVED_KEY, back))
        with self.assertLogs("oikonome.budget", "WARNING") as logs:
            self.assertEqual(budget.release_withdrawn_pairs(
                self.conn, [refund, back],
                older_than_days=adopt.RECONCILE_DAYS), 1)
        self.assertIn(back, " ".join(logs.output))
        self.assertIsNone(self._cat(charge))
        self.assertEqual(self._cat(other), "TRANSFER_OUT")

    def test_an_ambiguous_twin_is_never_released(self):
        from oikonome.sync import adopt, reanchor
        charge, refund = self._full_pair_withdrawn_by_the_feed()
        self._age_withdrawal(refund, adopt.RECONCILE_DAYS + 1)
        for _ in range(2):
            add_txn(self.conn, TODAY, -120.0, "GARDEN CENTER REFUND",
                    account="chk", primary="INCOME")
        out = reanchor.reanchor_stranded(
            self.conn, release_after_days=adopt.RECONCILE_DAYS)
        self.assertEqual(out["ambiguous"], 1)
        self.assertEqual(out["released"], 0)
        self.assertEqual(self._cat(charge), "TRANSFER_OUT")

    def test_release_leaves_pairs_on_rows_the_feed_did_not_withdraw(self):
        """A retired pending hold, a merged row and a live row keep their
        pairs: only the feed's own withdrawal of a posted row means the
        money is gone."""
        charge = add_txn(self.conn, TODAY, 120.0, "GARDEN CENTER")
        refund = add_txn(self.conn, TODAY, -120.0, "GARDEN CENTER REFUND",
                         account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, charge, refund)
        self.assertEqual(budget.release_withdrawn_pairs(
            self.conn, [refund, charge]), 0)
        self.conn.execute("UPDATE transactions SET removed = 1 WHERE id = %s",
                          (refund,))
        self.assertEqual(budget.release_withdrawn_pairs(
            self.conn, [refund]), 0)
        self.assertEqual(self._cat(charge), "TRANSFER_OUT")

    def test_a_deposit_whose_charge_was_withdrawn_is_income_again(self):
        pay = add_txn(self.conn, TODAY, -3000.0, "EMPLOYER DIRECT DEP",
                      account="chk", primary="INCOME")
        charge = add_txn(self.conn, TODAY, 200.0, "CONFERENCE FEE")
        d.link_reimbursement(self.conn, charge, pay, partial=True)
        self.assertEqual(reporting.month_flows(
            self.conn, TODAY.year, TODAY.month)["in"], 2800.0)
        _withdraw(self.conn, charge)
        self.assertEqual(reporting.month_flows(
            self.conn, TODAY.year, TODAY.month)["in"], 3000.0)

    def test_a_link_to_a_withdrawn_charge_claims_none_of_the_deposit(self):
        """What a deposit has left to give, and whether it is used up, read
        its links to live charges only: once the charge it repaid is gone,
        the deposit can repay another."""
        pay = add_txn(self.conn, TODAY, -200.0, "CLIENT REPAYMENT",
                      account="chk", primary="INCOME")
        first = add_txn(self.conn, TODAY, 200.0, "CONFERENCE FEE")
        d.link_reimbursement(self.conn, first, pay, partial=True)
        self.assertTrue(d._deposit_fully_applied(self.conn, pay))
        _withdraw(self.conn, first)
        self.assertFalse(d._deposit_fully_applied(self.conn, pay))
        second = add_txn(self.conn, TODAY, 150.0, "HOTEL")
        r = d.link_reimbursement(self.conn, second, pay, partial=True)
        self.assertEqual(r.get("received"), 150.0)

    def test_an_old_style_stamped_deposit_whose_charge_left_is_income(self):
        """A deposit stamped TRANSFER_IN with a partial link to a charge
        that has since been withdrawn: nothing claims it any more."""
        pay = add_txn(self.conn, TODAY, -200.0, "CLIENT REPAYMENT",
                      account="chk", primary="INCOME", override="TRANSFER_IN")
        charge = add_txn(self.conn, TODAY, 200.0, "CONFERENCE FEE")
        self.conn.execute(
            "INSERT INTO reimbursements (expense_id, reimburse_id, partial,"
            " amount) VALUES (%s, %s, 1, 200)", (charge, pay))
        self.assertEqual(reporting.month_flows(
            self.conn, TODAY.year, TODAY.month)["in"], 0.0)
        _withdraw(self.conn, charge)
        self.assertEqual(reporting.month_flows(
            self.conn, TODAY.year, TODAY.month)["in"], 200.0)


if __name__ == "__main__":
    unittest.main()
