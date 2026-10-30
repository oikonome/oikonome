"""A row's "apply to the whole merchant" correction takes the household's
merchant-identity lock first, before it locks the row.

Every merchant-identity writer (a rename, a merge, the nightly repair)
takes the identity lock, then the merchant's ledger rows, then its rules.
The row's correction took none of it: it locked the row (to drop a store's
split of it), then wrote the merchant's rule and re-filed its other rows,
crossing an identity writer on exactly those rows and rules, so one side
was killed as a deadlock or timed out. Taking identity first, the
correction waits its turn holding nothing the identity writer needs:
pinned here by an identity holder that must still reach the corrected row
while the correction is queued.
"""

import datetime as dt
import unittest

from oikonome.db import tenancy
from oikonome.engine import merchant_identity
from oikonome.web import data

from .lock_order_util import Side, tenant_of, waits_on_a_lock
from .util import TODAY, add_txn, make_db

LINE = "Northwind Bicycle Works"


class RowCorrectionLockOrderTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = tenant_of(self.conn)
        self.ids = [add_txn(self.conn, TODAY - dt.timedelta(days=back),
                            30.0, LINE, primary="SHOPPING")
                    for back in (60, 30, 10)]

    def test_an_identity_writer_can_still_reach_the_corrected_row(self):
        edited = self.ids[0]

        def correct(conn):
            # the route runs the correction inside its own transaction
            with conn.transaction():
                return data.set_category(conn, edited, "TRAVEL", scope="all")

        holder = tenancy.tenant_connect(self.tid)
        self.addCleanup(holder.close)
        writer = Side(self.tid, correct)
        reached = None
        with holder.transaction():
            merchant_identity._lock_identity(holder)
            writer.start()
            self.assertTrue(waits_on_a_lock(writer.pid, "advisory"),
                            "the correction did not wait for identity")
            holder.execute("SET LOCAL lock_timeout = '2s'")
            try:
                holder.execute("UPDATE transactions SET name = name "
                               "WHERE id = %s", (edited,))
                reached = True
            except Exception as e:                       # noqa: BLE001
                reached = e
        self.assertTrue(writer.finish(), "the correction hung")
        self.assertIs(reached, True,
                      f"the identity holder could not reach the row: "
                      f"{reached}")
        self.assertIsNone(writer.error, writer.error)
        self.assertIsNotNone(writer.result, "the correction taught nothing")
        self.assertEqual({r["category_primary"] for r in self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id <> %s "
            "AND name = %s", (edited, LINE)).fetchall()}, {"TRAVEL"})


if __name__ == "__main__":
    unittest.main()
