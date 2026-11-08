"""A non-finite number in any restored JSON cell costs that value, never the
whole restore.

Python's JSON reader takes NaN, Infinity and 1e999; PostgreSQL's jsonb
refuses them. The restore is ONE transaction, so a single such token in any
jsonb cell an archive carries (a frozen month's config or bill schedule, a
net-worth snapshot's class split, a tax document's amounts) aborted the
whole merge at that insert: every other row of the export was lost to one
bad cell. Where a cleaner checks the number (a frozen budget, a frozen bill
amount) the row is refused as malformed; elsewhere the value is read as
null, as the settings merge already did.

Also here: a frozen bill row that lacks the merchant or category key the
closed-month readers index is read as having none, not a KeyError on every
read of that month.
"""

import csv
import io
import json
import unittest
import zipfile

from oikonome.engine import budget
from oikonome.engine.compat import jsonb
from oikonome.sync import restore
from oikonome.web import lenses

from .util import TODAY, make_db, write_config


def _csv(fields, rows) -> str:
    s = io.StringIO()
    w = csv.DictWriter(s, fieldnames=fields)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return s.getvalue()


def _zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    return buf.getvalue()


SNAP_FIELDS = ["year", "month", "config", "bills", "captured_at", "source"]
GOOD_CFG = json.dumps({"food_monthly": 900, "other_monthly": 400})


class NonFiniteRestoreTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _snap(self, year, month):
        return self.conn.execute(
            "SELECT config, bills FROM budget_snapshots "
            "WHERE year=%s AND month=%s", (year, month)).fetchone()

    def test_a_non_finite_frozen_bill_amount_drops_that_row_only(self):
        bills = ('{"rows": [{"payee": "Rent", "amount": NaN, "raw": {}}, '
                 '{"payee": "Gym", "amount": -40, "raw": {}}], '
                 '"bills_monthly": Infinity}')
        restore.restore_zip(self.conn, _zip({
            "budget_snapshots.csv": _csv(SNAP_FIELDS, [
                {"year": 2026, "month": 3, "config": GOOD_CFG,
                 "bills": bills, "source": "close"},
                {"year": 2026, "month": 4, "config": GOOD_CFG,
                 "bills": "", "source": "close"}])}))
        row = self._snap(2026, 3)
        self.assertIsNotNone(row)
        self.assertEqual([b["payee"] for b in row["bills"]["rows"]], ["Gym"])
        self.assertIsNone(row["bills"]["bills_monthly"])
        self.assertIsNotNone(self._snap(2026, 4))

    def test_a_non_finite_frozen_budget_refuses_that_month_only(self):
        counts = restore.restore_zip(self.conn, _zip({
            "budget_snapshots.csv": _csv(SNAP_FIELDS, [
                {"year": 2026, "month": 3,
                 "config": '{"food_monthly": NaN, "other_monthly": 400}',
                 "bills": "", "source": "close"},
                {"year": 2026, "month": 4, "config": GOOD_CFG,
                 "bills": "", "source": "close"}])}))
        self.assertIsNone(self._snap(2026, 3))
        self.assertIsNotNone(self._snap(2026, 4))
        self.assertTrue(counts)

    def test_non_finite_numbers_in_other_json_cells_are_read_as_null(self):
        restore.restore_zip(self.conn, _zip({
            "networth_snapshot.csv": _csv(
                ["date", "total", "by_class"],
                [{"date": "2026-02-01", "total": "100.00",
                  "by_class": '{"cash": 1e999, "debt": -5}'}]),
            "income_documents.csv": _csv(
                ["id", "year", "form", "owner", "payer", "primary_amount",
                 "amounts", "notes"],
                [{"id": "1", "year": "2025", "form": "W-2",
                  "owner": "primary", "payer": "Example Payroll",
                  "primary_amount": "1000",
                  "amounts": '{"box1": -Infinity, "box2": 12.5}',
                  "notes": ""}])}))
        nw = self.conn.execute(
            "SELECT by_class FROM networth_snapshot WHERE date='2026-02-01'"
        ).fetchone()
        self.assertEqual(nw["by_class"], {"cash": None, "debt": -5})
        doc = self.conn.execute(
            "SELECT amounts FROM income_documents WHERE year=2025").fetchone()
        self.assertEqual(doc["amounts"], {"box1": None, "box2": 12.5})

    def test_a_frozen_bill_row_without_merchant_or_category_reads_as_none(self):
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO budget_snapshots (year, month, config, bills) "
            "VALUES (2026, 2, %s, %s)",
            (jsonb({"food_monthly": 900, "other_monthly": 400}),
             jsonb({"rows": [
                 {"payee": "Rent", "amount": -1500,
                  "raw": {"dueOn": "2026-02-01"}}]})))
        rows = budget.snapshot_bill_rows(self.conn, 2026, 2)
        self.assertEqual([(r["payee"], r["merchant"], r["category"])
                          for r in rows], [("Rent", None, None)])
        lenses.month_summary(self.conn, 2026, 2, today=TODAY)
        lenses.year_summary(self.conn, 2026, today=TODAY)



class ADeeplyNestedCellIsOneBadCell(unittest.TestCase):
    def test_a_cell_nested_past_the_walk_reads_as_null(self):
        from oikonome.sync import restore
        deep = cur = []
        for _ in range(3000):
            nxt = []
            cur.append(nxt)
            cur = nxt
        restore.jsonb({"a": deep})          # no RecursionError

    def test_a_deep_cell_through_every_reader_is_null(self):
        """The snapshot readers call the finite pass directly, and the JSON
        reader itself refuses a cell nested past its own limit: both read
        the cell as null."""
        from oikonome.sync import restore
        deep = cur = []
        for _ in range(3000):
            nxt = []
            cur.append(nxt)
            cur = nxt
        self.assertIsNone(restore._finite_only({"x": deep}))
        self.assertIsNone(restore._j("[" * 200000 + "]" * 200000))


if __name__ == "__main__":
    unittest.main()
