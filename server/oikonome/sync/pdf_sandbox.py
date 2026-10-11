"""PDF text extraction, run in a child process under hard limits.

pypdf builds a font's character map, its width table and its encoding
once per font NAME, on every page, in both extraction passes, and reaches
a page's fonts through more routes (inherited resources, a form's
/Parent, every /Pages node above the page) than any estimate made from
outside can follow. The budgets below refuse the cheap cases before
pypdf is called at all, but an estimate of a parser is only ever as good
as its last reading of that parser. So the extraction itself runs in a
separate interpreter (`run`) with a ceiling on its address space and its
CPU time and a wall-clock deadline on the wait: whatever a member's PDF
makes pypdf do — a shape these budgets miss included — costs at most
that child, which is killed and the upload refused with the same
size-limit message. The web process never parses a member's PDF itself.

The child is started fresh (never forked from the server, whose threads
and database pools do not survive a fork) and imports only this module
and pypdf: nothing here may import the rest of the app, the database or
the settings. It reads the PDF from a private temporary file and writes
its result as JSON to another, which the parent reads only when it is
within the size the extraction caps allow.

`IN_PROCESS` runs the extraction inline instead. It exists for tests
that substitute a constant or a pypdf call inside the extraction, which a
child process would not see; it is never switched on by the app.

first_page_within_budget stays in the caller's process: it walks a
page's objects with pypdf's capped decoders and builds no font, so none
of the per-name work the child exists to contain happens there.
"""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

import pypdf.filters
from pypdf import PdfReader
from pypdf.errors import LimitReachedError
from pypdf.generic import (ArrayObject, DictionaryObject, EncodedStreamObject,
                           NameObject, StreamObject)

log = logging.getLogger(__name__)

# ---- text extraction --------------------------------------------------------

_NO_TEXT_MSG = ("this PDF has no text layer (it is likely a scanned image) — "
                "OCR is not attempted; export a CSV or OFX/QFX from your "
                "bank instead")
_ENCRYPTED_MSG = ("this PDF is password-protected — save an unlocked copy "
                  "(print to PDF) or export a CSV/OFX from your bank instead")


MAX_PAGES = 500          # statements are dozens of pages at most
MAX_TEXT_BYTES = 2_000_000  # decompression-bomb ceiling on extracted text

# The text cap above only fires after a page has been extracted, and by
# then pypdf has already inflated the page's content stream and parsed it
# into Python objects — roughly 50 MB of memory and most of a second of
# CPU per MB of content. A 40 KB upload can inflate to tens of MB, which
# is enough to kill the whole app process. So the expansion itself is
# bounded, before it happens:
#   * every pypdf decoder stops at STREAM_LIMIT bytes (pypdf reads these
#     module globals at call time, and nothing else in the server uses
#     pypdf, so lowering them process-wide costs nothing);
#   * each page's content (its streams plus every form it can draw) is
#     measured before extraction and refused past PAGE_CONTENT_LIMIT, with
#     a running DOC_CONTENT_LIMIT across pages;
#   * a form drawn from a form drawn from a form re-parses the same small
#     stream thousands of times, so extraction also counts operators
#     (MAX_OPERATIONS) and watches a wall-clock deadline.
# A real statement or tax-return page carries tens to a few hundred KB of
# content and a few thousand operators, far inside every limit.
STREAM_LIMIT = 4_000_000
PAGE_CONTENT_LIMIT = 1_500_000
DOC_CONTENT_LIMIT = 40_000_000
MAX_OPERATIONS = 2_500_000
EXTRACT_SECONDS = 30.0
MAX_FORMS_PER_PAGE = 2_000

# Every name below exists from pypdf 6.11, the declared floor in
# pyproject.toml; the test suite checks each one is present and lowered.
DECODER_LIMITS = ("ZLIB_MAX_OUTPUT_LENGTH",
                  "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
                  "MAX_DECLARED_STREAM_LENGTH", "LZW_MAX_OUTPUT_LENGTH",
                  "RUN_LENGTH_MAX_OUTPUT_LENGTH", "JBIG2_MAX_OUTPUT_LENGTH",
                  "FLATE_MAX_BUFFER_SIZE")
for _name in DECODER_LIMITS:
    if hasattr(pypdf.filters, _name):
        setattr(pypdf.filters, _name,
                min(getattr(pypdf.filters, _name), STREAM_LIMIT))

# The layout rendering extract_statement reads column hints from is a
# second pass with costs of its own, which the plain pass's budgets do not
# see: pypdf multiplies out every transform since the last text-matrix
# reset for each run it places, pads each gap with spaces and joins a
# line's runs by repeated concatenation, so a page of a few thousand
# far-spaced runs costs time and memory quadratic in their number. A page
# with more text operators than LAYOUT_TEXT_OPS (a statement page draws a
# few hundred) gets no layout pass and so no hints; LAYOUT_SECONDS caps
# the layout passes' total time and LAYOUT_TEXT_BYTES their total output,
# past which the file gets no hints at all. Hints only ever refine a sign
# the keywords would otherwise guess, so going without them is safe.
LAYOUT_TEXT_OPS = 4_000
LAYOUT_SECONDS = 10.0
LAYOUT_TEXT_BYTES = 4 * 2_000_000
# pypdf's own padding ceilings for that pass (read at call time). A
# statement line is a couple of hundred characters wide and a page a
# hundred or so lines tall, so a gap wider than these is never a column.
LAYOUT_SPACE_LIMIT = 250
LAYOUT_NEWLINE_LIMIT = 200
try:
    from pypdf._text_extraction._layout_mode import (
        _fixed_width_page as _layout_page)
