"""A hand edit that commits while the detection pass is mid-flight must win.

The detection pass and the Bills page decide amount drift and due-date
rolls from rows read without a lock. A save that lands between that read
and the automatic write must not be overwritten: the person's pinned amount
or chosen due date is the one that survives, and the automatic write is
skipped rather than computed from the stale row.
"""

import datetime as dt
import json
import unittest
from unittest import mock

from oikonome.engine import bills, budget

from .util import make_db

TODAY = dt.date(2026, 9, 20)


class HandEditBeatsDetectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _bill(self, bid, amount, due, recurrence, extra=None):
        raw = {"source": "detected", "bill_type": "occurrence",
               "dueOn": due, "lastDueOn": None, "recurrence": recurrence,
               **(extra or {})}
        self.conn.execute(
            """INSERT INTO bills (id, type, payee, amount, frequency,
                                  monthly_amount, due_on, active,
                                  is_completed, raw)
               VALUES (%s, %s, %s, %s, 'MONTHLY', %s, %s, 1, 0, %s::jsonb)""",
            (bills._slug(bid), "INCOME" if amount > 0 else "BILL", bid,
             amount, amount,
             due, json.dumps(raw)))

    def _row(self, bid):
        return self.conn.execute("SELECT * FROM bills WHERE id=%s",
                                 (bills._slug(bid),)).fetchone()

    def _hand_edit(self, payee, amount, due, income=False):
        bills.save_bill(self.conn, payee=payee, amount=amount,
                        frequency="MONTHLY", next_due=due, source="manual",
                        income=income)

    # -- amount drift ---------------------------------------------------
    def test_hand_set_amount_survives_a_drift_decided_before_it(self):
        self._bill("gym", -50.0, "2026-10-05",
                   {"frequency": "MONTHLY", "interval": 1, "byMonthDay": [5]})
        snapshot = self._row("gym")
        self._hand_edit("gym", 65.0, "2026-10-05")
        stats = {"drift_applied": 0}
        bills._apply_drift(self.conn, snapshot, 58.0, {}, stats, TODAY)
        self.assertAlmostEqual(float(self._row("gym")["amount"]), -65.0)
        self.assertEqual(stats["drift_applied"], 0)

    def test_drift_still_applies_when_nothing_changed(self):
        self._bill("gym", -50.0, "2026-10-05",
                   {"frequency": "MONTHLY", "interval": 1, "byMonthDay": [5]})
        stats = {"drift_applied": 0}
        bills._apply_drift(self.conn, self._row("gym"), 58.0, {}, stats, TODAY)
        self.assertAlmostEqual(float(self._row("gym")["amount"]), -58.0)
        self.assertEqual(stats["drift_applied"], 1)

    # -- a Disable that commits mid-pass ----------------------------------
    def _disable(self, payee):
        # what the Bills page's Disable writes: the household's list, not
        # the bill row
        with budget.config_txn(self.conn) as cfg:
            cfg["disabled_bills"] = [payee]

    def test_a_bill_paused_after_the_snapshot_gets_no_drift(self):
        self._bill("gym", -50.0, "2026-10-05",
                   {"frequency": "MONTHLY", "interval": 1, "byMonthDay": [5]})
        snapshot = self._row("gym")
        self._disable("gym")
        stats = {"drift_applied": 0}
        bills._apply_drift(self.conn, snapshot, 58.0, {}, stats, TODAY)
        self.assertAlmostEqual(float(self._row("gym")["amount"]), -50.0)
        self.assertEqual(stats["drift_applied"], 0)

    def _proposals(self, payee):
        return self.conn.execute(
            "SELECT kind FROM bill_proposals WHERE payee=%s",
            (payee,)).fetchall()

    def _maintain_one_time(self, payee, disable_mid_pass):
        # a one-time bill long past due: the pass proposes its removal
        self._bill(payee, -40.0, "2026-06-01", {})
        snapshot = dict(self._row(payee), budget_disabled=False)
        if disable_mid_pass:
            self._disable(payee)
        stats = {"proposed_remove": 0, "drift_applied": 0,
                 "proposed_change": 0}
        bills._maintain_bills(self.conn, [snapshot], [], TODAY, stats)
        return stats

    def test_a_bill_paused_after_the_snapshot_gets_no_removal_offer(self):
        stats = self._maintain_one_time("visa fee", disable_mid_pass=True)
        self.assertEqual(stats["proposed_remove"], 0)
        self.assertEqual(self._proposals("visa fee"), [])

    def test_removal_is_still_offered_for_a_bill_left_enabled(self):
        stats = self._maintain_one_time("visa fee", disable_mid_pass=False)
        self.assertEqual(stats["proposed_remove"], 1)
        self.assertEqual([r["kind"] for r in self._proposals("visa fee")],
                         ["remove"])

    # -- due-date rolls -------------------------------------------------
    def _edit_during_projection(self, payee, amount, due, income=False):
        """Patch the projection step so the hand edit commits after the
        pass has read its rows but before it writes."""
        real = budget._occurrences_in_month
        fired = []

        def projecting(bill, y, m):
            if not fired:
                fired.append(True)
                self._hand_edit(payee, amount, due, income=income)
            return real(bill, y, m)
        return mock.patch.object(budget, "_occurrences_in_month", projecting)

    def test_hand_set_due_date_survives_the_nightly_roll(self):
        self._bill("water", -40.0, "2026-09-10",
                   {"frequency": "MONTHLY", "interval": 1, "byMonthDay": [10]})
        stats = {"due_advanced": 0}
        with self._edit_during_projection("water", 40.0, "2026-09-25"):
            bills._advance_due_dates(self.conn, TODAY, stats)
        self.assertEqual(self._row("water")["due_on"], dt.date(2026, 9, 25))
        self.assertEqual(stats["due_advanced"], 0)

    def test_hand_set_due_date_survives_the_received_income_roll(self):
        self._bill("payroll", 1200.0, "2026-09-15",
                   {"frequency": "MONTHLY", "interval": 1, "byMonthDay": [15]})
        deposit = [{"date": dt.date(2026, 9, 15), "amount": 1200.0,
                    "payee": "payroll", "name": "payroll"}]
        with self._edit_during_projection("payroll", 1200.0, "2026-09-28",
                                          income=True), \
                mock.patch.object(budget, "_inflow_rows",
                                  return_value=deposit), \
                mock.patch.object(budget, "row_merchant_id",
                                  return_value=None):
            n = bills.advance_received_income(self.conn, TODAY)
        self.assertEqual(self._row("payroll")["due_on"], dt.date(2026, 9, 28))
        self.assertEqual(n, 0)

    def test_roll_still_happens_when_nothing_changed(self):
        self._bill("water", -40.0, "2026-09-10",
                   {"frequency": "MONTHLY", "interval": 1, "byMonthDay": [10]})
        stats = {"due_advanced": 0}
        bills._advance_due_dates(self.conn, TODAY, stats)
        self.assertEqual(self._row("water")["due_on"], dt.date(2026, 10, 10))
        self.assertEqual(stats["due_advanced"], 1)


if __name__ == "__main__":
    unittest.main()
