"""A FITID the bank reuses for a different charge never overwrites the
charge already stored under it.

Some banks number FITIDs per download (1, 2, 3 …), so the same FITID in
next week's file names a different charge. A re-import may update a
stored row in place only when the new line is the same economic line the
bank restated (a posting day shifting, a settled amount): same name, same
sign, the same date or the same amount a few days apart, inside the
statement window the new file covers, and nothing else in the file or the
ledger fits that pairing as well. Otherwise the earlier charge would be
rewritten to the new one's date and amount and silently lost, and the
cross-source dedup never looks at the importer's own rows to catch it.
"""

import unittest

from oikonome.sync import ofximport

from .test_file_imports_keep_every_real_row import _ofx
from .util import make_db, write_config


def _ofx_window(start: str, end: str, *rows) -> bytes:
    """One checking statement covering start..end (yyyymmdd); rows are
    (fitid, yyyymmdd, amount, name)."""
    body = "".join(
        f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}"
        f"<NAME>{n}</STMTTRN>\n" for f, d, a, n in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\n"
            "ENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
            "OLDFILEUID:NONE\nNEWFILEUID:NONE\n\n<OFX>\n"
            "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
            f"<DTSERVER>{end}120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>\n"
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO"
            "</STATUS>\n<STMTRS><CURDEF>USD\n<BANKACCTFROM><BANKID>1"
            "<ACCTID>4321<ACCTTYPE>CHECKING</BANKACCTFROM>\n"
            f"<BANKTRANLIST><DTSTART>{start}<DTEND>{end}\n" + body +
            "</BANKTRANLIST>\n<LEDGERBAL><BALAMT>0<DTASOF>" + end +
            "</LEDGERBAL>\n</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n</OFX>\n"
            ).encode()


class ReusedFitid(unittest.TestCase):
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

    def test_weekly_downloads_numbered_per_download_keep_every_fare(self):
        week1 = _ofx_window("20260105", "20260111",
                            ("1", "20260109", "-3.00", "TRANSIT FARE"),
                            ("2", "20260110", "-3.00", "TRANSIT FARE"))
        week2 = _ofx_window("20260112", "20260118",
                            ("1", "20260112", "-3.00", "TRANSIT FARE"),
                            ("2", "20260113", "-3.00", "TRANSIT FARE"))
        ofximport.import_ofx(self.conn, "chk", week1)
        ofximport.import_ofx(self.conn, "chk", week2)
        want = [("2026-01-09", 3.0, "TRANSIT FARE"),
                ("2026-01-10", 3.0, "TRANSIT FARE"),
                ("2026-01-12", 3.0, "TRANSIT FARE"),
                ("2026-01-13", 3.0, "TRANSIT FARE")]
        self.assertEqual(self._rows(), want)
        ofximport.import_ofx(self.conn, "chk", week1)
        ofximport.import_ofx(self.conn, "chk", week2)
        self.assertEqual(self._rows(), want)

    def test_a_charge_before_the_new_statement_window_is_never_restated(self):
        ofximport.import_ofx(self.conn, "chk", _ofx_window(
            "20260105", "20260111",
            ("1", "20260110", "-3.00", "TRANSIT FARE")))
        ofximport.import_ofx(self.conn, "chk", _ofx_window(
            "20260112", "20260118",
            ("1", "20260112", "-3.00", "TRANSIT FARE")))
        self.assertEqual(self._rows(),
                         [("2026-01-10", 3.0, "TRANSIT FARE"),
                          ("2026-01-12", 3.0, "TRANSIT FARE")])

    def test_repeat_charges_that_could_pair_either_way_are_all_kept(self):
        """Inside one shared window, each new fare is as close to the
        other stored fare as to its own FITID's: the FITID is then the
        only thing pairing them, and a reused FITID cannot be trusted
        to — so nothing is overwritten."""
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("1", "20260112", "-3.00", "TRANSIT FARE"),
            ("2", "20260113", "-3.00", "TRANSIT FARE")))
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("1", "20260114", "-3.00", "TRANSIT FARE"),
            ("2", "20260115", "-3.00", "TRANSIT FARE")))
        self.assertEqual(len(self._rows()), 4)

    def test_a_file_that_reuses_a_fitid_for_another_charge_restates_nothing(
            self):
        """The bank put a different merchant on FITID 2 and the charge
        stored there is nowhere in the file: its FITIDs are numbering,
        not identity, so FITID 1's near-match is a new fare too."""
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("1", "20260112", "-3.00", "TRANSIT FARE"),
            ("2", "20260102", "-1500.00", "RENT")))
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("1", "20260114", "-3.00", "TRANSIT FARE"),
            ("2", "20260115", "-80.00", "GROCERY")))
        self.assertEqual(self._rows(),
                         [("2026-01-02", 1500.0, "RENT"),
                          ("2026-01-12", 3.0, "TRANSIT FARE"),
                          ("2026-01-14", 3.0, "TRANSIT FARE"),
                          ("2026-01-15", 80.0, "GROCERY")])

    def test_a_same_day_line_of_the_opposite_sign_is_its_own_row(self):
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("9", "20260110", "-25.00", "HARDWARE")))
        ofximport.import_ofx(self.conn, "chk", _ofx(
            ("9", "20260110", "25.00", "HARDWARE")))
        self.assertEqual(self._rows(),
                         [("2026-01-10", -25.0, "HARDWARE"),
                          ("2026-01-10", 25.0, "HARDWARE")])

    def test_a_restatement_and_a_shifted_sequence_in_one_file(self):
        """FITID 1 restates its charge's posting day while a new charge
        lands on FITID 2 and pushes the charge stored there onto FITID 3.
        The restated row is updated, the shifted row is found, the new
        charge is added, and importing either file again changes no
        count."""
        first = _ofx(("1", "20260110", "-25.00", "HARDWARE"),
                     ("2", "20260112", "-40.00", "DINER"))
        second = _ofx(("1", "20260111", "-25.00", "HARDWARE"),
                      ("2", "20260114", "-30.00", "BOOKSHOP"),
                      ("3", "20260112", "-40.00", "DINER"))
        ofximport.import_ofx(self.conn, "chk", first)
        ofximport.import_ofx(self.conn, "chk", second)
        want = [("2026-01-11", 25.0, "HARDWARE"),
                ("2026-01-12", 40.0, "DINER"),
                ("2026-01-14", 30.0, "BOOKSHOP")]
        self.assertEqual(self._rows(), want)
        ofximport.import_ofx(self.conn, "chk", second)
        self.assertEqual(self._rows(), want)
        ofximport.import_ofx(self.conn, "chk", first)
        self.assertEqual(sorted((a, n) for _, a, n in self._rows()),
                         [(25.0, "HARDWARE"), (30.0, "BOOKSHOP"),
                          (40.0, "DINER")])


if __name__ == "__main__":
    unittest.main()