except ImportError:                              # pragma: no cover
    _layout_page = None
else:
    for _name, _limit in (("WHITESPACE_LIMIT", LAYOUT_SPACE_LIMIT),
                          ("NEWLINE_LIMIT", LAYOUT_NEWLINE_LIMIT)):
        if hasattr(_layout_page, _name):
            setattr(_layout_page, _name,
                    min(getattr(_layout_page, _name), _limit))
# the operators that grow the layout pass's per-run work
_LAYOUT_TEXT_OPERATORS = frozenset((b"Tj", b"TJ", b"'", b'"', b"Td", b"TD",
                                    b"T*", b"Tm", b"cm"))

_TOO_BIG_MSG = ("this PDF expands past the size limit for a statement — "
                "export a CSV or OFX/QFX from your bank instead")

# What a page draws besides its operators, each with its own allowance so
# that a legitimately large font or scan does not crowd out the content
# budget above:
#   * PAGE_DATA_LIMIT — decoded fonts, ToUnicode maps, colour profiles,
#     shading and function samples: bytes a consumer reads but does not
#     parse as operators;
#   * IMAGE_PIXEL_LIMIT — the declared pixels of every image the
#     rasterizer would decode (a 12 MP phone scan is a third of it), and
#     IMAGE_DATA_LIMIT the bytes their generic filters inflate to;
#   * MAX_FONTS_PER_PAGE and MAX_OBJECTS_PER_PAGE bound the per-object work
#     a page can ask for, however small each object is.
PAGE_DATA_LIMIT = 24_000_000
IMAGE_PIXEL_LIMIT = 40_000_000
IMAGE_DATA_LIMIT = 200_000_000
MAX_FONTS_PER_PAGE = 200
MAX_OBJECTS_PER_PAGE = 20_000
# pypdf keeps every stream it decodes on the reader's objects for the rest
# of the document, so a per-page data allowance alone lets a few-KB file
# hold that allowance once per page. Text extraction also caps the
# DISTINCT font and map bytes across the whole document — a font every
# page shares counts once, as it is decoded once — and drops pypdf's
# decoded copy after each page, so what stays resident is one page's worth.
DOC_DATA_LIMIT = 64_000_000
# pypdf builds a font's character map (from its ToUnicode CMap, or from
# the embedded program of a Type 1 font without one) and its width table
# once per NAME the font is listed under — not once per font — on every
# page, in the plain pass and again in the layout pass, and all of it
# before the first content operator reaches the operator or time budgets
# above. The layout pass also reads the /Font dict of the page and of
# every /Pages node above it, each one name by name, even when they are
# all one shared dict. So before any extraction each pass's font names are
# counted and charged (_charge_font_names) — a page over budget in the
# plain pass is refused, one over budget in the layout pass gets no layout
# pass:
#   * MAX_FONT_NAMES_PER_PAGE names — a statement page uses a dozen or so;
#   * MAX_FONT_MAP_ENTRIES — the map and width entries those names build.
#     A one-line bfrange of a few bytes builds 65,536 entries, so the
#     decoded size of a map says nothing about this work;
#   * the decoded map and font-program bytes each name re-parses, against
#     PAGE_DATA_LIMIT.
MAX_FONT_NAMES_PER_PAGE = 64
MAX_FONT_MAP_ENTRIES = 400_000
# pypdf refuses a single map past 100,000 entries, so no font is charged
# more than that; it also bounds the estimate's own work.
_FONT_ENTRY_CAP = 100_001

# Filters pypdf inflates generically. The image codecs (DCT, JPX, JBIG2,
# CCITT) are left to the rasterizer, which decodes them into the image's
# declared dimensions, so those are bounded by the pixel budget instead.
_GENERIC_FILTERS = frozenset((
    "/FlateDecode", "/Fl", "/LZWDecode", "/LZW", "/ASCIIHexDecode", "/AHx",
    "/ASCII85Decode", "/A85", "/RunLengthDecode", "/RL"))
# Links that lead out of what a page draws — back up the page tree, to
# other pages or annotations, to actions and metadata — are not followed.
_NEVER_FOLLOW = frozenset((
    "/Parent", "/P", "/Pg", "/Popup", "/IRT", "/Dest", "/A", "/AA", "/Next",
    "/Prev", "/First", "/Last", "/Kids", "/Metadata", "/Thumb", "/Annots",
    "/StructParent", "/StructParents", "/PieceInfo"))
_NOT_DRAWN_TYPES = frozenset(("/Page", "/Pages", "/Catalog", "/Annot"))
# Keys inside a font that text extraction reads; the rasterizer reads all.
_TEXT_FONT_KEYS = frozenset((
    "/ToUnicode", "/Encoding", "/FontDescriptor", "/FontFile",
    "/DescendantFonts"))
_COMPONENTS = {"/DeviceGray": 1, "/G": 1, "/CalGray": 1, "/Indexed": 1,
               "/I": 1, "/Separation": 1, "/Pattern": 1, "/DeviceRGB": 3,
               "/RGB": 3, "/CalRGB": 3, "/Lab": 3, "/DeviceCMYK": 4,
               "/CMYK": 4}


class _OverBudget(Exception):
    pass


