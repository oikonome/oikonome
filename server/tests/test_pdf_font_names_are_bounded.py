"""Building a font's character map is work pypdf does once per font NAME,
on every page, in both the plain and the layout pass, before the first
content operator reaches any operator or time budget. A few-KB file that
lists one font with a 65,536-entry map under many names — on the page or
on the /Pages node above it, which only the layout pass reads — must be
refused or read quickly, never parsed once per name for seconds and
hundreds of MB. A real statement, a handful of fonts, still reads."""

import io
import time
import tracemalloc
import unittest
from unittest import mock

from oikonome.sync import pdf_sandbox, pdfimport

from .test_pdfimport import _layout_statement, assemble_pdf

# one bfrange line mapping every two-byte code, starting at 'a'
_FULL_RANGE_MAP = b"beginbfrange <%s> <%s> <%s> endbfrange" % (
    b"0" * 4, b"F" * 4, b"%04X" % ord("a"))
_BODY = b"BT /F1 10 Tf 36 700 Td (06/03 CHECK 1024 500.00) Tj ET"


def many_names_pdf(names: int, *, on_parent: bool,
                   pages: int = 1) -> bytes:
    """`names` font names, all one Type1 font whose ToUnicode is one
    full-range bfrange line, either on the /Pages node (the page's own
    resources hold one plain font) or in the page's own resources."""
    shared = b" ".join(b"/N%d 4 0 R" % i for i in range(names))
    kids = b" ".join(b"%d 0 R" % (7 + i) for i in range(pages))
    parent_res = (b"/Resources << /Font << %s >> >> " % shared
                  if on_parent else b"")
    page_fonts = b"/F1 5 0 R" + (b"" if on_parent else b" " + shared)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d %s>>"
        % (kids, pages, parent_res),
        b"<< /Length %d >>\nstream\n%s\nendstream"
        % (len(_FULL_RANGE_MAP), _FULL_RANGE_MAP),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/ToUnicode 3 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(_BODY), _BODY),
    ]
    for _ in range(pages):
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    b"/Resources << /Font << %s >> >> /Contents 6 0 R >>"
                    % page_fonts)
    return assemble_pdf(objs)


def bare_digit_map_pdf(names: int) -> bytes:
    """`names` names of one font whose map is the full-range bfrange line
    written WITHOUT angle brackets, which pypdf parses just the same."""
    cmap = b"beginbfrange\n%s %s %04X\nendbfrange" % (
        b"0" * 4, b"F" * 4, ord("a"))
    shared = b" ".join(b"/N%d 4 0 R" % i for i in range(names))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [7 0 R] /Count 1 >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/ToUnicode 3 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(_BODY), _BODY),
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R %s >> >> /Contents 6 0 R >>"
        % shared,
    ]
    return assemble_pdf(objs)


def deep_shared_resources_pdf(depth: int, names: int) -> bytes:
    """A chain of `depth` /Pages nodes over one page, the page and every
    node sharing ONE /Resources dict that lists `names` names of a
    full-range font: the layout pass rebuilds each name at each node."""
    shared = b" ".join(b"/N%d 4 0 R" % i for i in range(names))
    objs = [
        b"<< /Type /Catalog /Pages 7 0 R >>",
        b"<< /Font << /F1 5 0 R %s >> >>" % shared,
        b"<< /Length %d >>\nstream\n%s\nendstream"
        % (len(_FULL_RANGE_MAP), _FULL_RANGE_MAP),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/ToUnicode 3 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(_BODY), _BODY),
    ]
    for d in range(depth):
        num = 7 + d
        parent = b"/Parent %d 0 R " % (num - 1) if d else b""
        objs.append(b"<< /Type /Pages %s/Kids [%d 0 R] /Count 1 "
                    b"/Resources 2 0 R >>" % (parent, num + 1))
    objs.append(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
                b"/Resources 2 0 R /Contents 6 0 R >>" % (6 + depth))
    return assemble_pdf(objs)


