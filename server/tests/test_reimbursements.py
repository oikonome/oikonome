"""Explicit reimbursement pairing (web/data): link/unlink lifecycle,
auto-orientation, transfer semantics, shared-deposit refcounting, awaiting
flags, candidate scoping, multi-select."""

import datetime as dt
import unittest

from oikonome.web import data as d

from .util import TODAY, add_txn, make_db, write_config


class ReimbursementTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.exp = add_txn(self.conn, TODAY, 200.0, "OFFICE SUPPLY CO")
        self.dep = add_txn(self.conn, TODAY, -200.0, "EMPLOYER REIMB",
                           account="chk", primary="INCOME")

    def tearDown(self):
        self.conn.close()

    def _cat(self, tid):
        return self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_override"]

    def test_link_sets_transfer_semantics_and_auto_orients(self):
        # pass the DEPOSIT first — orientation must come from signs
        r = d.link_reimbursement(self.conn, self.dep, self.exp)
        self.assertEqual(r, {"expense": self.exp, "reimburse": self.dep})
        self.assertEqual(self._cat(self.exp), "TRANSFER_OUT")
        self.assertEqual(self._cat(self.dep), "TRANSFER_IN")
        self.assertEqual(len(d.reimbursement_pairs(self.conn, self.exp)), 1)

    def test_same_direction_rejected(self):
        exp2 = add_txn(self.conn, TODAY, 50.0, "OTHER CHARGE")
        r = d.link_reimbursement(self.conn, self.exp, exp2)
        self.assertIn("error", r)
        self.assertIsNone(self._cat(self.exp))

    def test_unlink_restores_categories(self):
        d.link_reimbursement(self.conn, self.exp, self.dep)
        d.unlink_reimbursement(self.conn, self.exp, self.dep)
        self.assertIsNone(self._cat(self.exp))
        self.assertIsNone(self._cat(self.dep))
        self.assertEqual(d.reimbursement_pairs(self.conn, self.exp), [])

    def test_recent_pairs_lists_both_sides_newest_first(self):
        """The Matched list both clients render as the undo surface: every
        pair with each side's payee, date and amount, newest first — a
        wrong match leaves the pending list, so this list is the only
        place it can be seen and unlinked."""
        d.link_reimbursement(self.conn, self.exp, self.dep)
        exp2 = add_txn(self.conn, TODAY, 80.0, "SECOND CHARGE")
        dep2 = add_txn(self.conn, TODAY, -80.0, "SECOND PAYBACK",
                       account="chk", primary="INCOME")
        d.link_reimbursement(self.conn, exp2, dep2)
        rows = d.recent_reimbursement_pairs(self.conn)
        self.assertEqual(len(rows), 2)
        by_exp = {r["expense_id"]: r for r in rows}
        self.assertIn(self.exp, by_exp)
        r = by_exp[self.exp]
        self.assertEqual(r["reimburse_id"], self.dep)
        self.assertEqual(r["expense_amount"], 200.0)
        self.assertEqual(r["deposit_amount"], -200.0)
        self.assertTrue(r["expense_payee"])
        self.assertTrue(r["deposit_payee"])
        # unlink empties it and both sides fall back — the undo really undoes
        d.unlink_reimbursement(self.conn, self.exp, self.dep)
        self.assertEqual(len(d.recent_reimbursement_pairs(self.conn)), 1)
        self.assertIsNone(self._cat(self.exp))

    def test_shared_deposit_keeps_override_until_last_unlink(self):
        """One check covering two expenses: unlinking one keeps the deposit
        classified until the last pair goes."""
        exp2 = add_txn(self.conn, TODAY, 100.0, "OFFICE SUPPLY CO 2")
        d.link_reimbursement(self.conn, self.exp, self.dep)
        d.link_reimbursement(self.conn, exp2, self.dep)
        d.unlink_reimbursement(self.conn, self.exp, self.dep)
        self.assertIsNone(self._cat(self.exp))          # freed
        self.assertEqual(self._cat(self.dep), "TRANSFER_IN")   # still paired
        d.unlink_reimbursement(self.conn, exp2, self.dep)
        self.assertIsNone(self._cat(self.dep))

    def test_flag_lifecycle(self):
        """Flag = a to-remember list entry, never a category change."""
        d.flag_reimbursement(self.conn, self.exp)
        d.flag_reimbursement(self.conn, self.exp)   # idempotent
        pend = d.pending_reimbursements(self.conn)
        self.assertEqual([p["id"] for p in pend], [self.exp])
        self.assertIsNone(self._cat(self.exp))      # category untouched
        d.unflag_reimbursement(self.conn, self.exp)
        self.assertEqual(d.pending_reimbursements(self.conn), [])

    def test_flag_cleared_by_matching(self):
        d.flag_reimbursement(self.conn, self.exp)
        d.link_reimbursement(self.conn, self.exp, self.dep)
        self.assertEqual(d.pending_reimbursements(self.conn), [])
        self.assertEqual(self._cat(self.exp), "TRANSFER_OUT")

    def test_flag_missing_txn_noop(self):
        d.flag_reimbursement(self.conn, "no-such-txn")
        self.assertEqual(d.pending_reimbursements(self.conn), [])

    def test_pending_list_oldest_first(self):
        old = add_txn(self.conn, TODAY - dt.timedelta(days=90), 75.0, "OLD CHARGE")
        d.flag_reimbursement(self.conn, self.exp)
        d.flag_reimbursement(self.conn, old)
        self.assertEqual([p["id"] for p in d.pending_reimbursements(self.conn)],
                         [old, self.exp])

    def test_candidates_opposite_side_sorted_by_amount(self):
        add_txn(self.conn, TODAY, -500.0, "PAYCHECK", account="chk")
        anchor, cands = d.reimbursement_candidates(self.conn, self.exp)
        self.assertEqual(anchor["id"], self.exp)
        self.assertTrue(all(c["amount"] < 0 for c in cands))
        self.assertEqual(cands[0]["id"], self.dep)      # $200 beats $500
        # text filter narrows
        _, cands = d.reimbursement_candidates(self.conn, self.exp, q="paycheck")
        self.assertEqual([c["payee"] for c in cands], ["PAYCHECK"])

    def test_deposit_candidates_from_checking_or_the_charged_card(self):
        """Anchor = a charge → deposit candidates come from checking
        accounts and from the charge's OWN account: a merchant refund is
        credited back to the card that was charged, so a checking-only
        list could never pair a $1 charge with its $1 refund. Credits on
        some other card stay out, and so does the charged card's own
        payment (a settlement, not a refund)."""
        add_txn(self.conn, TODAY, -200.0, "CARD REFUND", account="card")
        add_txn(self.conn, TODAY, -200.0, "CARD PAYMENT", account="card",
                primary="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('card2','it1','Other Card','credit','credit card',0)")
        add_txn(self.conn, TODAY, -200.0, "OTHER CARD CREDIT", account="card2")
        _, cands = d.reimbursement_candidates(self.conn, self.exp)
        self.assertEqual(sorted(c["payee"] for c in cands),
                         ["CARD REFUND", "EMPLOYER REIMB"])
        # anchor = the deposit → charge candidates stay account-wide
        _, cands = d.reimbursement_candidates(self.conn, self.dep)
        self.assertIn("OFFICE SUPPLY CO", [c["payee"] for c in cands])

    def test_charge_candidates_exclude_settlements_and_investments(self):
        """Charge-side candidates: card settlements and investment-account
        internals never appear; an already-TRANSFER_OUT charge STAYS
        selectable."""
        add_txn(self.conn, TODAY, 500.0, "AMEX EPAYMENT", account="chk",
                primary="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('inv','it1','Test Brokerage','investment','brokerage',1000)")
        add_txn(self.conn, TODAY, 300.0, "BROKERAGE INTERNAL", account="inv",
                primary="TRANSFER_OUT")
        d.set_category(self.conn, self.exp, "TRANSFER_OUT")   # old override
        _, cands = d.reimbursement_candidates(self.conn, self.dep)
        payees = [c["payee"] for c in cands]
        self.assertIn("OFFICE SUPPLY CO", payees)
        self.assertNotIn("AMEX EPAYMENT", payees)
        self.assertNotIn("BROKERAGE INTERNAL", payees)

    def test_many_charges_one_check(self):
        """6 charges → 1 reimbursement check in one multi-select shot."""
        exps = [self.exp] + [add_txn(self.conn, TODAY, 30.0 + i, f"OFFICE SUPPLY {i}")
                             for i in range(5)]
        r = d.link_reimbursements(self.conn, self.dep, exps)
        self.assertEqual(r, {"linked": 6, "errors": []})
        for e in exps:
            self.assertEqual(self._cat(e), "TRANSFER_OUT")
        self.assertEqual(self._cat(self.dep), "TRANSFER_IN")
        self.assertEqual(len(d.reimbursement_pairs(self.conn, self.dep)), 6)
        d.unlink_reimbursement(self.conn, exps[0], self.dep)
        self.assertEqual(self._cat(self.dep), "TRANSFER_IN")   # 5 remain

    def test_multi_link_reports_bad_rows(self):
        dep2 = add_txn(self.conn, TODAY, -50.0, "OTHER DEPOSIT", account="chk")
        r = d.link_reimbursements(self.conn, self.dep, [self.exp, dep2])
        self.assertEqual(r["linked"], 1)         # the charge linked
        self.assertEqual(len(r["errors"]), 1)    # same-direction row reported


class TestCategoryOverride(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_set_and_clear_round_trip(self):
        t = add_txn(self.conn, TODAY, 42.0, "SHOP")
        d.set_category(self.conn, t, "ENTERTAINMENT")
        row = self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s", (t,)).fetchone()
        self.assertEqual(row["category_override"], "ENTERTAINMENT")
        d.clear_category(self.conn, t)
        row = self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s", (t,)).fetchone()
        self.assertIsNone(row["category_override"])

    def test_search_amount_and_total(self):
        add_txn(self.conn, TODAY, 45.0, "TARGET")
        add_txn(self.conn, TODAY, 145.0, "BIGBOX")
        rows, total, s, _, _, _hits = d.search_transactions(self.conn, "45")
        self.assertEqual(total, 2)               # 45.00 and 145.00 both match
        self.assertEqual(s, 190.0)
        rows, total, s, _, _, _hits = d.search_transactions(self.conn, "target")
        self.assertEqual([r["payee"] for r in rows], ["TARGET"])


if __name__ == "__main__":
    unittest.main()