def _decoded_len(stream) -> int:
    """Decoded size of `stream`, measured WITHOUT keeping the result:
    get_data() caches the decoded bytes on the object, and the reader keeps
    every object it resolves, so measuring a few hundred small streams that
    each inflate to megabytes that way would itself hold gigabytes."""
    if isinstance(stream, EncodedStreamObject) and stream.decoded_self is None:
        return len(pypdf.filters.decode_stream_data(stream))
    return len(stream.get_data())


def _filters(stream) -> list[str]:
    f = stream.get("/Filter")
    f = f.get_object() if f is not None else None
    if isinstance(f, list):
        return [str(x.get_object()) for x in f]
    return [str(f)] if f is not None else []


def _components(cs) -> int:
    cs = cs.get_object() if cs is not None else None
    if isinstance(cs, list) and cs:
        head = str(cs[0].get_object())
        if head == "/ICCBased" and len(cs) > 1:
            n = cs[1].get_object().get("/N")
            return int(n) if isinstance(n, int) and 0 < n <= 32 else 4
        if head == "/DeviceN" and len(cs) > 1:
            names = cs[1].get_object()
            return max(1, min(len(names), 32)) if isinstance(names, list) else 4
        return _COMPONENTS.get(head, 4)
    # A named colour space defined in the resources, or none at all (a JPX
    # image carries its own), is charged as the widest common one.
    return _COMPONENTS.get(str(cs), 4) if cs is not None else 4


def _int(v, lo: int, hi: int, default: int) -> int:
    v = v.get_object() if v is not None else None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return max(lo, min(int(v), hi))
    return default


class _PageBudget:
    """One walk over everything a consumer would decode for a page, charged
    against the budgets above and refused (_OverBudget) at the first
    addition that crosses one — never after decoding the lot, because the
    decoding is the expensive part.

    `raster` names the consumer. Text extraction parses the page's content
    streams and the forms they draw, and reads each font's ToUnicode map
    and Type 1 font file; it never decodes an image. The rasterizer draws
    all of that plus images, soft masks, patterns, shadings, Type 3 glyph
    procedures, every embedded font program and each annotation's
    appearance stream, so for it every stream reachable from the page's
    resources and annotations is measured.

    `sizes` carries measured stream sizes between pages of one document,
    so a font or form every page shares is decoded once, as pypdf itself
    would, while still counting against each page that uses it."""

    def __init__(self, raster: bool, sizes: dict[int, int] | None = None):
        self.raster = raster
        self.sizes = {} if sizes is None else sizes
        self.content = self.data = self.pixels = self.image_bytes = 0
        self.forms = self.fonts = self.objects = 0
        self.new_data = 0            # data bytes no earlier page measured
        self.data_streams: list = []
        self.over = False
        self.seen: set[int] = set()

    def _size(self, stream) -> int:
        key = id(stream)
        if key not in self.sizes:
            self.sizes[key] = _decoded_len(stream)
        return self.sizes[key]

    def _charge_content(self, stream) -> None:
        self.forms += 1
        if self.forms > MAX_FORMS_PER_PAGE:
            raise _OverBudget
        self.content += self._size(stream)
        if self.content > PAGE_CONTENT_LIMIT:
            raise _OverBudget

    def _charge_data(self, stream) -> None:
        fresh = id(stream) not in self.sizes
        n = self._size(stream)
        if fresh:
            self.new_data += n
        self.data_streams.append(stream)
        self.data += n
        if self.data > PAGE_DATA_LIMIT:
            raise _OverBudget

    def _charge_image(self, img) -> None:
        w = _int(img.get("/Width"), 0, 1 << 31, 0)
        h = _int(img.get("/Height"), 0, 1 << 31, 0)
        self.pixels += w * h
        if self.pixels > IMAGE_PIXEL_LIMIT:
            raise _OverBudget
        if img.get("/ImageMask") is True:
            bpc, comps = 1, 1
        else:
            bpc = _int(img.get("/BitsPerComponent"), 1, 16, 8)
            comps = _components(img.get("/ColorSpace"))
        declared = (w * comps * bpc + 7) // 8 * h
        filters = _filters(img)
        generic = 0
        while generic < len(filters) and filters[generic] in _GENERIC_FILTERS:
            generic += 1
        if not generic:
            return
        if generic == len(filters):
            measured = img
        else:
            # Only the generic filters ahead of an image codec are inflated
            # here; the rasterizer inflates those in full before decoding.
            measured = EncodedStreamObject()
            measured._data = img._data
            measured[NameObject("/Filter")] = ArrayObject(
                NameObject(f) for f in filters[:generic])
            parms = img.get("/DecodeParms")
            parms = parms.get_object() if parms is not None else None
            if isinstance(parms, list):
                measured[NameObject("/DecodeParms")] = ArrayObject(
                    parms[:generic])
        try:
            n = _decoded_len(measured)
        except LimitReachedError:
            # A single generic filter is decoded row by row into the
            # declared image, so one that legitimately inflates past the
            # decoder cap is still bounded by the pixel budget; anything
            # else that does is a bomb.
            if len(filters) == 1 and declared > STREAM_LIMIT:
                n = STREAM_LIMIT
            else:
                raise _OverBudget
        # Image data that inflates well past what the declared dimensions
        # can hold is not a picture of anything.
        if n > declared + h + 65_536:
            raise _OverBudget
        self.image_bytes += n
        if self.image_bytes > IMAGE_DATA_LIMIT:
            raise _OverBudget

    def walk(self, root, role: str) -> None:
        """Roles: 'content' — a stream parsed as operators (or a dict of
        them: a /XObject or /CharProcs dict, an appearance dict);
        'resources' — a resource dict; 'font' — a font dict or a part of
        one; 'any' — everything else the rasterizer reads."""
        stack = [(root, role, None)]
        while stack:
            obj, role, key = stack.pop()
            obj = obj.get_object() if obj is not None else None
            if not isinstance(obj, (DictionaryObject, list)):
                continue
            if id(obj) in self.seen:
                continue
            self.seen.add(id(obj))
            self.objects += 1
            if self.objects > MAX_OBJECTS_PER_PAGE:
                raise _OverBudget
            if isinstance(obj, list):
                stack.extend((x, role, key) for x in obj)
                continue
            if obj.get("/Type") in _NOT_DRAWN_TYPES:
                continue
            if isinstance(obj, StreamObject):
                if obj.get("/Subtype") == "/Image":
                    if self.raster:
                        self._charge_image(obj)
                        stack.extend(self._children(obj, "any"))
                    continue
                # pypdf draws every XObject that is not an image as a
                # form, whatever its /Subtype says, so role decides.
                if (role == "content" or "/Resources" in obj
                        or obj.get("/Subtype") == "/Form"
                        or obj.get("/PatternType") == 1):
                    self._charge_content(obj)
                    stack.extend(self._children(obj, "any"))
                else:
                    self._charge_data(obj)
                    stack.extend(self._children(obj, role))
                continue
            if role == "font" and (obj.get("/Type") == "/Font"
                                   or "/BaseFont" in obj):
                self.fonts += 1
                if self.fonts > MAX_FONTS_PER_PAGE:
                    raise _OverBudget
            stack.extend(self._children(obj, role))

    def _children(self, obj, role: str):
        for key, value in obj.items():
            if key in _NEVER_FOLLOW or key in ("/Length", "/Filter"):
                continue
            if key == "/Resources":
                yield value, "resources", key
            elif role == "resources":
                if key == "/XObject":
                    yield value, "content", key
                elif key == "/Font":
                    yield value, "fontdict", key
                elif self.raster:
                    yield value, "any", key
            elif role == "fontdict":
                yield value, "font", key
            elif role == "font":
                if key == "/CharProcs":
                    if self.raster:
                        yield value, "content", key
                elif self.raster or key in _TEXT_FONT_KEYS:
                    yield value, "font", key
            elif role == "content" and not isinstance(obj, StreamObject):
                yield value, "content", key
            elif self.raster:
                yield value, "any", key


