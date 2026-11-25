"""A stored tax document's file name keeps only the last four of an SSN or
EIN it spells.

Payroll and tax software name downloads after the person ("W2_<SSN>.pdf")
or the employer ("W-2 EIN <EIN>.pdf"), and the stored-documents list shows
the name to every role in the household; the portability archive carries
it. The card-number cut leaves those shapes alone (it cuts runs of eight or
more and card layouts, and is shared with ledger names on purpose), so the
tax-document path masks them itself, where the name is first kept — and a
name an earlier build stored whole is masked wherever it is read back.
Every number here is invented and could not be issued (area 000)."""

import io
import json
import unittest
import zipfile

from oikonome import tenant_export
from oikonome.engine import taxdocs

from .test_taxdocs import SSA_XML
from .util import make_db, write_config

NAMES = {
    "W2_2025_000-00-1234.pdf": "W2_2025_…1234.pdf",
    "1040 Doe 000 00 1234.pdf": "1040 Doe …1234.pdf",
    "w2.000.00.1234.png": "w2.…1234.png",
    "W2_000001234.pdf": "W2_…1234.pdf",
    "W-2 EIN 12-3456789.pdf": "W-2 EIN …6789.pdf",
    "W-2 EIN 12 3456789.pdf": "W-2 EIN …6789.pdf",
    "c:\\Users\\a\\W2 000-00-1234.pdf": "W2 …1234.pdf",
    # spaced and typographic separators, and digits typed fullwidth
    "W2 000 - 00 - 1234.pdf": "W2 …1234.pdf",
    "W2 000\u201300\u20131234.pdf": "W2 …1234.pdf",
    "W2 000\u201400\u20141234.pdf": "W2 …1234.pdf",
    "W2 000\u221200\u22121234.pdf": "W2 …1234.pdf",
    "W2 000\u00a000\u00a01234.pdf": "W2 …1234.pdf",
    "W2 000\u2009\u2013 00 .1234.pdf": "W2 …1234.pdf",
    "W2 \uff10\uff10\uff10-\uff10\uff10-\uff11\uff12\uff13\uff14.pdf":
        "W2 …1234.pdf",
    "W-2 EIN 12 \u2013 3456789.pdf": "W-2 EIN …6789.pdf",
    # not identifiers: dates, years and form numbers stay as written
    "2025-01-15 statement.pdf": "2025-01-15 statement.pdf",
    "Form 1099 2025.pdf": "Form 1099 2025.pdf",
    "1040 - 2025.pdf": "1040 - 2025.pdf",
    "call 617 555 0142.pdf": "call 617 555 0142.pdf",
    "W2 2025-01-15.pdf": "W2 2025-01-15.pdf",
    "1040-2024.pdf": "1040-2024.pdf",
    "SSA earnings 2000-2024.xml": "SSA earnings 2000-2024.xml",
    "w2 1099 2025.pdf": "w2 1099 2025.pdf",
}


class TheNameIsMaskedWhereItIsKept(unittest.TestCase):
    def test_the_cleaned_name_keeps_only_the_last_four(self):
        for name, want in NAMES.items():
            with self.subTest(name):
                self.assertEqual(taxdocs._clean_filename(name), want)
                # a fixed point: masking the masked name changes nothing
                self.assertEqual(taxdocs._clean_filename(want), want)


class EveryReaderShowsTheMaskedName(unittest.TestCase):
    RAW = "earnings 000-00-1234.xml"
    SHOWN = "earnings …1234.xml"

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _store(self) -> int:
        out = taxdocs.analyze(self.conn, self.RAW, SSA_XML, "text/xml")
        taxdocs.commit(self.conn, out["token"], out["rows"])
        return self.conn.execute(
            "SELECT id FROM income_uploads ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]

    def _listed(self):
        return [u["filename"] for u in taxdocs.list_stored(self.conn)]

    def test_an_upload_is_stored_and_listed_masked(self):
        uid = self._store()
        self.assertEqual(self.conn.execute(
            "SELECT filename FROM income_uploads WHERE id = %s",
            (uid,)).fetchone()["filename"], self.SHOWN)
        self.assertEqual(self._listed(), [self.SHOWN])

    def test_a_name_an_earlier_build_stored_whole_is_masked_on_read(self):
        uid = self._store()
        self.conn.execute("UPDATE income_uploads SET filename = %s "
                          "WHERE id = %s", (self.RAW, uid))
        self.assertEqual(self._listed(), [self.SHOWN])
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        z = zipfile.ZipFile(io.BytesIO(tenant_export.build_archive(
            tid, operator="op", consented=True)))
        rows = json.loads(z.read("data/income_uploads.json"))
        self.assertEqual([r["filename"] for r in rows], [self.SHOWN])
        self.assertNotIn(b"000-00-1234", b"".join(
            z.read(n) for n in z.namelist()))

    def test_a_restore_lands_the_name_masked(self):
        from oikonome.sync import export, restore
        uid = self._store()
        self.conn.execute("UPDATE income_uploads SET filename = %s "
                          "WHERE id = %s", (self.RAW, uid))
        data = export.build_zip(self.conn)
        # the download itself carries no whole number either
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertNotIn(b"000-00-1234", z.read("income_uploads.csv"))
        dest = make_db()
        self.addCleanup(dest.close)
        write_config(dest)
        restore.restore_zip(dest, data)
        self.assertEqual([r["filename"] for r in dest.execute(
            "SELECT filename FROM income_uploads").fetchall()], [self.SHOWN])


if __name__ == "__main__":
    unittest.main()
