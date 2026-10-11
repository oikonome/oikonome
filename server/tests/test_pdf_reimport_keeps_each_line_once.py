"""Re-importing a statement lands every line on the row an earlier import
of it made, with the sign that import had good evidence for.

- A same-day in/out pair of one amount, imported when both were signed
  by keywords (occurrences 1 and 2 of one row shape), must come back as
  one deposit and one withdrawal — never two withdrawals, never a stale
  twin beside a new row.
- A page whose column hints the extraction's TIME budget denied has
  unknown signs, not keyword signs: a re-import on a loaded host must not
  rewrite a sign an idle import read from the columns.
"""

import unittest
from unittest import mock

from oikonome.sync import pdf_sandbox, pdfimport

from .test_pdfimport import HEADER, PERIOD, make_layout_pdf
from .util import make_db, write_config

NO_BALANCE_HEADER = (740, [(36, "Date"), (100, "Description"),
                           (380, "Withdrawals"), (460, "Deposits")])


def _pair(deposit_first: bool, balance: bool):
    dep = [(36, "06/03"), (100, "ONLINE TRANSFER SAVINGS"), (460, "20.00")]
    wd = [(36, "06/03"), (100, "ONLINE TRANSFER SAVINGS"), (380, "20.00")]
    if balance:
        dep.append((540, "1,020.00" if deposit_first else "1,000.00"))
        wd.append((540, "1,000.00" if deposit_first else "980.00"))
    first, second = (dep, wd) if deposit_first else (wd, dep)
    return make_layout_pdf([[PERIOD, HEADER if balance else NO_BALANCE_HEADER,
                             (720, first), (708, second)]])


class _Db(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _amounts(self):
        return sorted(float(r["amount"]) for r in self.conn.execute(
            "SELECT amount FROM transactions WHERE account_id='chk'"))


class InOutPairTests(_Db):

    def test_a_pair_imported_before_column_signs_comes_back_one_each(self):
        for deposit_first in (True, False):
            for balance in (False, True):
                with self.subTest(deposit_first=deposit_first,
                                  balance=balance):
                    self.conn.execute("DELETE FROM transactions")
                    pdf = _pair(deposit_first, balance)
                    plain = pdfimport.extract_text(pdf)
                    with mock.patch.object(pdfimport, "extract_statement",
                                           return_value=(plain, None)):
                        pdfimport.import_pdf(self.conn, "chk", pdf)
                    self.assertEqual(self._amounts(), [20.0, 20.0])
                    pdfimport.import_pdf(self.conn, "chk", pdf)
                    self.assertEqual(self._amounts(), [-20.0, 20.0])
                    pdfimport.import_pdf(self.conn, "chk", pdf)
                    self.assertEqual(self._amounts(), [-20.0, 20.0])

    def test_a_pair_first_imported_with_column_signs_is_stable(self):
        pdf = _pair(True, False)
        pdfimport.import_pdf(self.conn, "chk", pdf)
        pdfimport.import_pdf(self.conn, "chk", pdf)
        self.assertEqual(self._amounts(), [-20.0, 20.0])


class TimeCutHintsTests(_Db):

    def _statement(self):
        return make_layout_pdf([
            [PERIOD, HEADER, (720, [(36, "06/03"), (100, "CHECK 1024"),
                                    (380, "500.00"), (540, "1,500.00")])],
            [HEADER, (720, [(36, "06/20"), (100, "RMD DISTRIBUTION"),
                            (460, "900.00"), (540, "2,400.00")])],
        ])

    def test_a_loaded_host_keeps_the_sign_an_idle_one_stored(self):
        pdf = self._statement()
        with mock.patch.object(pdf_sandbox, "IN_PROCESS", True):
            pdfimport.import_pdf(self.conn, "chk", pdf)
        stored = self._amounts()
        self.assertEqual(stored, [-900.0, 500.0])
        with mock.patch.object(pdf_sandbox, "IN_PROCESS", True), \
                mock.patch.object(pdf_sandbox, "LAYOUT_SECONDS", 0.0):
            pdfimport.import_pdf(self.conn, "chk", pdf)
        self.assertEqual(self._amounts(), stored)


if __name__ == "__main__":
    unittest.main()
