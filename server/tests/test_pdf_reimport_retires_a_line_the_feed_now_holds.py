"""A re-import that reads a line the other way, when a bank feed already
holds that line, must retire the file row the earlier reading stored.

The corrected line is absorbed by the feed's row (cross-source dedup), so
it never reaches the importer's own re-key pass. Without a retirement the
earlier, wrong-signed file row stays beside the feed row and the money is
counted twice — a deposit read as spending stays spending."""

import datetime as dt
import unittest
from unittest import mock

from oikonome.sync import pdfimport

from .test_pdfimport import HEADER, PERIOD, make_layout_pdf
from .util import add_txn, make_db, write_config


def _statement():
    return make_layout_pdf([[
        PERIOD, HEADER,
        (720, [(36, "06/03"), (100, "GROCERY MART"), (380, "42.50"),
               (540, "1,957.50")]),
        # no bank keyword says which way this went; the column does
        (708, [(36, "06/05"), (100, "IRA DISTRIBUTION"), (460, "1,000.00"),
               (540, "2,957.50")]),
        (696, [(36, "06/07"), (100, "CHECK 1024"), (380, "500.00"),
               (540, "2,457.50")]),
    ]])


def _import_as_before_column_signs(conn, pdf, **kw):
    """Import the way an importer without column or balance signs read
    the statement: plain text, keywords only."""
    plain = pdfimport.extract_text(pdf)
    with mock.patch.object(pdfimport, "extract_statement",
                           return_value=(plain, None)), \
            mock.patch.object(pdfimport, "_balance_hints", return_value={}):
        return pdfimport.import_pdf(conn, "chk", pdf, amount_sign="bank", **kw)


class ReimportRetiresALineTheFeedNowHoldsTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _live(self, name):
        return [(r["id"], float(r["amount"])) for r in self.conn.execute(
            "SELECT id, amount FROM transactions WHERE account_id = 'chk'"
            "   AND removed = 0 AND name = %s ORDER BY id", (name,))]

    def test_the_earlier_wrong_signed_row_is_retired_onto_the_feed_row(self):
        pdf = _statement()
        _import_as_before_column_signs(self.conn, pdf)
        [(old_id, old_amount)] = self._live("IRA DISTRIBUTION")
        self.assertEqual(old_amount, 1000.0, "the earlier reading: spending")
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) VALUES (%s, %s)",
            (old_id, "rollover from the old plan"))
        # the bank feed is linked afterwards and holds the deposit
        feed = add_txn(self.conn, dt.date(2026, 6, 5), -1000.0,
                       "IRA DISTRIBUTION", account="chk",
                       primary="INCOME", txn_id="feed-ira-0605")

        res = pdfimport.import_pdf(self.conn, "chk", pdf, amount_sign="bank")

        self.assertEqual(self._live("IRA DISTRIBUTION"), [(feed, -1000.0)],
                         "one live row for the line: the feed's")
        old = self.conn.execute(
            "SELECT removed, retired_at FROM transactions WHERE id = %s",
            (old_id,)).fetchone()
        self.assertEqual(old["removed"], 1)
        self.assertIsNotNone(old["retired_at"])
        note = self.conn.execute(
            "SELECT note FROM transaction_notes WHERE txn_id = %s",
            (feed,)).fetchone()
        self.assertEqual(note and note["note"], "rollover from the old plan")
        self.assertGreaterEqual(res["corrected"], 1)
        self.assertEqual(res["skipped_duplicates"], 1)
        # the other lines are untouched, and a third import changes nothing
        self.assertEqual([a for _, a in self._live("CHECK 1024")], [500.0])
        before = sorted(map(tuple, self.conn.execute(
            "SELECT id, amount, removed FROM transactions"
            " WHERE account_id = 'chk'").fetchall()))
        pdfimport.import_pdf(self.conn, "chk", pdf, amount_sign="bank")
        after = sorted(map(tuple, self.conn.execute(
            "SELECT id, amount, removed FROM transactions"
            " WHERE account_id = 'chk'").fetchall()))
        self.assertEqual(after, before)

    def test_a_stored_row_of_another_printed_line_is_left_standing(self):
        """The earlier id alone is not proof: a stored row at it whose
        printed line differs is some other line and stays."""
        pdf = _statement()
        _import_as_before_column_signs(self.conn, pdf)
        [(old_id, _)] = self._live("IRA DISTRIBUTION")
        self.conn.execute(
            "UPDATE transactions SET raw = jsonb_set(raw, '{line}',"
            " '\"06/05 IRA DISTRIBUTION 1,000.00 9,999.00\"') WHERE id = %s",
            (old_id,))
        add_txn(self.conn, dt.date(2026, 6, 5), -1000.0, "IRA DISTRIBUTION",
                account="chk", primary="INCOME", txn_id="feed-ira-0605")
        pdfimport.import_pdf(self.conn, "chk", pdf, amount_sign="bank")
        self.assertEqual(sorted(self._live("IRA DISTRIBUTION")),
                         sorted([("feed-ira-0605", -1000.0),
                                 (old_id, 1000.0)]))


if __name__ == "__main__":
    unittest.main()
