"""A folder import shows each file's warnings and corrections.

A statement PDF whose lines print no sign, column or running balance has
their direction read from the wording, and the import says so in its
warnings so the person can check those lines; a re-import that reads a
line differently from an earlier import corrects it in place and counts
it. A single upload shows both. A folder of statements is the main way
PDFs arrive, so its job status must carry both per file too, or a line
read the wrong way from a folder import is never flagged.
"""

import unittest
from unittest import mock

from oikonome.db import staging
from oikonome.jobs import bulk_import_job
from oikonome.sync import pdfimport

from .bulk_job_util import run_job, submit
from .test_pdf_reimport_after_column_signs import _statement
from .util import _ensure_db, make_db, write_config


class FolderImportWarningsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _folder(self, files) -> dict:
        token = staging.put(self.conn, "bulk", files)
        sub = submit(self.conn, token, [
            {"index": i, "action": "import", "account_id": "chk",
             "amount_sign": "bank"} for i in range(len(files))])
        return run_job(self.conn, sub["job_id"])

    def test_each_file_carries_its_warnings_and_corrections(self):
        pdf = _statement()
        plain = pdfimport.extract_text(pdf)
        # first read as an importer without column signs did: plain text
        # only, so every line's direction comes from its wording
        with mock.patch.object(pdfimport, "extract_statement",
                               return_value=(plain, None)), \
                mock.patch.object(pdfimport, "_balance_hints",
                                  return_value={}):
            st = self._folder([("june.pdf", pdf)])
        f = st["files"][0]
        self.assertTrue(f.get("ok"), st)
        self.assertTrue(f.get("warnings"), f)
        self.assertTrue(any("wording" in w for w in f["warnings"]), f)
        self.assertEqual(f.get("corrected"), 0, f)

        # the same statement again, now read by its columns: the line the
        # wording signed wrongly is corrected in place, and counted
        st = self._folder([("june.pdf", pdf)])
        f = st["files"][0]
        self.assertTrue(f.get("ok"), st)
        self.assertEqual(f.get("corrected"), 1, f)
        self.assertEqual(f.get("warnings"), [], f)
        # builds that still read the run door's per-file results see the
        # same two fields
        row = bulk_import_job.compat_results(st)[0]
        self.assertEqual((row.get("corrected"), row.get("warnings")),
                         (1, []))

    def test_a_file_with_nothing_to_flag_says_so_plainly(self):
        csv = (b"Date,Description,Amount\n"
               b"2021-05-01,BOOK SHOP,-20.00\n"
               b"2021-05-02,PAYCHECK,1500.00\n")
        f = self._folder([("may.csv", csv)])["files"][0]
        self.assertTrue(f.get("ok"), f)
        self.assertEqual((f.get("warnings"), f.get("corrected")), ([], 0))


if __name__ == "__main__":
    unittest.main()
