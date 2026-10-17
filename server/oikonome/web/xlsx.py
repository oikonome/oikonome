"""A minimal .xlsx writer: one sheet, header row, text / number / date
cells, no styles, no dependency.

Why hand-rolled: the only spreadsheet the product writes is a table
someone opens in Excel, Numbers or Calc; the full OOXML libraries pull in
a stack this image does not otherwise carry, and a workbook is a zip of
five small XML parts. Strings are written inline (no shared-string
table), which every reader accepts and keeps the writer stateless.

Cells are typed from the Python value: int/float become numbers, bool a
boolean cell, date/datetime an ISO string (a true date cell needs a style
part; the ISO text sorts and filters the same), None an empty cell, and
anything else its str(). Text starting like a formula is the CALLER's
problem (see api._csv_safe): an inline string is never evaluated by a
spreadsheet, so no escaping is needed here, but a value that is later
pasted elsewhere still deserves the prefix.
"""
from __future__ import annotations

import datetime as dt
import io
import zipfile
from html import escape

_CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
       '<Default Extension="xml" ContentType="application/xml"/>'
       '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
       '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
       '</Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
         '</Relationships>')
_WB_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>')
_WB = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
       '<sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets></workbook>')

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _col(n: int) -> str:
    """0-based column index → A, B, …, Z, AA, AB, …"""
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _clean(text: str) -> str:
    # XML 1.0 forbids most control characters outright; a bank feed has
    # been known to carry one, and a reader refuses the whole workbook
    return "".join(ch for ch in text
                   if ch in "\t\n\r" or ord(ch) >= 32)


def _cell(ref: str, v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return f'<c r="{ref}" t="b"><v>{int(v)}</v></c>'
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):
            return ""
        return f'<c r="{ref}"><v>{v!r}</v></c>'
    if isinstance(v, (dt.date, dt.datetime)):
        v = v.isoformat()
    text = escape(_clean(str(v)))
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_name(sheet_name: str) -> str:
    return "".join(ch for ch in sheet_name if ch not in '[]:*?/\\')[:31] or "Sheet1"


def _row_xml(r: int, vals) -> str:
    cells = "".join(_cell(f"{_col(c)}{r}", v) for c, v in enumerate(vals))
    return f'<row r="{r}">{cells}</row>'


_SHEET_HEAD = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
               '<sheetData>')
_SHEET_TAIL = '</sheetData></worksheet>'


def workbook(sheet_name: str, header: list, rows) -> bytes:
    """Bytes of a one-sheet workbook: `header` on row 1, each item of
    `rows` (any iterable of sequences) on the rows below. `sheet_name` is
    cut to Excel's 31-character limit and stripped of the characters it
    forbids."""
    name = _sheet_name(sheet_name)
    parts = [_SHEET_HEAD]
    for r, vals in enumerate([list(header)] + [list(x) for x in rows], start=1):
        parts.append(_row_xml(r, vals))
    parts.append(_SHEET_TAIL)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", _WB.format(name=escape(name)))
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        z.writestr("xl/worksheets/sheet1.xml", "".join(parts))
    return buf.getvalue()


class _Sink:
    """A write-only file for zipfile: what is written is handed out by
    take(). Having no seek() makes zipfile write each member's sizes after
    its data (a data descriptor), which is what lets the archive leave as
    it is produced instead of being assembled whole first."""

    def __init__(self):
        self._parts: list[bytes] = []

    def write(self, b) -> int:
        self._parts.append(bytes(b))
        return len(b)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out = b"".join(self._parts)
        self._parts.clear()
        return out


def stream_workbook(sheet_name: str, header: list, batches):
    """workbook(), produced piece by piece: a generator of bytes whose
    concatenation is a one-sheet workbook. `batches` is an iterable of
    row lists; each batch is written into the deflated sheet and the
    compressed bytes so far are yielded, so only one batch of rows (and
    the compressor's window) is held at a time, however long the sheet."""
    name = _sheet_name(sheet_name)
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", _WB.format(name=escape(name)))
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        with z.open("xl/worksheets/sheet1.xml", "w") as sheet:
            sheet.write((_SHEET_HEAD + _row_xml(1, list(header))).encode())
            yield sink.take()
            r = 1
            for batch in batches:
                for vals in batch:
                    r += 1
                    sheet.write(_row_xml(r, list(vals)).encode())
                yield sink.take()
            sheet.write(_SHEET_TAIL.encode())
    yield sink.take()
