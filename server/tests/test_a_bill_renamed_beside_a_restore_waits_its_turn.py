"""A bill renamed and saved while a restore of the household is running
waits its turn; neither is killed as a deadlock.

The restore takes the bill-ceiling lock when its bills land and the
settings row when it merges the settings, later in the same transaction.
The bill editor's Save renames the bill (moving the settings keyed by its
name under the settings row) and then saves it (taking the bill-ceiling
lock). Taken in that order, the two closed a cycle — the restore held the
ceiling and waited for the row, the save held the row and waited for the
ceiling — and Postgres killed the save with "deadlock detected".
"""

import csv
import io
import json
import unittest
import zipfile
from unittest import mock

from oikonome.engine import bills
from oikonome.sync import restore

from . import lock_order_util as lu
from .util import add_bill, make_db, write_config


def _archive() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps({"food_monthly": 700})})
        z.writestr("tenant_settings.csv", s.getvalue())
        cols = ["id", "type", "payee", "amount", "frequency",
                "monthly_amount", "due_on", "category", "merchant",
                "is_completed", "active", "synced_at", "raw"]
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=cols)
        w.writeheader()
        w.writerow({"id": "gym-example", "type": "occurrence",
                    "payee": "Gym Example", "amount": "-40",
                    "frequency": "MONTHLY", "monthly_amount": "-40",
                    "due_on": "2026-07-20", "category": "", "merchant": "",
                    "is_completed": "0", "active": "1", "synced_at": "",
                    "raw": json.dumps({"frequency": "MONTHLY", "interval": 1,
                                       "source": "manual"})})
        z.writestr("bills.csv", s.getvalue())
    return buf.getvalue()


class ARenameBesideARestoreWaitsItsTurn(unittest.TestCase):
    def test_neither_side_is_killed(self):
        conn = make_db()
        write_config(conn)
        add_bill(conn, "Rent Example", 1200.0, source="manual")
        tid = lu.tenant_of(conn)
        conn.close()
        gate = lu.Gate()
        data = _archive()
        before = lu.deadlocks()
        with mock.patch.object(restore, "_settle_settings",
                               gate.before(restore._settle_settings)):
            a = lu.Side(tid, lambda c: restore.restore_zip(c, data))
            a.start()
            self.assertTrue(gate.reached.wait(30))     # bills landed

            def rename_and_save(c):
                with c.transaction():
                    bills.hold_bill_claims(c)
                    bills.rename_bill(c, "Rent Example", "Rent Renamed")
                    bills.save_bill(c, payee="Rent Renamed", amount=1200.0,
                                    bill_type="occurrence",
                                    frequency="MONTHLY", interval=1,
                                    source="manual")
            b = lu.Side(tid, rename_and_save)
            b.start()
            self.assertTrue(lu.waits_on_a_lock(b.pid, timeout=10))
            gate.go.set()
            self.assertTrue(a.finish(60))
            self.assertTrue(b.finish(60))
        self.assertIsNone(a.error)
        self.assertIsNone(b.error)
        self.assertEqual(lu.deadlocks_since(before), 0)


if __name__ == "__main__":
    unittest.main()
