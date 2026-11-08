"""A restore's summary counts the rows it wrote, and names every row it
did not write under the reason.

The restore tally holds, beside the rows written per table, one counter
per reason a row was left out (orphaned, malformed, out of range, ...) and
one per repair made to a written row. Summed together into "rows
restored", an archive with one orphaned note and one malformed charge
reports two rows more than landed, and a repaired charge is counted twice.
"""

import unittest

from oikonome.sync import export, restore
from oikonome.web.pages import restore_result

from .test_export_is_one_snapshot import _with_rows
from .util import make_db


class RestoreSummaryTests(unittest.TestCase):

    def test_refusals_and_repairs_are_not_rows_restored(self):
        r = restore_result({"transactions": 3, "accounts": 1,
                            "already_present": 4, "orphaned": 1,
                            "malformed": 2, "out_of_range": 1,
                            "over_cap": 1, "merged": 1, "rebuilt": 1,
                            "amount_repaired": 1})
        self.assertEqual(r["rows"], 4)
        self.assertEqual(r["imported"], 3)
        for said in ("3 transactions", "1 accounts", "4 already present",
                     "1 orphaned", "2 malformed", "1 out of range",
                     "1 over cap", "1 merged", "1 rebuilt"):
            self.assertIn(said, r["source"])
        # a repair is part of its table's count, not a row of its own
        self.assertNotIn("amount_repaired", r["source"])

    def test_restore_with_an_orphan_and_a_malformed_row_reports_written(self):
        src = make_db()
        try:
            src.execute(
                "INSERT INTO transactions (id, account_id, date, amount, "
                "name, pending, removed) VALUES ('kept-1', 'chk', "
                "'2026-06-01', 4.0, 'HARBOR LIGHTS CAFE', 0, 0)")
            data = export.build_zip(src)
        finally:
            src.close()
        data = _with_rows(data, {
            "transaction_notes.csv": [
                {"txn_id": "no-such-txn", "note": "stray"}],
            "transactions.csv": [
                {"id": "no-date-1", "account_id": "chk", "date": "",
                 "amount": "2.0", "name": "UNDATED"}],
        })
        dest = make_db()
        try:
            counts, out = restore.restore_outcomes(dest, data)
            self.assertEqual(counts.get("orphaned"), 1)
            self.assertEqual(counts.get("malformed"), 1)
            written = sum(out.written.values())
            r = restore_result(counts)
            self.assertEqual(r["rows"], written,
                             "rows restored is what the restore wrote")
            self.assertIn("1 orphaned", r["source"])
            self.assertIn("1 malformed", r["source"])
        finally:
            dest.close()


if __name__ == "__main__":
    unittest.main()
