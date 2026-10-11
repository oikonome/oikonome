"""Text extraction of a member's PDF runs in a child process with a ceiling
on its memory and its time, so that what a PDF makes pypdf do cannot cost
the server process either.

The in-process budgets estimate pypdf's per-font-name work from outside,
and every estimate of a parser misses some route the parser takes. Each
file below is a few KB and takes a route the estimate once missed — a
bfrange list whose parse state carries across a broken line, a form
reaching its fonts through /Parent, a /W written as a string, a map
written without angle brackets, a resource dict shared down a deep page
tree. Each must be refused or read within seconds, and the server
process's peak memory must not move: only the child may pay."""

import os
import resource
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from oikonome.sync import pdf_sandbox, pdfimport

from .test_pdf_font_names_are_bounded import (bare_digit_map_pdf,
                                              deep_shared_resources_pdf)
from .test_pdfimport import CHECKING_TEXT, _layout_statement, assemble_pdf, \
    make_pdf

_BODY = b"BT /F1 10 Tf 36 700 Td (06/03 CHECK 1024 500.00) Tj ET"
_FULL_RANGE_MAP = b"beginbfrange <0000> <FFFF> <0061> endbfrange"


def _stream(data: bytes, extra: bytes = b"") -> bytes:
    return b"<< %s/Length %d >>\nstream\n%s\nendstream" % (
        extra, len(data), data)


def _one_font_many_names(names: int, font: bytes,
                         extra: tuple[bytes, ...] = ()) -> bytes:
    """Object 4 is `font`, listed under `names` names on the page; object
    3 and objects 8 on are what it refers to."""
    shared = b" ".join(b"/N%d 4 0 R" % i for i in range(names))
    return assemble_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [7 0 R] /Count 1 >>",
        extra[0] if extra else b"null",
        font,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(_BODY),
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R %s >> >> /Contents 6 0 R >>"
        % shared,
        *extra[1:],
    ])


def desynced_bfrange_pdf(names: int = 60) -> bytes:
    """pypdf keeps a bfrange list open across a line that fails to parse;
    the next line `0000 FFFF 0061` then builds 65,439 entries that a
    reader resetting its state counts as four."""
    cmap = b"beginbfrange\n0000 0001 [ ZZZZ\n0000 FFFF 0061\nendbfrange"
    return _one_font_many_names(
        names, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/ToUnicode 3 0 R >>", (_stream(cmap),))


def form_parent_fonts_pdf(names: int = 120) -> bytes:
    """A form XObject with no /Resources of its own inherits a font dict
    through /Parent, which pypdf follows and a resource walk does not."""
    shared = b" ".join(b"/N%d 4 0 R" % i for i in range(names))
    form = b"BT /N0 10 Tf (x) Tj ET"
    return assemble_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [7 0 R] /Count 1 >>",
        _stream(_FULL_RANGE_MAP),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/ToUnicode 3 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream(_BODY + b" /Fm1 Do"),
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> /XObject << /Fm1 8 0 R >> >> "
        b"/Contents 6 0 R >>",
        _stream(form, b"/Type /XObject /Subtype /Form /BBox [0 0 1 1] "
                b"/Parent 9 0 R "),
        b"<< /Resources << /Font << %s >> >> >>" % shared,
    ])


def string_widths_pdf(names: int = 60) -> bytes:
    """A CID font whose /W holds a 65,535-character string where a width
    list belongs: pypdf takes any sequence as the list."""
    widths = b"(" + b"a" * 65_535 + b")"
    descendant = (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /X "
                  b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) "
                  b"/Supplement 0 >> /FontDescriptor 9 0 R /DW 1000 "
                  b"/W [0 3 0 R] >>")
    descriptor = (b"<< /Type /FontDescriptor /FontName /X /Flags 4 "
                  b"/FontBBox [0 0 1 1] /ItalicAngle 0 /Ascent 1 /Descent 0 "
                  b"/CapHeight 1 /StemV 1 >>")
    font = (b"<< /Type /Font /Subtype /Type0 /BaseFont /X "
            b"/Encoding /Identity-H /DescendantFonts [8 0 R] >>")
    return _one_font_many_names(names, font, (widths, descendant, descriptor))


