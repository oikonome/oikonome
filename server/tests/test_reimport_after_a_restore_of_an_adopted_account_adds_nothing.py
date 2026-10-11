"""A statement re-imported after export and restore of an adopted account
adds no rows.

A file row's id hashes the account id it was imported under, and a
reconnect that adopts the account onto a new id keeps the rows' ids. A
later re-import finds those rows only through the record of which id the
account stood under before. If the export or restore dropped that record,
the restored household's next import of an overlapping statement would
add every pre-feed line a second time. The restored record must not turn
the destination's one-shot reconnect guard back on either.
"""

import unittest

from oikonome.db import tenancy
from oikonome.sync import adopt, csvimport, export, restore

from .util import make_db, write_config

CSV = """Date,Description,Amount
2026-03-10,SAFEWAY STORE,-42.50
2026-03-10,SAFEWAY STORE,-42.50
2026-03-11,EMPLOYER PAYROLL,2000.00
"""
MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}


class RestoreKeepsAccountLineage(unittest.TestCase):

    def setUp(self):
        a = make_db()
        write_config(a)
        csvimport.import_csv(a, "chk", CSV, MAPPING)
        item = a.execute("SELECT item_id FROM accounts WHERE id = 'chk'"
                         ).fetchone()["item_id"]
        self.assertTrue(adopt._rewrite_account_id(a, "chk",
                                                  "plaid-new-chk", item))
        a.execute("INSERT INTO account_adoptions (account_id,"
                  " old_account_id, item_id, cutoff_date)"
                  " VALUES ('plaid-new-chk', 'chk', %s, '2026-04-01')",
                  (item,))
        self.data = export.build_zip(a)
        a.close()
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, "restore-lineage")
        finally:
            admin.close()
        self.b = tenancy.tenant_connect(tid)
        self.addCleanup(self.b.close)
        restore.restore_zip(self.b, self.data)

    def _count(self):
        return self.b.execute(
            "SELECT count(*) AS n FROM transactions WHERE id LIKE 'csv:%%'"
            " AND removed = 0").fetchone()["n"]

    def test_reimport_on_the_restored_account_adds_no_rows(self):
        self.assertEqual(self._count(), 3)
        csvimport.import_csv(self.b, "plaid-new-chk", CSV, MAPPING)
        self.assertEqual(self._count(), 3)

    def test_the_restored_lineage_never_arms_the_reconnect_guard(self):
        self.assertIsNone(adopt.pending(self.b, "plaid-new-chk"))
        row = self.b.execute(
            "SELECT old_account_id, cutoff_date, reconciled_at"
            " FROM account_adoptions").fetchone()
        self.assertEqual(row["old_account_id"], "chk")
        self.assertIsNone(row["cutoff_date"])
        self.assertIsNotNone(row["reconciled_at"])

    def test_a_second_restore_adds_no_second_lineage_row(self):
        restore.restore_zip(self.b, self.data)
        n = self.b.execute("SELECT count(*) AS n FROM account_adoptions"
                           ).fetchone()["n"]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
