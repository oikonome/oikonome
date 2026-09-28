"""The attention queue's one-tap action must be earned by evidence.

The queue (Bills page banner, both clients) offers exactly one action per
flagged bill, chosen server-side by the same evidence that raised the
flag. The rules under test: a confident action appears only when the
ledger is unambiguous — a merchant silent past the removal-proposal
threshold (disable), or a stable new amount over recent charges (accept) —
and anything weaker degrades to "review", which opens the options instead
of acting. A healthy bill offers nothing.
"""

import datetime as dt
import unittest

from oikonome.engine import bills

TODAY = dt.date(2026, 8, 30)


def _bill(amount=-100.0):
    return {"payee": "Acme Clinic", "amount": amount, "merchant": "acme"}


def _raw(freq="MONTHLY", day=22):
    return {"bill_type": "occurrence", "dueOn": "2026-09-22",
            "recurrence": {"frequency": freq, "interval": 1,
                           "byMonthDay": [day]}}


def _events(dates, amount=100.0):
    return [{"date": dt.date.fromisoformat(d), "amount": amount}
            for d in dates]


class AttentionSuggestionTests(unittest.TestCase):
    def test_one_missed_cycle_only_offers_review(self):
        h = bills._bill_health(_bill(), _raw(), _events(
            ["2026-05-22", "2026-06-22", "2026-07-22"]), TODAY)
        self.assertEqual(h["health"], "stale")
        self.assertEqual(h["suggestion"],
                         {"action": "review", "reason": "missed_one_cycle"})

    def test_two_silent_cycles_offer_disable(self):
        h = bills._bill_health(_bill(), _raw(), _events(
            ["2026-04-22", "2026-05-22", "2026-06-22"]), TODAY)
        self.assertEqual(h["health"], "stale")
        self.assertEqual(h["suggestion"],
                         {"action": "disable",
                          "reason": "silent_two_cycles"})

    def test_stable_new_amount_offers_accept_with_that_amount(self):
        # bill says $100; the merchant has settled at $150 for months —
        # enough history for a fit, so the queue may offer the new price
        h = bills._bill_health(_bill(), _raw(), _events(
            ["2026-03-22", "2026-04-22", "2026-05-22",
             "2026-06-22", "2026-07-22", "2026-08-22"], amount=150.0),
            TODAY)
        self.assertEqual(h["health"], "mismatch")
        self.assertEqual(h["suggestion"]["action"], "accept_amount")
        self.assertEqual(h["suggestion"]["amount"], 150.0)

    def test_scattered_amounts_degrade_to_review(self):
        # merchant present but amounts all over the place → no fit, and
        # the queue must NOT invent a number to accept
        ev = [{"date": dt.date.fromisoformat(d), "amount": a}
              for d, a in (("2026-06-03", 431.0), ("2026-07-19", 17.0),
                           ("2026-08-28", 260.0))]
        h = bills._bill_health(_bill(), _raw(), ev, TODAY)
        self.assertEqual(h["health"], "mismatch")
        self.assertEqual(h["suggestion"],
                         {"action": "review", "reason": "amount_scattered"})

    def test_healthy_bill_offers_nothing(self):
        h = bills._bill_health(_bill(), _raw(), _events(
            ["2026-06-22", "2026-07-22", "2026-08-22"]), TODAY)
        self.assertEqual(h["health"], "ok")
        self.assertIsNone(h["suggestion"])
        self.assertEqual(h["cycle_days"], 30)


class AcceptThresholdTests(unittest.TestCase):
    """The Accept offer needs the recent charges to AGREE, not merely to
    exist — a regular cadence with scattered amounts is still noise."""

    def test_regular_cadence_with_scattered_amounts_is_review(self):
        ev = [{"date": dt.date.fromisoformat(d), "amount": a}
              for d, a in (("2026-03-22", 120.0), ("2026-04-22", 150.0),
                           ("2026-05-22", 400.0), ("2026-06-22", 120.0),
                           ("2026-07-22", 150.0), ("2026-08-22", 400.0))]
        h = bills._bill_health(_bill(), _raw(), ev, TODAY)
        self.assertEqual(h["health"], "mismatch")
        self.assertEqual(h["suggestion"]["action"], "review")

    def test_income_that_settled_at_a_new_amount_offers_accept(self):
        # the paycheck path has no drift automation — the queue is the
        # only writer, so it must work for income too
        h = bills._bill_health(_bill(amount=4000.0), _raw(day=5), _events(
            ["2026-03-05", "2026-04-05", "2026-05-05",
             "2026-06-05", "2026-07-05", "2026-08-05"], amount=9000.0),
            TODAY)
        self.assertEqual(h["health"], "mismatch")
        self.assertEqual(h["suggestion"]["action"], "accept_amount")
        self.assertEqual(h["suggestion"]["amount"], 9000.0)


class AmountOnlyEditKeepsAnchorTests(unittest.TestCase):
    """Accept is an amount-only save: it must not re-anchor a monthly
    bill to the 1st (and flip it to misdated) when no due date rides
    along."""

    def setUp(self):
        from .util import make_db, write_config
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_save_without_next_due_keeps_by_month_day(self):
        bills.save_bill(self.conn, payee="Anchor Co", amount=50,
                        frequency="MONTHLY", interval=1,
                        next_due="2026-09-22", income=False)
        bills.save_bill(self.conn, payee="Anchor Co", amount=75,
                        frequency="MONTHLY", interval=1,
                        next_due=None, income=False)
        row = self.conn.execute(
            "SELECT raw, due_on FROM bills WHERE payee='Anchor Co'").fetchone()
        from oikonome.engine.compat import as_dict
        raw = as_dict(row["raw"])
        self.assertEqual(raw["recurrence"]["byMonthDay"], [22])
        # the due date itself must survive too — a null due_on drops the
        # bill out of the monthly plan and nothing ever recomputes it
        self.assertEqual(str(row["due_on"]), "2026-09-22")
        self.assertEqual(raw["dueOn"], "2026-09-22")
        self.assertEqual(abs(self.conn.execute(
            "SELECT amount FROM bills WHERE payee='Anchor Co'"
        ).fetchone()["amount"]), 75.0)


class AcceptCadenceGateTests(unittest.TestCase):
    """One tap can only change the AMOUNT: when the ledger's cadence moved
    too, accepting the new per-charge amount on the old schedule would
    book a fraction of the real cost — so it degrades to review."""

    def test_monthly_bill_turned_weekly_is_review_not_accept(self):
        ev = _events([f"2026-08-{d:02d}" for d in (1, 8, 15, 22, 29)]
                     + ["2026-07-25", "2026-07-18"], amount=18.0)
        ev.sort(key=lambda e: e["date"])
        h = bills._bill_health(_bill(amount=-50.0), _raw(), ev, TODAY)
        self.assertEqual(h["health"], "mismatch")
        self.assertEqual(h["suggestion"]["action"], "review")
        self.assertEqual(h["suggestion"]["reason"], "cadence_moved")


if __name__ == "__main__":
    unittest.main()
