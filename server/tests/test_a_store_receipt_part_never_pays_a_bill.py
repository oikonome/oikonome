"""A store receipt's split part never pays a bill; the charge it came from
still can, and so can a person's own split.

A bill is a whole charge the bank sent. A person who splits a charge is
saying it was really several, so their parts stand in for charges at the
bill matcher. A store matcher's parts are only how a receipt's items read:
a $15 coffee line inside a $100 order must not pay a $14.99 subscription
that was never charged — the bill would read paid, nothing would be
overdue, and the groceries would leave variable spending for the fixed
line. The whole charge the parts came from is still the bank's charge,
so if THAT is the bill, the bill is paid.
"""
import datetime as dt
import json
import unittest

from oikonome.engine import budget, splits
from oikonome.web import api as web_api

from .util import TODAY, add_bill, add_txn, make_db, write_config

DUE = dt.date(2026, 7, 8)
PRIME = "Amazon Prime"
FOOD, HOME = "FOOD_AND_DRINK", "HOME_IMPROVEMENT"


def _store_split(conn, txn, parts, kind="amazon"):
    for line, (cat, amt) in enumerate(parts, 1):
        conn.execute(
            "INSERT INTO transaction_splits (txn_id, line, category, amount,"
            " source) VALUES (%s, %s, %s, %s, %s)", (txn, line, cat, amt, kind))


class StoreReceiptPartTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, PRIME, 14.99, next_due=DUE,
                 last_seen=DUE - dt.timedelta(days=31), show_today=True)

    def tearDown(self):
        self.conn.close()

    def _status(self):
        return budget.month_status(self.conn, TODAY)

    def _overdue(self, st):
        return [o for o in st["overdue_unpaid"] if o[0] == PRIME]

    def _stats(self):
        return web_api._month_bill_stats(
            self.conn, budget.load_config(self.conn), TODAY)[PRIME]

    def test_a_receipt_line_does_not_pay_a_bill_never_charged(self):
        t = add_txn(self.conn, DUE + dt.timedelta(days=1), 100.0, "AMAZON.COM")
        _store_split(self.conn, t, [(FOOD, 15.0), (HOME, 85.0)])
        st = self._status()
        self.assertEqual(len(self._overdue(st)), 1)
        self.assertEqual(st["buckets"]["fixed"]["actual"], 0.0)
        # the groceries line stays variable spending
        self.assertAlmostEqual(st["buckets"]["food"]["actual"], 15.0, places=2)
        # the Bills page reads the same rows through the same matcher
        self.assertEqual(self._stats()["paid"], 0)

    def test_a_persons_split_still_pays_the_bill(self):
        t = add_txn(self.conn, DUE + dt.timedelta(days=1), 100.0, "AMAZON.COM")
        splits.set_split(self.conn, t, [{"category": "ENTERTAINMENT",
                                         "amount": 14.99},
                                        {"category": HOME, "amount": 85.01}])
        st = self._status()
        self.assertEqual(self._overdue(st), [])
        self.assertAlmostEqual(st["buckets"]["fixed"]["actual"], 14.99,
                               places=2)
        self.assertEqual(self._stats()["paid"], 1)

    def test_a_store_split_charge_that_is_the_bill_still_pays_it(self):
        t = add_txn(self.conn, DUE, 14.99, "AMAZON PRIME")
        _store_split(self.conn, t, [(FOOD, 10.0), (HOME, 4.99)])
        st = self._status()
        self.assertEqual(self._overdue(st), [])
        # the whole charge is the bill: every part leaves variable spending
        self.assertAlmostEqual(st["buckets"]["fixed"]["actual"], 14.99,
                               places=2)
        self.assertEqual(st["buckets"]["food"]["actual"], 0.0)
        stats = self._stats()
        self.assertEqual(stats["paid"], 1)
        self.assertAlmostEqual(stats["paid_amount"], 14.99, places=2)

    def test_a_receipt_line_is_not_named_a_bill_beyond_its_schedule(self):
        add_txn(self.conn, DUE, 14.99, "AMAZON PRIME")
        t = add_txn(self.conn, DUE + dt.timedelta(days=2), 100.0, "AMAZON.COM")
        _store_split(self.conn, t, [(FOOD, 15.0), (HOME, 85.0)])
        st = self._status()
        self.assertNotIn("beyond bill schedule", json.dumps(st["why"]))
        self.assertNotIn("beyond bill schedule", json.dumps(st["reasons"]))

    def test_the_seed_budget_counts_a_receipt_line_as_spending(self):
        add_bill(self.conn, "Amazon Market", 120.0, next_due=DUE,
                 last_seen=DUE - dt.timedelta(days=31))
        last = (TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=9)
        t = add_txn(self.conn, last, 400.0, "AMAZON MARKET ORDER")
        _store_split(self.conn, t, [(FOOD, 120.0), (HOME, 280.0)])
        whole = add_txn(self.conn, last - dt.timedelta(days=1), 120.0,
                        "AMAZON MARKET")
        _store_split(self.conn, whole, [(FOOD, 60.0), (HOME, 60.0)])
        seeded = budget._seed_config(self.conn, today=TODAY)
        # the $120 groceries line is spending; the $120 bill charge (split
        # by its receipt or not) is the bill's and counts nowhere. The seed
        # divides by the days covered (June 8 to yesterday), the same
        # factor for both figures, so their ratio is the 120:280 of the
        # order — to within the seed's rounding to $10.
        self.assertAlmostEqual(seeded["food_monthly"] / seeded["other_monthly"],
                               120 / 280, delta=0.03)