class FontNamesAreBoundedTests(unittest.TestCase):

    def _bounded(self, fn, pdf, seconds=2.0, peak_mb=60.0):
        """Bounded both ways: through the child process the server uses,
        and inline, where only the font-name budgets stand between the
        file and pypdf — they refuse these shapes without starting a
        child, so they are kept honest on their own."""
        for inline in (False, True):
            with mock.patch.object(pdf_sandbox, "IN_PROCESS", inline):
                self._bounded_once(fn, pdf, seconds, peak_mb)

    def _bounded_once(self, fn, pdf, seconds, peak_mb):
        tracemalloc.start()
        started = time.monotonic()
        try:
            try:
                fn(pdf)
            except ValueError as e:
                self.assertIn("size limit", str(e))
            spent = time.monotonic() - started
            peak = tracemalloc.get_traced_memory()[1] / 1e6
        finally:
            tracemalloc.stop()
        self.assertLess(spent, seconds)
        self.assertLess(peak, peak_mb)

    def test_many_names_on_the_parent_node_are_refused_quickly(self):
        """The /Pages node's fonts are read only by the layout pass, once
        per name, and nothing measured them."""
        pdf = many_names_pdf(100, on_parent=True)
        self.assertLess(len(pdf), 4_000)
        self._bounded(pdfimport.extract_statement, pdf)

    def test_many_names_on_the_page_are_refused_quickly(self):
        """The plain pass builds every font's map before the first
        operator, so its operator and time budgets never see this."""
        pdf = many_names_pdf(100, on_parent=False)
        for fn in (pdfimport.extract_text, pdfimport.extract_statement):
            with self.subTest(fn=fn.__name__):
                self._bounded(fn, pdf)

    def test_a_few_names_per_page_across_pages_stay_bounded(self):
        """Under the per-page name cap, the map entries each name costs
        are what bound the page."""
        pdf = many_names_pdf(12, on_parent=True, pages=3)
        self._bounded(pdfimport.extract_statement, pdf, seconds=3.0)

    def test_a_map_written_without_angle_brackets_is_counted(self):
        """pypdf reads a bfrange line of bare hex digits as the same
        65,536-entry range as its bracketed form; counted as nothing, 63
        names of it pass every cap and are parsed once per name."""
        pdf = bare_digit_map_pdf(63)
        self.assertLess(len(pdf), 2_000)
        for fn in (pdfimport.extract_text, pdfimport.extract_statement):
            with self.subTest(fn=fn.__name__):
                self._bounded(fn, pdf)

    def test_one_resource_dict_shared_down_a_deep_page_tree(self):
        """The layout pass rebuilds every name at every /Pages node, even
        when page and nodes share one dict; charged once, one name down
        a deep chain cost seconds per node inside one layout call. The
        hints are optional, so the page is read without them."""
        pdf = deep_shared_resources_pdf(40, 1)
        self._bounded(pdfimport.extract_statement, pdf)
        text, hints = pdfimport.extract_statement(pdf)
        self.assertIn("CHECK 1024", text)
        self.assertFalse(hints and any(hints))
        reader = pdfimport.PdfReader(io.BytesIO(pdf))
        self.assertFalse(pdfimport._charge_font_names(reader.pages[0], {}))

    def test_a_page_inheriting_its_parents_resources_keeps_its_layout(self):
        """pypdf copies inherited resources onto the page, so a real file
        whose fonts live on the /Pages node is read by the layout pass at
        both nodes, and still gets one."""
        pdf = deep_shared_resources_pdf(1, 2)
        reader = pdfimport.PdfReader(io.BytesIO(pdf))
        self.assertTrue(pdfimport._charge_font_names(reader.pages[0], {}))

    def test_a_handful_of_full_range_maps_still_reads(self):
        """A generator that writes one full-range map per font is real; a
        statement with two such fonts is read normally."""
        pdf = many_names_pdf(2, on_parent=False)
        self.assertIn("CHECK 1024", pdfimport.extract_text(pdf))
        text, _hints = pdfimport.extract_statement(pdf)
        self.assertIn("CHECK 1024", text)

    def test_the_layout_pass_does_not_extend_the_plain_deadline(self):
        """Layout time is charged to the same wall clock, so the two
        passes together stay inside one extraction budget."""
        real = pdf_sandbox.PdfReader

        def slow_layout(*a, **kw):
            r = real(*a, **kw)
            for page in r.pages:
                orig = page.extract_text

                def ext(*a, _orig=orig, **kw):
                    if kw.get("extraction_mode") == "layout":
                        time.sleep(0.3)
                    return _orig(*a, **kw)
                page.extract_text = ext
            return r
        pdf = many_names_pdf(1, on_parent=False, pages=4)
        # the substitution must reach the extraction, so it runs inline
        with mock.patch.object(pdf_sandbox, "IN_PROCESS", True), \
                mock.patch.object(pdf_sandbox, "PdfReader", slow_layout), \
                mock.patch.object(pdf_sandbox, "EXTRACT_SECONDS", 0.5):
            with self.assertRaises(ValueError) as cm:
                pdfimport.extract_statement(pdf)
        self.assertIn("size limit", str(cm.exception))

    def test_a_real_statement_keeps_its_column_hints(self):
        text, hints = pdfimport.extract_statement(_layout_statement())
        self.assertTrue(hints and any(hints))
        self.assertIn("IRA DISTRIBUTION", text)


if __name__ == "__main__":
    unittest.main()
