"""A month's frozen bill schedule comes back from a restore in the shape a
live bill would.

The restore runs every live bill's raw config through clean_bill_raw; the
same raw also sits inside each closed month's frozen schedule, which the
Month and Year lenses expand exactly like the live table. A frozen row
whose recurrence is a string, or whose interval is text, would otherwise
fail that month's lens and the whole year's, with nothing in the app able
to remove the row.
"""

import io
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.engine.compat import jsonb
from oikonome.sync import restore
from oikonome.web import lenses

from .export_ticket import export_get
from .util import TODAY, _ensure_db, make_db, seed_accounts, write_config

PW = "correct-horse-battery"


class RestoredFrozenScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def test_a_malformed_frozen_recurrence_is_cleaned_on_restore(self):
        client = TestClient(self.app)
        client.post("/api/signup", data={
            "email": f"frozen-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        tid = client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                "INSERT INTO budget_snapshots (year, month, config, bills) "
                "VALUES (2026, 6, %s, %s)",
                (jsonb({"food_monthly": 900, "other_monthly": 900}),
                 jsonb({"rows": [
                     {"payee": "Gym", "amount": -40.0, "merchant": None,
                      "category": None,
                      "raw": {"recurrence": "monthly"}},
                     {"payee": "Paycheck Co", "amount": 2000.0,
                      "merchant": None, "category": None,
                      "raw": {"recurrence": {"frequency": "WEEKLY",
                                             "interval": "2"}}},
                     {"payee": 7, "amount": "x", "raw": {}}]})))
        finally:
            conn.close()
        r = export_get(client, "/export")
        self.assertEqual(r.status_code, 200)
        dest = make_db()
        try:
            restore.restore_zip(dest, io.BytesIO(r.content).getvalue())
            rows = budget.snapshot_bill_rows(dest, 2026, 6)
            self.assertEqual([b["payee"] for b in rows], ["Gym", "Paycheck Co"])
            for b in rows:
                rec = b["raw"].get("recurrence")
                self.assertTrue(rec is None or isinstance(rec, dict), b)
                if rec:
                    self.assertIsInstance(rec.get("interval", 1), int, b)
            # the lenses that expand the frozen schedule read the month
            lenses.month_summary(dest, 2026, 6, today=TODAY)
            lenses.year_summary(dest, 2026, today=TODAY)
        finally:
            dest.close()


if __name__ == "__main__":
    unittest.main()