def _measure_page(page, raster: bool = False,
                  sizes: dict[int, int] | None = None) -> _PageBudget:
    """The budget walk over one page; `over` is set as soon as any budget
    above is crossed, and the walk stops there."""
    budget = _PageBudget(raster, sizes)
    try:
        budget.walk(page.get("/Contents"), "content")
        budget.walk(page.get_inherited("/Resources", None), "resources")
        if raster:
            budget.walk(page.get("/Group"), "any")
            annots = page.get("/Annots")
            annots = annots.get_object() if annots is not None else None
            for annot in annots if isinstance(annots, list) else ():
                annot = annot.get_object()
                if isinstance(annot, DictionaryObject):
                    budget.walk(annot.get("/AP"), "content")
    except _OverBudget:
        budget.over = True
    return budget


def _prepare_cmap(data: bytes) -> bytes:
    """`data` rewritten the way pypdf rewrites a ToUnicode CMap before it
    parses it line by line: each begin/end keyword on its own line, every
    <hex> string reduced to its bare digits, and [ ] split off as tokens.
    Counting from the same lines is what keeps the estimate honest — pypdf
    reads `0000 FFFF 0061` without angle brackets just as it reads
    `<0000> <FFFF> <0061>`."""
    cm = (data.strip()
          .replace(b"beginbfchar", b"\nbeginbfchar\n")
          .replace(b"endbfchar", b"\nendbfchar\n")
          .replace(b"beginbfrange", b"\nbeginbfrange\n")
          .replace(b"endbfrange", b"\nendbfrange\n")
          .replace(b"<<", b"\n{\n")
          .replace(b">>", b"\n}\n"))
    parts = cm.split(b"<")
    for i, part in enumerate(parts):
        j = part.find(b">")
        if j >= 0:
            content = b"." if j == 0 else part[:j].replace(b" ", b"")
            parts[i] = content + b" " + part[j + 1:]
    return (b" ".join(parts).replace(b"[", b" [ ").replace(b"]", b" ]\n ")
            .replace(b"\r", b"\n"))


