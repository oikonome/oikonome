"""An export restored onto another instance must bring free text back as it
was written.

The export puts an apostrophe in front of any text cell a spreadsheet would
read as a formula (one starting with = + - @ or a tab). The same archive is
the restore input, so the restore has to take that apostrophe off again —
on every column, not only the payee descriptors — or a note, a receipt line
or a memo that began with a minus sign comes back with a quote in front of
it after every round trip."""
import io
import os
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import restore

from .export_ticket import export_get
from .util import _ensure_db, make_db, seed_accounts, write_config

NOTE = "-$12 of this came back from a housemate"
LINE = "-INSTANT SAVINGS"
PLAIN = "plain words"


class RestoreRemovesTheExportFormulaQuoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"fq-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        cls.rid = str(uuid.uuid4())
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, pending, removed)
                   VALUES ('fq-1','chk','2026-07-10',80.0,'EXAMPLE STORE',0,0)""")
            conn.execute(
                "INSERT INTO transaction_notes (txn_id, note) VALUES "
                "('fq-1', %s)", (NOTE,))
            conn.execute(
                """INSERT INTO receipts (id, txn_id, image, mime, kind, status)
                   VALUES (%s,'fq-1',%s,'image/jpeg','receipt','parsed')""",
                (cls.rid, b"\xff\xd8\xff\xe0fake"))
            conn.execute(
                """INSERT INTO receipt_items (receipt_id, line, description,
                       qty, amount, tag)
                   VALUES (%s, 1, %s, 1, -5.0, ''), (%s, 2, %s, 1, 85.0, '')""",
                (cls.rid, LINE, cls.rid, PLAIN))
        finally:
            conn.close()

    def test_free_text_round_trips_without_the_quote(self):
        r = export_get(self.client, "/export")
        self.assertEqual(r.status_code, 200)
        # the export itself still guards the cell for a spreadsheet
        z = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertIn("'" + NOTE, z.read("transaction_notes.csv").decode())
        dest = make_db()
        try:
            restore.restore_zip(dest, r.content)
            note = dest.execute(
                "SELECT note FROM transaction_notes WHERE txn_id='fq-1'"
            ).fetchone()["note"]
            self.assertEqual(note, NOTE)
            items = [x["description"] for x in dest.execute(
                "SELECT description FROM receipt_items ORDER BY line")]
            self.assertEqual(items, [LINE, PLAIN])
        finally:
            dest.close()


if __name__ == "__main__":
    unittest.main()
