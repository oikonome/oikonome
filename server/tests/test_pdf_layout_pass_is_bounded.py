"""The layout rendering a statement import reads its column hints from is
bounded like the plain text pass: a tiny PDF must not hold a worker for
minutes or build strings of hundreds of megabytes, and a real statement
still gets its hints."""

import time
import unittest
from unittest import mock

from oikonome.sync import pdf_sandbox, pdfimport

from .test_pdfimport import (HEADER, PERIOD, _layout_statement,
                             assemble_pdf, make_layout_pdf)


def far_spaced_runs_pdf(runs: int) -> bytes:
    """One page of one text object holding a dated line and then `runs`
    single-letter runs, each moved far to the right of the last. The plain
    pass reads it in a blink; pypdf's layout mode pads every gap with
    spaces and joins the line by repeated concatenation. (Compressed, the
    same page is under a kilobyte.)"""
    body = (b"BT /F1 1 Tf 36 700 Td (06/03 CHECK 1024 500.00) Tj "
            + b"100000 0 Td (a) Tj " * runs + b"ET")
    return assemble_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(body), body),
    ])


class LayoutPassIsBoundedTests(unittest.TestCase):

    def _timed(self, pdf: bytes):
        started = time.monotonic()
        text, hints = pdfimport.extract_statement(pdf)
        return time.monotonic() - started, text, hints

    def test_many_far_spaced_runs_on_one_line_return_quickly(self):
        """A page of thousands of far-spaced runs must not pin the import:
        the layout pass's cost grows with the square of the runs on a
        line, and it must stay inside budgets of its own (unbounded, this
        page costs tens of seconds and tens of MB; the largest page the
        plain pass accepts, hours and hundreds of MB)."""
        spent, text, hints = self._timed(far_spaced_runs_pdf(3_000))
        self.assertLess(spent, 3.0)
        self.assertLess(len(text), 100_000)
        self.assertTrue(hints is None or not any(hints))
        self.assertIn("CHECK 1024", text)

    def test_a_page_just_under_the_operator_cap_is_padded_within_bounds(self):
        """Below the operator cap the layout pass still runs, so its own
        padding ceilings are what keep the rendered line small."""
        from pypdf._text_extraction._layout_mode import _fixed_width_page
        self.assertLessEqual(_fixed_width_page.WHITESPACE_LIMIT,
                             pdfimport.LAYOUT_SPACE_LIMIT)
        self.assertLessEqual(_fixed_width_page.NEWLINE_LIMIT,
                             pdfimport.LAYOUT_NEWLINE_LIMIT)
        runs = (pdfimport.LAYOUT_TEXT_OPS - 10) // 2
        spent, _text, _hints = self._timed(far_spaced_runs_pdf(runs))
        self.assertLess(spent, 5.0)

    def test_layout_time_is_capped_across_pages(self):
        """Once the layout passes have used their time, later pages get
        no layout instead of more time — and their lines are marked as of
        UNKNOWN sign, not as lines with no column, because on an idle host
        the same page would have had its hints."""
        pdf = make_layout_pdf([
            [PERIOD, HEADER, (720, [(36, "06/03"), (100, "CHECK 1024"),
                                    (380, "500.00"), (540, "1,500.00")])],
            [HEADER, (720, [(36, "06/20"), (100, "RMD DISTRIBUTION"),
                            (460, "900.00"), (540, "2,400.00")])],
        ])
        # the substitution must reach the extraction, so it runs inline
        with mock.patch.object(pdf_sandbox, "IN_PROCESS", True), \
                mock.patch.object(pdf_sandbox, "LAYOUT_SECONDS", 0.0):
            text, hints = pdfimport.extract_statement(pdf)
        pairs = list(zip(text.splitlines(), hints))
        self.assertEqual([h for ln, h in pairs if "CHECK" in ln], ["out"])
        self.assertEqual([h for ln, h in pairs if "RMD" in ln],
                         [pdfimport.UNKNOWN_SIGN])

    def test_layout_output_past_its_cap_drops_every_hint(self):
        with mock.patch.object(pdf_sandbox, "IN_PROCESS", True), \
                mock.patch.object(pdf_sandbox, "LAYOUT_TEXT_BYTES", 10):
            text, hints = pdfimport.extract_statement(_layout_statement())
        self.assertIsNone(hints)
        self.assertIn("IRA DISTRIBUTION", text)

    def test_an_overlong_layout_line_is_not_read_for_columns(self):
        """A line longer than any statement line is never searched by the
        column regexes (the parser skips such lines too)."""
        header = "Date  Description   Withdrawals   Deposits   Balance"
        long_line = "06/03 X" + " " * pdfimport.MAX_LINE_CHARS + "5.00"
        self.assertEqual(pdfimport._column_hints([header, long_line]),
                         [None, None])
        with mock.patch.object(pdfimport, "_trailing_amounts",
                               side_effect=AssertionError("read")):
            pdfimport._column_hints([header, long_line])

    def test_a_real_statement_keeps_its_column_hints(self):
        text, hints = pdfimport.extract_statement(_layout_statement())
        by_name = {r["name"]: r["amount"] for r in pdfimport.parse_statement(
            text, kind="bank", hints=hints)["rows"]}
        self.assertEqual(by_name["IRA DISTRIBUTION"], -1000.00)
        self.assertEqual(by_name["POS PURCHASE W/CASH BACK SAFEWAY"], 50.00)


if __name__ == "__main__":
    unittest.main()
