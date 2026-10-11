"""A statement imported again after its account changed id adds no rows.

A file row's id hashes the account id it was imported under. A reconnect
that adopts restored history moves the account onto the new link's id, and
a comma id is renamed, yet neither rewrites the ids of the rows already on
the account. Re-importing the same (or an overlapping) file must still land
on those rows instead of adding a second copy of every line beside them,
which would double spend, income and net worth for that span.
"""

import unittest

from oikonome.sync import adopt, csvimport

from .util import make_db, write_config

CSV = """Date,Description,Amount
2026-03-10,SAFEWAY STORE,-42.50
2026-03-10,SAFEWAY STORE,-42.50
2026-03-11,EMPLOYER PAYROLL,2000.00
2026-03-12,CARD 4111111111111111 PAYMENT,-15.00
"""
MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}


class ReimportAfterAccountIdChange(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _count(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE id LIKE 'csv:%%'"
            " AND removed = 0").fetchone()["n"]

    def _rename(self, old, new):
        item = self.conn.execute(
            "SELECT item_id FROM accounts WHERE id = %s",
            (old,)).fetchone()["item_id"]
        self.assertTrue(adopt._rewrite_account_id(self.conn, old, new, item))
        # the record adoption keeps beside every rewrite
        self.conn.execute(
            "INSERT INTO account_adoptions (account_id, old_account_id,"
            " item_id) VALUES (%s, %s, %s)", (new, old, item))

    def test_reimport_after_reconnect_adoption_adds_no_rows(self):
        csvimport.import_csv(self.conn, "chk", CSV, MAPPING)
        self.assertEqual(self._count(), 4)
        self._rename("chk", "plaid-new-chk")
        csvimport.import_csv(self.conn, "plaid-new-chk", CSV, MAPPING)
        self.assertEqual(self._count(), 4)

    def test_overlapping_statement_adds_only_its_new_lines(self):
        csvimport.import_csv(self.conn, "chk", CSV, MAPPING)
        self._rename("chk", "chk-renamed")
        more = CSV + ("2026-03-10,SAFEWAY STORE,-42.50\n"
                      "2026-03-13,HARDWARE,-9.99\n")
        csvimport.import_csv(self.conn, "chk-renamed", more, MAPPING)
        # a third identical Safeway line and the new hardware line are new
        self.assertEqual(self._count(), 6)

    def test_reimport_keeps_the_rows_children(self):
        csvimport.import_csv(self.conn, "chk", CSV, MAPPING)
        first = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'csv:%%'").fetchall()}
        self._rename("chk", "plaid-new-chk")
        csvimport.import_csv(self.conn, "plaid-new-chk", CSV, MAPPING)
        after = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'csv:%%'").fetchall()}
        self.assertEqual(first, after)

    def test_an_unrelated_row_that_looks_the_same_is_not_taken(self):
        # a same-content row this importer never hashed under a former id
        # of the account is a different charge, and stays one
        csvimport.import_csv(self.conn, "chk", CSV, MAPPING)
        self._rename("chk", "plaid-new-chk")
        self.conn.execute(
            "UPDATE transactions SET id = 'csv:' || md5(id)"
            " WHERE id LIKE 'csv:%%'")
        csvimport.import_csv(self.conn, "plaid-new-chk", CSV, MAPPING)
        self.assertEqual(self._count(), 8)


class FitidRowsAreNeverMergedByContent(unittest.TestCase):
    """Two charges the bank told apart by FITID stay two rows even when a
    later download carries only the second and both share date, amount
    and name."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def _ofx(fitid):
        from .test_file_imports_keep_every_real_row import _ofx
        return _ofx((fitid, "20260915", "-2.75", "MTA FARE"))

    def test_two_fitid_distinct_fares_stay_two_rows(self):
        from oikonome.sync import ofximport
        ofximport.import_ofx(self.conn, "chk", self._ofx("F1"))
        ofximport.import_ofx(self.conn, "chk", self._ofx("F2"))
        n = self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE id LIKE 'ofx:%%'"
            " AND removed = 0").fetchone()["n"]
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
