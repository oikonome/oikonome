"""A bill's remembered past amounts cannot break the bill matchers.

`raw.amount_history` is read by every bill matcher in the household, and
a restore ZIP hands raw over verbatim. A history that is an object rather
than a list, or an entry whose amount is infinite or too large for a
float, used to raise out of the band builder (one bad bill, every match
500s) or — worse, for an infinite amount — build a band of infinite
tolerance that claimed every charge at the payee. Entries that are not a
real past price are dropped, by the reader and by the restore alike.
"""

import csv
import datetime as dt
import io
import json
import unittest
import zipfile

from oikonome.engine import budget
from oikonome.sync import restore

from .util import make_db, write_config

COLS = ["id", "type", "payee", "amount", "frequency", "monthly_amount",
        "due_on", "category", "merchant", "is_completed", "active", "raw"]

BAD_HISTORIES = [
    {"amount": 30, "until": "2026-01-01"},           # an object, not a list
    7,
    "30",
    [{"amount": "inf", "until": "2026-01-01"}],
    [{"amount": 10 ** 400, "until": "2026-01-01"}],  # float() overflows
    [{"amount": "nan", "until": "2026-01-01"}],
    [{"amount": True, "until": "2026-01-01"}],
    [{"amount": 30, "until": "someday"}],
    [{"amount": 30}],
    [{"amount": 0, "until": "2026-01-01"}],
    ["x", None, [1]],
]


class BandsTests(unittest.TestCase):
    def test_a_malformed_history_leaves_only_the_current_band(self):
        for hist in BAD_HISTORIES:
            with self.subTest(hist=repr(hist)[:40]):
                bands = budget.bill_amount_bands(50, {"amount_history": hist})
                self.assertEqual(bands, [(50.0, budget.bill_tolerance(50), None)])
                self.assertFalse(budget.amount_fits(bands, 900.0))

    def test_good_entries_survive_beside_bad_ones(self):
        bands = budget.bill_amount_bands(50, {"amount_history": [
            {"amount": "inf", "until": "2026-01-01"},
            {"amount": 30, "until": "2026-01-01"}]})
        self.assertEqual([b[0] for b in bands], [50.0, 30.0])


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def test_clean_bill_raw_keeps_only_real_past_prices(self):
        for hist in BAD_HISTORIES:
            with self.subTest(hist=repr(hist)[:40]):
                self.assertNotIn("amount_history",
                                 restore.clean_bill_raw({"amount_history": hist}))
        raw = restore.clean_bill_raw({"amount_history": [
            {"amount": "inf", "until": "2026-01-01"},
            {"amount": "30.5", "until": "2026-02-01T00:00:00", "x": 1}]
            + [{"amount": i + 1, "until": "2025-01-01"} for i in range(20)]})
        self.assertEqual(len(raw["amount_history"]), budget.AMOUNT_HISTORY_MAX)
        self.assertEqual(raw["amount_history"][-1],
                         {"amount": 20.0, "until": "2025-01-01"})
        self.assertNotIn({"amount": 30.5, "until": "2026-02-01"},
                         raw["amount_history"])  # capped off the front

    def test_a_restored_bill_with_a_poisoned_history_still_matches(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=COLS)
            w.writeheader()
            for i, hist in enumerate(([{"amount": "inf", "until": "2026-01-01"}],
                                      {"amount": 30, "until": "2026-01-01"})):
                w.writerow({**{c: "" for c in COLS}, "id": f"b-{i}",
                            "type": "bill", "payee": f"Poison {i}",
                            "amount": "-40", "frequency": "MONTHLY",
                            "active": "1",
                            "raw": json.dumps({"amount_history": hist})})
            z.writestr("bills.csv", s.getvalue())
        restore.restore_zip(self.conn, buf.getvalue())
        rows = self.conn.execute("SELECT raw FROM bills").fetchall()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertNotIn("amount_history", r["raw"])
            budget.bill_amount_bands(40, r["raw"])
        today = dt.date(2026, 7, 15)
        self.assertIsInstance(budget.recurring_load(self.conn, today), dict)


if __name__ == "__main__":
    unittest.main()
