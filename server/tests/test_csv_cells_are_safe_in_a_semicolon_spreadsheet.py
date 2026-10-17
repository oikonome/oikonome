"""A CSV the app writes stays inert when a spreadsheet splits it on ';'.

Excel in a locale whose list separator is ';' (most of continental
Europe, much of Latin America) splits a .csv on ';', and a quote only
protects a field it opens — so `STARBUCKS;=cmd|...` arrives as a second
cell starting '='. Checking a value's first character is not enough: the
start of every piece a reader can cut the value into (after ';', a tab or
a line break) must not open a formula either.
"""

import csv
import datetime as dt
import io
import os
import re
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config

LEADS = ("=", "+", "-", "@")
INVISIBLE = "\t\r\n﻿ "
PAYEE = "STARBUCKS;=cmd|' /C calc'!A0"
NOTE = "split;+1+1;\t@SUM(A1)\nlunch\n=cmd|' /C calc'!A0;"


def _formula_cells(text: str, delimiter: str) -> list[str]:
    # newline="" so a lone CR ends a record, the way a spreadsheet reads it
    return [c for row in csv.reader(io.StringIO(text, newline=""),
                                    delimiter=delimiter)
            for c in row if c.lstrip(INVISIBLE)[:1] in LEADS
            and not re.fullmatch(r"-?\d+(\.\d+)?", c.strip())]


