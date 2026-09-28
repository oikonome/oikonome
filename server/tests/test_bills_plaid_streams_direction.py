"""A Plaid stream's DIRECTION is part of its identity.

`propose_from_plaid_streams` skips a stream when a pending add-proposal
already names the same payee. Payee alone is not the identity: the same
counterparty can be both an outflow and an inflow — rent paid out and a
roommate's share coming back, a card bill and its refund stream — and
keying on the payee token alone means whichever direction Plaid reported
first wins and the other is silently dropped, forever (the surviving
proposal stays pending, so it keeps swallowing its opposite).
"""

import unittest

from oikonome.engine import bills

from .util import make_db


def _stream(sid, merchant, direction, amount):
    return {"stream_id": sid, "direction": direction, "merchant": merchant,
            "description": merchant, "amount": amount, "frequency": "MONTHLY",
            "is_active": True, "status": "MATURE",
            "first_date": "2026-01-05", "last_date": "2026-08-05",
            "predicted_next_date": "2026-09-05", "transaction_ids": ["a", "b", "c"]}


class PlaidStreamDirectionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.conn.execute("DELETE FROM bill_proposals")

    def _pending(self):
        return self.conn.execute(
            "SELECT payee, evidence FROM bill_proposals "
            "WHERE status='pending' AND kind='add'").fetchall()

    def test_the_same_payee_in_both_directions_proposes_both(self):
        stats = bills.propose_from_plaid_streams(self.conn, [
            _stream("s-out", "Harbor Property Mgmt", "outflow", 1800),
            _stream("s-in", "Harbor Property Mgmt", "inflow", 900)])
        self.assertEqual(2, stats["proposed"], stats)
        got = sorted(bool(dict(r["evidence"]).get("income")) for r in self._pending())
        self.assertEqual([False, True], got,
                         "one bill and one income proposal must survive")

    def test_a_repeat_of_the_same_direction_is_still_deduped(self):
        """Direction widens the key; it must not disable the dedup — a
        second stream for the same payee and direction is still covered."""
        stats = bills.propose_from_plaid_streams(self.conn, [
            _stream("s-1", "Northwind Gas", "outflow", 60),
            _stream("s-2", "Northwind Gas", "outflow", 62)])
        self.assertEqual(1, stats["proposed"], stats)
        self.assertEqual(1, stats["covered"], stats)


if __name__ == "__main__":
    unittest.main()
