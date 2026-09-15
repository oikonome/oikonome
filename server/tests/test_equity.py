"""Owner-equity movements + the reimbursement workflow."""
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import entities, equity

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts,
                   TODAY)


class EquityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"eq-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Northwind LLC", structure="single_member_llc")

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def test_capital_account_balance(self):
        # synthetic mixed forms that sum to a round total
        for amt, form in ((500, "Cash"), (100, "Cash"), (400, "ACH transfer")):
            equity.record_movement(self.conn, self.ent["id"],
                                   kind="contribution", amount=amt, date=TODAY,
                                   form=form)
        cap = equity.capital_summary(self.conn, self.ent["id"])
        self.assertEqual(cap["contributions"], 1000.0)
        # no business income/expense in this fixture, so retained earnings
        # are 0 and the balance is contributions alone
        self.assertEqual(cap["net_income"], 0.0)
        self.assertEqual(cap["capital_balance"], 1000.0)
        self.assertEqual(cap["movements_balance"], 1000.0)
        # an owner draw reduces the capital account
        equity.record_movement(self.conn, self.ent["id"], kind="draw",
                               amount=200, date=TODAY)
        cap = equity.capital_summary(self.conn, self.ent["id"])
        self.assertEqual(cap["draws"], 200.0)
        self.assertEqual(cap["capital_balance"], 800.0)

    def test_capital_account_includes_retained_earnings(self):
        """An owner's capital account is contributions + net income − draws.
        Leave retained earnings out and a profitable entity that distributed
        its profit reads NEGATIVE — $1k in and $50k drawn against $60k
        earned would show as −$49k."""
        from oikonome.engine import books
        equity.record_movement(self.conn, self.ent["id"], kind="contribution",
                               amount=1000, date=TODAY)
        # $5,000 of revenue and $1,000 of cost on the entity's own account
        rev = add_txn(self.conn, TODAY, -5000.0, "CLIENT INVOICE",
                      account="card", primary="INCOME")
        exp = add_txn(self.conn, TODAY, 1000.0, "SUPPLIES", account="card")
        for t in (rev, exp):
            self.conn.execute("UPDATE transactions SET entity_id=%s WHERE id=%s",
                              (self.ent["id"], t))
        equity.record_movement(self.conn, self.ent["id"], kind="draw",
                               amount=3500, date=TODAY)
        cap = equity.capital_summary(self.conn, self.ent["id"])
        self.assertEqual(cap["net_income"], 4000.0)          # 5000 − 1000
        self.assertEqual(cap["movements_balance"], -2500.0)  # 1000 − 3500
        self.assertEqual(cap["capital_balance"], 1500.0)     # + 4000 earned
        # and the full P&L still attaches the same capital block without
        # recursing (pnl → capital_summary → pnl was an infinite loop)
        pl = books.pnl(self.conn, self.ent["id"])
        self.assertEqual(pl["capital"]["capital_balance"], 1500.0)
        self.assertEqual(pl["net_operating"], 4000.0)

    def test_validation(self):
        with self.assertRaises(ValueError):
            equity.record_movement(self.conn, self.ent["id"], kind="bogus",
                                   amount=10, date=TODAY)
        with self.assertRaises(ValueError):
            equity.record_movement(self.conn, self.ent["id"],
                                   kind="contribution", amount=0, date=TODAY)

    def test_contribute_expense_capitalizes(self):
        # a state filing fee paid on a personal card, capitalized
        t = add_txn(self.conn, TODAY, 120.00, "SECRETARY OF STATE", account="card")
        mv = equity.contribute_expense(self.conn, self.ent["id"], t)
        self.assertEqual(mv["kind"], "contribution")
        self.assertEqual(mv["amount"], 120.00)
        # the transaction is now assigned to the entity (→ business money)
        row = self.conn.execute(
            "SELECT entity_id FROM transactions WHERE id=%s", (t,)).fetchone()
        self.assertEqual(str(row["entity_id"]), self.ent["id"])
        # The capital account nets to ZERO, and that is the correct double
        # entry: the owner contributed the fee (capital up) to fund a
        # business cost of the same size (net income down). The movements-only
        # sub-total still shows the contribution. Retained earnings must be
        # part of the capital balance — leave them out and this reads as a
        # credit for the contribution while the expense it paid for is ignored.
        cap = equity.capital_summary(self.conn, self.ent["id"])
        self.assertEqual(cap["movements_balance"], 120.00)
        self.assertEqual(cap["net_income"], -120.00)
        self.assertEqual(cap["capital_balance"], 0.0)

    def test_contribute_expense_rejects_null_amount(self):
        # a NULL-amount transaction must raise a clean ValueError (400), not a
        # TypeError 500 from float(None) — transactions.amount is nullable.
        t = add_txn(self.conn, TODAY, None, "AMOUNTLESS", account="card")
        with self.assertRaises(ValueError):
            equity.contribute_expense(self.conn, self.ent["id"], t)
        with self.assertRaises(ValueError):
            equity.reimburse_expense(self.conn, self.ent["id"], t)

    def test_reimburse_expense_is_a_settlement_not_equity(self):
        t = add_txn(self.conn, TODAY, 50.0, "OFFICE SUPPLIES", account="card")
        mv = equity.reimburse_expense(self.conn, self.ent["id"], t)
        self.assertEqual(mv["kind"], "reimbursement")
        self.assertEqual(mv["amount"], 50.0)
        row = self.conn.execute(
            "SELECT entity_id FROM transactions WHERE id=%s", (t,)).fetchone()
        self.assertEqual(str(row["entity_id"]), self.ent["id"])
        cap = equity.capital_summary(self.conn, self.ent["id"])
        self.assertEqual(cap["reimbursements"], 50.0)
        # not equity: the $50 cost is now business, so it lowers net income,
        # but the reimbursement itself never touches the capital account
        self.assertEqual(cap["movements_balance"], 0.0)
        self.assertEqual(cap["capital_balance"], cap["net_income"])

    def test_rejected_movement_leaves_the_transaction_personal(self):
        """Assigning the transaction to the entity and recording the
        movement that explains it are ONE unit. Engine connections are
        autocommit, so without a transaction the assignment sticks even
        when the movement is rejected: the caller gets a 400 and believes
        nothing happened, while a personal transaction stays booked as a
        business expense with no contribution/reimbursement recording why —
        wrong P&L and capital summary, and no 'unassign' anywhere to undo
        it."""
        t = add_txn(self.conn, TODAY, 60.0, "PRINTER INK", account="card")
        for fn in (equity.contribute_expense, equity.reimburse_expense):
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(ValueError):
                    fn(self.conn, self.ent["id"], t, member_id="not-a-uuid")
                row = self.conn.execute(
                    "SELECT entity_id FROM transactions WHERE id=%s",
                    (t,)).fetchone()
                self.assertIsNone(row["entity_id"])
                self.assertEqual(
                    equity.list_movements(self.conn, self.ent["id"]), [])
        # and the same call with a valid member does both halves
        m = entities.add_member(self.conn, self.ent["id"], member_name="Owner")
        mv = equity.contribute_expense(self.conn, self.ent["id"], t,
                                       member_id=m["id"])
        self.assertEqual(mv["amount"], 60.0)
        row = self.conn.execute(
            "SELECT entity_id FROM transactions WHERE id=%s", (t,)).fetchone()
        self.assertEqual(str(row["entity_id"]), self.ent["id"])

    def test_list_and_delete(self):
        mv = equity.record_movement(self.conn, self.ent["id"],
                                   kind="contribution", amount=100, date=TODAY)
        self.assertEqual(len(equity.list_movements(self.conn, self.ent["id"])), 1)
        # a movement can't be deleted via a DIFFERENT entity's scope
        other = entities.create_entity(self.conn, name="Other",
                                       structure="sole_prop")
        self.assertFalse(equity.delete_movement(self.conn, mv["id"],
                                                other["id"]))
        self.assertEqual(len(equity.list_movements(self.conn, self.ent["id"])), 1)
        # …but its own entity can
        self.assertTrue(equity.delete_movement(self.conn, mv["id"],
                                               self.ent["id"]))
        self.assertEqual(equity.list_movements(self.conn, self.ent["id"]), [])


if __name__ == "__main__":
    unittest.main()
