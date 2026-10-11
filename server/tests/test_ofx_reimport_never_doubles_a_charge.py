"""An OFX line the account already holds is never inserted a second time.

The cross-source dedup skips the importer's own rows, so the FITID id
scheme alone decides whether a line is new. Three ways it must still land
on the stored row: a bank that numbers FITIDs per download moves a charge
to another FITID in an overlapping download; a bank restates a FITID's
posting date or amount; the account changed id (reconnect adoption or
comma rename) since the file was first imported. Each would otherwise add
a twin and double that span's spend. Two charges the bank keeps apart by
stable FITIDs must still stay two rows.
"""

import unittest

from oikonome.sync import adopt, ofximport

from .test_file_imports_keep_every_real_row import _ofx
from .util import make_db, write_config


class OfxReimport(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rows(self):
        return sorted((str(r["date"]), float(r["amount"]), r["name"])
                      for r in self.conn.execute(
                          "SELECT date, amount, name FROM transactions"
                          " WHERE id LIKE 'ofx:%%' AND removed = 0"
                          ).fetchall())

    def test_per_download_numbering_never_doubles_the_overlap(self):
        jan = _ofx(("1", "20260101", "-1200.00", "RENT"),
                   ("2", "20260115", "-60.00", "POWER"))
        feb = _ofx(("1", "20260115", "-60.00", "POWER"),
                   ("2", "20260203", "-35.17", "GROCERY"))
        ofximport.import_ofx(self.conn, "chk", jan)
        ofximport.import_ofx(self.conn, "chk", feb)
        self.assertEqual(sorted(n for _, _, n in self._rows()),
                         ["GROCERY", "POWER", "RENT"])
        ofximport.import_ofx(self.conn, "chk", feb)
        ofximport.import_ofx(self.conn, "chk", jan)
        self.assertEqual(len(self._rows()), 3)

    def test_a_new_row_shifting_the_fitid_sequence_never_doubles(self):
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("051", "20260105", "-20.00", "AMAZON MKTPL"),
            ("052", "20260105", "-35.00", "AMAZON MKTPL")))
        shifted = _ofx(("051", "20260105", "-50.00", "AMAZON MKTPL"),
                       ("052", "20260105", "-20.00", "AMAZON MKTPL"),
                       ("053", "20260105", "-35.00", "AMAZON MKTPL"))
        ofximport.import_ofx(self.conn, "chk", shifted)
        self.assertEqual(sorted(a for _, a, _ in self._rows()),
                         [20.0, 35.0, 50.0])
        ofximport.import_ofx(self.conn, "chk", shifted)
        self.assertEqual(sum(a for _, a, _ in self._rows()), 105.0)

    def test_a_restated_posting_date_updates_the_row(self):
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("7", "20260110", "-25.00", "HARDWARE")))
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("7", "20260111", "-25.00", "HARDWARE")))
        self.assertEqual(self._rows(), [("2026-01-11", 25.0, "HARDWARE")])

    def test_a_restated_amount_updates_the_row(self):
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("8", "20260110", "-40.00", "DINER")))
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("8", "20260110", "-48.00", "DINER")))
        self.assertEqual(self._rows(), [("2026-01-10", 48.0, "DINER")])

    def test_reimport_after_the_account_changed_id_adds_no_rows(self):
        data = _ofx(("1", "20260101", "-1200.00", "RENT"),
                    ("2", "20260115", "-60.00", "POWER"),
                    ("3", "20260115", "-60.00", "POWER"))
        ofximport.import_ofx(self.conn, "chk", data)
        item = self.conn.execute(
            "SELECT item_id FROM accounts WHERE id = 'chk'"
        ).fetchone()["item_id"]
        self.assertTrue(adopt._rewrite_account_id(
            self.conn, "chk", "plaid-new-chk", item))
        ofximport.import_ofx(self.conn, "plaid-new-chk", data)
        self.assertEqual(len(self._rows()), 3)

    def test_a_new_fitid_after_a_rename_is_still_a_new_charge(self):
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("F1", "20260915", "-2.75", "MTA FARE")))
        item = self.conn.execute(
            "SELECT item_id FROM accounts WHERE id = 'chk'"
        ).fetchone()["item_id"]
        adopt._rewrite_account_id(self.conn, "chk", "plaid-new-chk", item)
        ofximport.import_ofx(self.conn, "plaid-new-chk", _ofx(
            ("F2", "20260915", "-2.75", "MTA FARE")))
        self.assertEqual(len(self._rows()), 2)

    def test_identical_charges_under_stable_fitids_stay_apart(self):
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("F1", "20260915", "-2.75", "MTA FARE")))
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("F2", "20260915", "-2.75", "MTA FARE"),
            ("F2", "20260915", "-2.75", "MTA FARE")))
        self.assertEqual(len(self._rows()), 3)


if __name__ == "__main__":
    unittest.main()
