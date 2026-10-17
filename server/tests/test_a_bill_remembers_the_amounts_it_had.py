"""A bill remembers the amounts it had before, so a price change does
not turn the months paid at the old price into unmatched spending.

Every matcher reads the bill's amount bands — the current amount and
each earlier one with the last day it covers (budget.bill_amount_bands):
the ⟳ stamp on ledger rows, the categories a bill stamps, the health
read and the removal test. Drift reads the current amount only, so a
median over old and new prices can never walk the amount back."""

import datetime as dt
import unittest

from oikonome.engine import bills as rd
from oikonome.engine import budget
from oikonome.engine.compat import as_dict
from oikonome.web.api import _stamp_recurring

from .util import TODAY, add_bill, add_txn, make_db, write_config

DUE = TODAY + dt.timedelta(days=20)


def _months_ago(n: int, day: int = 1) -> dt.date:
    y, m = TODAY.year, TODAY.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return dt.date(y, m, day)


class ABillRemembersItsAmounts(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _raw(self, payee="Harbor Gym"):
        return as_dict(rd.get_bill(self.conn, payee)["raw"])

    def test_an_edit_files_the_old_amount_with_the_day_it_stopped(self):
        add_bill(self.conn, "Harbor Gym", 80.00, next_due=DUE)
        self.assertNotIn("amount_history", self._raw())
        rd.save_bill(self.conn, payee="Harbor Gym", amount=45.00,
                     frequency="MONTHLY", next_due=DUE, source="manual",
                     manual_touch=True)
        hist = self._raw()["amount_history"]
        self.assertEqual([h["amount"] for h in hist], [80.00])
        self.assertTrue(hist[0]["until"])
        # a save that keeps the amount keeps the history too
        rd.save_bill(self.conn, payee="Harbor Gym", amount=45.00,
                     frequency="MONTHLY", next_due=DUE, source="manual",
                     show_today=True)
        self.assertEqual([h["amount"] for h in self._raw()["amount_history"]],
                         [80.00])

    def test_old_price_payments_keep_their_recurring_mark(self):
        add_bill(self.conn, "Harbor Gym", 80.00, next_due=DUE)
        rd.save_bill(self.conn, payee="Harbor Gym", amount=45.00,
                     frequency="MONTHLY", next_due=DUE, source="manual",
                     manual_touch=True)
        rows = [
            {"payee": "harbor gym", "amount": 80.00,
             "date": _months_ago(2), "merchant_id": None},
            {"payee": "harbor gym", "amount": 80.00,
             "date": _months_ago(1), "merchant_id": None},
            {"payee": "harbor gym", "amount": 45.00,
             "date": TODAY, "merchant_id": None},
            # the old price long after the change is not this bill
            {"payee": "harbor gym", "amount": 80.00,
             "date": dt.date.fromisoformat(self._raw()["amount_history"][0]["until"])
             + dt.timedelta(days=60), "merchant_id": None},
        ]
        _stamp_recurring(self.conn, rows)
        self.assertEqual([r["recurring_bill"] for r in rows],
                         ["Harbor Gym", "Harbor Gym", "Harbor Gym", None])

    def test_health_and_removal_count_old_price_payments(self):
        add_bill(self.conn, "Harbor Gym", 80.00, next_due=DUE,
                 last_seen=_months_ago(1, 20))
        for n in (3, 2, 1):
            add_txn(self.conn, _months_ago(n, 20), 80.00, "HARBOR GYM")
        rd.save_bill(self.conn, payee="Harbor Gym", amount=45.00,
                     frequency="MONTHLY", next_due=DUE, source="manual",
                     manual_touch=True)
        rd.run(self.conn, TODAY)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM bill_proposals "
            "WHERE kind='remove' AND status='pending'").fetchone()["n"], 0)
        health = rd.analyze_bills(self.conn, TODAY, payee="Harbor Gym")["Harbor Gym"]
        self.assertIn(health["health"], ("ok", "drifting", "misdated"))
        self.assertEqual(str(health["last_match"]), _months_ago(1, 20).isoformat())

    def test_drift_reads_the_current_amount_only(self):
        """Three old-price payments and one at the new price: the median
        the drift pass reads must not include the old ones, or the
        amount a person just set would be walked back to the old price."""
        add_bill(self.conn, "Harbor Gym", 80.00, next_due=DUE,
                 last_seen=_months_ago(1))
        for n in (3, 2, 1):
            add_txn(self.conn, _months_ago(n), 80.00, "HARBOR GYM")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 45.00, "HARBOR GYM")
        rd.save_bill(self.conn, payee="Harbor Gym", amount=45.00,
                     frequency="MONTHLY", next_due=DUE, source="detected",
                     last_seen=TODAY - dt.timedelta(days=2))
        rd.run(self.conn, TODAY)
        self.assertEqual(abs(rd.get_bill(self.conn, "Harbor Gym")["amount"]),
                         45.00)

    def test_bands_tolerate_a_malformed_history(self):
        bands = budget.bill_amount_bands(
            -200.0, {"amount_history": [{"amount": "x"}, 7, {"amount": 120,
                                                               "until": "2026-01-15"}]})
        self.assertEqual([round(b[0], 2) for b in bands], [200.0, 120.0])
        # the old band covers charges up to 35 days past its last day
        self.assertTrue(budget.amount_fits(bands, 120.0, dt.date(2026, 2, 1)))
        self.assertFalse(budget.amount_fits(bands, 120.0, dt.date(2026, 6, 1)))
        self.assertTrue(budget.amount_fits(bands, 180.0, dt.date(2026, 6, 1)))


if __name__ == "__main__":
    unittest.main()
