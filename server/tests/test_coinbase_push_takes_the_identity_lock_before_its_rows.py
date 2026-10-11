"""A crypto push takes the merchant-identity lock before it writes a row.

The push restates its rows in one transaction and then names their
merchants, which takes the household identity lock. A rename of one of
those merchants (from the Merchants page, or the nightly repair of split
merchants) takes the identity lock first and then moves the rows. Asked
for in that order — rows, then identity — the push holds a restated row
while it waits for the rename, and the rename then waits for that row: a
cycle Postgres breaks by killing one side. When the push loses, its
merchants are silently not named; when the rename loses, the person sees
an error or the whole night's repair rolls back. Taking the identity lock
right after the pairing lock, before any row, leaves the push waiting and
holding nothing the rename needs.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import bills, merchant_dedup
from oikonome.sync import coinbase_push

from .lock_order_util import Gate, Side, deadlocks, deadlocks_since, \
    tenant_of, waits_on_a_lock
from .util import make_db, write_config

DAY = dt.date(2026, 3, 2).isoformat()


def _payload(*extra) -> dict:
    return {
        "items": [{"id": "coinbase-lock", "institution_name": "Coinbase"}],
        "accounts": [{"id": "coinbase-lock", "item_id": "coinbase-lock",
                      "name": "Coinbase (lock)", "type": "investment",
                      "subtype": "crypto", "balance_current": 500.0}],
        "transactions": [
            {"id": "coinbase:lk-one", "account_id": "coinbase-lock",
             "date": DAY, "amount": 100.0, "name": "MAPLE EXCHANGE"},
            *extra],
    }


class CoinbasePushIdentityLockTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.tid = tenant_of(self.conn)
        coinbase_push.import_payload(self.conn, _payload())
        self.display = self.conn.execute(
            f"SELECT {merchant_dedup.DISPLAY_MERCHANT} AS d FROM transactions"
            f" t {merchant_dedup.MC_JOIN} WHERE t.id = 'coinbase:lk-one'"
        ).fetchone()["d"]
        self.assertTrue(self.display)

    def test_a_push_meeting_a_rename_of_its_merchant_waits_instead_of_deadlocking(self):
        gate = Gate()

        def rename(conn):
            with conn.transaction():
                return merchant_dedup.rename(conn, self.display,
                                             "Maple Exchange Desk")

        # the rename holds the identity lock and is about to move the rows
        renamer = Side(self.tid, rename)
        # the push restates the rename's row and brings one new row whose
        # merchant it must name
        pusher = Side(self.tid, lambda c: coinbase_push.import_payload(
            c, _payload({"id": "coinbase:lk-two",
                         "account_id": "coinbase-lock", "date": DAY,
                         "amount": 40.0, "name": "BIRCH LEDGER FEE"})))
        before = deadlocks()
        with mock.patch.object(bills, "lock_merchant_rename",
                               gate.wrap(bills.lock_merchant_rename)):
            renamer.start()
            self.assertTrue(gate.reached.wait(20), "the rename never started")
            pusher.start()
            self.assertTrue(waits_on_a_lock(pusher.pid, "advisory"),
                            "the push never reached the identity lock")
            gate.go.set()
            self.assertTrue(renamer.finish(), "the rename hung")
            self.assertTrue(pusher.finish(), "the push hung")
        self.assertIsNone(renamer.error, renamer.error)
        self.assertIsNone(pusher.error, pusher.error)
        self.assertEqual(deadlocks_since(before), 0,
                         "the push and the rename deadlocked")
        # the push named the merchant of the row it brought
        self.assertIsNotNone(self.conn.execute(
            "SELECT merchant_id FROM transactions WHERE id='coinbase:lk-two'"
        ).fetchone()["merchant_id"])


if __name__ == "__main__":
    unittest.main()