def _cmap_entries(data: bytes) -> int:
    """How many entries pypdf's ToUnicode parser builds from `data`, read
    with its own line rules (_prepare_cmap): a bfchar line is one entry per
    pair of tokens; a bfrange line `lo hi dst` is hi - lo + 1, and
    `lo hi [ ... ]` one per listed string, the list running on across
    lines until its `]`. A line pypdf would skip as broken counts nothing.
    Stops at _FONT_ENTRY_CAP."""
    entries = 0
    in_range = in_char = in_list = False
    for line in _prepare_cmap(data).split(b"\n"):
        line = line.strip(b" \t")
        if not line or line[0] == 37:          # blank or a % comment
            continue
        line = line.replace(b"\t", b" ")
        if b"beginbfrange" in line:
            in_range = True
        elif b"endbfrange" in line:
            in_range = False
        elif b"beginbfchar" in line:
            in_char = True
        elif b"endbfchar" in line:
            in_char = False
        elif in_range:
            lst = line.split()
            if in_list:
                for tok in lst:
                    if tok == b"]":
                        in_list = False
                        break
                    entries += 1
            else:
                try:
                    lo, hi = int(lst[0], 16), int(lst[1], 16)
                    if lst[2] == b"[":
                        in_list = True
                        for tok in lst[3:]:
                            if tok == b"]":
                                in_list = False
                                break
                            entries += 1
                    else:
                        int(lst[2], 16)
                        entries += min(max(0, hi - lo + 1), _FONT_ENTRY_CAP)
                except (ValueError, IndexError):
                    pass
        elif in_char:
            entries += len([x for x in line.split(b" ") if x]) // 2
        if entries >= _FONT_ENTRY_CAP:
            return _FONT_ENTRY_CAP
    return entries


def _width_entries(w) -> int:
    """The entries a CID font's /W array expands to, read the way pypdf
    reads it: `c [w1 w2 ...]` or `c_first c_last w`."""
    w = w.get_object() if w is not None else None
    if not isinstance(w, list):
        return 0
    n, i, count = 0, 0, len(w)
    while i < count and n < _FONT_ENTRY_CAP:
        first = w[i].get_object()
        nxt = w[i + 1].get_object() if i + 1 < count else None
        if isinstance(first, (int, float)) and isinstance(nxt, list):
            n += len(nxt)
            i += 2
        elif (isinstance(first, (int, float))
              and isinstance(nxt, (int, float)) and i + 2 < count):
            n += max(0, min(int(nxt) - int(first) + 1, _FONT_ENTRY_CAP))
            i += 3
        else:
            i += 1
    return min(n, _FONT_ENTRY_CAP)


def _stream_bytes(stream) -> bytes:
    """Decoded data of `stream` without leaving it cached (_decoded_len)."""
    if isinstance(stream, EncodedStreamObject) and stream.decoded_self is None:
        return pypdf.filters.decode_stream_data(stream)
    return stream.get_data()


def _font_cost(font) -> tuple[int, int]:
    """(map and width entries, decoded bytes) pypdf builds and parses each
    time it reads `font` under one name."""
    entries = data = 0
    tu = font.get("/ToUnicode")
    tu = tu.get_object() if tu is not None else None
    if isinstance(tu, StreamObject):
        raw = _stream_bytes(tu)
        data += len(raw)
        entries += _cmap_entries(raw)
    elif font.get("/Subtype") == "/Type1":
        # with no map, pypdf reads the encoding out of the font program
        fd = font.get("/FontDescriptor")
        fd = fd.get_object() if fd is not None else None
        if isinstance(fd, DictionaryObject):
            for key in ("/FontFile", "/FontFile3"):
                ff = fd.get(key)
                ff = ff.get_object() if ff is not None else None
                if isinstance(ff, StreamObject):
                    data += _decoded_len(ff)
    widths = font.get("/Widths")
    widths = widths.get_object() if widths is not None else None
    if isinstance(widths, list):
        entries += len(widths)
    descendants = font.get("/DescendantFonts")
    descendants = (descendants.get_object() if descendants is not None
                   else None)
    for d in descendants if isinstance(descendants, list) else ():
        d = d.get_object()
        if isinstance(d, DictionaryObject):
            entries += _width_entries(d.get("/W"))
    return entries, data


class _FontCharge:
    """Names, map entries and decoded bytes pypdf builds for one pass over
    one page, charged one name at a time; `add` raises _OverBudget at the
    first name that crosses a budget, before measuring that font."""

    def __init__(self, costs: dict[int, tuple[int, int]]):
        self.costs = costs
        self.names = self.entries = self.data = 0

    def add(self, font) -> None:
        self.names += 1
        if self.names > MAX_FONT_NAMES_PER_PAGE:
            raise _OverBudget
        if id(font) not in self.costs:
            self.costs[id(font)] = _font_cost(font)
        e, n = self.costs[id(font)]
        self.entries += e
        self.data += n
        if self.entries > MAX_FONT_MAP_ENTRIES or self.data > PAGE_DATA_LIMIT:
            raise _OverBudget


def _charge_font_names(page, costs: dict[int, tuple[int, int]]) -> bool:
    """Charge the font NAMES a page makes pypdf build maps for, in each of
    the two passes. `costs` keeps each font's measured cost for the rest of
    the document, so a font every page shares is decoded once here; it is
    still charged once per name on every page, because that is how often
    pypdf builds it.

    The plain pass reads the page's (inherited) resources and those of the
    forms and Type 3 fonts they hold; a page over budget there is refused
    (_OverBudget). The layout pass reads only the /Font dict at the page
    and at EVERY /Pages node above it, rebuilding each name at each node —
    and since pypdf copies inherited resources onto the page, one dict
    shared by the page and its ancestors is the normal case, read once per
    node. Its hints are optional, so a page over budget there returns False
    (no layout pass) instead of being refused."""
    _charge_plain_fonts(page.get_inherited("/Resources", None), costs)
    layout = _FontCharge(costs)
    node, chain = page, set()
    try:
        while isinstance(node, DictionaryObject) and id(node) not in chain:
            chain.add(id(node))
            if len(chain) > MAX_OBJECTS_PER_PAGE:
                raise _OverBudget
            res = node.get("/Resources")
            res = res.get_object() if res is not None else None
            fonts = (res.get("/Font") if isinstance(res, DictionaryObject)
                     else None)
            fonts = fonts.get_object() if fonts is not None else None
            if isinstance(fonts, DictionaryObject):
                for ref in fonts.values():
                    font = ref.get_object() if ref is not None else None
                    if isinstance(font, DictionaryObject):
                        layout.add(font)
            parent = node.get("/Parent")
            node = parent.get_object() if parent is not None else None
    except _OverBudget:
        return False
    return True


