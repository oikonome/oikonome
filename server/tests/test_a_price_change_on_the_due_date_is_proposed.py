"""A charge from the bill's merchant on the due date at an amount the
bill does not recognise, with nothing it recognises that cycle, is a
price change — proposed, never applied, because a one-off at the
merchant on the due day looks the same.

Drift cannot see it (it reads matched charges only), so without the
proposal the bill reads unpaid every month until someone edits it.
Approving writes the amount and files the old one under the bill's
history; a rejected proposal stays rejected; a locked or freshly
hand-edited amount is left alone."""

import datetime as dt
import unittest

from oikonome.engine import bills as rd
from oikonome.engine.compat import as_dict, jsonb

from .util import TODAY, add_bill, add_txn, make_db, write_config


class APriceChangeIsProposed(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.due = TODAY - dt.timedelta(days=3)
        self.charged = TODAY - dt.timedelta(days=2)
        # paid at $80.00 for three months, then $45.00 on this cycle's due day
        for n in (3, 2, 1):
            y, m = TODAY.year, TODAY.month - n
            while m <= 0:
                y, m = y - 1, m + 12
            add_txn(self.conn, dt.date(y, m, max(1, self.due.day)), 80.00,
                    "HARBOR GYM")
        add_txn(self.conn, self.charged, 45.00, "HARBOR GYM")

    def tearDown(self):
        self.conn.close()

    def _bill(self, amount_override=None, **kw):
        add_bill(self.conn, "Harbor Gym", amount_override or 80.00,
                 next_due=self.due,
                 last_seen=self.due - dt.timedelta(days=30), **kw)

    def _pending(self):
        return self.conn.execute(
            "SELECT * FROM bill_proposals WHERE kind='amount_change' "
            "AND status='pending'").fetchall()

    def test_the_charge_becomes_a_proposal_with_the_charge_as_evidence(self):
        self._bill()
        rd.run(self.conn, TODAY)
        props = self._pending()
        self.assertEqual(len(props), 1, props)
        p = props[0]
        self.assertEqual(p["amount"], 45.00)
        ev = as_dict(p["evidence"])
        self.assertEqual(ev["old"], 80.00)
        self.assertEqual(ev["new"], 45.00)
        self.assertEqual(ev["charged_on"], self.charged.isoformat())
        # the bill itself is untouched until someone says so
        self.assertEqual(abs(rd.get_bill(self.conn, "Harbor Gym")["amount"]), 80.00)
        # a second pass proposes nothing more
        rd.run(self.conn, TODAY)
        self.assertEqual(len(self._pending()), 1)

    def test_approving_sets_the_amount_and_keeps_the_old_one(self):
        self._bill()
        rd.run(self.conn, TODAY)
        p = self._pending()[0]
        out = rd.apply_proposal(self.conn, p["id"], "approve")
        self.assertNotIn("error", out)
        bill = rd.get_bill(self.conn, "Harbor Gym")
        self.assertEqual(abs(bill["amount"]), 45.00)
        raw = as_dict(bill["raw"])
        self.assertEqual([h["amount"] for h in raw["amount_history"]], [80.00])
        self.assertEqual(raw["recurrence"]["frequency"], "MONTHLY")
        self.assertFalse(rd.in_manual_grace(raw, TODAY))

    def test_a_rejected_proposal_stays_rejected(self):
        self._bill()
        rd.run(self.conn, TODAY)
        p = self._pending()[0]
        rd.apply_proposal(self.conn, p["id"], "reject")
        rd.run(self.conn, TODAY)
        self.assertEqual(self._pending(), [])
        self.assertEqual(abs(rd.get_bill(self.conn, "Harbor Gym")["amount"]), 80.00)

    def test_a_recognised_payment_that_cycle_means_no_proposal(self):
        self._bill()
        add_txn(self.conn, self.due, 80.00, "HARBOR GYM")
        rd.run(self.conn, TODAY)
        self.assertEqual(self._pending(), [])

    def test_a_locked_or_freshly_edited_amount_is_left_alone(self):
        self._bill(source="manual", manual_touch=True)
        rd.run(self.conn, TODAY)
        self.assertEqual(self._pending(), [])
        self.conn.execute("UPDATE bill_proposals SET status='rejected'")
        self.conn.execute("UPDATE bills SET raw = raw - 'manual_edited_at' "
                          "|| %s::jsonb WHERE payee='Harbor Gym'",
                          (jsonb({"lock_amount": True}),))
        rd.run(self.conn, TODAY)
        self.assertEqual(self._pending(), [])

    def _age_the_decision(self, amount=None):
        """Re-file the decided proposal as though it was made on an earlier
        cycle (its id carries the charge's month), at `amount`."""
        self.conn.execute(
            "UPDATE bill_proposals SET id = 'prop:earlier-cycle', "
            "amount = COALESCE(%s, amount) WHERE kind = 'amount_change'",
            (amount,))

    def test_a_rejected_price_stays_rejected_on_later_cycles(self):
        """Rejecting a price is a statement about that price, not about one
        month: a later cycle charging it again — or a few cents either side
        of it, as a variable charge does — is not offered again."""
        self._bill()
        rd.run(self.conn, TODAY)
        rd.apply_proposal(self.conn, self._pending()[0]["id"], "reject")
        self._age_the_decision()
        rd.run(self.conn, TODAY)
        self.assertEqual(self._pending(), [])
        self._age_the_decision(amount=46.10)
        rd.run(self.conn, TODAY)
        self.assertEqual(self._pending(), [])

    def test_a_rejection_does_not_hide_a_different_price(self):
        self._bill()
        rd.run(self.conn, TODAY)
        rd.apply_proposal(self.conn, self._pending()[0]["id"], "reject")
        self._age_the_decision(amount=30.00)
        rd.run(self.conn, TODAY)
        self.assertEqual(len(self._pending()), 1)

    def test_a_rejection_made_at_another_bill_amount_does_not_hold(self):
        """The rejection answered "should this $80 bill become $45": once
        the bill's amount has moved on, the question is a new one."""
        self._bill()
        rd.run(self.conn, TODAY)
        rd.apply_proposal(self.conn, self._pending()[0]["id"], "reject")
        self._age_the_decision()
        self.conn.execute(
            "UPDATE bill_proposals SET evidence = evidence || %s::jsonb",
            (jsonb({"old": 95.00}),))
        rd.run(self.conn, TODAY)
        self.assertEqual(len(self._pending()), 1)

    def test_approving_after_a_hand_edit_of_the_amount_changes_nothing(self):
        """The offer was "$80 → $45". The person since typed $60 in by
        hand; approving the stale offer must not overwrite what they wrote.
        It is withdrawn instead, as the nightly would."""
        self._bill()
        rd.run(self.conn, TODAY)
        p = self._pending()[0]
        self._bill(amount_override=60.00)
        out = rd.apply_proposal(self.conn, p["id"], "approve")
        self.assertIn("error", out)
        self.assertEqual(abs(rd.get_bill(self.conn, "Harbor Gym")["amount"]), 60.00)
        st = self.conn.execute("SELECT status FROM bill_proposals WHERE id=%s",
                               (p["id"],)).fetchone()["status"]
        self.assertEqual(st, "superseded")

    def test_approving_after_the_amount_was_locked_changes_nothing(self):
        self._bill()
        rd.run(self.conn, TODAY)
        p = self._pending()[0]
        self.conn.execute("UPDATE bills SET raw = raw || %s::jsonb "
                          "WHERE payee='Harbor Gym'",
                          (jsonb({"lock_amount": True}),))
        out = rd.apply_proposal(self.conn, p["id"], "approve")
        self.assertIn("error", out)
        self.assertEqual(abs(rd.get_bill(self.conn, "Harbor Gym")["amount"]), 80.00)


if __name__ == "__main__":
    unittest.main()
