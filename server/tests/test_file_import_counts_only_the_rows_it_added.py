"""A file import's `imported` is the rows it ADDED; the lines that were
already in the ledger are counted under `matched`.

The upsert writes every line of a file, new or not, so counting what it
wrote told a person "3 imported" for a file that added nothing — and the
import's history line showed those 3 rows, with a Remove button offering
to take away 3 rows its rollback would never delete (a rollback deletes
only rows no other import holds). Every amount and name here is invented.
"""

import unittest

from oikonome.sync import batches, csvimport, ofximport

from .test_imported_dates_are_the_day_the_source_printed import _line, _ofx
from .util import make_db, write_config

MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}


def _csv(*lines):
    return "Date,Description,Amount\n" + "".join(f"{x}\n" for x in lines)


OLD_CSV = ("2026-03-02,CORNER GROCER,-12.00", "2026-03-03,HARDWARE BARN,-30.00",
           "2026-03-04,BOOKSHOP,-8.50")
NEW_CSV = ("2026-03-05,DINER,-14.00", "2026-03-06,TRANSIT FARE,-3.00")


class FileImportCounts(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _ofx(self, *fids):
        return _ofx("".join(_line(f, f"2026030{i}", "-5.00", f"SHOP {f}")
                            for i, f in enumerate(fids, 1)))

    def _batch_count(self, bid):
        return self.conn.execute("SELECT row_count FROM import_batches "
                                 "WHERE id = %s", (bid,)).fetchone()[
                                     "row_count"]

    def _ofx_import(self, data):
        bid = batches.create(self.conn, "ofx", "chk", "statement.ofx")
        r = ofximport.import_ofx(self.conn, "chk", data, batch_id=bid)
        batches.finish(self.conn, bid, r["imported"])
        return r, bid

    def test_an_ofx_file_imported_twice_adds_nothing_the_second_time(self):
        first, _ = self._ofx_import(self._ofx("A", "B", "C"))
        self.assertEqual((first["imported"], first["matched"]), (3, 0))
        again, bid = self._ofx_import(self._ofx("A", "B", "C"))
        self.assertEqual((again["imported"], again["matched"]), (0, 3))
        # the import history shows what this import added: nothing
        self.assertEqual(self._batch_count(bid), 0)

    def test_an_ofx_file_with_new_and_stored_lines_counts_each(self):
        self._ofx_import(self._ofx("A", "B", "C"))
        r, bid = self._ofx_import(self._ofx("A", "B", "C", "D", "E"))
        self.assertEqual((r["imported"], r["matched"]), (2, 3))
        self.assertEqual(self._batch_count(bid), 2)

    def test_a_csv_file_imported_twice_adds_nothing_the_second_time(self):
        first = csvimport.import_csv(self.conn, "chk", _csv(*OLD_CSV), MAPPING)
        self.assertEqual((first["imported"], first["matched"]), (3, 0))
        again = csvimport.import_csv(self.conn, "chk", _csv(*OLD_CSV), MAPPING)
        self.assertEqual((again["imported"], again["matched"]), (0, 3))

    def test_a_csv_file_with_new_and_stored_lines_counts_each(self):
        csvimport.import_csv(self.conn, "chk", _csv(*OLD_CSV), MAPPING)
        r = csvimport.import_csv(self.conn, "chk",
                                 _csv(*OLD_CSV, *NEW_CSV), MAPPING)
        self.assertEqual((r["imported"], r["matched"]), (2, 3))

    def test_a_split_ofx_file_sums_both_counts(self):
        two = (
            "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\n"
            "ENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
            "OLDFILEUID:NONE\nNEWFILEUID:NONE\n\n<OFX>\n"
            "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
            "<DTSERVER>20260310<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>\n"
            "<BANKMSGSRSV1>" + "".join(
                "<STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>"
                "<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>1"
                f"<ACCTID>{acct}<ACCTTYPE>CHECKING</BANKACCTFROM>"
                "<BANKTRANLIST><DTSTART>20260301<DTEND>20260310\n"
                + _line(f"{acct}1", "20260302", "-5.00", "SHOP")
                + "</BANKTRANLIST><LEDGERBAL><BALAMT>0<DTASOF>20260310"
                "</LEDGERBAL></STMTRS></STMTTRNRS>"
                for acct in ("55551111", "55552222"))
            + "</BANKMSGSRSV1>\n</OFX>\n").encode()
        first = ofximport.import_ofx(self.conn, "chk", two)
        self.assertEqual((first["imported"], first["matched"]), (2, 0))
        again = ofximport.import_ofx(self.conn, "chk", two)
        self.assertEqual((again["imported"], again["matched"]), (0, 2))


class ImportPageResultLine(unittest.TestCase):
    """The server-rendered Import page says what the import did."""

    def _line(self, **result):
        from oikonome.web import todayview
        html = todayview._env.get_template("import.html").render(
            title="Import", centered=False, accounts=[], recent=[],
            result=result)
        start = html.index('class="note"')
        return " ".join(html[start:html.index("Next:", start)].split())

    def test_a_file_that_added_nothing_says_so(self):
        line = self._line(imported=0, matched=3, rows=3)
        self.assertIn("Nothing new — <b>3</b> already here", line)
        self.assertNotIn("Imported", line)

    def test_new_and_stored_lines_are_both_counted(self):
        line = self._line(imported=2, matched=3, rows=5)
        self.assertIn("Imported <b>2</b> transactions · <b>3</b> already "
                      "here", line)

    def test_a_reply_without_matched_reads_as_before(self):
        line = self._line(imported=4, rows=4)
        self.assertIn("Imported <b>4</b> transactions", line)
        self.assertNotIn("already here", line)


if __name__ == "__main__":
    unittest.main()