def _charge_plain_fonts(root, costs: dict[int, tuple[int, int]]) -> None:
    """The plain pass's charge (_charge_font_names): every font name in the
    resource dict `root` and in the resources of what it holds, each dict
    read once."""
    charge = _FontCharge(costs)
    visited = 0
    seen: set[int] = set()
    stack = [root]
    while stack:
        res = stack.pop()
        res = res.get_object() if res is not None else None
        if not isinstance(res, DictionaryObject) or id(res) in seen:
            continue
        seen.add(id(res))
        visited += 1
        if visited > MAX_OBJECTS_PER_PAGE:
            raise _OverBudget
        for key, group in res.items():
            group = group.get_object() if group is not None else None
            if not isinstance(group, DictionaryObject):
                continue
            for ref in group.values():
                visited += 1
                if visited > MAX_OBJECTS_PER_PAGE:
                    raise _OverBudget
                obj = ref.get_object() if ref is not None else None
                if not isinstance(obj, DictionaryObject):
                    continue
                if key == "/Font":
                    charge.add(obj)
                # A font drawn by glyph procedures, a form, or any other
                # named object carrying resources of its own can list
                # more fonts; following all of them overcounts at worst.
                if obj.get("/Subtype") != "/Image":
                    stack.append(obj.get("/Resources"))


def _page_content_bytes(page, raster: bool = False,
                        sizes: dict[int, int] | None = None) -> int:
    """Decoded size of everything a page's consumer would parse as
    operators — its content streams and every form, appearance, pattern
    and glyph procedure it can draw — or PAGE_CONTENT_LIMIT + 1 as soon as
    that, or any other budget above, is crossed. Each object is counted
    once; one drawn many times is bounded by the operator budget instead.
    See _PageBudget for what `raster` adds."""
    budget = _measure_page(page, raster, sizes)
    if budget.over:
        return max(budget.content, PAGE_CONTENT_LIMIT + 1)
    return budget.content


