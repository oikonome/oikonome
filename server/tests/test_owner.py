"""Household ownership attribution ("yours, mine, ours"). Accounts
and transactions carry an owner; a transaction inherits its account's owner
with a per-txn override; transactions and net worth filter by it. CRITICAL:
ownership is a reporting/filter dimension — it must NEVER change the verdict
or spend math (ring-fence test)."""

import unittest

from oikonome.engine import reporting
from oikonome.web import data

from .util import add_txn, make_db


class OwnerAttributionTests(unittest.TestCase):
    def setUp(self):
        # make_db seeds chk (depository $5000) + card (credit $250) on it1
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _owner_of(self, tid):
        rows = data.transactions(self.conn, 2025, 7)
        return {r["id"]: r["owner"] for r in rows}.get(tid)

    def test_effective_owner_inherits_then_override_wins(self):
        data.set_account_owner(self.conn, "chk", "Alice")
        a = add_txn(self.conn, "2025-07-01", 20, "Coffee", account="chk")
        add_txn(self.conn, "2025-07-02", 30, "Gas", account="chk", txn_id="ovr")
        self.assertEqual(self._owner_of(a), "Alice")       # inherits account
        data.set_txn_owner(self.conn, "ovr", "Bob")
        self.assertEqual(self._owner_of("ovr"), "Bob")     # override wins
        data.set_txn_owner(self.conn, "ovr", None)          # clear → re-inherit
        self.assertEqual(self._owner_of("ovr"), "Alice")

    def test_transactions_filter_by_owner(self):
        data.set_account_owner(self.conn, "chk", "Alice")
        data.set_account_owner(self.conn, "card", "Bob")
        add_txn(self.conn, "2025-07-01", 20, "A", account="chk")
        add_txn(self.conn, "2025-07-02", 30, "B", account="card")
        alice = data.transactions(self.conn, 2025, 7, owner="Alice")
        self.assertEqual(len(alice), 1)
        self.assertTrue(all(r["owner"] == "Alice" for r in alice))

    def test_list_owners_distinct_sorted(self):
        data.set_account_owner(self.conn, "chk", "Alice")
        data.set_account_owner(self.conn, "card", "Bob")
        add_txn(self.conn, "2025-07-01", 10, "X", account="chk", txn_id="z")
        data.set_txn_owner(self.conn, "z", "Zoe")
        self.assertEqual(data.list_owners(self.conn), ["Alice", "Bob", "Zoe"])

    def test_networth_owner_lens(self):
        data.set_account_owner(self.conn, "chk", "Alice")   # +$5000 depository
        data.set_account_owner(self.conn, "card", "Bob")    # $250 credit → -250
        full = reporting.compute_networth(self.conn)["current_total"]
        alice = reporting.compute_networth(self.conn, owner="Alice")["current_total"]
        bob = reporting.compute_networth(self.conn, owner="Bob")["current_total"]
        self.assertEqual(alice, 5000)
        self.assertEqual(bob, -250)
        self.assertEqual(full, 4750)                        # household unchanged

    def test_RING_FENCE_owner_never_changes_spend_math(self):
        # same rows throughout; adding owner labels must change NOTHING in the
        # spend report or the household net-worth total.
        add_txn(self.conn, "2025-07-01", 100, "Store", account="chk", txn_id="s")
        add_txn(self.conn, "2025-07-02", 50, "Shop", account="card")
        before_spend = reporting.compute_spending(self.conn)
        before_nw = reporting.compute_networth(self.conn)["current_total"]
        # attribute every account + a per-txn override — no new rows
        data.set_account_owner(self.conn, "chk", "Alice")
        data.set_account_owner(self.conn, "card", "Bob")
        data.set_txn_owner(self.conn, "s", "Bob")
        after_spend = reporting.compute_spending(self.conn)
        after_nw = reporting.compute_networth(self.conn)["current_total"]
        self.assertEqual(before_spend["category_totals"],
                         after_spend["category_totals"])
        self.assertEqual(before_spend["by_year"], after_spend["by_year"])
        self.assertEqual(before_nw, after_nw)     # household total unchanged


if __name__ == "__main__":
    unittest.main()
