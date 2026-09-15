"""A bill restored from a ZIP must not crash every later Today load.

save_bill validates a bill's raw config; the restore wrote it verbatim,
so one malformed recurrence day, envelope pool or a raw that was not an
object at all raised out of the occurrence generator on every dashboard,
budget and email render for the household until someone edited the row
in the database.
"""

import csv
import datetime as dt
import io
import json
import unittest
import zipfile

from oikonome.engine import budget
from oikonome.sync import restore
from oikonome.web import api

from .util import make_db, write_config

COLS = ["id", "type", "payee", "amount", "frequency", "monthly_amount",
        "due_on", "category", "merchant", "is_completed", "active", "raw"]


def _zip(bills: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=COLS)
        w.writeheader()
        for b in bills:
            w.writerow({**{c: "" for c in COLS}, **b})
        z.writestr("bills.csv", s.getvalue())
    return buf.getvalue()


class CleanBillRawTests(unittest.TestCase):
    def test_non_object_raw_reads_as_empty(self):
        for bad in ([1, 2], "x", 7, None):
            self.assertEqual(restore.clean_bill_raw(bad), {})
            self.assertEqual(restore._raw_dict(bad), {})

    def test_malformed_recurrence_fields_are_dropped_not_the_bill(self):
        raw = restore.clean_bill_raw({
            "recurrence": {"frequency": "MONTHLY", "byMonthDay": ["x", 15],
                           "bySetPos": "q", "interval": "lots"},
            "envelope_months": "12x", "companions": "no"})
        self.assertEqual(raw["recurrence"],
                         {"frequency": "MONTHLY", "byMonthDay": [15]})
        self.assertNotIn("envelope_months", raw)
        self.assertNotIn("companions", raw)

    def test_recurrence_that_is_not_an_object_is_dropped(self):
        raw = restore.clean_bill_raw({"recurrence": "monthly"})
        self.assertNotIn("recurrence", raw)


class OccurrenceGuardTests(unittest.TestCase):
    def test_bad_month_day_falls_back_to_the_anchor(self):
        anchor = dt.date(2026, 3, 9)
        bill = {"recurrence": {"frequency": "MONTHLY", "byMonthDay": ["x"]},
                "anchor": anchor}
        self.assertEqual(budget._occurrences_in_month(bill, 2026, 7),
                         [dt.date(2026, 7, 9)])
        bill = {"recurrence": {"frequency": "MONTHLY", "byMonthDay": "15"},
                "anchor": anchor}
        self.assertEqual(budget._occurrences_in_month(bill, 2026, 7),
                         [dt.date(2026, 7, 9)])

    def test_unhashable_by_day_entries_are_skipped(self):
        anchor = dt.date(2026, 3, 9)
        bill = {"recurrence": {"frequency": "MONTHLY",
                               "byDay": [{"x": 1}, 7, "FR"]},
                "anchor": anchor}
        out = budget._occurrences_in_month(bill, 2026, 7)
        self.assertTrue(out)
        self.assertTrue(all(d.weekday() == 4 for d in out))
        self.assertEqual(
            restore.clean_bill_raw({"recurrence": {"byDay": [{"x": 1}, "MO"]}})
            ["recurrence"], {"byDay": ["MO"]})
        self.assertNotIn(
            "byDay", restore.clean_bill_raw(
                {"recurrence": {"byDay": [3]}})["recurrence"])

    def test_envelope_months_never_raises(self):
        self.assertEqual(budget.envelope_months({"envelope_months": "12x"}), 1)
        self.assertEqual(budget.envelope_months({"envelope_months": [12]}), 1)
        self.assertEqual(budget.envelope_months({"envelope_months": "12"}), 12)
        self.assertEqual(api._bill_cadence({"bill_type": "envelope",
                                            "envelope_months": {}}),
                         "ENVELOPE:1")
        self.assertEqual(api._bill_cadence({"recurrence": "weekly"}),
                         "ONE_TIME:1")


class RestoreThenRenderTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_poisoned_bill_row_does_not_break_the_plan(self):
        restore.restore_zip(self.conn, _zip([
            {"id": "b-1", "type": "bill", "payee": "Bad Anchor Co",
             "amount": "-40", "frequency": "MONTHLY", "active": "1",
             "raw": json.dumps({"recurrence": {"frequency": "MONTHLY",
                                               "byMonthDay": ["x"]}})},
            {"id": "b-2", "type": "bill", "payee": "Bad Pool Co",
             "amount": "-90", "frequency": "MONTHLY", "active": "1",
             "raw": json.dumps({"bill_type": "envelope",
                                "envelope_months": "twelve"})},
            {"id": "b-3", "type": "bill", "payee": "Scalar Raw Co",
             "amount": "-10", "frequency": "MONTHLY", "active": "1",
             "raw": json.dumps([1, 2, 3])}]))
        # the rows landed, the plan renders, and the poisoned fields are
        # simply absent
        today = dt.date(2026, 7, 15)
        bl = budget._recurring_bills(self.conn)
        budget.month_occurrences(bl, today.year, today.month)
        self.assertIsInstance(budget.recurring_load(self.conn, today), dict)
        rows = self.conn.execute(
            "SELECT payee, raw FROM bills ORDER BY payee").fetchall()
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertIsInstance(r["raw"], dict)


if __name__ == "__main__":
    unittest.main()
