"""A card's pending charges are card debt the cash timeline walks: the
pay-now walk starts from checking less them, and the autopay walk pays
them at the statement after the current one (they are on no statement
yet). Once a charge posts the feed retires its hold and the posted
balance carries it, so it is counted once on either side of the handover
— and once when a bill it paid is already marked paid. Figures and names
are invented."""

import datetime as dt
import unittest

from oikonome.engine import forecast
from oikonome.engine.compat import jsonb
from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

DUE = TODAY + dt.timedelta(days=6)
NEXT_DUE = DUE.replace(month=DUE.month + 1)


class PendingCardChargeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)
        self.conn.execute("UPDATE accounts SET balance_available=3000, "
                          "balance_current=3000 WHERE id='chk'")
        self.conn.execute("UPDATE accounts SET balance_current=400 "
                          "WHERE id='card'")
        self.conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)",
            (jsonb({"last_statement_balance": 300.0,
                    "next_payment_due_date": DUE.isoformat()}),))

    def tearDown(self):
        self.conn.close()

    def _stmt(self, f):
        out: dict[str, float] = {}
        for d, a, _ in f["card_events_stmt"]:
            out[d] = round(out.get(d, 0.0) + a, 2)
        return out

    def test_a_pending_charge_is_card_debt_paid_at_the_next_statement(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 85.0,
                "COPPER LANTERN GRILL", pending=1, txn_id="hold1")
        f = forecast.build(self.conn, today=TODAY, days=60)
        self.assertEqual(f["card_debt"], 485.0)
        self.assertEqual(f["start"], 3000.0 - 485.0)
        self.assertEqual(self._stmt(f), {DUE.isoformat(): -300.0,
                                         NEXT_DUE.isoformat(): -185.0})
        self.assertEqual([(d, a) for d, a, _ in f["card_events"]],
                         [(DUE.isoformat(), -485.0)])
        # the autopay walk ends the horizon with the whole debt paid
        self.assertAlmostEqual(f["pace_stmt"]["end"], 3000.0 - 485.0,
                               places=2)

    def test_a_card_with_only_pending_charges_is_walked(self):
        self.conn.execute("UPDATE accounts SET balance_current=0 "
                          "WHERE id='card'")
        add_txn(self.conn, TODAY, 60.0, "COPPER LANTERN GRILL", pending=1)
        f = forecast.build(self.conn, today=TODAY, days=60)
        self.assertEqual(f["card_debt"], 60.0)
        self.assertEqual(sum(self._stmt(f).values()), -60.0)

    def test_the_handover_counts_the_charge_once(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 85.0,
                "COPPER LANTERN GRILL", pending=1, txn_id="hold2")
        before = forecast.build(self.conn, today=TODAY, days=60)
        # the charge posts: a new row names the hold, the balance carries it
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 85.0,
                "COPPER LANTERN GRILL", txn_id="post2")
        self.conn.execute("UPDATE transactions SET pending_transaction_id="
                          "'hold2' WHERE id='post2'")
        self.conn.execute("UPDATE accounts SET balance_current=485 "
                          "WHERE id='card'")
        # a feed that has not retired the hold yet still counts it once
        mid = forecast.build(self.conn, today=TODAY, days=60)
        self.conn.execute("UPDATE transactions SET removed=1 "
                          "WHERE id='hold2'")
        after = forecast.build(self.conn, today=TODAY, days=60)
        for f in (before, mid, after):
            self.assertEqual(f["card_debt"], 485.0)
            self.assertEqual(sum(self._stmt(f).values()), -485.0)

    def test_a_pending_credit_is_not_money_yet(self):
        add_txn(self.conn, TODAY, -40.0, "COPPER LANTERN GRILL", pending=1)
        f = forecast.build(self.conn, today=TODAY, days=60)
        self.assertEqual(f["card_debt"], 400.0)

    def test_a_bill_paid_by_a_pending_card_charge_leaves_checking_once(self):
        add_bill(self.conn, "STREAMBOX", 25.0,
                 next_due=TODAY + dt.timedelta(days=1),
                 last_seen=TODAY - dt.timedelta(days=30))
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 25.0, "STREAMBOX",
                pending=1)
        f = forecast.build(self.conn, today=TODAY, days=60)
        july = [e for e in f["events"]
                if e[2] == "STREAMBOX" and e[0] < "2026-08-01"]
        self.assertEqual(july, [])
        self.assertEqual(f["card_debt"], 425.0)

    def test_a_card_payment_bill_gives_way_to_pending_debt_too(self):
        """A hand-tracked card-payment bill stands in for a card only
        while the card machinery pays nothing; a card holding only
        pending charges is paid by the machinery, so the bill goes."""
        self.conn.execute("UPDATE accounts SET balance_current=0 "
                          "WHERE id='card'")
        add_bill(self.conn, "TALLPINE CARD AUTOPAY", 150.0,
                 next_due=TODAY + dt.timedelta(days=4),
                 last_seen=TODAY - dt.timedelta(days=26))
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 90.0,
                "COPPER LANTERN GRILL", pending=1)
        f = forecast.build(self.conn, today=TODAY, days=60)
        self.assertEqual([e for e in f["events"]
                          if e[2] == "TALLPINE CARD AUTOPAY"], [])
        self.assertEqual(sum(a for _d, a, _p in f["card_events_stmt"]),
                         -90.0)

    def test_the_cash_card_prints_the_debt_it_walks(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 85.0,
                "COPPER LANTERN GRILL", pending=1)
        st = report.gather(self.conn, TODAY)
        ctx = todayview.build_context(st)
        _s, plain, html = report.build(st)
        self.assertEqual(st["forecast"]["card_debt"], 485.0)
        self.assertIn("$485", html)
        # the plain text mirrors the tile
        self.assertIn("card debt $485", plain)
        # the current statement's row is the posted statement alone; the
        # pending charge waits for the next one
        rows = [r for r in ctx["fc_rows"] if r[0] == DUE.isoformat()]
        self.assertEqual(sum(r[1] or 0 for r in rows), -300.0)


if __name__ == "__main__":
    unittest.main()
