"""The Cash Flow picture's fixed split reflects a bill edit at once.

Closed months' fixed totals are cached per process to keep an all-history
window cheap. A cache keyed on time alone would keep showing the pre-edit
split for up to ten minutes after a bill was added — disagreeing with the
Budget page it promises to agree with.
"""

import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import add_bill, add_txn, make_db, seed_accounts, write_config


class FixedSplitTests(unittest.TestCase):
    def test_a_new_bill_moves_the_split_without_waiting_for_the_ttl(self):
        conn = make_db()
        try:
            write_config(conn)
            seed_accounts(conn)
            bank = conn.execute(
                "SELECT id FROM accounts WHERE type='depository' LIMIT 1"
            ).fetchone()["id"]
            today = dt.date(2026, 6, 15)
            # a charge in a CLOSED month, so its month lands in the cache
            add_txn(conn, dt.date(2026, 4, 3), 120.0, "Rent Co",
                    account=bank, primary="RENT_AND_UTILITIES")
            first = reporting.flow_breakdown(conn, today, "1y")
            self.assertEqual(first["out"]["fixed"], 0.0)
            add_bill(conn, "Rent Co", 120.0, frequency="MONTHLY",
                     next_due=dt.date(2026, 7, 3))
            second = reporting.flow_breakdown(conn, today, "1y")
            self.assertEqual(second["out"]["fixed"], 120.0,
                             "bill edit invisible to the fixed split")
        finally:
            conn.close()
