"""A file or feed row is dated on the calendar day its source meant.

An OFX stamp carries its own zone (`20260401000000[+1:CET]`); the day the
bank printed is the YYYYMMDD in front of it. Converting the stamp to UTC
first and then taking the date moves a midnight row of a +offset bank a
day early and an evening row of a -offset bank a day late — across a
month end, into the wrong month's spend and budget.
"""

import datetime as dt
import unittest

from oikonome.sync import feed_dates, ofximport

from .util import add_txn, make_db, write_config


def _ofx(lines: str, start: str = "20260301", end: str = "20260430") -> bytes:
    return (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\n"
        "ENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
        "OLDFILEUID:NONE\nNEWFILEUID:NONE\n\n<OFX>\n"
        "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>\n"
        "<DTSERVER>20260430120000[-5:EST]<LANGUAGE>ENG</SONRS>"
        "</SIGNONMSGSRSV1>\n"
        "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO"
        "</STATUS>\n<STMTRS><CURDEF>USD\n"
        "<BANKACCTFROM><BANKID>123456789<ACCTID>11112222<ACCTTYPE>CHECKING"
        "</BANKACCTFROM>\n"
        f"<BANKTRANLIST><DTSTART>{start}<DTEND>{end}\n{lines}"
        "</BANKTRANLIST>\n"
        "<LEDGERBAL><BALAMT>100.00<DTASOF>20260430120000[-5:EST]"
        "</LEDGERBAL>\n</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n</OFX>\n"
    ).encode()


def _line(fitid: str, posted: str, amount: str = "-12.00",
          name: str = "CORNER GROCER") -> str:
    return (f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{posted}<TRNAMT>{amount}"
            f"<FITID>{fitid}<NAME>{name}</STMTTRN>\n")


class OfxDatesAreThePrintedDay(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _dates(self):
        return {r["id"]: r["date"] for r in self.conn.execute(
            "SELECT id, date FROM transactions WHERE id LIKE 'ofx:%'")}

    def test_midnight_stamp_of_a_positive_offset_bank_keeps_its_day(self):
        ofximport.import_ofx(self.conn, "chk", _ofx(
            _line("A1", "20260401000000[+1:CET]")))
        self.assertEqual(self._dates()["ofx:chk:A1"], dt.date(2026, 4, 1))

    def test_evening_stamp_of_a_negative_offset_bank_keeps_its_day(self):
        ofximport.import_ofx(self.conn, "chk", _ofx(
            _line("B1", "20260331210000.000[-5:EST]")))
        self.assertEqual(self._dates()["ofx:chk:B1"], dt.date(2026, 3, 31))

    def test_statement_window_is_the_printed_days(self):
        rows = ofximport.parse_ofx(_ofx(
            _line("C1", "20260415"),
            start="20260401000000[+1:CET]",
            end="20260430235959[-8:PST]"))
        self.assertEqual(rows[0]["window"],
                         (dt.date(2026, 4, 1), dt.date(2026, 4, 30)))

    def test_reimport_of_a_row_stored_on_its_utc_day_moves_it_not_doubles_it(self):
        # the row an earlier import stored a day early (its UTC day)
        add_txn(self.conn, "2026-03-31", 12.00, "CORNER GROCER",
                account="chk", txn_id="ofx:chk:D1")
        ofximport.import_ofx(self.conn, "chk", _ofx(
            _line("D1", "20260401000000[+1:CET]")))
        self.assertEqual(self._dates(), {"ofx:chk:D1": dt.date(2026, 4, 1)})


    def test_reimport_of_a_first_day_row_stored_before_the_window_does_not_double(self):
        # the statement opens on the same midnight stamp: the earlier
        # import stored both the row and the window a day early, so the
        # stored row now sits one day before the printed window
        add_txn(self.conn, "2026-03-31", 12.00, "CORNER GROCER",
                account="chk", txn_id="ofx:chk:E1")
        ofximport.import_ofx(self.conn, "chk", _ofx(
            _line("E1", "20260401000000[+1:CET]")
            + _line("E2", "20260402000000[+1:CET]", name="HARDWARE BARN"),
            start="20260401000000[+1:CET]",
            end="20260403000000[+1:CET]"))
        self.assertEqual(self._dates(), {"ofx:chk:E1": dt.date(2026, 4, 1),
                                         "ofx:chk:E2": dt.date(2026, 4, 2)})

class PrintedDayHelper(unittest.TestCase):
    def test_ofx_stamp_forms(self):
        for stamp, day in [
                ("20260401", dt.date(2026, 4, 1)),
                ("20260401000000[+1:CET]", dt.date(2026, 4, 1)),
                ("20260331210000.123[-5:EST]", dt.date(2026, 3, 31)),
                ("20260331235959[-3.30:NST]", dt.date(2026, 3, 31)),
                ("202603312359", dt.date(2026, 3, 31))]:
            self.assertEqual(feed_dates.ofx_printed_day(stamp), day, stamp)

    def test_unreadable_stamp_is_left_for_the_parser_to_refuse(self):
        for stamp in ("", "2026-04-01", "garbage", "2026040", "20261341"):
            self.assertIsNone(feed_dates.ofx_printed_day(stamp), stamp)

    def test_epoch_feed_is_dated_by_its_utc_day(self):
        # SimpleFIN sends a bare epoch with no zone; the UTC day is the
        # documented reading (see feed_dates.epoch_day)
        self.assertEqual(
            feed_dates.epoch_day(int(dt.datetime(
                2026, 4, 1, 0, 30, tzinfo=dt.timezone.utc).timestamp())),
            dt.date(2026, 4, 1))
        self.assertEqual(
            feed_dates.epoch_day(int(dt.datetime(
                2026, 3, 31, 23, 59, tzinfo=dt.timezone.utc).timestamp())),
            dt.date(2026, 3, 31))


if __name__ == "__main__":
    unittest.main()
