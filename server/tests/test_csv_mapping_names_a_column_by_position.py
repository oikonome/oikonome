"""A mapped CSV column is named by its position, so a header that repeats
a name (two Description or two Amount columns, common in bank exports)
still lets either column be chosen — and the import reads exactly the
column the mapping card previewed. By name, the dict reader keeps only
the LAST such column, while the preview showed the first."""

import unittest

from oikonome.db import staging
from oikonome.sync import csvimport
from oikonome.web.pages import run_mapped_import

from .util import make_db, write_config

DUP = ("Date,Description,Description,Amount\n"
       "2026-07-01,SAFEWAY 123,Groceries memo,-42.50\n"
       "2026-07-02,PAYROLL ACME,Salary memo,2000.00\n")


class PositionalColumnTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _names(self):
        return sorted(r["name"] for r in self.conn.execute(
            "SELECT name FROM transactions WHERE account_id='chk'"))

    def test_the_first_of_two_same_named_columns_can_be_imported(self):
        csvimport.import_csv(self.conn, "chk", DUP,
                             {"date": "#0", "name": "#1", "amount": "#3"})
        self.assertEqual(self._names(), ["PAYROLL ACME", "SAFEWAY 123"])

    def test_the_second_is_its_own_choice(self):
        csvimport.import_csv(self.conn, "chk", DUP,
                             {"date": "#0", "name": "#2", "amount": "#3"})
        self.assertEqual(self._names(), ["Groceries memo", "Salary memo"])

    def test_a_header_name_still_maps_by_name(self):
        csvimport.import_csv(self.conn, "chk", DUP,
                             {"date": "Date", "name": "Description",
                              "amount": "Amount"})
        self.assertEqual(len(self._names()), 2)

    def test_the_mapped_door_accepts_positions(self):
        token = staging.put(self.conn, "csv", [("dup.csv", DUP.encode())],
                            {"account_id": "chk", "amount_sign": "bank"})
        out = run_mapped_import(self.conn, token,
                                {"date_col": "#0", "name_col": "#1",
                                 "amount_col": "#3"})
        self.assertEqual(out.get("imported"), 2, out)
        self.assertEqual(self._names(), ["PAYROLL ACME", "SAFEWAY 123"])


if __name__ == "__main__":
    unittest.main()