class SemicolonSpreadsheetTests(unittest.TestCase):

    def test_the_writer_neutralises_every_piece_of_a_cell(self):
        from oikonome.web import api
        buf = io.StringIO()
        w = api._csv_writer(buf)
        w.writerow(["2026-10-01", api._csv_safe(PAYEE), -4.5,
                    api._csv_safe(NOTE), "-12.50"])
        out = buf.getvalue()
        self.assertEqual(_formula_cells(out, ";"), [])
        self.assertEqual(_formula_cells(out, "\t"), [])
        # read the ordinary way, the words are all still there and a
        # signed amount is still a number
        row = next(csv.reader(io.StringIO(out)))
        self.assertIn("STARBUCKS;", row[1])
        self.assertIn("cmd|' /C calc'!A0", row[1])
        self.assertEqual(row[2], "-4.5")
        self.assertEqual(row[4], "-12.50")

    def test_a_plain_value_is_written_as_it_was(self):
        from oikonome.web import api
        buf = io.StringIO()
        api._csv_writer(buf).writerow(["rent; late fee", "a\tb", "x;y"])
        self.assertEqual(next(csv.reader(io.StringIO(buf.getvalue()))),
                         ["rent; late fee", "a\tb", "x;y"])

    def test_a_line_break_starts_a_piece(self):
        # in a ';' read the quote csv.writer puts round a multi-line cell
        # sits mid-field, so the line break ends the record and the next
        # line is read as fresh cells
        from oikonome.web import api
        for note, written in (
                ("lunch\n=cmd|' /C calc'!A0;", "lunch\n'=cmd|' /C calc'!A0;"),
                ("lunch\r\n=cmd|' /C calc'!A0;",
                 "lunch\r\n'=cmd|' /C calc'!A0;"),
                ("lunch\r@SUM(A1)", "lunch\r'@SUM(A1)"),
                ("a\n \t-1+cmd", "a\n \t'-1+cmd")):
            with self.subTest(note=note):
                buf = io.StringIO()
                api._csv_writer(buf).writerow(
                    ["2026-10-01", "Coffee", -4.5, api._csv_safe(note),
                     "-12.50", "id"])
                out = buf.getvalue()
                self.assertEqual(_formula_cells(out, ";"), [])
                self.assertEqual(_formula_cells(out, "\t"), [])
                # read the ordinary way, the note is one cell with one mark
                row = next(csv.reader(io.StringIO(out, newline="")))
                self.assertEqual(row[3], written)

    def test_a_run_of_tabs_is_written_in_linear_time(self):
        # the piece guard runs on every text cell of every export, and a
        # restored note is not held to the API's length cap: a long run of
        # breaks must cost one pass, not one pass per break
        from oikonome.web import api
        tabs = "\t" * 200_000
        cells = ["a" + tabs + "b",            # no formula character at all
                 "=a" + tabs + "b",           # one, far from the run
                 "a;" + tabs + "=b",          # one behind the whole run
                 ("x;\t" * 100_000) + "@y"]  # breaks and pieces alternating
        buf = io.StringIO()
        w = api._csv_writer(buf)
        began = time.perf_counter()
        w.writerow(cells)
        took = time.perf_counter() - began
        self.assertLess(took, 0.5, f"{took:.2f}s for 800k characters")
        limit = csv.field_size_limit(1 << 24)
        self.addCleanup(csv.field_size_limit, limit)
        self.assertEqual(_formula_cells(buf.getvalue(), ";"), [])
        self.assertEqual(_formula_cells(buf.getvalue(), "\t"), [])

    def test_only_the_break_next_to_the_formula_is_marked(self):
        from oikonome.web import api
        self.assertEqual(api._csv_piece_safe("a;\t\t=x"), "a;\t\t'=x")
        self.assertEqual(api._csv_piece_safe("l\r\n=x"), "l\r\n'=x")
        self.assertEqual(api._csv_piece_safe("a; =x;b"), "a;' =x;b")
        self.assertEqual(api._csv_piece_safe("a;b=c"), "a;b=c")
        self.assertEqual(api._csv_piece_safe("rent - late;fee"),
                         "rent - late;fee")

    def test_a_double_quote_after_the_break_does_not_hide_the_formula(self):
        # csv doubles the quote, and a reader that splits on the break opens
        # a quoted field there, closes it at the doubled quote and reads the
        # rest as the cell: the quote must not clear the formula flag
        from oikonome.web import api
        import csv, io
        for cell in ('a;"=HYPERLINK(""http://x"",""y"")', 'x;"@SUM(A1)',
                     'a\t"=1+1', 'a\n"=1+1'):
            safe = api._csv_piece_safe(cell)
            buf = io.StringIO(); csv.writer(buf, lineterminator="\n").writerow([safe])
            for delim in (";", "\t"):
                self.assertEqual(_formula_cells(buf.getvalue(), delim), [], (cell, delim))
        self.assertEqual(api._csv_piece_safe('say "hi";-ok'), 'say "hi";\'-ok')

    def test_every_csv_the_api_writes_goes_through_the_safe_writer(self):
        src = (Path(__file__).resolve().parents[1]
               / "oikonome" / "web" / "api.py").read_text()
        start = src.index("\ndef _csv_writer(")
        end = src.index("\ndef ", start + 1)
        bare = [src.count("\n", 0, m.start()) + 1
                for m in re.finditer(r"\b_?c(?:sv)?\.writer\(", src)
                if not start < m.start() < end]
        self.assertEqual(bare, [], "a csv.writer outside _csv_writer "
                                   "(api.py lines listed)")


class TransactionsDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"csvsemi-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            tx = add_txn(conn, TODAY.replace(day=1), 4.5, PAYEE)
            from oikonome.web import data
            data.set_note(conn, tx, NOTE)
        finally:
            conn.close()

    def test_the_transactions_csv_has_no_formula_in_a_semicolon_reader(self):
        now = dt.datetime.combine(TODAY, dt.time(9, 0))
        with mock.patch("oikonome.localtime.now_local", return_value=now):
            r = self.client.get(
                f"/api/transactions/export?y={TODAY.year}&m={TODAY.month}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("STARBUCKS", r.text)
        self.assertEqual(_formula_cells(r.text, ";"), [])
        self.assertEqual(_formula_cells(r.text, "\t"), [])


if __name__ == "__main__":
    unittest.main()