def first_page_within_budget(data: bytes) -> None:
    """Refuse, with ValueError, a PDF whose first page would make the
    rasterizer inflate more than the budgets above allow. pdfium caps
    nothing it decodes — content, images, fonts, appearance streams — so
    a 40 KB bomb attached as a receipt would be inflated there instead.
    Measuring uses pypdf's capped decoders (set at import) and keeps none
    of what it decodes, so the check itself stays small."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) == 0:
            raise ValueError("the PDF has no pages")
        if (_page_content_bytes(reader.pages[0], raster=True)
                > PAGE_CONTENT_LIMIT):
            raise ValueError("this PDF's first page expands past the "
                             "size limit")
    except LimitReachedError:
        raise ValueError("this PDF expands past the size limit")
    except ValueError:
        raise
    except Exception as e:                        # noqa: BLE001
        raise ValueError(f"couldn't read this PDF ({type(e).__name__})")



def _extract(data: bytes, *, with_layout: bool
             ) -> tuple[str, list[str], list[str | None]]:
    """The page loop behind extract_text / extract_statement: the joined
    plain text, each page's plain text, and — when `with_layout` asks —
    each page's layout rendering (None for a page the layout budgets
    above left out; an empty list when not asked, or when the file as a
    whole went past them)."""
    ops = 0
    page_text_ops = 0
    over_budget = False
    deadline = time.monotonic() + EXTRACT_SECONDS

    def count_operation(operator=None, operands=None, *_args) -> None:
        # Called by pypdf for every operator, inside forms too, so this
        # bounds the repeated-form fan-out the byte budget cannot see.
        # pypdf logs and swallows an error raised while it draws a form,
        # so the trip is sticky: every later operator raises at once (the
        # unwinding costs one call per nesting level) and the flag is
        # checked again after the page returns.
        nonlocal ops, over_budget, page_text_ops
        ops += 1
        if operator in _LAYOUT_TEXT_OPERATORS:
            # a TJ array places each of its strings and kerns separately
            if (operator == b"TJ" and operands
                    and isinstance(operands[0], list)):
                page_text_ops += max(1, len(operands[0]))
            else:
                page_text_ops += 1
        if (over_budget or ops > MAX_OPERATIONS
                or time.monotonic() > deadline):
            over_budget = True
            raise ValueError(_TOO_BIG_MSG)

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")      # empty user password still opens
            except Exception:
                ok = None
            if not ok:
                raise ValueError(_ENCRYPTED_MSG)
        if len(reader.pages) > MAX_PAGES:
            raise ValueError(
                f"this PDF has {len(reader.pages)} pages — statements are "
                f"far smaller; split it or export CSV instead")
        parts, total, content, doc_data, sizes = [], 0, 0, 0, {}
        font_costs: dict[int, tuple[int, int]] = {}
        # per page: its layout rendering, None (no columns to read there),
        # or False (the time budget ran out before it — signs unknown)
        laid_parts: list[str | None | bool] = []
        layout_ok = with_layout and _layout_page is not None
        layout_spent = 0.0
        layout_bytes = 0
        layout_dropped = False     # past the output cap: no hints at all
        time_cut = False           # the time budget, not the page, said no
        for page in reader.pages:
            try:
                layout_fonts_ok = _charge_font_names(page, font_costs)
            except _OverBudget:
                raise ValueError(_TOO_BIG_MSG) from None
            budget = _measure_page(page, sizes=sizes)
            content += budget.content
            doc_data += budget.new_data
            if (budget.over or content > DOC_CONTENT_LIMIT
                    or doc_data > DOC_DATA_LIMIT):
                raise ValueError(_TOO_BIG_MSG)
            page_text_ops = 0
            chunk = page.extract_text(
                visitor_operand_before=count_operation) or ""
            laid = None
            eligible = (layout_fonts_ok and not over_budget
                        and page_text_ops <= LAYOUT_TEXT_OPS)
            if layout_ok and eligible and time.monotonic() < deadline:
                # the same page rendered at its run positions, within the
                # layout budgets above. Its time runs on the same clock as
                # the plain pass, so the two together stay inside
                # EXTRACT_SECONDS: a layout pass that runs long leaves the
                # next page's plain pass less time, never more.
                started = time.monotonic()
                try:
                    laid = page.extract_text(extraction_mode="layout") or ""
                except Exception:                  # noqa: BLE001
                    laid = None
                spent = time.monotonic() - started
                layout_spent += spent
                layout_bytes += len(laid or "")
                if layout_bytes > LAYOUT_TEXT_BYTES:
                    layout_ok, layout_dropped, laid = False, True, None
                elif layout_spent > LAYOUT_SECONDS:
                    layout_ok = False     # this page keeps its rendering
                    time_cut = True
            elif (with_layout and _layout_page is not None and eligible
                  and not layout_dropped
                  and (time_cut or time.monotonic() >= deadline)):
                # A page the CLOCK denied hints is not a page with no
                # columns: on a loaded host the same statement would get
                # them. False (not None) tells the caller this page's
                # signs are unknown, so a re-import keeps whatever sign an
                # earlier import stored for its lines instead of letting
                # the wall clock decide the ledger.
                time_cut = True
                laid = False
            if with_layout:
                laid_parts.append(laid)
            # get_data() left each font and map it decoded cached on the
            # reader's objects; drop those copies so the next page starts
            # from nothing resident (a shared font is simply re-decoded).
            for stream in budget.data_streams:
                if isinstance(stream, EncodedStreamObject):
                    stream.decoded_self = None
            if over_budget:
                raise ValueError(_TOO_BIG_MSG)
            total += len(chunk)
            if total > MAX_TEXT_BYTES:       # decompression-bomb ceiling
                raise ValueError(
                    "this PDF expands past the text-size limit — export a "
                    "CSV or OFX/QFX from your bank instead")
            parts.append(chunk)
        text = "\n".join(parts)
    except ValueError:
        raise
    except LimitReachedError as e:           # a pypdf decoder hit STREAM_LIMIT
        raise ValueError(_TOO_BIG_MSG) from e
    except MemoryError:                      # the child's address-space cap
        raise ValueError(_TOO_BIG_MSG) from None
    except Exception as e:                   # malformed / not a PDF at all
        raise ValueError(
            f"could not read this PDF ({e.__class__.__name__}) — export a "
            "CSV or OFX/QFX from your bank instead") from e
    if not text.strip():
        raise ValueError(_NO_TEXT_MSG)
    return text, parts, ([] if layout_dropped else laid_parts)

# ---- the child process ------------------------------------------------------

# Tests only: run the extraction in this process (see the module docstring).
IN_PROCESS = False

# The child's address-space allowance, over what the interpreter has
# mapped when it starts. Measured: a real 60-page statement with a dozen
# fonts grows the address space by about 55 MB (the pypdf import itself
# by about 50), so this is several times what any statement needs, while
# a font map built once per name for a hundred names (well over a GB)
# stops here. It is counted from start-up, not from zero, because the
# interpreter's own start-up reservations differ by build — a few MB in
# the slim container, hundreds on a desktop distribution's Python.
CHILD_MEMORY = 384 * 1024 * 1024
# Children one process runs at once. Two at their full allowance plus the
# server can exceed a small container's memory limit; what keeps the
# server safe then is that each child asks the kernel to be the first
# one killed when memory runs out (oom_score_adj below), so the worst
# case is one refused upload, never a restarted app.
CHILD_SLOTS = 2
# Over EXTRACT_SECONDS for interpreter start-up and the pypdf import; the
# extraction's own deadline normally ends it well inside this.
CHILD_GRACE_SECONDS = 8.0
# The largest result the extraction's own caps let through: every char of
# the plain text and of the layout rendering at four UTF-8 bytes, plus
# JSON escaping of control characters and the per-page framing.
MAX_RESULT_BYTES = 6 * (MAX_TEXT_BYTES + LAYOUT_TEXT_BYTES) + 1_000_000

_BUSY_MSG = ("too many PDFs are being read right now — try again in a "
             "minute")

_slots = threading.BoundedSemaphore(CHILD_SLOTS)

# Run by the child interpreter with -I (no environment variables, no user
# site, not the working directory on sys.path). The limits are set before
# pypdf, or anything else, is imported; the address-space one is the
# allowance above what the interpreter already maps (see CHILD_MEMORY).
_BOOT = """\
import sys
root, src, dst, mode, mem, cpu = sys.argv[1:7]
mem, cpu = int(mem), int(cpu)
try:
    with open("/proc/self/status") as f:
        mem += next(int(ln.split()[1]) * 1024 for ln in f
                    if ln.startswith("VmSize:"))
