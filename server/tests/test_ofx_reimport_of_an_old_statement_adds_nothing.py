"""Re-importing an OFX statement long after its rows were first stored
adds nothing.

A household re-imports an old statement (or a file overlapping rows it
imported months ago) and expects the ledger to stay as it is: a line whose
(account, FITID) already stands on the account is that row, whatever the
dates of the file or the clock, and is updated in place exactly as an
in-window re-import is. The FITID belongs to its account, so the same
FITID on another account is another charge. Amounts and names below are
invented.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome import localtime
from oikonome.sync import ofximport

from .test_ofx_reused_fitid_keeps_every_charge import _ofx_window
from .util import make_db, write_config


JAN = _ofx_window("20260101", "20260131",
                  ("A100", "20260105", "-12.34", "CORNER BAKERY"),
                  ("A101", "20260112", "-56.78", "HARDWARE STORE"),
                  ("A102", "20260120", "-9.10", "BOOK SHOP"))


def _later(days: int):
    """The clock moved forward `days` from the statement's month."""
    real = localtime.now_local

    def fake(cfg):
        return real(cfg).replace(year=2026, month=1, day=31) \
            + dt.timedelta(days=days)
    return mock.patch.object(localtime, "now_local", fake)


class OldStatementReimport(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rows(self, account="chk"):
        return sorted((r["id"], str(r["date"]), float(r["amount"]), r["name"])
                      for r in self.conn.execute(
                          "SELECT id, date, amount, name FROM transactions"
                          " WHERE account_id = %s AND removed = 0",
                          (account,)).fetchall())

    def test_the_same_statement_eight_months_later_adds_nothing(self):
        with _later(0):
            ofximport.import_ofx(self.conn, "chk", JAN)
        before = self._rows()
        self.assertEqual(len(before), 3)
        with _later(240):
            ofximport.import_ofx(self.conn, "chk", JAN)
        self.assertEqual(self._rows(), before)

    def test_a_later_file_overlapping_old_rows_adds_only_the_new_ones(self):
        with _later(0):
            ofximport.import_ofx(self.conn, "chk", JAN)
        before = self._rows()
        wide = _ofx_window("20260101", "20260930",
                           ("A100", "20260105", "-12.34", "CORNER BAKERY"),
                           ("A101", "20260112", "-56.78", "HARDWARE STORE"),
                           ("A102", "20260120", "-9.10", "BOOK SHOP"),
                           ("A900", "20260915", "-3.21", "NEWS STAND"))
        with _later(240):
            ofximport.import_ofx(self.conn, "chk", wide)
        after = self._rows()
        self.assertEqual(len(after), 4)
        self.assertTrue(set(before) <= set(after))

    def test_an_old_line_outside_the_new_files_window_is_its_stored_row(self):
        with _later(0):
            ofximport.import_ofx(self.conn, "chk", JAN)
        before = self._rows()
        # the bank lists a months-old line in a statement declaring only
        # September: the FITID on this account is still that row
        sept = _ofx_window("20260901", "20260930",
                           ("A101", "20260112", "-56.78", "HARDWARE STORE"),
                           ("A900", "20260915", "-3.21", "NEWS STAND"))
        with _later(240):
            ofximport.import_ofx(self.conn, "chk", sept)
        after = self._rows()
        self.assertEqual(len(after), 4)
        self.assertTrue(set(before) <= set(after))

    def test_with_a_history_window_the_old_rows_are_still_recognised(self):
        with _later(0):
            ofximport.import_ofx(self.conn, "chk", JAN)
        before = self._rows()
        self.conn.execute(
            "UPDATE tenant_settings SET config = config || "
            "'{\"history_months\": 3}'::jsonb")
        with _later(240):
            ofximport.import_ofx(self.conn, "chk", JAN)
        self.assertEqual(self._rows(), before)
        # the window is live at that clock: a never-seen line as old is left out
        with _later(240):
            out = ofximport.import_ofx(self.conn, "chk", _ofx_window(
                "20260101", "20260131",
                ("A777", "20260125", "-4.56", "FLOWER STAND")))
        self.assertEqual(out["skipped_older"], 1)
        self.assertEqual(self._rows(), before)

    def test_a_restated_amount_months_later_follows_the_in_window_rule(self):
        restated = _ofx_window(
            "20260101", "20260131",
            ("A100", "20260105", "-12.34", "CORNER BAKERY"),
            ("A101", "20260112", "-57.00", "HARDWARE STORE"),
            ("A102", "20260120", "-9.10", "BOOK SHOP"))
        with _later(0):
            ofximport.import_ofx(self.conn, "chk", JAN)
            ofximport.import_ofx(self.conn, "chk", restated)
        in_window = self._rows()
        self.conn.execute("DELETE FROM transactions")
        with _later(0):
            ofximport.import_ofx(self.conn, "chk", JAN)
        with _later(240):
            ofximport.import_ofx(self.conn, "chk", restated)
        self.assertEqual(self._rows(), in_window)
        # the in-window rule: same day, same name, one candidate either
        # way round, inside the file's window -> updated in place
        self.assertEqual(len(in_window), 3)
        self.assertIn(57.0, [r[2] for r in in_window])

    def test_the_same_fitid_on_two_accounts_is_two_rows(self):
        one = _ofx_window("20260101", "20260131",
                          ("A100", "20260105", "-12.34", "CORNER BAKERY"))
        with _later(0):
            ofximport.import_ofx(self.conn, "chk", one)
        with _later(240):
            ofximport.import_ofx(self.conn, "card", one)
            ofximport.import_ofx(self.conn, "chk", one)
            ofximport.import_ofx(self.conn, "card", one)
        self.assertEqual(len(self._rows("chk")), 1)
        self.assertEqual(len(self._rows("card")), 1)


if __name__ == "__main__":
    unittest.main()
