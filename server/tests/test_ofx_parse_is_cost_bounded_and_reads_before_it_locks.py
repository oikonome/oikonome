"""An OFX/QFX parse can neither be made slow by its own markup, nor be
monopolised by one household, nor stall the household's edits while it
runs.

* ofxtools reads CDATA with a greedy, line-bound regex, so each CDATA
  opener costs the rest of its line: a line packed with openers costs the
  square of its length. Such a file is refused from its bytes, before any
  parse slot or child is involved; one well-formed section per line still
  imports.
* The process has two parse slots; a household may hold one of them, and
  its second file is refused at once instead of taking the other.
* The parse (slot wait plus child, up to a minute each) runs before the
  household's pairing and identity locks are taken, on the single upload
  and the folder import alike, so edits and renames are not held behind
  it — and both locks are held by the time rows are written.
"""

import threading
import time
import unittest
from unittest import mock

from ofxtools.Parser import OFXTree

from oikonome.db import staging
from oikonome.sync import ofximport

from .bulk_job_util import SignedInClient, submit, tenant_of
from .test_a_pdf_import_reads_before_it_takes_the_household_locks import \
    _locks_free
from .test_ofximport import OFX
from .util import _ensure_db, make_db, write_config


def _never(*_a, **_k):
    raise AssertionError("the file reached the parser")


class CdataIsBoundedBeforeTheParse(unittest.TestCase):
    def _refused(self, data: bytes):
        with mock.patch.object(OFXTree, "parse", _never), \
                mock.patch.object(ofximport.subprocess, "Popen", _never), \
                self.assertRaises(ValueError) as caught:
            ofximport.parse_ofx(data, tenant="t-cdata")
        self.assertIn("CDATA", str(caught.exception))

    def test_a_line_packed_with_unterminated_openers_is_refused_at_once(self):
        bomb = OFX.replace(b"<NAME>SAFEWAY STORE",
                           b"<NAME>" + b"<A><![CDATA[x" * 25_000)
        started = time.monotonic()
        self._refused(bomb)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_closed_sections_sharing_a_line_are_refused(self):
        self._refused(OFX.replace(
            b"<NAME>SAFEWAY STORE",
            b"<NAME>" + b"<A><![CDATA[x]]></A>" * 25_000))

    def test_a_section_closed_only_on_a_later_line_is_refused(self):
        self._refused(OFX.replace(b"<NAME>SAFEWAY STORE",
                                  b"<NAME><![CDATA[SAFEWAY\nSTORE]]>"))

    def test_one_closed_section_per_line_still_imports(self):
        data = OFX.replace(b"<NAME>SAFEWAY STORE",
                           b"<NAME><![CDATA[SAFEWAY STORE]]>")
        rows = ofximport.parse_ofx(data, tenant="t-cdata-ok")
        self.assertEqual(rows[0]["name"], "SAFEWAY STORE")
        self.assertEqual(len(rows), 3)


class OneSlotPerHousehold(unittest.TestCase):
    def test_a_household_cannot_hold_a_second_slot(self):
        inside, release = threading.Event(), threading.Event()

        class _Gated:
            def __init__(self, *_a, **_k):
                inside.set()

            def wait(self, timeout=None):
                release.wait(30)
                return 1

        out: dict = {}

        def first():
            try:
                ofximport.parse_ofx(OFX, tenant="t-one")
            except ValueError as e:
                out["first"] = str(e)

        with mock.patch.object(ofximport.subprocess, "Popen", _Gated), \
                self.assertLogs(ofximport.log, "WARNING"):
            worker = threading.Thread(target=first)
            worker.start()
            try:
                self.assertTrue(inside.wait(30))
                started = time.monotonic()
                with self.assertRaises(ValueError) as caught:
                    ofximport.parse_ofx(OFX, tenant="t-one")
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertIn("this household", str(caught.exception))
                # the slot it did not get is still free for another one
                self.assertTrue(ofximport._slots.acquire(blocking=False))
                ofximport._slots.release()
            finally:
                release.set()
                worker.join(30)
        self.assertFalse(worker.is_alive())
        # the first file's slot and claim are both given back
        self.assertEqual(ofximport._slot_holders, set())
        self.assertEqual(len(ofximport.parse_ofx(OFX, tenant="t-one")), 3)


class OfxReadsBeforeItLocks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _watch(self, tid: str, run_import):
        reading, release = threading.Event(), threading.Event()
        at_write: dict = {}
        real_parse = ofximport.parse_ofx
        real_upsert = ofximport.upsert_transactions

        def held_parse(data, tenant=None):
            reading.set()
            release.wait(30)
            return real_parse(data, tenant=tenant)

        def upsert(*a, **kw):
            at_write.setdefault("locks", _locks_free(tid))
            return real_upsert(*a, **kw)

        out: dict = {}
        with mock.patch.object(ofximport, "parse_ofx",
                               side_effect=held_parse), \
                mock.patch.object(ofximport, "upsert_transactions",
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
        self.assertEqual(during, {"pairing": True, "identity": True},
                         "a lock was held while the OFX file was parsed")
        self.assertEqual(at_write.get("locks"),
                         {"pairing": False, "identity": False},
                         "the import wrote rows without both locks")
        return out.get("result")

    def test_a_single_ofx_upload_parses_before_it_locks(self):
        hh = SignedInClient("ofxlk")

        def upload():
            r = hh.c.post("/api/import",
                          data={"account_id": "chk", "amount_sign": "bank"},
                          files={"file": ("july.qfx", OFX,
                                          "application/octet-stream")})
            self.assertEqual(r.status_code, 200, r.text)
            return r.json()["result"] or {}

        result = self._watch(hh.tid, upload)
        self.assertFalse(result.get("error"), result)
        self.assertEqual(result.get("imported"), 3, result)

    def test_a_folder_import_parses_each_ofx_before_it_locks(self):
        from oikonome.jobs import bulk_import_job
        conn = make_db()
        try:
            write_config(conn)
            tid = tenant_of(conn)
            token = staging.put(conn, "bulk", [("july.ofx", OFX)])
            job_id = submit(conn, token, [
                {"index": 0, "action": "import", "account_id": "chk",
                 "amount_sign": "bank"}])["job_id"]
            self._watch(tid, lambda: bulk_import_job.run_job(tid, job_id))
            st = bulk_import_job.status(conn, job_id)
            self.assertEqual(st["state"], "done", st)
            self.assertTrue(st["files"][0].get("ok"), st)
            self.assertEqual(st["files"][0].get("imported"), 3, st)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