except (OSError, StopIteration, ValueError):
    mem += 256 * 1024 * 1024
try:
    with open("/proc/self/oom_score_adj", "w") as f:
        f.write("1000")
except OSError:
    pass
try:
    import resource
except ImportError:
    pass
else:
    # never above an inherited hard limit (a lower ulimit would make
    # setrlimit raise and every PDF refused), and no core dump on abort
    def _cap(kind, soft, hard):
        _, h = resource.getrlimit(kind)
        if h != resource.RLIM_INFINITY:
            soft, hard = min(soft, h), min(hard, h)
        resource.setrlimit(kind, (soft, hard))
    _cap(resource.RLIMIT_AS, mem, mem)
    _cap(resource.RLIMIT_CPU, cpu, cpu + 2)
    _cap(resource.RLIMIT_CORE, 0, 0)
sys.path.insert(0, root)
from oikonome.sync.pdf_sandbox import _child_main
sys.exit(_child_main(src, dst, mode))
"""

_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _child_command(src: str, dst: str, mode: str) -> list[str]:
    cpu = int(EXTRACT_SECONDS + CHILD_GRACE_SECONDS) + 1
    return [sys.executable, "-I", "-c", _BOOT, _PACKAGE_ROOT, src, dst,
            mode, str(CHILD_MEMORY), str(cpu)]


def _child_main(src: str, dst: str, mode: str) -> int:
    """The child's whole life: read the PDF, extract, write the result.
    A refusal the extraction raises itself travels back as its message;
    running out of memory exits non-zero, which the parent reads as the
    file being too big."""
    logging.disable(logging.WARNING)       # pypdf warns per broken object
    try:
        with open(src, "rb") as f:
            data = f.read()
        try:
            _text, parts, laid = _extract(data, with_layout=mode == "layout")
            result = {"parts": parts, "laid": laid}
        except ValueError as e:
            result = {"error": str(e)}
        payload = json.dumps(result, ensure_ascii=False).encode(
            "utf-8", "surrogatepass")
        with open(dst, "wb") as f:
            f.write(payload)
    except MemoryError:
        return 3
    return 0


def _kill(proc: subprocess.Popen) -> None:
    """Kill the child's whole process group and reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    proc.wait()


def run(data: bytes, *, with_layout: bool
        ) -> tuple[str, list[str], list[str | None | bool]]:
    """_extract(data, with_layout=...) in a child process: the same result,
    the same ValueError messages for an encrypted, image-only or
    unreadable file, and the size-limit refusal for a child that runs out
    of memory, CPU or time, crashes, or answers with anything malformed."""
    if IN_PROCESS:
        return _extract(data, with_layout=with_layout)
    wait = EXTRACT_SECONDS + CHILD_GRACE_SECONDS
    if not _slots.acquire(timeout=wait):
        raise ValueError(_BUSY_MSG)
    try:
        with tempfile.TemporaryDirectory(prefix="oikonome-pdf-") as tmp:
            src = os.path.join(tmp, "in.pdf")
            dst = os.path.join(tmp, "out.json")
            with open(src, "wb") as f:
                f.write(data)
            result = _run_child(src, dst, "layout" if with_layout
                                else "plain", tmp, wait)
    finally:
        _slots.release()
    if "error" in result:
        error = result["error"]
        raise ValueError(error if isinstance(error, str) and error
                         else _TOO_BIG_MSG)
    parts, laid = result.get("parts"), result.get("laid")
    if (not isinstance(parts, list) or not isinstance(laid, list)
            or not all(isinstance(p, str) for p in parts)
            or not all(p is None or p is False or isinstance(p, str)
                       for p in laid)):
        raise ValueError(_TOO_BIG_MSG)
    return "\n".join(parts), parts, laid


def _run_child(src: str, dst: str, mode: str, tmp: str,
               wait: float) -> dict:
    err_path = os.path.join(tmp, "err.txt")
    with open(err_path, "wb") as err:
        # a fixed argv (this interpreter, a constant script, paths this
        # function made): no shell and nothing from the request
        proc = subprocess.Popen(  # noqa: S603
            _child_command(src, dst, mode), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=err, cwd=tmp,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "LC_ALL": "C.UTF-8"},
            close_fds=True, start_new_session=True)
        try:
            code = proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            _kill(proc)
            log.warning("PDF extraction child passed its %.0f s deadline",
                        wait)
            raise ValueError(_TOO_BIG_MSG) from None
        except BaseException:
            _kill(proc)
            raise
    if code != 0:
        log.warning("PDF extraction child exited %s: %s", code,
                    _tail(err_path))
        raise ValueError(_TOO_BIG_MSG)
    try:
        size = os.path.getsize(dst)
        if size > MAX_RESULT_BYTES:
            raise ValueError(_TOO_BIG_MSG)
        with open(dst, "rb") as f:
            result = json.loads(f.read().decode("utf-8", "surrogatepass"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError,
            RecursionError):
        raise ValueError(_TOO_BIG_MSG) from None
    if not isinstance(result, dict):
        raise ValueError(_TOO_BIG_MSG)
    return result


def _tail(path: str, limit: int = 2_000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - limit))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""
