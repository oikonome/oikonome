"""Partial reimbursement deposit-side cap.

Both sides are capped: the EXPENSE side (Σ received per charge ≤ its gross)
and the DEPOSIT side. Without the deposit cap one $100 insurance deposit
could be partial-linked to N different $100 charges, each link recording the
FULL deposit amount — netting them ALL to $0 shown spend while the true
economics are $100 out.

Chosen semantics (remaining-balance): each new partial link records
``min(what the charge still needs, what the deposit still has)``; the sum
of link amounts per deposit can never exceed ``abs(deposit.amount)``, and a
deposit with nothing left to hand out is refused with a clear error. The
same clamp applies on the expense side (a deposit larger than the charge's
remaining need records only the need — the rest stays available for other
charges). The FULL (non-partial) pairing path is untouched by design.
"""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import data

from .util import TODAY, add_txn, make_db, write_config


class DepositSideCapTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _spend(self):
        rows = budget._spend_rows(self.conn, TODAY.replace(day=1),
                                  TODAY + dt.timedelta(days=1))
        return sum(r["amount"] for r in rows)

    def _amounts(self, reimburse_id):
        return [float(r["amount"]) for r in self.conn.execute(
            "SELECT amount FROM reimbursements WHERE reimburse_id=%s "
            "AND partial=1 ORDER BY created_at", (reimburse_id,)).fetchall()]

    def test_one_deposit_cannot_fully_net_two_full_charges(self):
        """The canonical case: $100 deposit + two $100 medical
        charges. The second partial link must be refused — net spend stays
        $100, never $0."""
        a = add_txn(self.conn, TODAY, 100.0, "MEDICAL A", account="chk",
                    primary="MEDICAL")
        b = add_txn(self.conn, TODAY, 100.0, "MEDICAL B", account="chk",
                    primary="MEDICAL")
        dep = add_txn(self.conn, TODAY, -100.0, "INSURANCE CO",
                      account="chk", primary="INCOME")
        out = data.link_reimbursement(self.conn, a, dep, partial=True)
        self.assertEqual(out.get("received"), 100.0)
        out = data.link_reimbursement(self.conn, b, dep, partial=True)
        self.assertIn("error", out)
        self.assertIn("fully applied", out["error"])
        self.assertAlmostEqual(self._spend(), 100.0, places=2,
                               msg="one $100 deposit must never net two "
                                   "$100 charges to $0")

    def test_deposit_splits_across_smaller_charges(self):
        """$100 deposit against a $60 and a $40 charge: both fully nettable
        because the per-link amounts (60 + 40) sum to the deposit."""
        a = add_txn(self.conn, TODAY, 60.0, "CLINIC COPAY", account="chk",
                    primary="MEDICAL")
        b = add_txn(self.conn, TODAY, 40.0, "PHARMACY", account="chk",
                    primary="MEDICAL")
        dep = add_txn(self.conn, TODAY, -100.0, "INSURANCE CO",
                      account="chk", primary="INCOME")
        out = data.link_reimbursement(self.conn, a, dep, partial=True)
        self.assertEqual(out.get("received"), 60.0)
        out = data.link_reimbursement(self.conn, b, dep, partial=True)
        self.assertEqual(out.get("received"), 40.0)
        self.assertEqual(self._amounts(dep), [60.0, 40.0])
        self.assertAlmostEqual(self._spend(), 0.0, places=2)
        # the deposit is now exhausted — a third charge gets nothing
        c = add_txn(self.conn, TODAY, 25.0, "MEDICAL C", account="chk",
                    primary="MEDICAL")
        out = data.link_reimbursement(self.conn, c, dep, partial=True)
        self.assertIn("error", out)
        self.assertAlmostEqual(self._spend(), 25.0, places=2)

    def test_second_link_clamps_to_deposit_remaining(self):
        """$100 deposit → $70 charge (records 70), then → $80 charge: only
        the $30 the deposit still has is recorded; total net spend is the
        true $50 out."""
        a = add_txn(self.conn, TODAY, 70.0, "LAB WORK", account="chk",
                    primary="MEDICAL")
        b = add_txn(self.conn, TODAY, 80.0, "SPECIALIST", account="chk",
                    primary="MEDICAL")
        dep = add_txn(self.conn, TODAY, -100.0, "INSURANCE CO",
                      account="chk", primary="INCOME")
        out = data.link_reimbursement(self.conn, a, dep, partial=True)
        self.assertEqual(out.get("received"), 70.0)
        out = data.link_reimbursement(self.conn, b, dep, partial=True)
        self.assertEqual(out.get("received"), 30.0)
        # 70 + 80 out, 100 back = $50 true spend
        self.assertAlmostEqual(self._spend(), 50.0, places=2)

    def test_order_independent(self):
        """Same books, opposite link order — Σ per deposit still ≤ deposit."""
        a = add_txn(self.conn, TODAY, 80.0, "SPECIALIST", account="chk",
                    primary="MEDICAL")
        b = add_txn(self.conn, TODAY, 70.0, "LAB WORK", account="chk",
                    primary="MEDICAL")
        dep = add_txn(self.conn, TODAY, -100.0, "INSURANCE CO",
                      account="chk", primary="INCOME")
        out = data.link_reimbursement(self.conn, a, dep, partial=True)
        self.assertEqual(out.get("received"), 80.0)
        out = data.link_reimbursement(self.conn, b, dep, partial=True)
        self.assertEqual(out.get("received"), 20.0)
        self.assertAlmostEqual(self._spend(), 50.0, places=2)

    def test_multi_select_one_deposit_many_charges(self):
        """The bulk path (one check → many charges) drains the deposit and
        reports the refused remainder instead of over-applying."""
        exps = [add_txn(self.conn, TODAY, 100.0, f"MEDICAL {i}",
                        account="chk", primary="MEDICAL") for i in range(3)]
        dep = add_txn(self.conn, TODAY, -100.0, "INSURANCE CO",
                      account="chk", primary="INCOME")
        r = data.link_reimbursements(self.conn, dep, exps, partial=True)
        self.assertEqual(r["linked"], 1)
        self.assertEqual(len(r["errors"]), 2)
        self.assertAlmostEqual(self._spend(), 200.0, places=2)

    def test_expense_side_cap_still_holds(self):
        """The expense-side cap composes: a charge already fully reimbursed refuses further
        deposits — spend never goes negative."""
        a = add_txn(self.conn, TODAY, 50.0, "URGENT CARE", account="chk",
                    primary="MEDICAL")
        d1 = add_txn(self.conn, TODAY, -50.0, "INSURANCE CO", account="chk",
                     primary="INCOME")
        d2 = add_txn(self.conn, TODAY, -50.0, "INSURANCE CO 2",
                     account="chk", primary="INCOME")
        data.link_reimbursement(self.conn, a, d1, partial=True)
        out = data.link_reimbursement(self.conn, a, d2, partial=True)
        self.assertIn("error", out)
        self.assertIn("fully reimbursed", out["error"])
        self.assertAlmostEqual(self._spend(), 0.0, places=2)

    def test_full_pairing_path_untouched(self):
        """FULL (non-partial) pairing keeps its by-design semantics: both
        sides become TRANSFER_*, no amount math, one deposit may still pair
        with several charges (refcounted on unlink)."""
        a = add_txn(self.conn, TODAY, 100.0, "MEDICAL A", account="chk",
                    primary="MEDICAL")
        b = add_txn(self.conn, TODAY, 100.0, "MEDICAL B", account="chk",
                    primary="MEDICAL")
        dep = add_txn(self.conn, TODAY, -100.0, "EMPLOYER REIMB",
                      account="chk", primary="INCOME")
        r = data.link_reimbursements(self.conn, dep, [a, b])
        self.assertEqual(r, {"linked": 2, "errors": []})

        def cat(t):
            return self.conn.execute(
                "SELECT category_override FROM transactions WHERE id=%s",
                (t,)).fetchone()["category_override"]
        self.assertEqual(cat(a), "TRANSFER_OUT")
        self.assertEqual(cat(b), "TRANSFER_OUT")
        self.assertEqual(cat(dep), "TRANSFER_IN")


if __name__ == "__main__":
    unittest.main()
