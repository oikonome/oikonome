"""A statement PDF is read before the household's ledger locks are taken.

A file import holds the household's reimbursement-pairing lock (exclusive)
and its merchant-identity lock from before its first ledger row to its
commit, so that it and a person's edit never take the two in opposite
orders. Every edit door waits for the pairing lock, and category writes
and renames wait for the identity lock; tenant connections carry no lock
timeout. Reading a PDF runs a sandboxed extraction that can take the
better part of a minute. Taken before that read, the locks would stall
every note, category and split edit in the household for the whole of it,
once per statement in a folder. So both doors read first and lock after:
while the extraction runs another connection can take both locks, and by
the time the first row is written the import holds both.
"""

import threading
import unittest
from unittest import mock

from oikonome.db import staging, tenancy
from oikonome.engine import merchant_identity
from oikonome.sync import pdfimport

from .bulk_job_util import SignedInClient, submit, tenant_of
from .test_pdf_reimport_after_column_signs import _statement
from .util import _ensure_db, make_db, write_config

_PAIRING_FREE = (
    "SELECT pg_try_advisory_xact_lock_shared(hashtext(COALESCE("
    "current_setting('app.tenant_id', true), '')), "
    "hashtext('reimbursement-link')) AS free")
_IDENTITY_FREE = (
    "SELECT pg_try_advisory_xact_lock(%s, hashtext("
    "coalesce(current_setting('app.tenant_id', true), ''))) AS free")


def _locks_free(tid: str) -> dict:
    """Whether another connection of the household could take each lock
    right now. Autocommit: each probe's lock ends with its statement."""
    conn = tenancy.tenant_connect(tid)
    try:
        return {
            "pairing": conn.execute(_PAIRING_FREE).fetchone()["free"],
            "identity": conn.execute(
                _IDENTITY_FREE,
                (merchant_identity._IDENTITY_LOCK_SPACE,)).fetchone()["free"],
        }
    finally:
        conn.close()


class PdfImportReadsBeforeItLocksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _watch(self, tid: str, run_import) -> tuple[dict, dict, object]:
        """Run `run_import` on a thread with the extraction held open:
        the locks as another connection finds them while the PDF is read,
        and again at the import's first ledger write."""
        reading, release = threading.Event(), threading.Event()
        at_write: dict = {}
        real_extract = pdfimport.extract_statement
        real_adopt = pdfimport._adopt_earlier_ids
        real_upsert = pdfimport.upsert_transactions

        def held_extract(data):
            reading.set()
            release.wait(30)
            return real_extract(data)

        def adopt(*a, **kw):
            at_write.setdefault("locks", _locks_free(tid))
            return real_adopt(*a, **kw)

        def upsert(*a, **kw):
            at_write.setdefault("locks", _locks_free(tid))
            return real_upsert(*a, **kw)

        out: dict = {}
        with mock.patch.object(pdfimport, "extract_statement",
                               side_effect=held_extract), \
                mock.patch.object(pdfimport, "_adopt_earlier_ids",
                                  side_effect=adopt), \
                mock.patch.object(pdfimport, "upsert_transactions",
                                  side_effect=upsert):
            worker = threading.Thread(
                target=lambda: out.update(result=run_import()))
            worker.start()
            try:
                self.assertTrue(reading.wait(30),
                                "the import never started reading")
                during = _locks_free(tid)
            finally:
                release.set()
                worker.join(timeout=90)
        self.assertFalse(worker.is_alive())
        return during, at_write.get("locks"), out.get("result")

    def _assert_read_unlocked_then_written_locked(self, during, at_write):
        self.assertEqual(during, {"pairing": True, "identity": True},
                         "a lock was held while the PDF was being read")
        self.assertEqual(at_write, {"pairing": False, "identity": False},
                         "the import wrote rows without both locks")

    def test_a_single_pdf_upload_reads_before_it_locks(self):
        hh = SignedInClient("pdflk")

        def upload():
            r = hh.c.post("/api/import",
                          data={"account_id": "chk", "amount_sign": "bank"},
                          files={"file": ("june.pdf", _statement(),
                                          "application/pdf")})
            self.assertEqual(r.status_code, 200, r.text)
            return r.json()["result"] or {}

        during, at_write, result = self._watch(hh.tid, upload)
        self.assertFalse(result.get("error"), result)
        self.assertEqual(result.get("imported"), 3, result)
        self._assert_read_unlocked_then_written_locked(during, at_write)

    def test_a_folder_import_reads_each_pdf_before_it_locks(self):
        from oikonome.jobs import bulk_import_job
        conn = make_db()
        try:
            write_config(conn)
            tid = tenant_of(conn)
            token = staging.put(conn, "bulk", [("june.pdf", _statement())])
            job_id = submit(conn, token, [
                {"index": 0, "action": "import", "account_id": "chk",
                 "amount_sign": "bank"}])["job_id"]
            during, at_write, _ = self._watch(
                tid, lambda: bulk_import_job.run_job(tid, job_id))
            st = bulk_import_job.status(conn, job_id)
            self.assertEqual(st["state"], "done", st)
            self.assertTrue(st["files"][0].get("ok"), st)
            self.assertEqual(st["files"][0].get("imported"), 3, st)
            self._assert_read_unlocked_then_written_locked(during, at_write)
        finally:
            conn.close()

    def test_a_csv_still_locks_before_its_first_row(self):
        """The early lock stays for every other format: their parse is
        cheap, and a lock taken before the account is created keeps the
        one order the lock helper documents."""
        hh = SignedInClient("csvlk")
        csv = (b"Date,Description,Amount\n"
               b"2026-03-02,CORNER BAKERY,-12.00\n")
        at_write: dict = {}
        from oikonome.sync import csvimport
        real = csvimport.upsert_transactions

        def upsert(*a, **kw):
            at_write.setdefault("locks", _locks_free(hh.tid))
            return real(*a, **kw)

        with mock.patch.object(csvimport, "upsert_transactions",
                               side_effect=upsert):
            r = hh.c.post("/api/import",
                          data={"account_id": "chk", "amount_sign": "bank"},
                          files={"file": ("chk.csv", csv, "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse((r.json()["result"] or {}).get("error"), r.text)
        self.assertEqual(at_write.get("locks"),
                         {"pairing": False, "identity": False})


if __name__ == "__main__":
    unittest.main()
