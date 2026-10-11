"""A statement PDF takes the household's pairing and identity locks after
its extraction, and before it writes a ledger row.

Reading a statement runs in a sandboxed child that can take most of a
minute. Every edit of a transaction waits on the household's pairing lock
and every rename on its identity lock, and a tenant connection has no lock
timeout, so an import holding both across the extraction makes every note,
category or split edit in the household hang behind each file of a folder
import. The locks belong around the writes, not the read: taken after the
extraction, still before the first row, in the order every other writer
takes them (pairing, then identity). The importer takes them itself, so a
caller that skips its own early lock for a PDF, a test or the no-JS form
door all get the same order.
"""

import threading
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.sync import pdfimport

from .test_pdfimport import CHECKING_TEXT, make_pdf
from .util import make_db, write_config

PAIRING = ("SELECT pg_try_advisory_xact_lock(hashtext(COALESCE("
           "current_setting('app.tenant_id', true), '')), "
           "hashtext('reimbursement-link')) AS ok")


def _identity_sql() -> str:
    from oikonome.engine import merchant_identity
    return ("SELECT pg_try_advisory_xact_lock(%d, hashtext(coalesce("
            "current_setting('app.tenant_id', true), ''))) AS ok"
            % merchant_identity._IDENTITY_LOCK_SPACE)


class StatementImportLocksAfterExtraction(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.other = tenancy.tenant_connect(tid)
        self.addCleanup(self.other.close)

    def _free(self) -> dict:
        """Whether another session of the household could take each lock
        right now (taken and released at once, in a rolled-back
        transaction)."""
        out = {}
        for name, sql in (("pairing", PAIRING), ("identity", _identity_sql())):
            self.other.execute("BEGIN")
            try:
                out[name] = bool(self.other.execute(sql).fetchone()["ok"])
            finally:
                self.other.execute("ROLLBACK")
        return out

    def _import_with_probes(self):
        extracting, release = threading.Event(), threading.Event()
        real_extract = pdfimport.extract_statement
        seen: dict = {}

        def slow_extract(data):
            extracting.set()
            self.assertTrue(release.wait(30), "the test never released")
            return real_extract(data)

        def at_first_write(real):
            def wrapped(*a, **kw):
                seen.setdefault("at_write", self._free())
                return real(*a, **kw)
            return wrapped

        out: dict = {}

        def run():
            try:
                with self.conn.transaction():
                    out["result"] = pdfimport.import_pdf(
                        self.conn, "chk", make_pdf(CHECKING_TEXT))
            except Exception as e:     # surfaced by the assertions below
                out["error"] = e

        with mock.patch.object(pdfimport, "extract_statement", slow_extract), \
                mock.patch.object(pdfimport, "_adopt_earlier_ids",
                                  at_first_write(pdfimport._adopt_earlier_ids)), \
                mock.patch.object(pdfimport, "upsert_transactions",
                                  at_first_write(pdfimport.upsert_transactions)):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(extracting.wait(30), "extraction never started")
            seen["during_extraction"] = self._free()
            release.set()
            worker.join(60)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", out, out.get("error"))
        self.assertGreater(out["result"]["imported"], 0)
        return seen

    def test_an_edit_is_not_blocked_while_the_statement_is_read(self):
        seen = self._import_with_probes()
        self.assertEqual(seen["during_extraction"],
                         {"pairing": True, "identity": True})

    def test_both_locks_are_held_before_the_first_row_is_written(self):
        seen = self._import_with_probes()
        self.assertEqual(seen["at_write"],
                         {"pairing": False, "identity": False})

    def test_pairing_is_taken_before_identity(self):
        from oikonome.engine import merchant_identity
        from oikonome.sync import reanchor
        order: list[str] = []
        real_pair = reanchor.hold_pairing_lock
        real_ident = merchant_identity._lock_identity
        with mock.patch.object(
                reanchor, "hold_pairing_lock",
                lambda c: (order.append("pairing"), real_pair(c))), \
                mock.patch.object(
                    merchant_identity, "_lock_identity",
                    lambda c: (order.append("identity"), real_ident(c))), \
                mock.patch.object(pdfimport, "extract_statement",
                                  lambda d: (order.append("extract"),
                                             (CHECKING_TEXT, None))[1]):
            with self.conn.transaction():
                pdfimport.import_pdf(self.conn, "chk", b"%PDF-")
        self.assertEqual(order[:3], ["extract", "pairing", "identity"])


if __name__ == "__main__":
    unittest.main()