def _alive(pid: int) -> bool:
    """Running, as opposed to gone or a zombie awaiting its reaper."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class ExtractionRunsInABoundedChildTests(unittest.TestCase):

    def setUp(self):
        self.assertFalse(pdf_sandbox.IN_PROCESS)

    def _contained(self, fn, pdf, seconds=20.0, parent_mb=50.0):
        before = _peak_rss_mb()
        started = time.monotonic()
        try:
            fn(pdf)
        except ValueError as e:
            self.assertIn("size limit", str(e))
        self.assertLess(time.monotonic() - started, seconds)
        self.assertLess(_peak_rss_mb() - before, parent_mb)

    def test_abusive_font_routes_cost_the_server_nothing(self):
        fixtures = {
            "bare-digit bfrange": bare_digit_map_pdf(63),
            "desynced bfrange list": desynced_bfrange_pdf(),
            "form fonts through /Parent": form_parent_fonts_pdf(120),
            "string /W": string_widths_pdf(),
            "deep shared page tree": deep_shared_resources_pdf(40, 1),
        }
        for name, pdf in fixtures.items():
            self.assertLess(len(pdf), 70_000)
            for fn in (pdfimport.extract_text, pdfimport.extract_statement):
                with self.subTest(fixture=name, fn=fn.__name__):
                    self._contained(fn, pdf)

    def test_a_real_statement_keeps_its_column_hints_through_the_child(self):
        text, hints = pdfimport.extract_statement(_layout_statement())
        by_name = {r["name"]: r["amount"] for r in pdfimport.parse_statement(
            text, kind="bank", hints=hints)["rows"]}
        self.assertEqual(by_name["IRA DISTRIBUTION"], -1000.00)
        self.assertEqual(by_name["POS PURCHASE W/CASH BACK SAFEWAY"], 50.00)
        pdf_sandbox.IN_PROCESS = True
        try:
            self.assertEqual(pdfimport.extract_statement(_layout_statement()),
                             (text, hints))
        finally:
            pdf_sandbox.IN_PROCESS = False

    def test_the_childs_own_refusals_keep_their_messages(self):
        with self.assertRaises(ValueError) as cm:
            pdfimport.extract_text(b"Date,Amount\n06/03,12.00\n")
        self.assertIn("could not read this PDF", str(cm.exception))
        self.assertIn("06/08 POS PURCHASE SAFEWAY #12",
                      pdfimport.extract_text(make_pdf(CHECKING_TEXT)))

    def test_a_child_past_its_memory_ceiling_is_refused_not_raised(self):
        """The ceiling is real: a child allowed almost nothing over its
        start-up cannot even import pypdf, and the caller sees the
        size-limit refusal, never a crash."""
        with mock.patch.object(pdf_sandbox, "CHILD_MEMORY", 1024 * 1024):
            with self.assertRaises(ValueError) as cm:
                pdfimport.extract_text(make_pdf(CHECKING_TEXT))
        self.assertIn("size limit", str(cm.exception))

    def test_malformed_child_output_is_refused(self):
        script = ("import sys; open(sys.argv[2], 'w').write('[1, 2')")
        with mock.patch.object(pdf_sandbox, "_child_command",
                               lambda src, dst, mode: [
                                   sys.executable, "-c", script, src, dst]):
            with self.assertRaises(ValueError) as cm:
                pdfimport.extract_text(make_pdf(CHECKING_TEXT))
        self.assertIn("size limit", str(cm.exception))

    def test_children_past_the_slot_limit_wait_then_are_turned_away(self):
        """Each child may use its whole allowance, so only a few run at
        once; an upload that cannot get a slot within the deadline is told
        to try again rather than starting one more."""
        full = threading.BoundedSemaphore(1)
        full.acquire()
        with mock.patch.object(pdf_sandbox, "_slots", full), \
                mock.patch.object(pdf_sandbox, "EXTRACT_SECONDS", 0.2), \
                mock.patch.object(pdf_sandbox, "CHILD_GRACE_SECONDS", 0.2), \
                mock.patch.object(pdf_sandbox.subprocess, "Popen",
                                  side_effect=AssertionError("started")):
            with self.assertRaises(ValueError) as cm:
                pdfimport.extract_text(make_pdf(CHECKING_TEXT))
        self.assertIn("try again", str(cm.exception))

    def test_a_child_that_hangs_is_killed_at_the_deadline_and_reaped(self):
        """The wait is the last line of defence: a child that never
        finishes is killed with its whole process group when the deadline
        passes, and nothing it started is left behind."""
        script = ("import subprocess, sys, time\n"
                  "p = subprocess.Popen([sys.executable, '-c', "
                  "'import time; time.sleep(60)'])\n"
                  "open(sys.argv[1], 'w').write(str(p.pid))\n"
                  "time.sleep(60)\n")
        procs = []
        real_popen = subprocess.Popen

        def popen(*a, **kw):
            procs.append(real_popen(*a, **kw))
            return procs[-1]

        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "grandchild.pid")
            with mock.patch.object(pdf_sandbox, "_child_command",
                                   lambda src, dst, mode: [
                                       sys.executable, "-c", script,
                                       pid_file]), \
                    mock.patch.object(pdf_sandbox, "EXTRACT_SECONDS", 0.5), \
                    mock.patch.object(pdf_sandbox, "CHILD_GRACE_SECONDS",
                                      1.0), \
                    mock.patch.object(pdf_sandbox.subprocess, "Popen", popen):
                started = time.monotonic()
                with self.assertRaises(ValueError) as cm:
                    pdfimport.extract_text(make_pdf(CHECKING_TEXT))
                spent = time.monotonic() - started
            with open(pid_file) as f:
                grandchild = int(f.read())
        self.assertIn("size limit", str(cm.exception))
        self.assertGreaterEqual(spent, 1.5)
        self.assertLess(spent, 4.0)
        (proc,) = procs
        self.assertIsNotNone(proc.returncode)          # reaped
        with self.assertRaises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)
        self.assertFalse(os.path.exists(f"/proc/{proc.pid}"))
        # killed with the group; whoever adopted it reaps it shortly
        deadline = time.monotonic() + 2.0
        while (_alive(grandchild) and time.monotonic() < deadline):
            time.sleep(0.05)
        self.assertFalse(_alive(grandchild))


if __name__ == "__main__":
    unittest.main()