class ABillPaidByAStoreSplitChargeOwnsEveryPart(unittest.TestCase):
    """A store-split charge that pays a bill is the bill's, whole. Neither
    of its receipt parts may also be drawn by an envelope that matches the
    part's category — the money would be counted twice, once as the bill
    paid and again as envelope spending — and the Bills page must hand the
    envelope exactly the rows Today does, or the two pages disagree about
    the same month.
    """

    SUBSCRIPTION = "Subscribe & Save"

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Groceries", 400.0, bill_type="envelope",
                 frequency=None, interval=1, category=FOOD,
                 match_category=True)
        add_bill(self.conn, self.SUBSCRIPTION, 45.0, next_due=DUE,
                 last_seen=DUE - dt.timedelta(days=31))
        t = add_txn(self.conn, DUE, 45.0, "SUBSCRIBE & SAVE")
        _store_split(self.conn, t, [(FOOD, 30.0), (HOME, 15.0)])
        # an ordinary grocery run, so the envelope has something of its own
        add_txn(self.conn, DUE + dt.timedelta(days=2), 20.0, "CORNER MARKET",
                primary=FOOD)

    def tearDown(self):
        self.conn.close()

    def test_the_envelope_never_draws_a_part_of_the_bill_charge(self):
        st = budget.month_status(self.conn, TODAY)
        verdict = next(e for e in st["buckets"]["fixed"]["envelopes"]
                       if e["payee"] == "Groceries")
        stats = web_api._month_bill_stats(
            self.conn, budget.load_config(self.conn), TODAY)
        self.assertAlmostEqual(stats["Groceries"]["used"], verdict["used"],
                               places=2)
        # and the shared answer is the right one: only the grocery run
        self.assertAlmostEqual(stats["Groceries"]["used"], 20.0, places=2)

    def test_the_bill_reads_paid_by_the_whole_charge(self):
        stats = web_api._month_bill_stats(
            self.conn, budget.load_config(self.conn), TODAY)
        self.assertEqual(stats[self.SUBSCRIPTION]["paid"], 1)
        self.assertAlmostEqual(stats[self.SUBSCRIPTION]["paid_amount"], 45.0,
                               places=2)


if __name__ == "__main__":
    unittest.main()
