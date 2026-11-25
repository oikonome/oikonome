"""Restore an Oikonome export ZIP into the current tenant — the other half
of /export: a self-hosted export imports into a hosted tenant and vice
versa, so nothing about the data is locked to one instance.

Restores: items (as sync-less shells — tokens never travel; reconnect
aggregators after migrating), accounts, transactions (original ids,
idempotent), recurring bills, category overrides, reimbursement pairs +
flags, business flags, tenant settings, and — converter v2 (migration
008) — liabilities, holdings, crypto_holdings, networth_recorded,
networth_snapshot, income_annual, income_documents, merchant_canonical,
merchant_renames, merchant_categories, amazon_orders/matches/summaries,
costco_receipts/matches. Also the offers the household has already
answered — bill_proposals and merchant_merge_proposals — and the
import batches its file imports are tagged with, which is what keeps
those imports reversible on the destination.
Skipped: alerts history, sync logs, job heartbeats, staged uploads
(operational residue).
Every insert is ON CONFLICT on the original ids — re-restoring the same
ZIP is a no-op. A row already here is kept, with one exception: where the
destination holds a MACHINE row (a seed or model category rule, a derived
alias, a bill's pin, an offer still pending) and the archive holds the
PERSON's row for the same key, the person's wins. A merchant the
destination already holds under its own id for the same aggregator entity
is the archive's merchant, and the archive's pointers are mapped onto it.
Counts report rows WRITTEN, each one a row its insert returned (`_Outcomes.
land`); every archived row not written is counted once, under the reason
(REFUSALS: already present, orphaned, out of range, malformed, over cap,
merged, rebuilt), so a restore into a populated tenant can't claim it
imported the whole ZIP and a refusal can't pass for a duplicate.
An archive older than a migration that rewrote stored rows is brought up
to date by the steps `archive_upgrades` keys by migration name. The passes
apply them to every archive, whatever its `manifest.json` claims: a cut
of card numbers or an amount rule binds every row a restore writes, and
an archive is text anyone can edit. No migration file is ever run here.
"""

from __future__ import annotations

import contextvars
import csv
import datetime as _dt
import io
import json
import logging
import math
import re
import uuid
import zipfile
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from . import account_sources as _account_sources
from . import base as _sync_base
from . import archive_upgrades as _upgrades
from . import card_number
from . import upload_zip as _upload_zip
from ..engine.compat import MAX_AMOUNT, as_date, asset_day, strip_nul
from ..engine.compat import jsonb as _compat_jsonb
from ..engine import bills as _bills
from ..engine import budget as _budget

log = logging.getLogger(__name__)


# A restore member's DECLARED byte size (bounded by _check_zip_sizes) does
# NOT bound peak memory: materialized into dicts, a 128 MB CSV of millions
# of tiny rows (compressing to well under the 50 MB upload cap) balloons to
# multiple GB of live objects. Stream lazily from the ZIP (never read the
# whole member) and cap the row count. A personal-finance export is a few
# tens of thousands of rows; the cap is orders of magnitude above that.
MAX_ROWS_PER_MEMBER = 2_000_000


# The largest legitimate cell is a receipt image: base64 of a MAX_IMAGE
# upload, ~6.8 MB. Python's csv module refuses fields over 128 KB by
# default, so one photo receipt would fail the whole receipts member. The limit is process-wide, so it is
# raised once, here, to exactly what the biggest honest cell needs; the
# per-row checks below still refuse anything bigger than a live upload.
_MAX_CELL = 8 * 1024 * 1024
csv.field_size_limit(max(csv.field_size_limit(), _MAX_CELL))


# Progress reporting for a restore running as a background job. A
# ContextVar rather than a threaded-through argument because _rows() is
# called from three dozen places and the alternative is editing every
# one of them to carry a value only one caller ever sets. Set inside the
# worker thread (a new thread starts from an empty context), so a
# synchronous restore in the request path reports nothing and pays
# nothing.
_PROGRESS: contextvars.ContextVar = contextvars.ContextVar(
    "oikonome_restore_progress", default=None)
_TICK_EVERY = 500


def _tick(table: str, rows: int, *, done: bool = False) -> None:
    cb = _PROGRESS.get()
    if cb is not None:
        try:
            cb(table, rows, done)
        except Exception:                                # noqa: BLE001
            log.debug("restore progress callback failed", exc_info=True)


# csv limits each field, never the number of fields, and a member's
# declared size can sit at MAX_MEMBER_BYTES while the ZIP is a few hundred
# KB on the wire: one line of commas deflates to almost nothing and becomes
# tens of millions of empty cells — gigabytes, in the web process every
# household shares — before the row cap has counted it as one row. So each
# member is read through _MemberLines below: the header under a budget no
# real header comes near and a column limit far above the widest table the
# export writes, every data RECORD (all its lines, since a quoted field may
# hold newlines) under a budget sized to the largest honest cell, and a
# record whose commas alone would make it absurdly wide is refused before
# csv builds a cell of it. Counting every comma, quoted ones included,
# over-counts only for a cell holding a list of a million values, which no
# export writes; what it bounds is the list csv would otherwise build.
MAX_HEADER_CHARS = 64 * 1024
MAX_COLUMNS = 200
MAX_RECORD_CHARS = _MAX_CELL + 1024 * 1024
MAX_RECORD_COMMAS = 1_000_000
# an unquoted comma or two in a hand-edited cell is the honest overflow
MAX_ROW_OVERFLOW = 50


class _ArchiveRefused(ValueError):
    """The whole archive is refused: nothing in it lands (the restore is
    one transaction). Raised only by a rule that judges the archive, never
    by a reader meeting one odd cell, so a row's reader can let this
    through while it costs a cell it cannot read only its row. A
    ValueError, so every door reports the sentence to the person."""


def _not_an_export(name: str, why: str) -> _ArchiveRefused:
    return _ArchiveRefused(f"{name}: {why} — this doesn't look like a "
                           "genuine Oikonome export")


class _MemberLines:
    """The lines of one archive member, read under a character and comma
    budget per CSV record. csv pulls lines only while it assembles one
    record, and _MemberReader calls reset() before asking for each, so
    everything spent between two resets belongs to that record. Every
    read asks the text layer for at most what is left of the budget, so an
    oversized line is discovered without ever being held whole."""

    def __init__(self, fh, name: str):
        self.fh, self.name = fh, name
        self.reset(MAX_HEADER_CHARS)

    def reset(self, chars: int = MAX_RECORD_CHARS) -> None:
        self.budget = self.left = chars
        self.commas = MAX_RECORD_COMMAS

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = self.fh.readline(self.left + 1)
        if not line:
            raise StopIteration
        self.left -= len(line)
        self.commas -= line.count(",")
        if self.left < 0:
            raise _not_an_export(
                self.name, "a record is longer than "
                f"{self.budget // 1024:,} KB")
        if self.commas < 0:
            raise _not_an_export(
                self.name, f"a record has more than {MAX_RECORD_COMMAS:,} "
                "separators")
        return line


class _MemberReader:
    """The csv.reader under a DictReader, refreshing the line budget before
    each record and refusing a header wider than MAX_COLUMNS or a row more
    than MAX_ROW_OVERFLOW cells past it — before it becomes a dict."""

    def __init__(self, it, lines: _MemberLines):
        self.it, self.lines = it, lines
        self.width: int | None = None

    def __iter__(self):
        return self

    def __next__(self):
        row = next(self.it)
        if self.width is None:
            if len(row) > MAX_COLUMNS:
                raise _not_an_export(
                    self.lines.name, f"the header has {len(row):,} columns "
                    f"(max {MAX_COLUMNS})")
            self.width = len(row)
        elif len(row) > self.width + MAX_ROW_OVERFLOW:
            raise _not_an_export(
                self.lines.name, f"line {self.it.line_num:,} has "
                f"{len(row):,} cells under a {self.width}-column header")
        self.lines.reset()              # a fresh budget for the next record
        return row

    @property
    def line_num(self):
        return self.it.line_num

    @property
    def dialect(self):
        return self.it.dialect


def _rows(z: zipfile.ZipFile, name: str):
    """Yield CSV rows one at a time — O(row) memory, hard row cap.

    Every cell comes back with the export's spreadsheet-formula quote
    removed. The export writes EVERY text cell through the same guard, so
    undoing it for a few named columns left the apostrophe on everything
    else — a note, a receipt line, a memo that began with a minus sign came
    back with a quote in front of it after every round trip."""
    try:
        fh = z.open(name)
    except KeyError:
        return
    moved_txns = _MOVED_TXNS.get()
    id_cells = (("id", "pending_transaction_id") + _TXN_ID_CELLS
                if name == "transactions.csv" else _TXN_ID_CELLS)
    list_cells = _TXN_ID_LIST_CELLS.get(name, ())
    nul_cells = _NUL_KEY_CELLS.get()
    nul_sample = _NUL_KEY_SAMPLE.get()
    with fh:
        # newline="": csv does its own line handling, and a universal-
        # newline read would turn a CR inside a quoted cell into a line feed
        lines = _MemberLines(io.TextIOWrapper(fh, encoding="utf-8",
                                              newline=""), name)
        reader = csv.DictReader(lines)
        reader.reader = _MemberReader(reader.reader, lines)
        _tick(name, 0)
        try:
            i = -1
            for i, row in enumerate(reader):
                if i >= MAX_ROWS_PER_MEMBER:
                    raise _ArchiveRefused(
                        f"{name}: too many rows (limit "
                        f"{MAX_ROWS_PER_MEMBER:,}) — this doesn't look like "
                        "a genuine Oikonome export")
                if i and i % _TICK_EVERY == 0:
                    _tick(name, i)
                # U+0000 cannot be stored in text and means nothing in
                # any cell; a jsonb cell's escaped one is dropped on write
                out = {strip_nul(k): _unescape(strip_nul(v))
                       for k, v in row.items()}
                if nul_cells is not None:
                    # a JSON cell can spell a NUL as an escape, in a key;
                    # _j drops such a value and names its file and column.
                    # Only the row being landed is kept, per member: the
                    # map is the size of one row, never of the archive.
                    nul_cells[name] = {
                        v: col for col, v in out.items()
                        if isinstance(v, str) and _NUL_ESCAPE in v}
                    if nul_sample is not None:
                        # a lander that reads its member whole before it
                        # lands a row still gets the first few named
                        for v, col in nul_cells[name].items():
                            if len(nul_sample) >= _NUL_SAMPLE_MAX:
                                break
                            if len(v) <= _NUL_SAMPLE_TEXT_MAX:
                                nul_sample.setdefault(v, (name, col))
                if moved_txns:
                    for cell in id_cells:
                        v = out.get(cell)
                        if v and v in moved_txns:
                            out[cell] = moved_txns[v]
                    for cell in list_cells:
                        if out.get(cell):
                            out[cell] = _carry_id_list(out[cell], moved_txns)
                yield out
            _tick(name, i + 1, done=True)
        except csv.Error as e:
            # an over-wide cell (or a torn CSV) is a bad file, not a crash
            raise _ArchiveRefused(f"{name}: {e} — this doesn't look like a "
                             "genuine Oikonome export")
        except UnicodeDecodeError:
            # the export writes UTF-8: bytes that are not are a bad file,
            # said in words, not the codec's account of the byte
            raise _not_an_export(name, "it is not UTF-8 text") from None


# Per member, the cells of the row last read from it whose text spells a
# NUL escape, by text: column, so a value _j drops can be named. A lander
# lands each row before the next is read, so the row _j is handed is the
# one recorded. Set per restore (_restore); None elsewhere, where _j still
# drops such a value.
_NUL_KEY_CELLS: contextvars.ContextVar = contextvars.ContextVar(
    "oikonome_restore_nul_cells", default=None)
# and, for a lander that reads a member whole before landing it, the first
# _NUL_SAMPLE_MAX such cells of the restore (each at most
# _NUL_SAMPLE_TEXT_MAX characters), by text: (member, column)
_NUL_KEY_SAMPLE: contextvars.ContextVar = contextvars.ContextVar(
    "oikonome_restore_nul_sample", default=None)
_NUL_SAMPLE_MAX = 64
_NUL_SAMPLE_TEXT_MAX = 64 * 1024
# the (member, column) of each value _j dropped for a NUL in a key
_NUL_KEY_DROPS: contextvars.ContextVar = contextvars.ContextVar(
    "oikonome_restore_nul_drops", default=None)
_NUL_ESCAPE = "\\u0000"


def _nul_in_a_key(v) -> bool:
    """Whether a key anywhere in the parsed document holds U+0000."""
    stack = [v]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            for k, y in x.items():
                if isinstance(k, str) and "\x00" in k:
                    return True
                stack.append(y)
        elif isinstance(x, list):
            stack.extend(x)
    return False


def _j(v):
    if not v:
        return None
    try:
        got = json.loads(v)
    except (ValueError, RecursionError):
        # a cell the reader cannot walk is malformed, never fatal
        return None
    if isinstance(v, str) and _NUL_ESCAPE in v and _nul_in_a_key(got):
        # A key holding a NUL is never rewritten (compat.strip_nul: a key
        # check must not see one spelling and the store keep another), and
        # no genuine export writes one. The value is malformed: it lands as
        # an empty cell would, and the rest of the archive with it; the
        # summary names its file and column.
        drops = _NUL_KEY_DROPS.get()
        if drops is not None:
            where = ("an archive file", "a JSON cell")
            for member, cells in (_NUL_KEY_CELLS.get() or {}).items():
                if v in cells:
                    where = (member, cells[v])
                    break
            else:
                where = (_NUL_KEY_SAMPLE.get() or {}).get(v, where)
            drops.add(where)
        return None
    if isinstance(v, str) and _LONE_SURROGATE_ESCAPE.search(v):
        # JSON may spell half of a surrogate pair on its own ("\ud800"),
        # which no UTF-8 text holds: PostgreSQL refuses it in jsonb and
        # psycopg cannot send it as text, so one such cell would end the
        # whole restore. It reads as the replacement character.
        try:
            got = _no_lone_surrogates(got)
        except RecursionError:
            return None
    return got


_LONE_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F][0-9a-fA-F]{2}")
_SURROGATE = re.compile("[\ud800-\udfff]")


def _no_lone_surrogates(v):
    if isinstance(v, str):
        return _SURROGATE.sub("\ufffd", v)
    if isinstance(v, dict):
        return {_no_lone_surrogates(k): _no_lone_surrogates(x)
                for k, x in v.items()}
    if isinstance(v, list):
        return [_no_lone_surrogates(x) for x in v]
    return v


def jsonb(v):
    """Every jsonb value this module writes, with non-finite numbers read
    as null. The ONE pass for the class: PostgreSQL refuses NaN and
    Infinity in jsonb, the restore is a single transaction, and one such
    token in any column an archive fills would cost the household every
    other row of the export. A cleaner that judges a number (a goal's
    target, a frozen budget) sees the NaN and refuses its entry first;
    everything no cleaner reads is nulled here, at the write."""
    return _compat_jsonb(_finite_only(v))


def _finite_only(v):
    """`v` with every non-finite number in it read as null. Python's JSON
    reader takes NaN and 1e999 and PostgreSQL's jsonb refuses them (see
    jsonb above); the cleaners have already judged the amounts they
    read. A value nested past what the walk can reach is malformed as a
    whole and reads as null: one cell, never the whole archive. Every
    caller (the jsonb writes and the snapshot readers) gets that here."""
    try:
        return _finite_walk(v)
    except RecursionError:
        return None


def _finite_walk(v):
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _finite_walk(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite_walk(x) for x in v]
    return v


# The longest figure, count or day cell a restore reads. The export writes
# none longer than a few dozen characters (a float's repr is at most 24, a
# moment with its zone 32), and a cell may be megabytes: every reader of a
# figure or a day refuses a longer cell by its length BEFORE any pattern or
# parser sees it. The restore runs in the web process and a regular
# expression holds the interpreter lock, so one pattern that backtracks on
# a long cell stalls every household the process serves; the length cap is
# the guard that does not depend on each pattern being written right.
MAX_FIGURE_CELL = 64


def _too_long(v) -> bool:
    return isinstance(v, str) and len(v) > MAX_FIGURE_CELL


# A figure as the export writes it: ASCII digits, a point, an exponent.
# float() also reads "1_000" and other scripts' digits, which no export
# holds; the words for a non-finite number are let through to float() so
# they refuse the archive below. Each run of digits has one way to match
# (the point is not optional between two runs), so a failing match is
# linear in the cell.
_FIGURE = re.compile(r"\s*[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
                     r"(?:[eE][+-]?[0-9]+)?\s*", re.ASCII)
_NON_FINITE = re.compile(r"\s*[+-]?(?:nan|inf|infinity)\s*",
                         re.ASCII | re.IGNORECASE)


def _f(v):
    """CSV cell → float or None ('' travels for NULL).

    float() happily reads 'nan', 'inf' and '1e999'. Every other ingest door
    refuses them, and they must not slip in here: a non-finite value in a
    money column makes every JSON reply that carries the row fail
    (allow_nan=False) and every total over it NaN or infinite. No genuine
    export writes one, so the archive is refused — the restore is a single
    transaction, so nothing lands."""
    if v in (None, ""):
        return None
    if _too_long(v):
        raise _NotANumber(v[:20], over="a value that is not a number")
    if isinstance(v, str) and not (_FIGURE.fullmatch(v)
                                   or _NON_FINITE.fullmatch(v)):
        raise _NotANumber(str(v)[:20], over="a value that is not a number")
    try:
        f = float(v)
    except (TypeError, ValueError) as e:
        # text where a number belongs: that one row's cell, never the
        # archive (the reader refuses the row as malformed)
        raise _NotANumber(str(v)[:20], over="a value that is not a number"
                          ) from e
    if not math.isfinite(f):
        raise _ArchiveRefused(f"a number in this archive is not finite ({str(v)[:20]!r}) "
                         "— this doesn't look like a genuine Oikonome export")
    return f


class _BadValue(ValueError):
    """One row's cell the column cannot take; `over` says what was wrong
    with it, for the summary."""

    def __init__(self, cell: str, over: str = ""):
        super().__init__(cell)
        self.over = over


class _AmountOutOfRange(_BadValue):
    """One row's value is past what its column holds: an amount larger
    than any real sum (see `_money`), or past a bounded column (see
    `_numeric`), whose bound `over` names for the summary."""


class _NotANumber(_BadValue):
    """One row's figure is text, not a number (see `_numeric`)."""


class HouseholdBeingDeleted(_ArchiveRefused):
    """The household's erasure has begun: a restore writes nothing."""


def _money(v):
    """A money cell: `_f`, and no larger than any real sum. 1e308 is
    finite and survives _f, but every cents conversion of it overflows —
    the matchers then fail the household's hourly and nightly passes on
    every run. Older import doors accepted such values, so a genuine
    archive can hold one: it raises _AmountOutOfRange, and the reader
    skips that one row and counts it (see `_out_of_range_note`) rather
    than refusing the whole archive."""
    f = _f(v)
    if f is not None and abs(f) > MAX_AMOUNT:
        raise _AmountOutOfRange(str(v)[:20])
    return f


def _balance(v):
    """An account balance cell: `_money`, except that one beyond any real
    sum is left empty rather than the row skipped. The account has to land,
    or every transaction and bill on it goes too; the next sync or a hand
    edit fills the balance in again."""
    try:
        return _money(v)
    except _AmountOutOfRange:
        log.warning("restore: an account balance beyond any real sum "
                    "(%s) was left empty", str(v)[:20])
        return None


def _out_of_range_note(n: int) -> str:
    return (f"{n} row{'s' if n != 1 else ''} with an amount larger than "
            f"any real sum (over {MAX_AMOUNT:,.0f}) "
            f"{'were' if n != 1 else 'was'} left out")


def _numeric(v, what: str, precision: int, scale: int):
    """A cell bound for a NUMERIC(precision, scale) column: `_f`, and no
    more whole digits than the column holds once rounded to its scale.
    Such a value is finite, so `_f` passes it, but the column refuses it
    and the refusal rolls back the whole restore; it raises
    _AmountOutOfRange instead, naming `what` and the column's bound, and
    the reader refuses that one row or leaves the value empty."""
    try:
        f = _f(v)
    except ValueError as e:
        if isinstance(v, str) and _is_non_finite(v):
            raise              # the archive-wide rule in `_f`
        raise _NotANumber(str(v)[:20], over=f"{what} that is not a number"
                          ) from e
    if f is None:
        return f
    if not _fits_numeric(f, precision, scale):
        bound = Decimal(10) ** (precision - scale) - Decimal(1).scaleb(-scale)
        raise _AmountOutOfRange(str(v)[:20],
                                over=f"{what} over {bound:,.{scale}f}")
    return f


def _fits_numeric(v, precision: int, scale: int) -> bool:
    """Whether a NUMERIC(precision, scale) column takes `v` (a float or a
    Decimal) as psycopg sends it: a float8 the server reads at 15
    significant digits, a Decimal exactly; either rounded half away from
    zero to the column's scale."""
    limit = Decimal(10) ** (precision - scale)
    if isinstance(v, float):
        if not math.isfinite(v) or abs(v) >= limit:
            return False
        v = Decimal(f"{v:.15g}")
    elif not v.is_finite() or abs(v) >= limit:
        return False
    d = v.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
    return abs(d) < limit


def _is_non_finite(v: str) -> bool:
    try:
        return not math.isfinite(float(v))
    except ValueError:
        return False


def _bounded_notes(over: dict, *, left: str) -> list[str]:
    return [f"{n} row{'s' if n != 1 else ''} with {what} "
            f"{'were' if n != 1 else 'was'} {left}"
            for what, n in sorted(over.items())]


class _Unreadable(ValueError):
    """One row's required cell does not read as its type: that row is
    refused as malformed, never the archive."""


# A day as the export writes it, alone or leading a moment
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def _as_exported(v) -> str | None:
    """The cell's text when it spells a day or a moment the way the export
    writes one, else None. A day read from its first ten characters took
    an impossible hour (a "T25:00" after the day) or trailing text as
    that day, and the compact eight-digit form, which no export writes, as
    it too."""
    if not isinstance(v, str) or _too_long(v):
        return None
    t = v.strip()
    if not _DAY.match(t):
        return None
    if len(t) > 10:
        try:
            _dt.datetime.fromisoformat(t)
        except ValueError:
            return None
    return t


def _opt_date(v):
    """An optional date cell, or None when it is blank or unreadable: the
    row keeps everything else it says."""
    if v in (None, ""):
        return None
    if isinstance(v, str):
        v = _as_exported(v)
        if v is None:
            return None
    try:
        return as_date(v)
    except (ValueError, TypeError):
        return None


def _req_date(v):
    """A required date cell; _Unreadable when it is blank or unreadable,
    so the reader refuses that row as malformed and the rest lands."""
    d = _opt_date(v)
    if d is None:
        raise _Unreadable(str(v)[:20])
    return d


# the migration that gave each wage figure a record of what the W-2s wrote
_W2_FIGURE_MARKS = "226_a_w2_figure_remembers_what_it_wrote.sql"


def _i(v, default=None):
    """CSV cell → int (accepts '1.0'-style floats) or default. A non-finite
    cell refuses the archive the way _f does ('inf' would otherwise escape
    as OverflowError, which the restore door does not report as a bad file)."""
    return int(_f(v)) if v not in (None, "") else default


def _b(v):
    """CSV bool cell ("True"/"t"/"1") → Python bool. Module scope, so every
    reader in this file can reach it."""
    return str(v).strip().lower() in ("true", "t", "1")


def _ts(v):
    """CSV cell → datetime or None. Accepts sqlite datetime('now') form
    ('YYYY-MM-DD HH:MM:SS'), ISO-T, and bare dates. psycopg dumps str as
    text (which won't coerce to timestamptz) — so parse here."""
    if not v:
        return None
    if isinstance(v, str):
        v = _as_exported(v)
        if v is None:
            return None
    try:
        return _dt.datetime.fromisoformat(str(v))
    except ValueError:
        try:
            return _dt.datetime.combine(as_date(v), _dt.time())
        except (ValueError, TypeError):
            return None


# credential-shaped config keys — the export scrubs these (pages.py), the
# restore keeps the DESTINATION's values. Shared by the merge below.
_SECRET_SUBSTR = ("secret", "token", "password", "client_id",
                  "access_url", "api_key", "extra_body")

# Policy keys neither an export nor a restore ZIP may carry —
# the demo lockdown pair (demo_login holds the shared plaintext password;
# flipping demo_mode opens every data door) and the SMTP cleartext opt-out
# (a ZIP setting smtp_starttls:false would silently downgrade mail to
# cleartext). The destination's own values always win.
# Keys a restore must never carry IN: they are decisions about THIS
# instance, not data about the household. taxdocs_allow_remote_llm is the
# sharpest of them — it is the consent that lets a W-2 image (Social
# Security number and all) leave for a remote model, and a ZIP that set it
# would grant that consent on the importing instance without anyone saying
# yes. Consent is given here or not at all.
# notify_phone is the same class: it is a VERIFIED, CONSENTED destination —
# the worker sends the household's budget verdict wherever it says, on the
# strength of `verified: true`. Verification and consent happen on this
# instance (a code sent to the number and typed back) or not at all; a ZIP
# that carried them would plant any number as verified. Both the record
# and its pending half stay behind; the owner re-verifies after a move.
# plaid_enrich_used is this instance's own running count of the Enrich rows
# Plaid billed this month — the one thing holding a household to its cap.
# It describes what the instance spent, not what the household owns, so the
# destination's count always wins: an old export would otherwise hand back a
# spent month, and a hand-edited negative count would lift the cap entirely,
# billed to whoever owns the Plaid keys.
# outbound_paused is the record a credential rotation writes: the mail
# relay, recipients and AI endpoint stand down until the owner presses
# Resume (notify/outbound_pause). It is this instance's decision about
# this instance's settings, never data: an archive that carried it could
# lift a pause by merging a record that names nothing (the Resume door
# asks for a step-up; a restore must not be the way round it), or plant a
# stale pause from the instance it was taken on.
# The merchant city list is an operator's knob, set in the stored settings
# where the operator works: the settings door neither shows nor takes it,
# and the bounds it used to apply went with it, so an archive (hand-edited,
# or another household's) must not be the way to set it either — a long
# list is expanded into variants and run against every merchant name in
# the nightly pass. The list of cities the pass has seen is its own
# bookkeeping, rewritten by the pass.
_POLICY_KEYS = ("demo_mode", "demo_login", "smtp_starttls",
                "taxdocs_allow_remote_llm", "notify_phone",
                "notify_phone_pending", "plaid_enrich_used",
                "outbound_paused", "merchant_strip_cities",
                "merchant_cities_seen")

# Settings that describe a CREDENTIAL rather than the household: which
# aggregator environment a set of keys belongs to, and the user those keys
# created at the aggregator. An archive never carries the keys themselves
# (see _SECRET_SUBSTR), so a restore that let these in would pair the
# destination's own keys with the source's environment and user — MX calls
# made with this instance's keys against another client's user, or
# production Plaid keys sent to the sandbox host. They stay with the keys
# they describe: the destination's own value, or none.
# The `<provider>_keys_saved_at` stamps are the same class: they date when
# THIS instance's keys were saved, and the bank-link hold's banner shows
# them so the owner can tell whether the held keys predate the pause. An
# archive's stamp would date keys it never carried — held keys would read
# as saved at a time they were not.
_CREDENTIAL_COMPANION_KEYS = ("plaid_env", "mx_env", "mx_user_guid",
                              "plaid_keys_saved_at", "mx_keys_saved_at")


# What makes up the relay and the AI endpoint: a restore or bundle that
# leaves all of these as they were brought no relay or endpoint in, so it
# has nothing new to pause — whatever is there is the destination's own,
# set (and, if it was saved after the pause, confirmed) by the owner here.
_RELAY_KEYS = ("smtp_host", "smtp_user", "smtp_from")
_LLM_KEYS = ("llm_url", "llm_roles", "llm_backends")


def _owner_addresses(conn) -> set[str]:
    """The current tenant's owner addresses, lowercased. The owner is
    mailed by construction, so their own address is never a recipient a
    pause holds back."""
    if conn is None:
        return set()
    row = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS tid").fetchone()
    tid = row["tid"] if row else None
    if not tid:
        return set()
    from ..web import mailguard
    return mailguard.owner_addresses(tid)


def widen_armed_pause(before: dict, after: dict, conn=None) -> None:
    """While a credential rotation's pause is armed, whatever outbound
    setting an import brings in is paused with the rest.

    The pause names what was set when the credential rotated; a restore or
    a connections bundle applied during it can bring a mail relay, extra
    recipients or an AI endpoint the record never named, and those would
    send at once while the settings the owner has yet to confirm stay
    held. So the record is widened (never narrowed, as a second rotation
    widens it) by what the import CHANGED: the relay when `after`'s relay
    differs from `before`'s, the AI endpoint likewise, and every recipient
    `before` did not already list — the ones already there were the
    owner's own or confirmed since. A relay the owner saved here after the
    pause is theirs, so an import that leaves it as it was must not stand
    it down. The owner's own address is never held (`conn`, the tenant
    connection, is how it is found). The record's time and reason are
    kept, so the banner still names the rotation the owner has to answer.
    Mutates `after`; nothing when no pause is armed in `before`."""
    from ..notify import outbound_pause
    prior = outbound_pause.paused(before)
    if prior is None:
        return
    now = outbound_pause._what_is_set(after, "")
    was = outbound_pause._what_is_set(before, "")
    added = set(now["recipients"]) - set(was["recipients"])
    if added:
        added -= _owner_addresses(conn)
    relay_changed = any(before.get(k) != after.get(k) for k in _RELAY_KEYS)
    llm_changed = any(before.get(k) != after.get(k) for k in _LLM_KEYS)
    after[outbound_pause.KEY] = {
        **(before.get(outbound_pause.KEY) or {}),
        "at": prior["at"], "reason": prior["reason"],
        "smtp": prior["smtp"] or (now["smtp"] and relay_changed),
        "recipients": sorted(set(prior["recipients"]) | added),
        "llm": prior["llm"] or (now["llm"] and llm_changed)}


# Who started this restore. A restore runs for minutes and applies the
# settings LAST, long after the step-up that let it start; a credential
# rotation in between ends the starter's session. If the session (or
# device token) that started the restore is gone by the settings step,
# whoever started it is no longer signed in, and what the archive brings in
# for the mail relay, recipients or AI endpoint must not start sending: a
# rotation with nothing outbound set leaves no pause record for
# widen_armed_pause to widen, so this is the only place that can tell.
# It must be the STARTING credential, not the household's sessions: a
# password change keeps the changer's own session and leaves every other
# member's alone, so "some session survives" is true through exactly the
# rotation this exists for. The value is ("cred", fingerprint) whenever the
# restore was started by a request that carried a credential, or
# ("household", {session ids}) only when it was started outside one (a
# command-line restore: every session gone still means the starter is
# gone; a survivor proves less). A starter whose credential is already gone
# when the restore begins stops it outright (StarterSignedOut) — never a
# fall back to the household's sessions, whose survivor is the very
# session a rotation keeps.
_START_SESSIONS: contextvars.ContextVar = contextvars.ContextVar(
    "restore_start_sessions", default=None)


def _tenant_of(conn) -> str | None:
    row = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS tid").fetchone()
    return (row["tid"] if row else None) or None


def _live_sessions(conn) -> frozenset:
    """Ids of the current tenant's unexpired sessions, read on the control
    connection (sessions are control-plane). Empty when the tenant is not
    pinned or the read fails, which disarms the check rather than pausing
    a restore nobody signed in started (a CLI or test restore)."""
    try:
        tid = _tenant_of(conn)
        if not tid:
            return frozenset()
        from ..db import tenancy
        cc = tenancy.control_connect()
        try:
            return frozenset(str(r["id"]) for r in cc.execute(
                "SELECT id FROM sessions WHERE tenant_id=%s "
                "AND expires_at > now()", (uuid.UUID(str(tid)),)).fetchall())
        finally:
            cc.close()
    except Exception:                                    # noqa: BLE001
        log.warning("restore: could not read the household's sessions",
                    exc_info=True)
        return frozenset()


def _live_credentials(conn, fingerprint: str) -> frozenset:
    """Token hashes of this tenant's unexpired sessions and device tokens
    whose hash begins with `fingerprint` (the request's credential
    fingerprint is that prefix — see web.security.credential_fingerprint).
    Empty when nothing matches or the read fails.

    A rotation DELETEs sessions but only marks device tokens revoked (the
    row stays for the audit trail, its expiry untouched), so a device
    token counts only while unrevoked — the same rule its resolver
    applies — or a restore started from the phone would never be paused."""
    try:
        tid = _tenant_of(conn)
        if not tid or not fingerprint:
            return frozenset()
        from ..db import tenancy
        cc = tenancy.control_connect()
        try:
            return frozenset(r["h"] for r in cc.execute(
                "SELECT token_hash AS h FROM sessions WHERE tenant_id=%s"
                " AND expires_at > now() AND left(token_hash, %s) = %s"
                " UNION ALL "
                "SELECT token_hash FROM device_tokens WHERE tenant_id=%s"
                " AND revoked_at IS NULL"
                " AND expires_at > now() AND left(token_hash, %s) = %s",
                (uuid.UUID(str(tid)), len(fingerprint), fingerprint,
                 uuid.UUID(str(tid)), len(fingerprint), fingerprint),
            ).fetchall())
        finally:
            cc.close()
    except Exception:                                    # noqa: BLE001
        log.warning("restore: could not read the starter's credential",
                    exc_info=True)
        return frozenset()


class StarterSignedOut(_ArchiveRefused):
    """The credential that started this restore was gone before the restore
    began. A ValueError, so the job reports the sentence to the person."""

    def __init__(self):
        super().__init__(
            "the restore did not start: you were signed out after uploading "
            "(a password, login email or second-factor change ends other "
            "sessions), so nothing was applied — sign in and upload the file "
            "again")


def _starter_watch(conn, starter: str | None):
    """What pause_if_starter_signed_out watches for this restore (see
    _START_SESSIONS). `starter` is the starting request's credential
    fingerprint; None reads it from the request in flight.

    A starter whose credential no longer resolves raises StarterSignedOut
    before a row is written. The restore runs on a worker thread that
    first waits for its connections and the household's sync lock, and a
    rotation landing in that wait ends the starter's session while keeping
    the rotating owner's own; looking past the starter to "any live
    session" would read that survivor as the starter still being here."""
    if starter is None:
        try:
            from ..web import security
            starter = security.current_credential()
        except Exception:                                # noqa: BLE001
            starter = ""
    if starter:
        if not _live_credentials(conn, starter):
            raise StarterSignedOut()
        return ("cred", starter)
    live = _live_sessions(conn)
    return ("household", live) if live else None


def pause_if_starter_signed_out(before: dict, after: dict, conn) -> None:
    """Arm an outbound pause over what this restore brought in when every
    session that started it has since gone (see _START_SESSIONS). Only
    what the restore CHANGED is held, exactly as widen_armed_pause holds it
    during an armed pause; the owner's own address is never held. Nothing
    when a pause is already armed (widen_armed_pause covers it), when the
    restore started with no session to watch, or when one survives."""
    from ..notify import outbound_pause
    watch = _START_SESSIONS.get()
    if not watch or outbound_pause.paused(before) is not None:
        return
    kind, what = watch
    if kind == "cred":
        if _live_credentials(conn, what):
            return
    elif what & _live_sessions(conn):
        return
    now = outbound_pause._what_is_set(after, "")
    was = outbound_pause._what_is_set(before, "")
    added = set(now["recipients"]) - set(was["recipients"])
    if added:
        added -= _owner_addresses(conn)
    smtp = bool(now["smtp"]) and any(
        before.get(k) != after.get(k) for k in _RELAY_KEYS)
    llm = bool(now["llm"]) and any(
        before.get(k) != after.get(k) for k in _LLM_KEYS)
    if not (smtp or added or llm):
        return
    after[outbound_pause.KEY] = {
        "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "reason": outbound_pause.ROTATION,
        "smtp": smtp, "recipients": sorted(added), "llm": llm}


# credentials under a name no substring rule reads: a relay can take its
# API token as the login (Postmark does)
_SECRET_NAMES = frozenset({"smtp_user"})


def _secret_key(k) -> bool:
    """Is this config key credential-shaped? The one carve-out is the exact
    key `tokens` — savings-goal entries carry merchant match WORDS under it
    (user data living in list-of-dict config), and no credential store in
    this config uses that name."""
    return k in _SECRET_NAMES or (
        k != "tokens" and any(s in k for s in _SECRET_SUBSTR))


def _scrub_list(items: list) -> list:
    """Scrub list elements: dicts get the same key scrub as scrub_config
    (llm_backends is the live case — a LIST of dicts each carrying an
    encrypted `api_key` that must never ride an export), nested lists
    recurse, scalars pass through."""
    return [scrub_config(v) if isinstance(v, dict)
            else _scrub_list(v) if isinstance(v, list) else v
            for v in items]


# Config keys whose value is an endpoint URL. A URL can carry a credential
# of its own (a password in the userinfo, a key in the query string), which
# no key-name rule can see; Settings masks those parts on every display
# (llm_categorize.redact_url) and the scrub masks them the same way, so an
# export never carries more than Settings would show a viewer.
_URL_KEYS = ("llm_url", "url")


def scrub_config(cfg: dict) -> dict:
    """Shared export/restore config scrub: drop credential-shaped and
    policy keys, recursing into nested dicts AND lists so neither a nested
    {"password":...} (demo_login's shape) nor a list of dicts each holding
    an api_key (llm_backends' shape) can ever ride along under a key the
    substring match misses. Endpoint URLs keep their host and path, with
    any credential inside them masked."""
    out = {}
    for k, v in cfg.items():
        if _secret_key(k) or k in _POLICY_KEYS:
            continue
        if k in _URL_KEYS and isinstance(v, str):
            from ..engine.llm_categorize import redact_url
            out[k] = redact_url(v)
            continue
        # list-shaped-by-contract keys are normalized, not trusted: a
        # crafted/hand-edited ZIP carrying email_muted as a scalar 500'd
        # every Settings load and silently killed the tenant's scheduled
        # mail. The readers guard too — this keeps
        # the bad shape out of storage in the first place.
        if k == "email_muted":
            if isinstance(v, list):
                out[k] = sorted({str(m).lower() for m in v if str(m).strip()})
            continue
        out[k] = (scrub_config(v) if isinstance(v, dict)
                  else _scrub_list(v) if isinstance(v, list) else v)
    return out


def _settle_masked_urls(cfg: dict, dest: dict) -> None:
    """Never save a masked endpoint URL as a live one.

    An export masks the credential inside every endpoint URL, so what comes
    back holds the mask in its place. When the destination's own URL is the
    one that mask was made from (a restore onto the instance that wrote the
    archive), that URL is kept, credential and all. Otherwise the masked
    value is dropped — the destination's own top-level URL stands, and a
    backend is left without one — because saving the literal mask would
    break the backend with nothing to say why."""
    from ..engine.llm_categorize import URL_SECRET_MASK, unredact_url

    def settle(sent, stored):
        if not isinstance(sent, str) or URL_SECRET_MASK not in sent:
            return sent
        try:
            return unredact_url(sent, [s for s in stored
                                       if isinstance(s, str)])
        except ValueError:
            return None

    if "llm_url" in cfg:
        kept = settle(cfg["llm_url"], [dest.get("llm_url")])
        if kept is None:
            cfg.pop("llm_url")
        else:
            cfg["llm_url"] = kept
    if isinstance(cfg.get("llm_backends"), list):
        prior = {d.get("id"): d for d in dest.get("llm_backends") or []
                 if isinstance(d, dict) and d.get("id") is not None}
        for b in cfg["llm_backends"]:
            if isinstance(b, dict) and "url" in b:
                mine = prior.get(b.get("id")) or {}
                kept = settle(b["url"], [mine.get("url")])
                b["url"] = "" if kept is None else kept


# Every key the AI route is read from (ai_route); a document pair that
# differs in none of them has the same route, and nothing is decrypted.
_AI_ROUTE_KEYS = ("llm_url", "llm_model", "llm_vision_model", "llm_api_key",
                  "llm_extra_body", "llm_backends", "llm_roles",
                  "outbound_paused")


def _credential_digest(conn, stored) -> str:
    """A digest of one stored AI credential's plaintext, held only for the
    comparison and never logged or kept. Decrypted because a save or a
    restore can re-encrypt the same key (a fresh ciphertext is not a new
    credential); a value that cannot be decrypted here is compared as
    stored."""
    import hashlib
    if not stored:
        return ""
    from ..db import crypto
    try:
        plain = crypto.decrypt(conn, stored) or ""
    except Exception:                                    # noqa: BLE001
        plain = str(stored)
    return hashlib.sha256(plain.encode()).hexdigest()


def ai_route(conn, cfg: dict) -> tuple:
    """Where each AI task's text goes: the endpoint, the model, and the
    credential it is sent with (on a router service the key decides whose
    account receives and logs the prompts). Read through the same resolver
    the tasks use, so a document that only respells the route — the legacy
    endpoint folded into the backend list, a masked URL echoed back, the
    same list re-posted, the same key re-encrypted — has the same route."""
    from ..engine.llm_categorize import ROLES, resolve_role
    out = []
    for role in ROLES:
        layer, b = resolve_role(cfg, role)
        if layer == "backend":
            model = ((b.get("vision_model") or b.get("model"))
                     if role == "vision" else b.get("model"))
            url, key, extra = b.get("url"), b.get("api_key"), \
                b.get("extra_body")
        elif layer == "legacy":
            model = ((cfg.get("llm_vision_model") or cfg.get("llm_model"))
                     if role == "vision" else cfg.get("llm_model"))
            url, key, extra = cfg.get("llm_url"), cfg.get("llm_api_key"), \
                cfg.get("llm_extra_body")
        else:
            # the operator's backend; only a household-chosen vision model
            # rides on it
            model = cfg.get("llm_vision_model") if role == "vision" else ""
            url = key = extra = ""
        out.append((role, str(url or "").strip(), str(model or "").strip(),
                    _credential_digest(conn, key),
                    _credential_digest(conn, extra)))
    return tuple(out)


def ai_route_moved(conn, before: dict, after: dict) -> bool:
    """Does `after` send any AI task's text somewhere `before` did not?"""
    before, after = before or {}, after or {}
    if all(before.get(k) == after.get(k) for k in _AI_ROUTE_KEYS):
        return False
    return ai_route(conn, before) != ai_route(conn, after)


def _tell_the_owner_the_ai_route_moved(conn) -> None:
    """The settings save's notice, worded for the act that happened (the
    owner reading it touched no Settings page), in the household owner's
    name: a restore runs as a job with no request, and only an owner may
    start one. Never raises — the restore it reports on has committed."""
    try:
        row = conn.execute("SELECT current_setting('app.tenant_id', true)"
                           " AS tid").fetchone()
        tid = row["tid"] if row else None
        if not tid:
            return
        from ..web import mailguard
        from ..web.app import _notify_security_change
        for email in sorted(mailguard.owner_addresses(tid)):
            _notify_security_change({"email": email, "tenant_id": tid},
                                    "ai_route_restored")
    except Exception:                                    # noqa: BLE001
        log.exception("restore: the AI route notice could not be sent")


# Which setting names the destination each stored credential is sent to.
# A restore carries a credential back only onto the destination it was
# saved for: an archive is a file anyone holding it can edit, and one
# naming another endpoint or relay host under the same entry would
# otherwise hand the stored key to it. `url` is a backend entry's own
# field (inside llm_backends); the rest are top-level keys. smtp_user is
# here because a relay can take its API token as the login.
_DESTINATION_OF = {
    "api_key": "url", "extra_body": "url",
    "llm_api_key": "llm_url", "llm_extra_body": "llm_url",
    "smtp_password": "smtp_host", "smtp_user": "smtp_host",
}


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _destination(v) -> tuple:
    """A destination as compared: an endpoint URL by scheme, host, port
    (the scheme's default written out or left off alike) and path, the host
    case-blind; a bare relay host case-blind, as written. The relay's port
    is its own setting and not part of where the password goes."""
    from urllib.parse import urlsplit
    s = str(v or "").strip()
    try:
        parts = urlsplit(s)
        if parts.scheme and parts.netloc:
            scheme = parts.scheme.lower()
            # the scheme's own port written out is the same address
            port = parts.port
            if port == _DEFAULT_PORTS.get(scheme):
                port = None
            return (scheme, (parts.hostname or "").lower(), port,
                    parts.username or "", parts.path.rstrip("/"),
                    parts.query)
    except ValueError:
        pass
    return ("", s.lower())


def _same_destination(dest: dict, incoming: dict, secret: str) -> bool:
    """May the stored `secret` of `dest` ride onto `incoming`? Yes when
    nothing names where it goes, or when `incoming` names the same place
    `dest` does (or does not name one, leaving the stored one standing)."""
    field = _DESTINATION_OF.get(secret)
    if field is None or field not in incoming:
        return True
    return _destination(dest.get(field)) == _destination(incoming.get(field))


# What a person is told was not carried, in this order, each at most once:
# the clients word the message from these names.
_DROPPED_KIND = {"api_key": "ai_key", "extra_body": "ai_key",
                 "llm_api_key": "ai_key", "llm_extra_body": "ai_key",
                 "smtp_password": "mail_password",
                 "smtp_user": "mail_password"}
_DROPPED_ORDER = ("ai_key", "mail_password")


def dropped_kinds(secrets) -> list[str]:
    """The fixed names for these left-behind credentials, in order."""
    kinds = {_DROPPED_KIND[s] for s in secrets if s in _DROPPED_KIND}
    return [k for k in _DROPPED_ORDER if k in kinds]


def _drop_moved_credentials(dest: dict, archive: dict,
                            merged: dict) -> list[str]:
    """The top-level half of the same rule: the merge keeps a stored flat
    credential the archive does not carry (an export never does), so one
    whose destination the archive moved is taken out here. Answers the
    secrets it took out."""
    dropped = []
    for secret, field in _DESTINATION_OF.items():
        if field == "url" or secret in archive or secret not in merged:
            continue
        if not _same_destination(dest, archive, secret):
            merged.pop(secret)
            dropped.append(secret)
    return dropped


def _backend_credentials_left_behind(dest: dict, merged: dict) -> list[str]:
    """The stored backend secrets `carry_forward_secrets` did not carry onto
    an entry of the same id because the entry's URL moved."""
    prior = {b.get("id"): b for b in dest.get("llm_backends") or []
             if isinstance(b, dict) and b.get("id") is not None}
    left = []
    for b in merged.get("llm_backends") or []:
        was = prior.get(b.get("id")) if isinstance(b, dict) else None
        if not was:
            continue
        left += [k for k in ("api_key", "extra_body")
                 if was.get(k) and not b.get(k)
                 and not _same_destination(was, b, k)]
    return left


class RestoreCounts(dict):
    """A restore's counts (rows written per table, `_notes`), carrying
    beside them the credentials it did not carry back, by their fixed
    names (`dropped_kinds`). Kept off the dict's keys because every key
    there reads as a table's row count."""
    credentials_dropped: list[str] = []
    # the rows no reader could read, per table (`skipped_rows`)
    skipped: list[dict] = []


def carry_forward_secrets(dest, incoming):
    """Put the destination's credential-shaped values back into an incoming
    config value the export scrubbed them out of.

    A ZIP's config is always credential-free. For a FLAT key that costs
    nothing: the key is absent from the ZIP entirely, so the destination's
    own value rides through the merge untouched. For a CONTAINER it is the
    opposite — `llm_backends` arrives as a list that IS present, only with
    every entry's `api_key`/`extra_body` stripped, so merging the ZIP's copy
    over the destination's replaces live credentials with nothing. Exporting
    an instance and restoring it onto itself then silently unconfigures
    every AI backend.

    So match the two sides up — dicts by key, lists of objects by `id` — and
    restore the destination's secrets onto the entries that still exist.
    Everything non-secret stays the ZIP's to define: renames, new URLs and
    removed entries all still land — but a secret is carried only onto the
    destination it was saved for (_same_destination): an entry the ZIP
    moved to another URL lands without it.
    """
    if isinstance(dest, dict) and isinstance(incoming, dict):
        out = dict(incoming)
        for k, v in dest.items():
            if _secret_key(k):
                if k not in out and _same_destination(dest, incoming, k):
                    out[k] = v
            elif k in out:
                out[k] = carry_forward_secrets(v, out[k])
        return out
    if isinstance(dest, list) and isinstance(incoming, list):
        prior = {d["id"]: d for d in dest
                 if isinstance(d, dict) and d.get("id") is not None}
        return [carry_forward_secrets(prior[v["id"]], v)
                if isinstance(v, dict) and v.get("id") in prior else v
                for v in incoming]
    return incoming


# Every ZIP member the restore reads back. The export writes MORE than
# this — every RLS-scoped table minus a skip set — and the difference is
# deliberate: sync logs, job heartbeats and staged uploads are this
# instance's operating state, not the household's data, and restoring them
# would replay half-finished jobs into the destination. The alert log is
# read for its dismissed rows only (the mute is the household's own; the
# rest the next Today build rebuilds). The export's README names which files come
# back and which are kept for the person's records only, from this set.
# `bills` is read under a variable (older archives call it recurring.csv,
# and their bill offers recurring_proposals.csv),
# so a source test that only scans literal `_rows(z, "…")` calls misses it.
RESTORED_MEMBERS: frozenset[str] = frozenset(['account_adoptions', 'alerts_log', 'account_links', 'account_sources', 'accounts', 'amazon_matches', 'amazon_orders', 'amazon_summaries', 'bill_proposals', 'bills', 'budget_snapshots', 'business_entity', 'business_flags', 'business_txn_class', 'compliance_obligation', 'costco_matches', 'costco_receipts', 'crypto_holdings', 'entity_membership', 'equity_movement', 'forecast_nights', 'holdings', 'import_batches', 'income_annual', 'income_documents', 'income_upload_values', 'income_uploads', 'items', 'liabilities', 'manual_categories', 'merchant_canonical', 'merchant_categories', 'merchant_merge_proposals', 'merchant_renames', 'merchants', 'mileage_log', 'networth_recorded', 'networth_snapshot', 'receipt_items', 'receipts', 'recipient_invites', 'recurring', 'recurring_proposals', 'reimburse_flags', 'reimbursements', 'tenant_settings', 'transaction_notes', 'transaction_splits', 'transactions', 'vendor_1099', 'activity_log'])


# A restored activity-log row is held to the size of one the app writes. The
# feed returns every row's target and detail on each read, and a CSV cell may
# be megabytes (_MAX_CELL is sized for a receipt photo), so one crafted row
# would be paid for on every read of the feed from then on. A real target is
# an id; a real detail is a before/after pair of short values. The row is
# the household's record and still lands — clipped, never refused.
ACTIVITY_TARGET_MAX = 200
ACTIVITY_DETAIL_MAX = 16 * 1024          # bytes of JSON
# An alert message is one sentence the app builds; a longer one is crafted.
ALERT_MESSAGE_MAX = 500


# A restored account-id lineage row is read on every file import into its
# account (sync/base FileImportIds), so a crafted archive gets a fixed
# number of them, far above any household's reconnect history, and an id
# no longer than an account id is ever sensibly written.
_MAX_ADOPTION_LINEAGE = 1000
_MAX_ACCOUNT_ID = 512


# A crafted archive must not be able to fill a control-plane table. A live
# recipient list is capped at 20 (mailguard.MAX_RECIPIENTS) and this table
# also keeps a row for every address ever asked, so the ceiling is generous
# — but finite, which is the point.
_MAX_RECIPIENT_INVITES = 500


def _check_config(cfg: dict) -> list[str]:
    """The ZIP's config is merged into this tenant's settings — apply
    the SAME netguard policy as the Settings save and the .oikx bundle
    import. Without this, a crafted export ZIP sets
    llm_url=http://169.254.169.254/… and the worker fetches it: the guard
    bypassed by a third door.

    Endpoints that the guard refuses are DROPPED here, not grounds to
    refuse the archive: an export is a person's whole financial history,
    and the endpoints in it are the most instance-local thing it carries —
    a self-hoster's `http://ollama:11434` or a LAN SMTP relay is
    unreachable from anywhere else by construction, so aborting on one
    would mean a migration between instances could never
    complete for a large export. Dropping is strictly safer than
    merging — the URL never
    lands in stored config, so nothing can fetch it later — and the caller
    reports what was dropped. A MALFORMED payload still raises: that is a
    corrupt file, not an unreachable one.

    Returns human-readable notes naming what was dropped (empty when
    everything survived)."""
    from ..web.netguard import BlockedURL, check_host, check_url
    from .. import homepages
    # the home-page keys are a closed vocabulary the clients redirect to
    # blind — the Settings save checks them, and this door must too
    notes: list[str] = homepages.scrub(cfg)
    if cfg.get("llm_url"):
        try:
            check_url(str(cfg["llm_url"]), what="llm_url in this export")
        except BlockedURL:
            cfg.pop("llm_url", None)
            notes.append("the AI endpoint (llm_url) was dropped — not "
                         "reachable from this instance")
    if cfg.get("smtp_host"):
        try:
            check_host(str(cfg["smtp_host"]), what="smtp_host in this export")
        except BlockedURL:
            cfg.pop("smtp_host", None)
            notes.append("the mail server (smtp_host) was dropped — not "
                         "reachable from this instance")
    # The multi-backend list is where the AI endpoints actually live —
    # the flat llm_url above is the legacy single-endpoint key. Merged in
    # raw, a ZIP could point categorize/vision/assistant at any URL and the
    # next sync or receipt parse would post merchants, amounts and receipt
    # images there. The .oikx bundle already validates the list (field
    # whitelist, ceiling, reserved/duplicate ids, netguard on every url);
    # the ZIP gets the identical rules, and the cleaned list replaces the
    # raw one so nothing else in the entries rides through.
    if "llm_backends" in cfg:
        from .connections_bundle import _clean_backends
        dropped: list[str] = []
        cfg["llm_backends"] = _clean_backends(cfg.get("llm_backends"),
                                              drop_unreachable=dropped)
        if dropped:
            notes.append(
                f"{len(dropped)} AI backend(s) dropped — not reachable from "
                f"this instance: " + ", ".join(sorted(dropped)))
    if "llm_roles" in cfg:
        if not isinstance(cfg["llm_roles"], dict):
            raise _ArchiveRefused(
                "the AI role routing in this export is malformed")
        # a role left pointing at a backend that did not survive would
        # route nowhere. Unset it, which is how this instance's own default
        # takes over — pinning it to "bundled" instead would silently
        # override a destination that routes that role somewhere better.
        kept = {str(b.get("id")) for b in (cfg.get("llm_backends") or [])
                if isinstance(b, dict)}
        rerouted = [r for r, v in cfg["llm_roles"].items()
                    if v and str(v) != "bundled" and str(v) not in kept]
        for r in rerouted:
            cfg["llm_roles"].pop(r, None)
        if rerouted:
            notes.append("AI routing for " + ", ".join(sorted(rerouted))
                         + " fell back to this instance's default")
    return notes


# The keys inside a `raw` blob that readers walk as LISTS. SQL's
# jsonb_array_elements / jsonb_array_length do not skip a row whose value
# is the wrong shape — they abort the entire statement — so one restored
# blob carrying an object under any of these can blank a query for every
# other row in the household.
_ARRAY_RAW_KEYS = ("counterparties", "companions", "_batches")


# A receipt's total and dates feed the waiting-receipt matcher, which does
# cents and calendar arithmetic on them for every waiting receipt at once.
# A total past the app-wide money ceiling, or a date off the calendar Python
# can represent, is no genuine receipt; the row still restores, without it.
_RECEIPT_TOTAL_MAX = 1e12
_RECEIPT_YEAR_MIN = 1900


def _receipt_parsed(v) -> dict:
    parsed = _raw_dict(v)
    if "total" in parsed:
        t = parsed["total"]
        try:
            ok = (not isinstance(t, bool)
                  and math.isfinite(float(t))
                  and abs(float(t)) <= _RECEIPT_TOTAL_MAX)
        except (TypeError, ValueError, OverflowError):
            ok = False
        if not ok and t is not None:
            parsed = {**parsed, "total": None}
    return parsed


def _receipt_optimized(r) -> tuple[bytes | None, str | None]:
    """The cleaned-up copy of a receipt photo from its archive row, or
    (None, None). Only an upload makes one, so a restore that dropped it
    would leave the receipt serving and re-reading the raw photo for good.
    Held to what the optimizer itself writes — a JPEG, under the upload's
    size cap — because the stored type is what the image door serves the
    bytes back as; anything else is dropped and the original stands in, as
    it does for a receipt that never had a copy."""
    import base64

    from ..engine import receipts as _receipts
    cell = r.get("image_optimized")
    if not cell or str(r.get("optimized_mime") or "").strip().lower() \
            != "image/jpeg":
        return None, None
    if len(cell) > _receipts.MAX_IMAGE * 4 // 3 + 4:
        return None, None
    try:
        data = base64.b64decode(cell, validate=True)
    except Exception:                                   # noqa: BLE001
        return None, None
    if not data or len(data) > _receipts.MAX_IMAGE \
            or not data.startswith(b"\xff\xd8\xff"):
        return None, None
    return data, "image/jpeg"


def _receipt_ts(v):
    """_ts, or None (the column's default stands in) for a timestamp no
    receipt could carry: before 1900 or after next year."""
    t = _ts(v)
    if t is None:
        return None
    if not _RECEIPT_YEAR_MIN <= t.year <= _dt.date.today().year + 1:
        return None
    return t


def _raw_dict(v) -> dict:
    """A `raw` column is read with `.get` and merged with `||` everywhere;
    a ZIP that carries a list, string or number there would land as-is
    and crash the first reader. The list-shaped keys one level down get the
    same treatment: a value that is not a list is dropped, and every reader
    already treats an absent key as "the source never said". The one
    exception is the list of import batches that own a row: there an absent
    key means "owned by its latest batch" (rows from before owner lists),
    so a value that is not a list lands as an EMPTY list, owned by nobody,
    and no import's undo can then take the row."""
    # a document nested past what the jsonb write can walk is written as
    # null, which no `raw` column takes: it is malformed as a whole
    v = _finite_only(v)
    if not isinstance(v, dict):
        return {}
    bad = [k for k in _ARRAY_RAW_KEYS if k in v and not isinstance(v[k], list)]
    if not bad:
        return v
    out = {k: x for k, x in v.items() if k not in bad}
    if "_batches" in bad:
        out["_batches"] = []
    return out


# A full account, loan or routing number never belongs in a plaintext
# `raw` column: raw leaves the instance verbatim through /export and the
# portability archive. Every ingest path keeps them out now (the MX account
# pull, the Plaid liabilities pull, the CSV and OFX importers), but an
# archive taken before those rules carries them, so a restore applies the
# same cuts on the way in. Migration 146 applied them once to rows already
# stored. Keep the three in step.
# the identifiers a Plaid object carries (sync/plaid.PLAID_RAW_IDENTIFIERS,
# the one list every write and the export cut), plus a routing number
from .plaid import PLAID_RAW_IDENTIFIERS as _PLAID_RAW_IDS
_ACCOUNT_RAW_IDS = ("routing_number",) + _PLAID_RAW_IDS
_LIABILITY_RAW_IDS = _PLAID_RAW_IDS


def scrub_account_raw(raw: dict) -> dict:
    """An account's raw without the full account and routing numbers (the
    last-4 mask is its own column)."""
    return {k: v for k, v in raw.items() if k not in _ACCOUNT_RAW_IDS}


def scrub_liability_raw(raw: dict) -> dict:
    """A liability's raw without the loan's own account number."""
    return {k: v for k, v in raw.items() if k not in _LIABILITY_RAW_IDS}


def _is_file_row(raw: dict) -> bool:
    """Whether a raw is a row a file importer stored: every value text,
    empty, or a list of text (a CSV line longer than its header, the batch
    tags). An aggregator's raw always carries a number, a flag or a nested
    object, and its keys are the aggregator's own field names — cutting
    flagged as identifiers. The notes the app itself keeps on a file row
    (that a bank feed set a row aside against it, in this format or the
    earlier one) are not the source's columns and do not decide it: a file
    row carrying one is cut like any other, as migration 146 cut it."""
    return all(v is None or isinstance(v, str)
               or (isinstance(v, list)
                   and all(isinstance(x, str) for x in v))
               for k, v in raw.items() if k not in _FILE_ROW_NOTES)


# the app's own notes on a file row (base.STOOD_IN_KEY and the copies it
# replaced), which are objects where the source's columns are text
_FILE_ROW_NOTES = (_sync_base.STOOD_IN_KEY, _sync_base._LEGACY_COPIES_KEY)


def scrub_transaction_raw(raw: dict) -> dict:
    """A transaction's raw with identifier-named source columns cut to
    their last 4, as the CSV importers write them (file-import rows only),
    the OFX statement account hint likewise, and the statement importer's
    kept line with every card or long number masked to its last 4."""
    from .csvimport import scrub_identifiers
    from .pdfimport import _mask_line
    out = (scrub_identifiers(raw) if _is_file_row(raw)
           else {k: v for k, v in raw.items() if k not in _PLAID_RAW_IDS})
    _sync_base.stood_in_from_legacy_copies(out)
    acct = out.get("acct")
    if (isinstance(acct, (str, int, float)) and not isinstance(acct, bool)
            and len(str(acct)) > 4):
        out["acct"] = str(acct)[-4:]
    # the statement importer's rule: a long digit run in the kept line
    # (an account or card number printed on the statement) keeps its last 4
    if isinstance(out.get("line"), str):
        out["line"] = _mask_line(out["line"])
    return out


def _json_list(v) -> list:
    """A column the schema declares a JSON array — the line items an order
    or a receipt carries. Anything else becomes an empty list, so the order
    still lands with its amount, date and category and only its items read
    as none. `x or []` is NOT this check: it substitutes on a falsy value
    and waves an object or a number straight through."""
    return v if isinstance(v, list) else []


def clean_excluded_accounts(cfg: dict) -> None:
    """Coerce the Accounts page's "excl" list in a restored config into the
    shape the toggle itself would have written, dropping what cannot be an
    account id and removing the key when nothing is left.

    Same class as `clean_bill_raw` on the field beside it: the settings blob
    goes in verbatim, and every money surface iterates this list. A `true`
    or a number under the key is TRUTHY, so the `value or []` idiom hands it
    straight to `list()` and the forecast, the calendar strip and every
    report raise instead of rendering — with no UI for the household to
    take the value back out."""
    if "excluded_accounts" not in cfg:
        return
    from ..engine.budget import excluded_account_ids
    ids = excluded_account_ids(cfg)
    if ids:
        cfg["excluded_accounts"] = ids
    else:
        cfg.pop("excluded_accounts", None)


def _amount_or_none(v):
    """A settings amount as the settings door accepts it: a real number
    (a bool is not one), finite, and no larger than any real sum; None for
    anything else. A numeric string passes, read by the parser the door
    uses, so a decimal-comma "12,50" restores as 12.5 — deleting every
    comma would bring it back as 1250."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        if isinstance(v, str):
            from .csvimport import parse_money_text
            if not v.strip():
                return None
            n = parse_money_text(v)
        else:
            n = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(n) or abs(n) > MAX_AMOUNT:
        return None
    return n


def clean_budget_amounts(cfg: dict) -> list[str]:
    """Bound and type-check the budget amounts in a restored config the way
    the settings door does, and say what was dropped.

    The settings blob goes in verbatim, and Today, Budget and the daily
    email compare and sum these values directly: a string, a bool or a
    non-finite number in one of them makes those pages answer 500, with no
    door the household can use to take it back out. The per-month pools
    are dropped when unusable (the instance then seeds them as for a new
    household); a bucket keeps its name and rules and only loses an amount
    that cannot be one."""
    notes: list[str] = []
    for k in ("food_monthly", "other_monthly"):
        if k in cfg:
            n = _amount_or_none(cfg[k])
            if n is None:
                cfg.pop(k)
                notes.append(f"the {k.split('_')[0]} budget was not a usable "
                             "amount and was left out")
            else:
                cfg[k] = max(0.0, n)
    if "budgeted_income_monthly" in cfg:
        n = _amount_or_none(cfg["budgeted_income_monthly"])
        if n is None or n <= 0:
            if n is None:
                notes.append("the budgeted monthly income was not a usable "
                             "amount and was left out")
            cfg.pop("budgeted_income_monthly")
        else:
            cfg["budgeted_income_monthly"] = n
    if "custom_buckets" in cfg:
        buckets = cfg["custom_buckets"]
        if not isinstance(buckets, list):
            cfg.pop("custom_buckets")
            notes.append("the budget categories were not a list and were "
                         "left out")
        else:
            bad = 0
            kept = []
            for e in buckets[:_BUCKET_CAP]:
                e = _bucket(e, {k["name"].lower() for k in kept})
                if e is None:
                    continue
                if e.pop("_bad_amount", False):
                    bad += 1
                kept.append(e)
            if bad:
                notes.append(f"{bad} budget categor"
                             f"{'ies' if bad != 1 else 'y'} had an amount "
                             "that was not usable and now budget $0")
            dropped = len(buckets) - len(kept)
            if dropped:
                notes.append(f"{dropped} budget categor"
                             f"{'ies' if dropped != 1 else 'y'} could not "
                             "be used and were left out")
            if kept:
                cfg["custom_buckets"] = kept
            else:
                cfg.pop("custom_buckets")
    return notes


# the settings door's bucket caps: 40 categories, each with at most 40
# category and 60 merchant matchers of at most 120 characters
_BUCKET_CAP, _BUCKET_NAME_MAX = 40, 40
_BUCKET_CATS, _BUCKET_MERCH, _BUCKET_MATCHER_MAX = 40, 60, 120


def _bucket(e, seen: set) -> dict | None:
    """One budget category as the settings door stores it, or None when
    the door would refuse it. An amount that is no amount budgets $0 (and
    is marked so the caller can say so); everything else the door refuses
    drops the category, since the Budget page matches every spend row
    against every matcher."""
    from ..engine.budget import RESERVED_BUCKET_NAMES
    if not isinstance(e, dict):
        return None
    name = e.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if (not name or len(name) > _BUCKET_NAME_MAX
            or name.lower() in RESERVED_BUCKET_NAMES
            or name.lower() in seen
            or e.get("parent") not in ("food", "other")):
        return None
    lists = []
    for key, cap in (("categories", _BUCKET_CATS),
                     ("merchants", _BUCKET_MERCH)):
        raw = e.get(key) or []
        if not isinstance(raw, list) or len(raw) > cap:
            return None
        # the door keeps a number typed as a matcher as its text; a
        # matcher that is neither, or longer than the door takes, is
        # dropped on its own
        words = (str(s).strip() for s in raw
                 if isinstance(s, (str, int, float))
                 and not isinstance(s, bool)
                 and (not isinstance(s, str) or len(s) <= _BUCKET_MATCHER_MAX))
        lists.append([w for w in words if w and len(w) <= _BUCKET_MATCHER_MAX])
    out = {"name": name, "parent": e["parent"], "monthly": 0.0,
           "categories": lists[0], "merchants": lists[1]}
    if e.get("monthly") not in (None, ""):
        n = _amount_or_none(e["monthly"])
        if n is None:
            out["_bad_amount"] = True
        else:
            out["monthly"] = max(0.0, n)
    return out


# The list caps the settings door applies; a restored list is cut to the
# same length, so an archive cannot hand the pages a list no save could.
_SCENARIO_CAP, _GOAL_CAP, _ASSET_CAP = 20, 30, 30


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _named(e, seen: set) -> str | None:
    """The entry's name as the settings door stores it, or None when the
    entry is not an object, has no name, or repeats one already kept."""
    if not isinstance(e, dict):
        return None
    name = str(e.get("name") or "").strip()[:60]
    if not name or name.lower() in seen:
        return None
    seen.add(name.lower())
    return name


def _stated_amount(e: dict, key: str):
    """An optional amount field: 0.0 when absent or blank, None when it is
    present but no usable amount (the entry is then dropped, as the door
    refuses it)."""
    v = e.get(key)
    if v is None or v == "":
        return 0.0
    return _amount_or_none(v)


def clean_income_scenarios(cfg: dict) -> list[str]:
    """The income scenarios of a restored config, kept to what the settings
    door accepts, with a note per drop.

    Today, the forecast and Settings read the active scenario's take-home
    straight into a multiplication: a string, NaN or a non-list here makes
    them answer 500 with no door left to take it back out. An entry the
    door would refuse (no name, a repeat, no take-home above zero, an
    unknown cadence) is dropped; the list is cut at the door's cap; the
    active name falls back to the first kept scenario. The legacy
    saving/not-saving pair is read into the same shape, so it is held to
    the same amount rule."""
    from ..engine.budget import CADENCE_MONTHLY
    notes: list[str] = []
    for k in ("income_biweekly_saving", "income_biweekly_not_saving"):
        if k in cfg and cfg[k] not in (None, ""):
            n = _amount_or_none(cfg[k])
            if n is None or n <= 0:
                cfg.pop(k)
                notes.append("a legacy income amount was not usable and "
                             "was left out")
            else:
                cfg[k] = round(n, 2)
    # the active name is one of the scenario names, which the door cuts
    # at 60 characters
    if "active_scenario" in cfg and not (
            cfg["active_scenario"] is None
            or isinstance(cfg["active_scenario"], str)
            and len(cfg["active_scenario"]) <= 60):
        cfg.pop("active_scenario")
    if "income_scenarios" not in cfg:
        return notes
    scen_in = cfg["income_scenarios"]
    if not isinstance(scen_in, list):
        cfg.pop("income_scenarios")
        cfg.pop("active_scenario", None)
        notes.append("the income scenarios were not a list and were left out")
        return notes
    kept, seen = [], set()
    for e in scen_in[:_SCENARIO_CAP]:
        name = _named(e, seen)
        if name is None:
            continue
        take = _amount_or_none(e.get("take_home"))
        cadence = str(e.get("cadence") or "biweekly").strip().lower()
        if take is None or take <= 0 or cadence not in CADENCE_MONTHLY:
            seen.discard(name.lower())
            continue
        kept.append({**e, "name": name, "take_home": round(take, 2),
                     "cadence": cadence})
    dropped = len(scen_in) - len(kept)
    if dropped:
        notes.append(f"{_plural(dropped, 'income scenario', 'income scenarios')}"
                     " could not be used and were left out")
    if kept:
        cfg["income_scenarios"] = kept
        if cfg.get("active_scenario") not in {s["name"] for s in kept}:
            cfg["active_scenario"] = kept[0]["name"]
    else:
        cfg.pop("income_scenarios")
        cfg.pop("active_scenario", None)
    return notes


def clean_savings_goals(cfg: dict) -> list[str]:
    """The savings goals of a restored config, kept to what the settings
    door accepts, with a note per drop.

    Today, the forecast, the savings card and the email read target, plan
    and starting balance through float() and the match words as a list: a
    non-list, a NaN or a string there is a 500, and a bare string of match
    words would be read letter by letter and match nearly every transfer.
    A goal the door would refuse (no name, a repeat, an amount that is not
    one, match words that are not a list, a target date that is not a
    date) is dropped; the list is cut at the door's cap."""
    notes: list[str] = []
    if "savings_goals" not in cfg:
        return notes
    goals_in = cfg["savings_goals"]
    if not isinstance(goals_in, list):
        cfg.pop("savings_goals")
        cfg.pop("savings_goal_state", None)
        notes.append("the savings goals were not a list and were left out")
        return notes
    kept, seen = [], set()
    for g in goals_in[:_GOAL_CAP]:
        name = _named(g, seen)
        if name is None:
            continue
        target = _stated_amount(g, "target")
        plan = _stated_amount(g, "monthly_plan")
        start = _stated_amount(g, "start_balance")
        tokens = g.get("tokens") or []
        td = str(g.get("target_date") or "").strip()
        try:
            if _too_long(td):
                raise ValueError(td[:20])
            td = _dt.date.fromisoformat(td).isoformat() if td else None
            date_ok = True
        except ValueError:
            date_ok = False
        if (target is None or plan is None or start is None
                or not isinstance(tokens, list) or not date_ok):
            seen.discard(name.lower())
            continue
        goal = {**g, "name": name, "target": round(max(0.0, target), 2),
                "monthly_plan": round(max(0.0, plan), 2),
                "account_id": str(g.get("account_id") or "") or None,
                "tokens": [str(t).strip()[:60] for t in tokens[:8]
                           if str(t).strip()],
                "start_balance": round(start, 2)}
        goal.pop("mode", None)
        if str(g.get("mode") or "").strip().lower() == "sweep":
            goal["mode"] = "sweep"
        goal.pop("target_date", None)
        if td:
            goal["target_date"] = td
        kept.append(goal)
    dropped = len(goals_in) - len(kept)
    if dropped:
        notes.append(f"{_plural(dropped, 'savings goal', 'savings goals')}"
                     " could not be used and were left out")
    if kept:
        cfg["savings_goals"] = kept
    else:
        cfg.pop("savings_goals")
        cfg.pop("savings_goal_state", None)
    return notes


def clean_manual_assets(cfg: dict) -> list[str]:
    """The Net Worth page's hand-entered assets, held to the settings
    door's rules: a list of named objects whose value is a usable amount
    and whose as-of is a date, cut at the door's cap. A NaN or huge value
    would otherwise ride into the net-worth total."""
    notes: list[str] = []
    if "manual_assets" not in cfg:
        return notes
    assets_in = cfg["manual_assets"]
    if not isinstance(assets_in, list):
        cfg.pop("manual_assets")
        notes.append("the hand-entered assets were not a list and were left "
                     "out")
        return notes
    kept = []
    for a in assets_in[:_ASSET_CAP]:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "").strip()[:60]
        value = _stated_amount(a, "value")
        try:
            as_of = asset_day(a["as_of"]) if a.get("as_of") else None
            date_ok = True
        except (TypeError, ValueError):
            date_ok = False
        if not name or value is None or not date_ok:
            continue
        # the day it entered the household: as archived when that is a
        # date, else its as-of (migration 228's rule for a row stored
        # before the field existed)
        try:
            acquired = (asset_day(a["acquired_on"]) if a.get("acquired_on")
                        else as_of)
        except (TypeError, ValueError):
            acquired = as_of
        # only what the settings door stores: any other key, of any size
        # or value (a NaN aborts the settings write), would ride in unread
        entry = {"name": name,
                 "kind": str(a.get("kind") or "other").strip()[:30],
                 "value": round(value, 2)}
        if as_of is not None:
            entry["as_of"] = as_of.isoformat()
        if acquired is not None:
            entry["acquired_on"] = acquired.isoformat()
        kept.append(entry)
    dropped = len(assets_in) - len(kept)
    if dropped:
        notes.append(f"{_plural(dropped, 'hand-entered asset', 'hand-entered assets')}"
                     " could not be used and were left out")
    if kept:
        cfg["manual_assets"] = kept
    else:
        cfg.pop("manual_assets")
    return notes


# ---- the shape of every other known settings key ---------------------------
#
# Each key below is written by its own door (bill save, the hint dismiss,
# the wizard step save, the retirement settings), which type-checks it. The
# restore merge bypasses every door, so the restored value is held here to
# the shape that door writes, once, before the merge: a list where a dict
# belongs lands verbatim otherwise, and every page that reads the key (and
# the door that could repair it) trips on it on every load. A coercer
# answers the cleaned value plus how many entries it dropped, or _DROP when
# the key as a whole is unusable. A key the table does not name passes as
# the archive carried it, past _finite_only.

_DROP = object()
# per-key entry cap: no door writes more entries than the household has
# bills, and an archive must not hand the pages a list no save could
_SHAPE_ENTRY_CAP = 2000
_WIZARD_STEPS = {
    "wizard_steps": ("appearance", "connect", "sync", "import", "bills",
                     "budgets", "email", "finish"),
    "biz_wizard_steps": ("what", "when", "accounts", "startup", "finish"),
    "ret_wizard_steps": ("age", "spend", "saving", "assumptions", "done"),
}


# What the retirement wizard saves beside its step marks: the number typed
# at each step. Each answer is held to the range the retirement projection
# takes it in (web/api.py retirement_projection clamps to these; the rate
# bounds are the ones its _rate reads from here), so any answer the page
# would compute with can be saved, and the settings door and the restore
# hold an answer to this one rule. Never a bool: JSON true would otherwise
# pass as 1.
RET_RATE_MIN, RET_RATE_MAX = -20.0, 30.0
RET_WIZARD_RANGES = {
    "age": (18, 90),
    "end": (19, 110),
    "spend": (0, 100_000_000),
    "employer_mo": (0, 10_000_000),
    # saving into Roth accounts (employer_mo is the tax-deferred figure);
    # a client sends it as the top-level `ret_wizard_roth_mo`, as the
    # claim age rides (specs/retirement-figures.md)
    "roth_mo": (0, 10_000_000),
    "taxable_mo": (0, 10_000_000),
    "ret": (RET_RATE_MIN, RET_RATE_MAX),
    "infl": (RET_RATE_MIN, RET_RATE_MAX),
    # the Social Security claim age the plan was last run at (the page's
    # own claim-age range); saved so every device opens on the same one
    "ssage": (62, 70),
}
RET_WIZARD_ANSWERS = tuple(RET_WIZARD_RANGES)


# Each answer as the wizard labels its field, for the refusal a person
# reads word for word (the restore drops a bad answer and says nothing).
RET_WIZARD_LABELS = {
    "age": "Current age",
    "end": "Plan to age",
    "spend": "Target spend $/year",
    "employer_mo": "Employer retirement $/mo",
    "roth_mo": "Roth retirement $/mo",
    "taxable_mo": "Taxable saving $/mo",
    "ret": "Expected return",
    "infl": "Inflation",
    "ssage": "Social Security claim age",
}


def ret_wizard_refusal(key: str) -> str:
    """The sentence a refused answer is answered with: the field's label
    and the range it takes, in the units the field is typed in."""
    lo, hi = RET_WIZARD_RANGES[key]

    def num(v):
        text = f"{int(v):,}" if float(v).is_integer() else f"{v:,}"
        return text.replace("-", "\u2212")
    if key in RET_WHOLE_ANSWERS:
        return (f"{RET_WIZARD_LABELS[key]} must be a whole number from "
                f"{num(lo)} to {num(hi)}.")
    if key in ("ret", "infl"):
        lo_s, hi_s = f"{num(lo)}%", f"{num(hi)}%"
    elif key in ("age", "end", "ssage"):
        lo_s, hi_s = num(lo), num(hi)
    else:
        lo_s, hi_s = f"${num(lo)}", f"${num(hi)}"
    return (f"{RET_WIZARD_LABELS[key]} must be between {lo_s} "
            f"and {hi_s}.")


# answers that are ages in whole years: the claim age is a choice among
# 62..70, so 66.5 is refused, not truncated
RET_WHOLE_ANSWERS = ("ssage",)


def ret_wizard_answer(key: str, v):
    """The answer to `key` as stored, or None when it is not one."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    lo, hi = RET_WIZARD_RANGES[key]
    # every float() of `v` sits inside the try: an int too large for a
    # float raises there, and JSON hands one over as readily as any number
    try:
        if key in RET_WHOLE_ANSWERS and not float(v).is_integer():
            return None
        if not math.isfinite(v) or not lo <= v <= hi:
            return None
    except OverflowError:                 # an int too large for a float
        return None
    return v


# Which saved answers a current client wrote (it sends `answers_v: 2`
# with them): only these are inputs to the projection. Older wizards saved
# every prefilled box — an assumed spend, an untouched $0 saving — so an
# answer outside this list still prefills the walkthrough but is never
# read as the household's own. Kept inside the answers object, so a
# restore carries it with them.
RET_ANSWERS_MARK = "answers_v2"


def ret_marked_answers(v) -> list[str]:
    """The answer keys a marked save wrote, from a stored answers object."""
    m = v.get(RET_ANSWERS_MARK) if isinstance(v, dict) else None
    return sorted({k for k in m if isinstance(k, str)
                   and k in RET_WIZARD_RANGES}) if isinstance(m, list) else []


def _ret_answers(v):
    if not isinstance(v, dict):
        return _DROP, 0
    kept = {k: x for k, x in v.items()
            if k in RET_WIZARD_RANGES and ret_wizard_answer(k, x) is not None}
    dropped = len(v) - len(kept)
    marked = [k for k in ret_marked_answers(v) if k in kept]
    if RET_ANSWERS_MARK in v:
        dropped -= 1
        if marked:
            kept[RET_ANSWERS_MARK] = marked
    return kept, dropped


def _payee_map(entry):
    """A dict keyed by bill payee whose values pass `entry` (which answers
    the kept value or _DROP)."""
    def coerce(v):
        if not isinstance(v, dict):
            return _DROP, 0
        kept = {}
        for k, x in list(v.items())[:_SHAPE_ENTRY_CAP]:
            y = entry(x) if isinstance(k, str) and k.strip() else _DROP
            if y is not _DROP:
                kept[k] = y
        return kept, len(v) - len(kept)
    return coerce


def _occurrence_cap(x):
    # the bill door stores int(cap) >= 1; budget reads it as a slice bound
    if isinstance(x, bool) or not isinstance(x, (str, int)):
        return _DROP
    try:
        if _too_long(str(x)):
            return _DROP
        n = int(str(x).strip())
    except ValueError:
        return _DROP
    return n if n >= 1 else _DROP


def _payee_list(v):
    if not isinstance(v, list):
        return _DROP, 0
    kept = list(dict.fromkeys(x for x in v[:_SHAPE_ENTRY_CAP]
                              if isinstance(x, str) and x.strip()))
    return kept, len(v) - len(kept)


def _steps(names):
    def coerce(v):
        if not isinstance(v, dict):
            return _DROP, 0
        kept = {k: x for k, x in v.items()
                if k in names and x in ("done", "skipped")}
        return kept, len(v) - len(kept)
    return coerce


def _flag(v):
    return bool(v), 0


def _past_date(v):
    try:
        d = (_dt.date.fromisoformat(v)
             if isinstance(v, str) and not _too_long(v) else None)
    except ValueError:
        d = None
    if d is None or d >= _dt.date.today():
        return _DROP, 0
    return d.isoformat(), 0


def _hint_dismissal(x):
    """A 💡-hint dismissal as the dismiss door writes it: an object whose
    amount, when present, is a finite number (bills.fit_matches_dismissal
    compares it)."""
    import math
    if not isinstance(x, dict):
        return _DROP
    a = x.get("amount")
    if a is None:
        return x
    try:
        if isinstance(a, bool) or not isinstance(a, (int, float)) \
                or not math.isfinite(a):
            return _DROP
    except OverflowError:                 # an int too large for a float
        return _DROP
    return x


def _paycheck_words(v):
    """The household's paycheck word per payer as the income-payer door
    writes it: {payer key: bool}."""
    from ..engine import paychecks
    if not isinstance(v, dict):
        return _DROP, 0
    kept = {k: x for k, x in v.items()
            if paychecks.valid_key(k) and isinstance(x, bool)}
    return kept, len(v) - len(kept)


# The longest text any of the plain string settings below can hold. The
# settings door caps the body, not each box, so this is a ceiling no real
# value comes near (a zone name, a model name, a host, an address, an
# account id), and a megabyte of text under one of them is no setting.
_SETTING_TEXT_MAX = 2048
# the settings door's caps for the investment maps (api_settings
# _INVEST_MAX_ROWS) and its removed-member memory (_REMOVED_CAP)
_INVEST_ROWS, _REMOVED_ROWS = 50, 50


def _finite_number(x, lo: float, hi: float):
    """`x` as a float when it is a real number (never a bool) inside
    [lo, hi], else None. An int too large for a float is no amount."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        n = float(x)
    except OverflowError:
        return None
    return n if math.isfinite(n) and lo <= n <= hi else None


def _text(max_len: int = _SETTING_TEXT_MAX, allowed=None, check=None, *,
          strip: bool = True):
    """A non-blank string as the door stores it: stripped (unless it is an
    id compared exactly), no longer than `max_len`, one of `allowed` when
    given, passing `check` when given."""
    def coerce(v):
        if not isinstance(v, str) or len(v) > max_len:
            return _DROP, 0
        s = v.strip() if strip else v
        if not s.strip() or (allowed is not None and s not in allowed) or (
                check is not None and not check(s)):
            return _DROP, 0
        return s, 0
    return coerce


def _whole_in(choices):
    """A whole number (never a bool) from `choices`."""
    def coerce(v):
        if isinstance(v, bool) or not isinstance(v, int) or v not in choices:
            return _DROP, 0
        return v, 0
    return coerce


def _account_mask(x):
    """An account mask as the settings door stores one (api_settings
    _invest_mask): 2 to 8 ASCII letters or digits, upper-cased."""
    if isinstance(x, bool) or not isinstance(x, (str, int)):
        return None
    if isinstance(x, int) and not 0 <= x < 10 ** 8:
        return None                  # longer than any mask (or negative)
    if isinstance(x, str) and len(x) > 64:
        return None
    m = str(x).strip().upper()
    return m if m.isascii() and m.isalnum() and 2 <= len(m) <= 8 else None


def _ss_estimates(v):
    """{claim age 62..70: monthly dollars}, as the settings door writes it:
    the age a string key, the amount a finite number from 0 to the money
    ceiling. The Retirement page multiplies these out over decades, so an
    amount near the float ceiling there is an infinity on every load."""
    if not isinstance(v, dict):
        return _DROP, 0
    kept = {}
    for k, x in list(v.items())[:_SHAPE_ENTRY_CAP]:
        try:
            age = int(k) if isinstance(k, str) and len(k) <= 3 else None
        except ValueError:
            age = None
        amt = _finite_number(x, 0.0, MAX_AMOUNT)
        if age is not None and 62 <= age <= 70 and amt is not None:
            kept[str(age)] = amt
    return (kept, len(v) - len(kept)) if kept else (_DROP, 0)


def _basis_override(v):
    """{account mask: dollars put in}: a mask the door would take, an
    amount from 0 to the money ceiling, at most the door's row cap."""
    if not isinstance(v, dict):
        return _DROP, 0
    kept = {}
    for k, x in list(v.items())[:_INVEST_ROWS]:
        m, amt = _account_mask(k), _finite_number(x, 0.0, MAX_AMOUNT)
        if m is not None and amt is not None:
            kept[m] = round(amt, 2)
    return (kept, len(v) - len(kept)) if kept else (_DROP, 0)


def _advisory_fees(v):
    """{institution fragment: yearly rate}: a lower-cased fragment of 1 to
    60 characters (a blank one would match every institution) and a rate
    that is a fraction from 0 to 1, at most the door's row cap."""
    if not isinstance(v, dict):
        return _DROP, 0
    kept = {}
    for k, x in list(v.items())[:_INVEST_ROWS]:
        frag = k.strip().lower() if isinstance(k, str) else ""
        rate = _finite_number(x, 0.0, 1.0)
        if frag and len(frag) <= 60 and rate is not None:
            kept[frag] = rate
    return (kept, len(v) - len(kept)) if kept else (_DROP, 0)


def _exempt_masks(v):
    """The self-directed accounts' masks: a list of masks, no repeats."""
    if not isinstance(v, list):
        return _DROP, 0
    kept = list(dict.fromkeys(
        m for m in (_account_mask(x) for x in v[:_INVEST_ROWS])
        if m is not None))
    return (kept, len(v) - len(kept)) if kept else (_DROP, 0)


def _id_list(v):
    """A list of ids the app compares exactly (the dismissed link
    prompts): non-blank strings no longer than any id, no repeats."""
    if not isinstance(v, list):
        return _DROP, 0
    kept = list(dict.fromkeys(
        x for x in v[:_SHAPE_ENTRY_CAP]
        if isinstance(x, str) and x.strip() and len(x) <= 255))
    return kept, len(v) - len(kept)


def _removed_digests(v):
    """The removed members' address digests: hex SHA-256 strings."""
    if not isinstance(v, list):
        return _DROP, 0
    kept = [x for x in v[-_REMOVED_ROWS:] if isinstance(x, str)
            and re.fullmatch(r"[0-9a-f]{64}", x)]
    return (kept, len(v) - len(kept)) if kept else (_DROP, 0)


def _email_prefs(v):
    """{kind: [addresses that turned it off]}, the kinds the mail knows,
    each list of addresses bounded as the mute list is."""
    from ..notify.email_prefs import KINDS
    if not isinstance(v, dict):
        return _DROP, 0
    kept, lost = {}, 0
    for kind, addrs in v.items():
        if kind not in KINDS or not isinstance(addrs, list):
            lost += 1
            continue
        good = sorted({a.strip().lower() for a in addrs[:_SHAPE_ENTRY_CAP]
                       if isinstance(a, str) and a.strip()
                       and len(a) <= 320})
        lost += len(addrs) - len(good)
        if good:
            kept[kind] = good
    return (kept, lost) if kept else (_DROP, 0)


def _debt_plan(v):
    """The debt plan, through the one normalizer its save door uses."""
    from ..engine.debt import normalize_plan
    try:
        return normalize_plan(v), 0
    except (ValueError, TypeError, OverflowError, RecursionError):
        return _DROP, 0


def _goal_state(v):
    """The nightly savings pass's own record per goal: an object whose
    milestone is a whole percent, whose verdict is true, false or none and
    whose plan is an object or none. The pass reads each straight into
    int() and dict lookups."""
    if not isinstance(v, dict):
        return _DROP, 0
    kept = {}
    for name, e in list(v.items())[:_SHAPE_ENTRY_CAP]:
        if not isinstance(e, dict):
            continue
        ms = e.get("milestone", 0)
        if (isinstance(ms, bool) or not isinstance(ms, int)
                or not 0 <= ms <= 100
                or e.get("on_pace") not in (True, False, None)
                or not isinstance(e.get("plan"), (dict, type(None)))):
            continue
        kept[name] = e
    return kept, len(v) - len(kept)


def _smtp_port(v):
    if isinstance(v, str) and v.strip().isdigit() and len(v) <= 5:
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535:
        return _DROP, 0
    return v, 0


def _valid_zone(s: str) -> bool:
    from .. import localtime
    return localtime.valid_zone(s)


def _int_in(lo: int, hi: int):
    def coerce(v):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            return _DROP, 0
        return v, 0
    return coerce


# Keys another pass of this module holds to their door's shape (each named
# beside it), so the table below need not name them twice. With the
# table, the policy keys, the credential companions and the
# credential-shaped names, this is every settings key the app reads or
# writes; the shape test walks the code for the keys and fails on one
# that none of them hold.
HELD_BY_OTHER_CLEANERS = {
    **dict.fromkeys(("email_schedule", "email_send_hour_utc",
                     "email_recipients", "email_muted"),
                    "normalize_notify_shapes"),
    "excluded_accounts": "clean_excluded_accounts",
    **dict.fromkeys(("food_monthly", "other_monthly",
                     "budgeted_income_monthly", "custom_buckets"),
                    "clean_budget_amounts"),
    **dict.fromkeys(("income_scenarios", "active_scenario",
                     "income_biweekly_saving", "income_biweekly_not_saving"),
                    "clean_income_scenarios"),
    "savings_goals": "clean_savings_goals",
    "manual_assets": "clean_manual_assets",
    **dict.fromkeys(("home_web", "home_mobile", "llm_backends", "llm_roles"),
                    "_check_config"),
    "plaid_enrich_cap": "_settle_settings",
}


_CONFIG_SHAPES = {
    "occurrence_caps": _payee_map(_occurrence_cap),
    "paycheck_payers": _paycheck_words,
    "disabled_bills": _payee_list,
    "dismissed_hints": _payee_map(_hint_dismissal),
    "dismissed_health": _payee_map(lambda x: x if isinstance(x, dict)
                                   else _DROP),
    **{k: _steps(names) for k, names in _WIZARD_STEPS.items()},
    "ret_wizard_answers": _ret_answers,
    "wizard_done": _flag,
    "budgets_saved_by_user": _flag,
    "retirement_saving": _flag,
    "dynamic_variable_budget": _flag,
    "birthdate": _past_date,
    # the Retirement page and the Net worth report
    "ss_estimates": _ss_estimates,
    "investment_basis_override": _basis_override,
    "advisory_fees": _advisory_fees,
    "advisory_fee_exempt_masks": _exempt_masks,
    "debt_plan": _debt_plan,
    # which account the forecast reads, and the ledger's window
    "checking_account_id": _text(255, strip=False),
    "history_months": _whole_in(_sync_base.HISTORY_MONTHS),
    "combine_entities": _flag,
    # the bank-link prompts the household waved away
    "link_dismissed": _id_list,
    "link_card_dismissed": _flag,
    # the nightly savings pass's record
    "savings_goal_state": _goal_state,
    # the look and the bill finder
    "theme": _text(16, allowed=("auto", "light", "dark")),
    "today_view": _text(16, allowed=("summary", "detail")),
    "setup_mode": _text(16, allowed=("auto", "manual")),
    "bill_detection_auto": _flag,
    "merchant_logos": _flag,
    "plaid_recurring": _flag,
    "feedback_enabled": _flag,
    "timezone": _text(64, check=_valid_zone),
    # mail: who asked for what, who left, and the relay (its user and
    # password never ride an archive)
    "email_prefs": _email_prefs,
    "email_removed_members": _removed_digests,
    "smtp_host": _text(253),
    "smtp_port": _smtp_port,
    "smtp_from": _text(320),
    # the AI endpoint (the backends list is held by _check_config)
    "llm_url": _text(),
    "llm_model": _text(200),
    "llm_vision_model": _text(200),
    # the demonstration household's generator inputs
    "demo_seed": _int_in(-(2 ** 63), 2 ** 63 - 1),
    "demo_years": _int_in(1, 100),
}


def clean_config_shapes(cfg: dict) -> list[str]:
    """Every key of `_CONFIG_SHAPES` the restored config carries, held to
    the shape its door writes; one note naming what was dropped."""
    dropped_keys, dropped_entries = [], 0
    for key, coerce in _CONFIG_SHAPES.items():
        if key not in cfg:
            continue
        value, lost = coerce(cfg[key])
        if value is _DROP:
            cfg.pop(key)
            dropped_keys.append(key)
        else:
            cfg[key] = value
            dropped_entries += lost
    notes = []
    if dropped_keys or dropped_entries:
        what = ", ".join(k.replace("_", " ") for k in dropped_keys)
        parts = ([f"{what} left out"] if what else []) + (
            [_plural(dropped_entries, "entry", "entries") + " dropped"]
            if dropped_entries else [])
        notes.append("some settings were not in the shape this version "
                     "keeps them in: " + "; ".join(parts))
    return notes


# The comma-free name each comma id in the archive being restored takes,
# decided once per restore by _plan_comma_renames before any row lands.
# A ContextVar for the same reason as _PROGRESS: _account_id is called from
# every table's loop, and only restore_zip ever sets it.
_COMMA_RENAMES: contextvars.ContextVar = contextvars.ContextVar(
    "oikonome_restore_comma_renames", default=None)


# Account ids this household moved away from — adopted onto a new link, or
# merged into the live account a link already held — and the account each
# now lives on. Read once per restore before any row lands, so restoring
# the same archive again files its rows under the account they moved to
# instead of re-creating the old id as an empty second copy of it.
_MOVED_ACCOUNTS: contextvars.ContextVar = contextvars.ContextVar(
    "oikonome_restore_moved_accounts", default=None)


def _plan_moved_accounts(conn) -> dict[str, str]:
    """old id -> the account it lives on now, following a chain of moves
    (restored, adopted, merged again) to the account that still exists."""
    rows = conn.execute(
        "SELECT old_account_id, account_id FROM account_adoptions"
        " ORDER BY adopted_at").fetchall()
    step = {r["old_account_id"]: r["account_id"] for r in rows
            if r["old_account_id"] and r["account_id"]
            and r["old_account_id"] != r["account_id"]}
    # A split re-key records the old id with its number cut
    # (split_lineage_id), so two numbered ids sharing their last four leave
    # one key. Which account each went to is then unknown here, and
    # filing both onto one would merge two accounts' rows: such a key is
    # not followed, and the archive's account lands and is re-keyed again
    # on its own (its rows already here stay where they are).
    targets: dict[str, set] = {}
    for r in rows:
        if "\u2026" in (r["old_account_id"] or ""):
            targets.setdefault(r["old_account_id"], set()).add(
                r["account_id"])
    for k, v in targets.items():
        if len(v) > 1:
            step.pop(k, None)     # _resolve_cut_lineage may still place it
    live = {r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE id = ANY(%s)",
        (list(step),)).fetchall()} if step else set()
    out: dict[str, str] = {}
    for old in step:
        if old in live:
            continue                 # the old id is an account again
        cur, hops = step[old], 0
        while cur in step and cur not in live and hops < 16:
            cur, hops = step[cur], hops + 1
        if cur != old:
            out[old] = cur
    return out


# Archived rows per numbered account read to place it under a cut lineage
# key that names several accounts: a few are enough to find them here.
_CUT_LINEAGE_PROBE = 2_000


def _ambiguous_key(aid: str, ambiguous: dict) -> str | None:
    """The cut lineage key (base.lineage_keys) of `aid` naming several
    accounts, if any."""
    return next((k for k in _sync_base.lineage_keys(aid) if k in ambiguous),
                None)


def _resolve_cut_lineage(conn, z, moved: dict) -> None:
    """Place each archived split account whose id spells a full number
    under a cut lineage key naming several accounts (two cards sharing
    their last four, both moved): onto the one account here holding its
    archived rows' ids, which hash the full id and so tell the cards
    apart. One no account, or several, hold stays unplaced, lands, and is
    re-keyed on its own, as _plan_moved_accounts leaves it — never filed
    onto the other card's account."""
    rows = conn.execute(
        "SELECT old_account_id, account_id FROM account_adoptions"
        " WHERE item_id = 'manual' AND strpos(old_account_id, %s) > 0",
        ("\u2026",)).fetchall()
    targets: dict[str, set] = {}
    for r in rows:
        targets.setdefault(r["old_account_id"], set()).add(r["account_id"])
    ambiguous = {k: v for k, v in targets.items() if len(v) > 1}
    if not ambiguous:
        return
    ids: dict[str, list] = {}
    for r in _rows(z, "transactions.csv"):
        tid, aid = r.get("id"), r.get("account_id")
        if not tid or not aid:
            continue
        aid = _comma_free(str(aid))
        if (aid in moved or not _sync_base.NUMBERED_MANUAL_ID_RE.search(aid)
                or _ambiguous_key(aid, ambiguous) is None):
            continue
        got = ids.setdefault(aid, [])
        if len(got) < _CUT_LINEAGE_PROBE:
            got.append((str(tid), r.get("date"), r.get("amount"),
                        r.get("name")))
    # rows re-id'd here under the household's secret answer to the keyed
    # form of the archived id (base.key_file_import_ids), and rows a re-key
    # re-hashed onto the account they moved to to the same line hashed
    # under that account (base.rehash_file_rows)
    secret = _sync_base.txn_id_secret(conn)
    for aid, got in ids.items():
        same: dict[tuple, int] = defaultdict(int)
        for tid, day, amount, name in got:
            same[_sync_base.file_row_shape(tid, day, amount, name)] += 1
        lines = [(tid, day, amount, name,
                  same[_sync_base.file_row_shape(tid, day, amount, name)])
                 for tid, day, amount, name in got]
        held = set()
        for target in sorted(ambiguous[_ambiguous_key(aid, ambiguous)]):
            tids = [t for t, _, _, _ in got]
            if secret is not None:
                tids += [_sync_base.keyed_file_id(secret, t) for t in tids]
            # each line hashed once, however often the archive repeats it
            tids += list(_sync_base.rehashed_file_ids(
                aid, target, lines).values())
            if conn.execute(
                    "SELECT 1 FROM transactions WHERE id = ANY(%s)"
                    "   AND account_id = %s LIMIT 1",
                    (tids, target)).fetchone():
                held.add(target)
        if len(held) == 1:
            moved[aid] = held.pop()


def _account_id(v):
    """An archived account id as it restores: comma-free, the same rename
    the database migration gave ids already stored (see
    adopt.stored_account_id), and then onto the account this household
    already moved it to, if any (see _MOVED_ACCOUNTS). The accounts table
    refuses a comma, and inside the one restore transaction a single
    refused row would abort the whole archive. Blank stays blank for the
    caller's `or None`."""
    if v is None or v == "":
        return v
    v = _comma_free(str(v))
    moved = _MOVED_ACCOUNTS.get()
    if not moved:
        return v
    if v not in moved and _sync_base.NUMBERED_MANUAL_ID_RE.search(v):
        # an id spelling a full number moved under its cut form, its
        # trailing group read as a door's suffix or as the number's own
        for key in _sync_base.lineage_keys(v):
            if key in moved:
                return moved[key]
        return v
    return moved.get(v, v)


# Where a transaction this household already moved now lives: archived id
# -> the live row's id. When a bank is linked after a restore, the
# reconnect guard moves each restored charge the backfill re-delivered onto
# the id the bank gave it, deleting the old id (adopt._rewrite_txn_id), and
# nothing records the old one. Restoring an archive that holds the old id
# again — the same archive, or an older one after a newer whose account
# lineage came with it — files the row onto the live account through
# _MOVED_ACCOUNTS, and an insert that recognises a row only by its id puts
# it back beside its twin: a month of charges counted twice, reported as
# imported. So before the rows land, each archived row on a moved account
# whose id is gone is paired with a live row on that account that the
# archive does not name (the carry copied the row whole, so the date and
# the amount agree and the names share a word), and _rows hands every
# member that names the old id the live one instead: the insert then meets
# the row it already holds, and the row's notes, splits and pins land on
# the live row rather than on nothing.
# The line an export's README carries when its cells were written by the
# lossless guard (pages._csv_cell: the head rule with its doubled
# apostrophe, then the piece rule). Read once per restore into _GUARD_V2,
# which _unescape consults for every cell; absent, the archive is an older
# one and gets the reverse it was written for.
GUARD_MARK = "Spreadsheet guard: v2"
_GUARD_V2: contextvars.ContextVar = contextvars.ContextVar(
    "restore_guard_v2", default=False)


def archive_guard_v2(z: zipfile.ZipFile) -> bool:
    """Was this archive written with the lossless spreadsheet guard?"""
    try:
        with z.open("README.txt") as fh:
            return GUARD_MARK.encode() in fh.read(256 * 1024)
    except KeyError:
        return False


_MOVED_TXNS: contextvars.ContextVar = contextvars.ContextVar(
    "oikonome_restore_moved_txns", default=None)
# the cells of each member that name a transaction by its id
_TXN_ID_CELLS = ("txn_id", "transaction_id", "expense_id", "reimburse_id")
# the cells holding a JSON LIST of transaction ids, by member: each id
# follows the carried map like a scalar cell, first-seen order kept and
# repeats folded (base.carry_unmatch_memory moves them the same way)
_TXN_ID_LIST_CELLS = {"receipts.csv": ("unmatched_txn_ids",)}


def _carry_id_list(v: str, moved: dict) -> str:
    """A JSON list cell of transaction ids with each id moved to the one it
    lands under here; a cell that is not a JSON list is left as written
    (its own pass judges it)."""
    try:
        ids = json.loads(v)
    except (ValueError, RecursionError):
        return v
    if not isinstance(ids, list):
        return v
    out = list(dict.fromkeys(
        moved.get(x, x) for x in ids if isinstance(x, str)))
    return json.dumps(out)


def _txn_key(date, amount) -> tuple | None:
    if _too_long(amount):
        return None
    try:
        day = (date if isinstance(date, _dt.date)
               else _dt.date.fromisoformat(str(date)[:10]))
        return (day, round(float(amount) * 100))
    except (TypeError, ValueError, OverflowError):
        return None


# What the pairing below may hold is sized by the DATABASE, never by the
# archive: a member may declare millions of rows while the ZIP is a few
# hundred KB on the wire (one repeated line deflates to almost nothing),
# and the restore runs inside the web process every household shares.
# So the live rows of the moved accounts — already held, and bounded by
# what the household holds — are what is indexed, and the archive is
# streamed past them, checked against the database a batch at a time.
_MOVED_BATCH = 5000
# More live charges than this on one account, one day, one amount is no
# real ledger; past it the extra ones are simply not offered as twins.
_TWINS_PER_KEY = 32
# An archive may carry more rows on a moved account than the household
# still holds there (charges deleted since), but not without limit: past
# this many, or a few times the live rows, it is not a real export.
_MOVED_ROWS_FLOOR = 100_000
_MOVED_ROWS_PER_LIVE = 4


def _moved_rows(z, moved: dict):
    """(archived id, live account, row) for each archived row on a moved
    account, streamed."""
    for r in _rows(z, "transactions.csv"):
        tid, aid = r.get("id"), r.get("account_id")
        if not tid or not aid:
            continue
        dest = moved.get(_comma_free(str(aid)))
        if dest:
            yield tid, dest, r


def _plan_moved_txns(conn, z) -> dict[str, str]:
    moved = _MOVED_ACCOUNTS.get()
    if not moved:
        return {}
    from .dedup import _tokens
    dests = sorted(set(moved.values()))
    # every live row on a moved account, by (account, date, cents); an
    # entry is [id, tokens], and a taken or disqualified one has id None
    pool: dict[tuple, list] = {}
    by_id: dict[str, list] = {}
    live = 0
    # The live ids on the moved-to accounts (bounded by the database), so
    # the first pass can mark the archive's own rows before the pool is
    # built: a row the archive still names is held, not a carried twin, and
    # must not take a place among the twins kept per key.
    live_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE account_id = ANY(%s)", (dests,))}
    live = len(live_ids)
    if not live:
        return {}
    # First pass: count the archive's rows on moved accounts and collect
    # the live rows the archive itself names. It has to finish before any
    # pairing, since a row may name a live id after an earlier row could
    # have paired with it.
    cap = max(_MOVED_ROWS_FLOOR, _MOVED_ROWS_PER_LIVE * live)
    n = 0
    named: set[str] = set()
    for tid, _dest, _r in _moved_rows(z, moved):
        n += 1
        if n > cap:
            raise _ArchiveRefused(
                f"transactions.csv: too many rows on a moved account (limit "
                f"{cap:,}) — this doesn't look like a genuine Oikonome "
                "export")
        if tid in live_ids:
            named.add(tid)
    del live_ids
    for r in conn.execute(
            """SELECT id, account_id, date, amount, name, merchant_name
                 FROM transactions
                WHERE account_id = ANY(%s)
                ORDER BY id""", (dests,)):
        if r["id"] in named:
            continue
        key = _txn_key(r["date"], r["amount"])
        if key is None:
            continue
        twins = pool.setdefault((r["account_id"], key), [])
        if len(twins) < _TWINS_PER_KEY:
            entry = [r["id"], frozenset(_tokens(r["name"],
                                                r["merchant_name"]))]
            twins.append(entry)
            by_id[r["id"]] = entry
    if not by_id:
        return {}
    out: dict[str, str] = {}

    def _pair(batch: dict) -> None:
        held = {r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE id = ANY(%s)",
            (list(batch),)).fetchall()}
        for tid, (dest, key, toks) in batch.items():
            if tid in held:
                continue
            for live_id, live_toks in pool.get((dest, key), ()):
                if live_id is None:
                    continue
                if (toks & live_toks) or not (toks or live_toks):
                    out[tid] = live_id
                    entry = by_id[live_id]
                    entry[0] = None   # each live row stands for one charge
                    break

    # Second pass: pair a batch at a time, keeping only the id map. A row
    # whose key no live row shares cannot pair, so it is never held.
    batch: dict[str, tuple] = {}
    for tid, dest, r in _moved_rows(z, moved):
        if tid in out or tid in batch or tid in by_id:
            continue                  # paired, queued, or itself live
        key = _txn_key(r.get("date"), r.get("amount"))
        if key is None or (dest, key) not in pool:
            continue
        batch[tid] = (dest, key, frozenset(_tokens(
            _descriptor(r.get("name")),
            _descriptor(r.get("merchant_name")))))
        if len(batch) >= _MOVED_BATCH:
            _pair(batch)
            batch = {}
    if batch:
        _pair(batch)
    return out


def _plan_keyed_txns(conn, z) -> dict[str, str]:
    """Archived id -> the id this household holds the same row under,
    for each archived file row it holds only under another: its keyed
    form, or the id a re-key re-hashed it to.

    A row whose id hashed a full account number is re-id'd under the
    household's secret (base.key_file_import_ids), or re-hashed under the
    id its account moved to (base.rehash_file_rows), and an archive
    written before that names it by the old id: an insert that knows a
    row only by its id would put the line back beside itself, and its
    notes and pins would land on the copy. Both new forms are functions
    of the old id and the row's own fields, so this map is exact; a
    household with no secret has keyed none of its rows."""
    out = _plan_rehashed_txns(conn, z)
    if _sync_base.txn_id_secret(conn) is None:
        return out
    unkeyed = re.compile(_sync_base.UNKEYED_FILE_ID_PATTERN)
    batch: list[str] = []

    def _flush():
        held = _sync_base.held_forms(conn, batch)
        out.update({a: h for a, h in held.items() if h != a})
        batch.clear()

    for r in _rows(z, "transactions.csv"):
        tid = r.get("id")
        if not tid or tid in out or not unkeyed.match(tid):
            continue
        batch.append(tid)
        if len(batch) >= _MOVED_BATCH:
            _flush()
    if batch:
        _flush()
    return out


def _archived_numbered_formers(z) -> dict[str, list[str]]:
    """Archived account id -> every id spelling a full number its rows may
    have been hashed under: itself, when it spells one, and each id the
    archive's adoption records moved onto it in full, followed back
    through earlier moves."""
    back: dict[str, set] = defaultdict(set)
    for r in _rows(z, "account_adoptions.csv"):
        new, old = r.get("account_id"), r.get("old_account_id")
        if new and old and new != old:
            back[_comma_free(str(new))].add(str(old).strip())
    out: dict[str, list[str]] = {}
    for aid in {_comma_free(str(r["id"])) for r in _rows(z, "accounts.csv")
                if r.get("id")} | set(back):
        seen, todo, found = {aid}, [aid], []
        while todo and len(seen) < 32:
            cur = todo.pop(0)
            if _sync_base.NUMBERED_MANUAL_ID_RE.search(cur) \
                    and not _sync_base.SPLIT_ID_RE.search(cur):
                found.append(cur)
            for old in sorted(back.get(cur, ())):
                if old not in seen:
                    seen.add(old)
                    todo.append(old)
        if found:
            out[aid] = found
    return out


def _plan_rehashed_txns(conn, z) -> dict[str, str]:
    """Archived id -> the id a re-key here re-hashed the same row to, for
    each archived file row hashed under a hand-made id that spelled a full
    number which this household has since moved off (base.
    rekey_numbered_accounts): the same line hashed under the account the
    row lands on, or an OFX FITID row renamed onto it, proved from the
    archived row's own fields (base.rehashed_file_id) and taken only when
    held there. A restore of an older archive into a household that
    already moved then meets every row it holds; one it does not hold
    lands under its archived id and is re-hashed with the rest after."""
    formers = _archived_numbered_formers(z)
    if not formers:
        return {}
    got: list[tuple] = []
    same: dict[tuple, int] = defaultdict(int)
    for r in _rows(z, "transactions.csv"):
        tid, aid = r.get("id"), r.get("account_id")
        if not tid or not aid:
            continue
        aid = _comma_free(str(aid))
        if aid not in formers:
            continue
        got.append((str(tid), aid, r.get("date"), r.get("amount"),
                    r.get("name")))
        same[(aid,) + _sync_base.file_row_shape(
            str(tid), r.get("date"), r.get("amount"), r.get("name"))] += 1
    secret = _sync_base.txn_id_secret(conn)
    out: dict[str, str] = {}
    want: dict[str, tuple[str, str]] = {}

    def _flush():
        by_target: dict[str, list[str]] = defaultdict(list)
        for new, target in want.values():
            by_target[target].append(new)
        for target, news in by_target.items():
            held = _sync_base.held_forms(conn, news, target)
            for tid, (new, t) in want.items():
                if t == target and new in held:
                    out[tid] = held[new]
        want.clear()

    # one pass per archived account and former id, each line hashed once
    # however often the archive repeats it (base.rehashed_file_ids); a row
    # the first former proves is not asked of the next
    by_aid: dict[str, list] = defaultdict(list)
    for tid, aid, day, amount, name in got:
        by_aid[aid].append((tid, day, amount, name, same[
            (aid,) + _sync_base.file_row_shape(tid, day, amount, name)]))
    for aid in sorted(by_aid):
        target = _account_id(aid)
        if not target:
            continue
        rest = by_aid[aid]
        for former in formers[aid]:
            if not rest:
                break
            proved = _sync_base.rehashed_file_ids(former, target, rest,
                                                  secret)
            for tid, new in proved.items():
                if new != tid:
                    want[tid] = (new, target)
                if len(want) >= _MOVED_BATCH:
                    _flush()
            rest = [x for x in rest if x[0] not in proved]
    if want:
        _flush()
    return out


def _plan_foreign_keyed_txns(conn, z, carried: dict) -> dict[str, str]:
    """Archived id -> the id it lands under here, for each archived file
    row under a keyed id (base.keyed_file_id) this household does not hold.

    Such an id was keyed under the secret of the household the archive
    came from, which never travels, so nothing here can recompute it: a
    re-import would find the row only by content (FileImportIds.
    _adopt_by_content), and every door that looks a row up by the id an
    earlier reading gave it would miss it. So the row lands under the id
    an import here would key it by: the plain id an import mints for that
    line on the account it lands on (base.file_row_id — its date, cents,
    masked name, and its place among identical lines of the archive),
    keyed under this household's secret, minted for it if there is none.
    Where this household already holds that line (plain, or keyed by
    itself), the archived row is that row, as an archive under plain ids
    meets it, and its notes and pins land there. Where it holds the line
    under an id nothing here recomputes — keyed by itself from an id the
    archive cannot spell (the pre-masking name, the old numbered account),
    or minted over a name the ledger stores cut or without its NULs — the
    archived row takes an unclaimed row of the same importer, account and
    line (base.rows_by_line_shape), as a re-import would. Each row here
    stands for one archived line, and a row the archive itself holds is
    never another line's. A row whose account, date or amount cannot be
    read keeps its archived id."""
    keyed_re = re.compile(_sync_base.KEYED_FILE_ID_PATTERN)
    out: dict[str, str] = {}
    # every row here the archive names is that archived line's own
    claimed = set(carried.values())
    named: list[str] = []
    foreign = False
    for r in _rows(z, "transactions.csv"):
        tid = r.get("id")
        if _sync_base.is_file_import_id(tid):
            named.append(tid)
            foreign = foreign or bool(keyed_re.match(tid))
    if not foreign:
        return out
    claimed |= _sync_base._held_ids(conn, named)
    del named
    places: dict[tuple, int] = {}
    secret: bytes | None = None
    batch: list[tuple] = []

    def _flush():
        nonlocal secret
        held = _sync_base._held_ids(conn, [b[0] for b in batch])
        dates: dict[tuple, set] = defaultdict(set)
        for tid, prefix, aid, shape, _amount, _name in batch:
            if tid not in held:
                dates[(prefix, aid)].add(shape[0])
        pools: dict[tuple, dict] = {}

        def _same_line(prefix, aid, shape):
            if (prefix, aid) not in pools:
                pools[(prefix, aid)] = _sync_base.rows_by_line_shape(
                    conn, prefix, aid, dates[(prefix, aid)], plain=True,
                    skip=claimed)
            rows = pools[(prefix, aid)].get(shape) or []
            while rows and rows[0] in claimed:
                rows.pop(0)
            return rows.pop(0) if rows else None

        for tid, prefix, aid, shape, amount, name in batch:
            if tid in held or tid in out:
                continue
            key = (prefix, aid, shape)
            while True:
                n = places.get(key, 0) + 1
                places[key] = n
                plain = _sync_base.file_row_id(prefix, aid, shape[0], amount,
                                               name, n)
                have = _sync_base.held_forms(conn, [plain], aid).get(plain)
                if have is not None:
                    if have in claimed:
                        continue     # an earlier archived line took it
                    target = have
                else:
                    target = _same_line(prefix, aid, shape)
                    if target is None:
                        if secret is None:
                            secret = _sync_base.txn_id_secret(conn,
                                                              create=True)
                        target = _sync_base.keyed_file_id(secret, plain)
                        if target in claimed:
                            continue
                break
            claimed.add(target)
            out[tid] = target
        batch.clear()

    for r in _rows(z, "transactions.csv"):
        tid = r.get("id")
        if (not tid or not keyed_re.match(tid)
                or _too_long(r.get("amount")) or _too_long(r.get("date"))):
            continue
        aid = _account_id(r.get("account_id"))
        shape = _sync_base.line_shape(r.get("date"), r.get("amount"),
                                      r.get("name"))
        if not aid or shape is None:
            continue
        batch.append((tid, tid.split(":", 1)[0], aid, shape,
                      float(r.get("amount")), r.get("name") or ""))
        if len(batch) >= _MOVED_BATCH:
            _flush()
    if batch:
        _flush()
    return out


def _plan_plain_by_content(conn, z, carried: dict) -> dict[str, str]:
    """Archived id -> a row of the same importer, account and line this
    household holds under an id nothing here recomputes, for each archived
    file row under a plain id it holds in no form and no exact map placed.

    The last pass, after every exact one. An archive names a line by the
    id an import minted for it, its name hashed masked; a household that
    imported the same line before names were masked holds it under the id
    hashed over the whole name, which the archive (storing the name cut)
    cannot spell. Without this the line lands beside itself and its notes
    and pins land on the copy. The archived row takes an unclaimed row of
    the same line (base.rows_by_line_shape), as a re-import would
    (FileImportIds._adopt_by_content): each row stands for one archived
    line, in id order, and a row the archive itself names, or an earlier
    pass placed a row on, is never taken."""
    unkeyed = re.compile(_sync_base.UNKEYED_FILE_ID_PATTERN)
    out: dict[str, str] = {}
    named: list[str] = []
    for r in _rows(z, "transactions.csv"):
        tid = r.get("id")
        if _sync_base.is_file_import_id(tid):
            named.append(tid)
    if not any(unkeyed.match(t) and t not in carried for t in named):
        return out
    claimed = set(carried.values()) | _sync_base._held_ids(conn, named)
    del named
    batch: list[tuple] = []

    def _flush():
        dates: dict[tuple, set] = defaultdict(set)
        for tid, prefix, aid, shape in batch:
            if tid not in claimed:
                dates[(prefix, aid)].add(shape[0])
        pools = {k: _sync_base.rows_by_line_shape(
                     conn, k[0], k[1], ds, plain=True, skip=claimed)
                 for k, ds in dates.items()}
        for tid, prefix, aid, shape in batch:
            if tid in claimed:
                continue                  # held under its own id
            rows = pools[(prefix, aid)].get(shape) or []
            while rows and rows[0] in claimed:
                rows.pop(0)
            if rows:
                target = rows.pop(0)
                claimed.add(target)
                out[tid] = target
        batch.clear()

    for r in _rows(z, "transactions.csv"):
        tid = r.get("id")
        if not tid or tid in carried or not unkeyed.match(tid):
            continue
        aid = _account_id(r.get("account_id"))
        shape = _sync_base.line_shape(r.get("date"), r.get("amount"),
                                      r.get("name"))
        if not aid or shape is None:
            continue
        batch.append((tid, tid.split(":", 1)[0], aid, shape))
        if len(batch) >= _MOVED_BATCH:
            _flush()
    if batch:
        _flush()
    return out


def _comma_free(v: str) -> str:
    if "," not in v:
        return v
    planned = _COMMA_RENAMES.get()
    if planned and v in planned:
        return planned[v]
    from .adopt import account_id_without_comma
    return account_id_without_comma(v)


def _plan_comma_renames(conn, z) -> dict[str, str]:
    """Where each comma id in the archive restores. The plain rename,
    unless the archive (or the household) already holds a different
    account under that name: then the id takes the migration's other
    spelling, adopt.account_id_when_taken — otherwise the two accounts
    would restore as one, the second row skipped as already present and
    its transactions filed under the first."""
    from .adopt import (account_id_when_taken, account_id_without_comma,
                        stored_account_id)
    ids = {str(r["id"]) for r in _rows(z, "accounts.csv") if r.get("id")}
    claimed = {i for i in ids if "," not in i}
    out: dict[str, str] = {}
    for aid in sorted(i for i in ids if "," in i):
        new = (account_id_when_taken(aid)
               if account_id_without_comma(aid) in claimed
               else stored_account_id(conn, aid))
        claimed.add(new)
        out[aid] = new
    return out


def _rename_account_keys(cfg: dict, renamed: dict[str, str]) -> None:
    """Carry the comma renames into a restored settings document, in the
    keys that hold an account id — otherwise an excluded or pinned account
    silently stops being excluded or pinned."""
    if not renamed:
        return
    from .adopt import _rewrite_account_keys
    for old, new in renamed.items():
        _rewrite_account_keys(cfg, old, new)


def clean_bill_raw(v) -> dict:
    """A bill's raw config in the shape save_bill would have written: the
    recurrence lists as ints, envelope_months a number, companions a
    list. The normal write path validates these; a restore bypassed it,
    and one malformed anchor crashed every Today load for the household."""
    raw = dict(_raw_dict(v))
    # scheduled_since dropped here is supplied again by the nightly fill
    # (bills.fill_scheduled_since)
    for key in ("lastDueOn", "dueOn", "scheduled_since"):
        if key in raw:
            try:
                if not isinstance(raw[key], str):
                    raise ValueError
                as_date(raw[key])
            except (TypeError, ValueError, OverflowError):
                raw.pop(key, None)
    rec = raw.get("recurrence")
    if rec is not None and not isinstance(rec, dict):
        raw.pop("recurrence", None)
        rec = None
    if isinstance(rec, dict):
        rec = dict(rec)
        # a date that does not read as one (an impossible day, a year
        # outside the range the writers accept) is dropped here, so no
        # later reader meets it: the readers treat a missing end as no end,
        # a missing floor as no floor, and a database cast would not
        for key in ("endOn", "dueOn"):
            if key in rec:
                try:
                    if not isinstance(rec[key], str):
                        raise ValueError
                    as_date(rec[key])
                except (TypeError, ValueError, OverflowError):
                    rec.pop(key, None)
        for key in ("byMonthDay", "byMonth", "bySetPos"):
            if key in rec:
                vals = rec[key] if isinstance(rec[key], (list, tuple)) else []
                ints = []
                for x in vals:
                    # a parsed JSON number can be Infinity (int() raises
                    # OverflowError) or a 400-digit integer (float() does),
                    # and either must drop the field like any other bad
                    # value rather than abort the whole restore — so every
                    # cleaner here also catches OverflowError
                    try:
                        ints.append(int(x))
                    except (TypeError, ValueError, OverflowError):
                        continue
                if ints:
                    rec[key] = ints
                else:
                    rec.pop(key, None)
        if "byDay" in rec:
            codes = ([c for c in rec["byDay"] if isinstance(c, str)]
                     if isinstance(rec["byDay"], list) else [])
            if codes:
                rec["byDay"] = codes
            else:
                rec.pop("byDay", None)
        if "interval" in rec:
            try:
                rec["interval"] = max(1, min(240, int(rec["interval"])))
            except (TypeError, ValueError, OverflowError):
                rec.pop("interval", None)
        if "frequency" in rec and not isinstance(rec["frequency"], str):
            rec.pop("frequency", None)
        # 240 is the count cap, not the length cap: a YEARLY:240 bill rolls
        # its due date centuries out, the nightly skips it for good and it
        # reads overdue forever. The count is brought down to the longest
        # cycle the engine accepts for this frequency — the nearest bill
        # that can still be maintained and edited.
        if isinstance(rec.get("interval"), int):
            while (rec["interval"] > 1
                   and _bills.cadence_too_long(rec.get("frequency"),
                                               rec["interval"])):
                rec["interval"] -= 1
        raw["recurrence"] = rec
    if "envelope_months" in raw:
        try:
            raw["envelope_months"] = 12 if int(raw["envelope_months"]) >= 12 else 1
        except (TypeError, ValueError, OverflowError):
            raw.pop("envelope_months", None)
    if "companions" in raw and not isinstance(raw["companions"], list):
        raw.pop("companions", None)
    if isinstance(raw.get("companions"), list):
        # each companion's amount is cast in SQL by the ledger query and
        # the daily report's due_soon — free text there aborted both, the
        # same blast radius as a liability's "N/A" statement balance
        kept = []
        for c in raw["companions"]:
            if not isinstance(c, dict):
                continue
            try:
                amt = float(c.get("amount"))
            except (TypeError, ValueError, OverflowError):
                continue
            if (isinstance(c.get("amount"), bool) or not math.isfinite(amt)
                    or abs(amt) > MAX_AMOUNT):
                continue
            kept.append({**c, "amount": amt})
        raw["companions"] = kept
    if "amount_history" in raw:
        # every bill matcher reads this; the engine's own reader decides
        # what a real past price is, so the restore keeps exactly that
        hist = _budget.amount_history_of(raw)
        if hist:
            raw["amount_history"] = hist
        else:
            raw.pop("amount_history", None)
    return raw


_LIABILITY_AMOUNTS = ("last_statement_balance", "minimum_payment_amount",
                      "last_payment_amount")
_LIABILITY_DATES = ("next_payment_due_date", "last_payment_date",
                    "last_statement_issue_date")


def clean_liability_raw(v) -> dict:
    """A card's liabilities raw in the shape the aggregator pull writes:
    the balances numbers, the dates ISO days. The forecast casts
    last_statement_balance in SQL and parses next_payment_due_date in
    Python, and neither path was ever meant to see free text — one
    restored "N/A" raised out of every Today load, the Accounts page and
    the nightly email for the household. A value that is not a finite
    number / a real day is dropped; the readers treat a missing field as
    "the issuer never said", which is the conservative case."""
    raw = dict(_raw_dict(v))
    for key in _LIABILITY_AMOUNTS:
        if key not in raw:
            continue
        val = raw[key]
        try:
            num = float(val)
        except (TypeError, ValueError, OverflowError):
            num = None
        # bool is an int to float(); a True balance is not a number anyone
        # meant, and 'nan'/'inf' parse but poison the min()/max() walk
        if isinstance(val, bool) or num is None or not math.isfinite(num):
            raw.pop(key, None)
        elif isinstance(val, str):
            raw[key] = num
    for key in _LIABILITY_DATES:
        if key not in raw:
            continue
        try:
            day = as_date(raw[key])
        except ValueError:
            day = None
        if day is None:
            raw.pop(key, None)
        else:
            raw[key] = day.isoformat()
    return raw


def normalize_notify_shapes(cfg: dict) -> list[str]:
    """Coerce the notification settings a ZIP carries into the shapes the
    settings door would have written, dropping what cannot be. The worker
    reads these every hour; a value the door refuses (an hour of "x", a
    recipient list that is one string, a phone that is not an object)
    would raise out of the due-check and silence every cadence for the
    household, or be read letter by letter. Returns the notes to show."""
    notes: list[str] = []
    sched = cfg.get("email_schedule")
    if "email_schedule" in cfg:
        if not isinstance(sched, dict) or not sched:
            cfg.pop("email_schedule", None)
            sched = None
    if isinstance(sched, dict):
        clean: dict = {}
        for cad in ("daily", "monthly", "yearly"):
            c = sched.get(cad)
            if not isinstance(c, dict):
                continue
            try:
                hour = int(c.get("hour", 7))
            except (TypeError, ValueError, OverflowError):
                hour = -1
            if not 0 <= hour <= 23:
                notes.append(f"the {cad} email schedule in this export was "
                             "malformed and was not restored")
                continue
            # a stray field (an old weekly entry's weekday, say) is ignored,
            # not a reason to drop the entry
            entry = {"on": bool(c.get("on")), "hour": hour}
            for flag in ("sms", "push"):
                if c.get(flag):
                    entry[flag] = True
            if cad == "daily" and c.get("summary"):
                entry["summary"] = True
            clean[cad] = entry
        if clean:
            cfg["email_schedule"] = clean
        else:
            cfg.pop("email_schedule", None)
    if "email_send_hour_utc" in cfg:
        try:
            cfg["email_send_hour_utc"] = max(0, min(
                23, int(cfg["email_send_hour_utc"])))
        except (TypeError, ValueError, OverflowError):
            cfg.pop("email_send_hour_utc", None)
    rec = cfg.get("email_recipients")
    if isinstance(rec, str):
        rec = [x.strip() for x in rec.split(",")]
    if rec is not None and not isinstance(rec, list):
        rec = []
    if rec is not None:
        rec = [x.strip() for x in rec if isinstance(x, str) and x.strip()]
        if rec:
            cfg["email_recipients"] = rec
        else:
            cfg.pop("email_recipients", None)
    muted = cfg.get("email_muted")
    if muted is not None:
        # same cap as the settings door — a list of strings, bounded, so a
        # crafted ZIP cannot plant a document every config load re-parses
        from ..web.mailguard import MAX_RECIPIENTS
        muted = ([m.strip().lower() for m in muted
                  if isinstance(m, str) and m.strip()][:MAX_RECIPIENTS]
                 if isinstance(muted, list) else [])
        if muted:
            cfg["email_muted"] = muted
        else:
            cfg.pop("email_muted", None)
    for key in ("notify_phone", "notify_phone_pending"):
        if key in cfg and not isinstance(cfg[key], dict):
            cfg.pop(key, None)
    return notes


def _drop_sms_toggles_without_verified_phone(cfg: dict, dest: dict) -> None:
    sched = cfg.get("email_schedule")
    if not isinstance(sched, dict):
        return
    phone = dest.get("notify_phone") or {}
    if isinstance(phone, dict) and phone.get("verified") and phone.get("number"):
        return
    for entry in sched.values():
        if isinstance(entry, dict) and entry.get("sms"):
            entry["sms"] = False


# A restore ZIP is attacker-shaped input (script tokens can reach the
# import door) — a zip bomb declares gigabytes behind a few hundred bytes.
# Caps are checked against the directory's DECLARED sizes before any member
# is read; Python's zipfile never inflates past the declared size, so the
# declared sum is the real expansion bound.
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024

# The members whose size follows the household's photos rather than its
# ledger. Every receipt travels as base64 of its image and its optimised
# copy, so a few dozen phone photos pass the per-file cap, and a household
# that keeps receipts could not restore its own export. What these members
# hold is already compressed (JPEG, PNG, WebP, PDF), so the honest member
# expands to about a third more than it takes in the ZIP; a bomb expands a
# thousandfold. They are bounded by that ratio instead of by size, each row
# is still held to a live upload's limit as it streams in, and the rest of
# the archive keeps the caps above.
BLOB_MEMBERS = frozenset({"receipts.csv"})
BLOB_MEMBER_MAX_RATIO = 4


def zip_size_problem(infos) -> str | None:
    """Why a restore would refuse an archive with these members, or None.
    The one rule both sides read: the restore refuses on it, and the export
    checks what it just wrote against it."""
    infos = list(infos)
    if len(infos) > _upload_zip.MAX_ARCHIVE_MEMBERS:
        return (f"this ZIP holds {len(infos):,} files — more than the "
                f"{_upload_zip.MAX_ARCHIVE_MEMBERS:,} a restore opens")
    total = 0
    for info in infos:
        if info.filename in BLOB_MEMBERS:
            if (info.file_size > MAX_MEMBER_BYTES
                    and info.file_size > BLOB_MEMBER_MAX_RATIO
                    * max(info.compress_size, 1)):
                return (f"{info.filename} in this ZIP expands "
                        f"{info.file_size // max(info.compress_size, 1)} "
                        "times over — more than photos ever do")
            continue
        if info.file_size > MAX_MEMBER_BYTES:
            return (f"{info.filename} in this ZIP is too large to restore "
                    f"({info.file_size // (1024 * 1024)} MB — the per-file "
                    f"limit is {MAX_MEMBER_BYTES // (1024 * 1024)} MB)")
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        return (f"this ZIP expands to {total // (1024 * 1024)} MB — more "
                f"than the {MAX_TOTAL_BYTES // (1024 * 1024)} MB restore "
                "limit")
    return None


def _check_zip_sizes(z: zipfile.ZipFile) -> None:
    problem = zip_size_problem(z.infolist())
    if problem:
        raise _ArchiveRefused(problem)


def _txn_exists(conn, txn_id) -> bool:
    """Does this restored transaction exist yet?

    Migration 067 gave the transaction-child tables real foreign keys, which
    means restore can no longer insert an annotation whose transaction is
    absent. That is normally impossible — transactions.csv is restored first
    — but a backup taken BEFORE 067 can legitimately contain orphans, and a
    restore is exactly the wrong moment to abort on one. Skip the dangling
    row and carry on; the caller counts it as orphaned, apart from the
    rows that were simply already present.
    """
    if not txn_id:
        return False
    return conn.execute("SELECT 1 FROM transactions WHERE id=%s",
                        (txn_id,)).fetchone() is not None


# Why an archived row did not land as a row of its own. Every row a pass
# reads ends in exactly one place: written (counted under its table), or
# under one of these. Each is the whole truth of what its counter holds.
REFUSALS: dict[str, str] = {
    "present": "the destination already holds the row, or one that takes "
               "its place: the same key, the same income document, the one "
               "fronted movement a charge may carry, the person's own split "
               "on that charge",
    "orphaned": "its parent (a transaction, account, business or receipt) "
                "is not here: the archive did not carry it, or this restore "
                "left it out",
    "out_of_range": "an amount beyond any real sum",
    "malformed": "a row no door of the app writes: a required cell blank, an "
                 "id that is no id, a value outside its set, an image that "
                 "does not decode, a split that does not add up to its charge",
    "over_cap": "past a ceiling every household is held to: a receipt image "
                "over the upload size or the household's file storage, an "
                "account's lineage past its bound, a partial reimbursement "
                "with nothing of its charge left to repay",
    "merged": "the same thing the destination holds under an id of its own, "
              "so the archive's pointers now name that one instead",
    "rebuilt": "left for the destination to derive again: a store receipt's "
               "split parts, which the store match rebuilds from the "
               "restored receipts, an alert nobody dismissed, which the "
               "next Today build raises again if it still holds, and a "
               "merchant nothing here names, which the merchant pass "
               "removes and makes again when a row names it, and a forecast "
               "night dated the household's today or later, which the "
               "instance keeps itself",
}

# Written rows that were changed on the way in. Each is a subset of its
# table's written count, never a refusal.
REPAIRS: dict[str, str] = {
    "amount_repaired": "transactions the archive gave no amount, restored at "
                       "0 so their receipts, notes and splits keep a charge "
                       "to hang on",
    "value_emptied": "an optional figure past what its column holds (a "
                     "business's tax rate, a member's share), left empty so "
                     "the row and what hangs on it land",
    "payback_trimmed": "a partial reimbursement that, with the other "
                       "paybacks, repaid more than its charge cost or "
                       "claimed more than its deposit brought in, cut to "
                       "it (the newest first)",
}

# the result key a table or refusal is reported under, where it is not its
# own name: the names the import hub has always shown
_RESULT_KEY = {"present": "already_present", "manual_categories": "overrides",
               "tenant_settings": "settings"}


def _write_rows(conn, sql: str, rows: list) -> list:
    if len(rows) == 1:
        return [conn.execute(sql, rows[0]).fetchone()]
    cur = conn.cursor()
    # returning=True keeps executemany pipelined and hands back one
    # result per tuple, in order: which rows landed, not how many
    cur.executemany(sql, rows, returning=True)
    return [cur.fetchone() for _ in cur.results()]


# ---- what a column takes ------------------------------------------------
#
# The restore is ONE transaction, so a cell the database refuses (text in
# a uuid column, a whole number past an INTEGER, a name past its varchar)
# would roll back every other row of the archive. Each row is judged in
# Python, before its write, against the columns its INSERT names, as the
# catalogue declares them: a cell a REQUIRED (NOT NULL) column cannot take
# refuses its row, and one an optional column cannot take is left empty.
# No savepoint: a restore holding thousands of them overflows the
# subtransaction cache every snapshot on the server then pays for. A
# database error that still happens ends the restore, as it always did.

_SHAPES: dict[str, tuple | None] = {}
_COLUMNS: contextvars.ContextVar = contextvars.ContextVar(
    "restore_columns", default=None)
_INSERT_HEAD = re.compile(
    r"\s*INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*(VALUES\s*\(|SELECT\s)",
    re.I | re.S)
_DIRECT = re.compile(r"%s(::[\w\[\]]+)?")
_DEFAULTED = re.compile(r"COALESCE\(\s*%s\s*,.*\)", re.I | re.S)
_SELECT_END = re.compile(r"\s(WHERE|FROM|ON)\b", re.I)


def _insert_shape(sql: str):
    """(table, per parameter: (column, defaulted) or None) for an INSERT
    whose value list names its columns one placeholder each; None when the
    statement is not one (then nothing is judged before it runs)."""
    if sql in _SHAPES:
        return _SHAPES[sql]
    shape = None
    m = _INSERT_HEAD.match(sql)
    if m:
        cols = [c.strip() for c in m.group(2).split(",")]
        values = m.group(3).upper().startswith("VALUES")
        rest, exprs, cur, depth, quoted = sql[m.end():], [], "", 0, False
        for i, ch in enumerate(rest):
            if ch == "'":
                quoted = not quoted
            elif not quoted:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        exprs.append(cur)
                        break
                    depth -= 1
                elif ch == "," and depth == 0:
                    exprs.append(cur)
                    cur = ""
                    continue
                elif (not values and depth == 0
                      and _SELECT_END.match(rest, i)):
                    exprs.append(cur)
                    break
            cur += ch
        if len(exprs) == len(cols):
            params: list = []
            for col, e in zip(cols, exprs):
                e = e.strip()
                if _DIRECT.fullmatch(e):
                    params.append((col, False))
                elif e.count("%s") == 1 and _DEFAULTED.fullmatch(e):
                    params.append((col, True))
                else:
                    params.extend([None] * e.count("%s"))
            total = sql.count("%s")
            if len(params) <= total:
                params.extend([None] * (total - len(params)))
                shape = (m.group(1), params)
    _SHAPES[sql] = shape
    return shape


def _table_columns(conn, table: str) -> dict:
    """The table's columns as the catalogue declares them, read once per
    restore."""
    cache = _COLUMNS.get()
    if cache is None:
        cache = {}
    if table not in cache:
        cache[table] = {r["column_name"]: r for r in conn.execute(
            """SELECT column_name, udt_name, is_nullable = 'YES' AS nullable,
                      character_maximum_length AS chars,
                      numeric_precision AS precision, numeric_scale AS scale
                 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s""",
            (table,)).fetchall()}
    return cache[table]


_INT_RANGE = {"int2": 2**15, "int4": 2**31, "int8": 2**63}
_TYPE_NAMES = {"int2": "smallint", "int4": "integer", "int8": "bigint",
               "uuid": "uuid", "date": "date", "timestamptz": "timestamp",
               "timestamp": "timestamp", "bool": "boolean", "text": "text"}


def _type_name(col: dict) -> str:
    udt = col["udt_name"]
    if udt == "numeric" and col["precision"] is not None:
        return f"numeric({col['precision']},{col['scale']})"
    if udt in ("varchar", "bpchar") and col["chars"] is not None:
        return f"text of at most {col['chars']} characters"
    return _TYPE_NAMES.get(udt, udt)


def _takes(col: dict, v) -> bool:
    """Whether the column takes `v` as psycopg sends it."""
    udt = col["udt_name"]
    if isinstance(v, str) and "\x00" in v:
        return False
    if _too_long(v) and (udt in _INT_RANGE or udt in (
            "numeric", "uuid", "date", "timestamptz", "timestamp")):
        return False       # no figure, id or day is this long (see _f)
    if udt in _INT_RANGE:
        if isinstance(v, bool):
            return True
        if isinstance(v, str):
            try:
                v = int(v.strip())
            except ValueError:
                return False
        if isinstance(v, (int, float, Decimal)):
            try:
                n = round(v)
            except (ValueError, OverflowError):
                return False
            return -_INT_RANGE[udt] <= n < _INT_RANGE[udt]
        return True
    if udt == "numeric" and col["precision"] is not None:
        if isinstance(v, bool) or not isinstance(v, (int, float, Decimal,
                                                     str)):
            return True
        try:
            d = (v if isinstance(v, (float, Decimal))
                 else Decimal(str(v).strip()))
            return _fits_numeric(d, col["precision"], col["scale"])
        except (ArithmeticError, ValueError):
            return False
    if udt == "uuid" and isinstance(v, str):
        try:
            uuid.UUID(v)
            return True
        except ValueError:
            return False
    if (udt in ("varchar", "bpchar") and col["chars"] is not None
            and isinstance(v, str)):
        # Postgres trims trailing spaces past the bound instead of refusing
        return len(v.rstrip(" ")) <= col["chars"]
    if udt == "date" and isinstance(v, str):
        try:
            _dt.date.fromisoformat(v.strip()[:10])
            return True
        except ValueError:
            return False
    if udt in ("timestamptz", "timestamp") and isinstance(v, str):
        try:
            _dt.datetime.fromisoformat(v.strip())
            return True
        except ValueError:
            try:
                _dt.date.fromisoformat(v.strip()[:10])
                return True
            except ValueError:
                return False
    return True


def _fit_row(conn, sql: str, row: tuple):
    """(the row as it lands, the labels of optional cells left empty, the
    label that refuses the row or None). See the section note above."""
    shape = _insert_shape(sql)
    if shape is None or len(shape[1]) != len(row):
        return row, [], None
    table, params = shape
    cols = _table_columns(conn, table)
    out, emptied = list(row), []
    for i, p in enumerate(params):
        if p is None or p[0] not in cols:
            continue
        name, defaulted = p
        col, v = cols[name], row[i]
        if v is None:
            if not col["nullable"] and not defaulted:
                return row, [], f"no {name}"
            continue
        if _takes(col, v):
            continue
        label = f"a {name} that is not a valid {_type_name(col)}"
        if col["nullable"] or defaulted:
            out[i] = None
            emptied.append(label)
        else:
            return row, [], label
    return tuple(out), emptied, None


# how many of a table's skipped rows the summary names
_SKIP_SAMPLE = 5


class _Outcomes:
    """The one tally of a restore. Rows are written through `land`, which
    is the only thing that counts a row written or present; a pass that
    refuses a row says why through `refuse`. So a counter means what its
    entry in REFUSALS says, and nothing a pass adds can make it lie."""

    def __init__(self) -> None:
        self.written: dict[str, int] = {}
        self.refused: dict[tuple[str, str], int] = {}
        self.repaired: dict[tuple[str, str], int] = {}
        # out_of_range refusals and emptied values past a bounded column,
        # by the bound each names (see `_numeric`); the rest of
        # out_of_range is amounts past any real sum
        self.over_bound: dict[str, int] = {}
        self.emptied_bound: dict[str, int] = {}
        # malformed rows a column could not take, by what it was
        self.misfits: dict[str, int] = {}
        # rows whose reading failed in a way no check anticipated, by
        # table: [how many, the first few of them by id or place]
        self.skipped: dict[str, list] = {}
        # what the row being read has counted so far (see begin_row)
        self._row: list | None = None

    def land(self, conn, table: str, sql: str, rows: list,
             *, as_one: bool = False) -> list:
        """Run `sql` once per parameter tuple in `rows` and return, per
        row and in order, the first column its RETURNING clause gave back,
        or None for a row not written. `sql` must end in RETURNING. Each
        row is first judged against the columns it writes (`_fit_row`): a
        row a required column cannot take is refused as malformed, and an
        optional cell the column cannot take is left empty. Any other row
        not written is one the destination already held: a pass checks
        every other reason before it gets here.

        `as_one`: the tuples are spellings of ONE archived row (an alias
        written under its cut and its full key): it counts once, written
        if any spelling was."""
        # per row, in order, the label `_fit_row` refused it under (None
        # for a row it let through), for a hook that must tell a row the
        # destination held from one the column refused
        self.last_misfits: list = []
        if not rows:
            return []
        fitted, refused, emptied = [], [], []
        for row in rows:
            fit, empty, why = _fit_row(conn, sql, row)
            fitted.append(fit)
            refused.append(why)
            emptied.append(empty)
        self.last_misfits = list(refused)
        live = [i for i, why in enumerate(refused) if why is None]
        written = _write_rows(conn, sql, [fitted[i] for i in live])
        if len(written) != len(live):       # pragma: no cover — psycopg
            raise RuntimeError("restore: a write lost track of its rows")
        got: list = [None] * len(rows)
        for i, g in zip(live, written):
            got[i] = g
        # counted by the row coming back, never by its key's value
        n = sum(1 for g in got if g is not None)
        out = [None if g is None else next(iter(g.values())) for g in got]
        if as_one:
            self.wrote(table, 1 if n else 0)
            if n:
                for label in next(e for g, e in zip(got, emptied)
                                  if g is not None):
                    self.emptied(table, label)
            elif live:
                self._add(self.refused, table, "present", 1)
            else:
                self._misfit(table, refused[0])
            return out
        self.wrote(table, n)
        for g, why, empty in zip(got, refused, emptied):
            if why is not None:
                self._misfit(table, why)
            elif g is not None:
                for label in empty:
                    self.emptied(table, label)
        present = sum(1 for g, why in zip(got, refused)
                      if g is None and why is None)
        if present:
            self._add(self.refused, table, "present", present)
        return out

    def _misfit(self, table: str, label: str) -> None:
        self.refuse(table, "malformed")
        self._count(self.misfits, label, 1)

    def begin_row(self) -> None:
        """Start recording what one row's reading counts, so a skip can
        take it back (`skip`)."""
        self._row = []

    def end_row(self) -> None:
        self._row = None

    def skip(self, table: str, ident: str, n: int = 1) -> None:
        """A row of `table` its reading failed on in a way no check
        anticipated, standing for `n` archived rows: whatever its reading
        counted is taken back, then it is refused as malformed and named in
        the summary by `ident` (its id or its place, never what its cells
        hold)."""
        for into, key, k in reversed(self._row or ()):
            into[key] -= k
            if not into[key]:
                del into[key]
        self._row = None
        self.refuse(table, "malformed", n)
        got = self.skipped.setdefault(table, [0, []])
        got[0] += n
        if len(got[1]) < _SKIP_SAMPLE:
            got[1].append(ident)

    def _count(self, into: dict, key, n: int) -> None:
        into[key] = into.get(key, 0) + n
        if self._row is not None:
            self._row.append((into, key, n))

    def wrote(self, table: str, n: int) -> None:
        """`n` rows of `table` written by a statement `land` cannot run
        (a split replaced whole, the settings document)."""
        self.written[table] = self.written.get(table, 0) + n

    def refuse(self, table: str, reason: str, n: int = 1) -> None:
        if reason not in REFUSALS:
            # a pass naming an outcome there is none of is a defect, never
            # a cell: not a ValueError, which a row's reader skips
            raise LookupError(f"no such restore outcome: {reason}")
        self._add(self.refused, table, reason, n)

    def out_of_range(self, table: str, over: str) -> None:
        """A row refused for a value past the bound `over` names."""
        self.refuse(table, "out_of_range")
        self._count(self.over_bound, over, 1)

    def emptied(self, table: str, over: str) -> None:
        """An optional value past the bound `over` names, left empty so
        its row lands."""
        self.repair(table, "value_emptied")
        self._count(self.emptied_bound, over, 1)

    def repair(self, table: str, reason: str, n: int = 1) -> None:
        if reason not in REPAIRS:
            raise LookupError(f"no such restore repair: {reason}")
        self._add(self.repaired, table, reason, n)

    def _add(self, into: dict, table: str, reason: str, n: int) -> None:
        if n:
            self._count(into, (table, reason), n)

    def total(self, reason: str) -> int:
        src = self.repaired if reason in REPAIRS else self.refused
        return sum(n for (_, r), n in src.items() if r == reason)

    def counts(self) -> dict:
        """The restore's result: rows written per table, then each outcome
        that happened, under its result key."""
        out: dict = {_RESULT_KEY.get(t, t): n for t, n in self.written.items()}
        for reason in (*REFUSALS, *REPAIRS):
            n = self.total(reason)
            if n:
                out[_RESULT_KEY.get(reason, reason)] = n
        return out


# A merged-away merchant points at its survivor; the pointer is not
# guaranteed to be one hop, and an archive can carry a chain or even a
# cycle. Same bound the resolver uses.
_MERGE_HOPS = 16


def _normalise_merchant_graph(conn) -> set[str]:
    """Make every merchant-to-merchant pointer name a merchant that is here.

    A restored `parent_id` or `merged_into` naming a merchant the archive
    did not carry becomes NULL, a merge CHAIN is collapsed to its terminal
    survivor, and a merge CYCLE is broken (nobody in it is merged away, so
    the ledger keeps showing all of them rather than resolving forever).

    Returns the ids that really exist, for screening the transaction and
    alias rows that follow."""
    rows = conn.execute(
        "SELECT id::text AS id, parent_id::text AS parent_id, "
        "       merged_into::text AS merged_into FROM merchants").fetchall()
    have = {r["id"] for r in rows}
    merged = {r["id"]: r["merged_into"] for r in rows}

    for r in rows:
        fixes = {}
        if r["parent_id"] is not None and r["parent_id"] not in have:
            fixes["parent_id"] = None
        if r["merged_into"] is not None:
            end = _terminal_survivor(merged, have, r["id"])
            if end != r["merged_into"]:
                fixes["merged_into"] = end
        if fixes:
            sets = ", ".join(f"{c} = %s" for c in fixes)
            conn.execute(f"UPDATE merchants SET {sets} WHERE id = %s",
                         (*fixes.values(), r["id"]))
    return have


def _known_merchant(value, known: set, mapped: dict | None = None):
    """A merchant id from the archive, or NULL when it names no merchant.

    `mapped` sends an archive id the restore folded onto a live merchant
    (same aggregator entity, the destination's own id) to that merchant.

    Compared lower-cased: Postgres renders a uuid lower-case, and a
    hand-edited CSV should not lose a pointer to letter case."""
    v = (value or "").strip().lower() or None
    if v is not None and mapped:
        v = mapped.get(v, v)
    return v if v in known else None


def _uuid_or_none(v):
    try:
        return str(uuid.UUID(str(v).strip()))
    except (TypeError, ValueError, AttributeError):
        return None


def _fold_mapped_merchants(conn, mapped: dict, folded: list) -> None:
    """Carry what the household decided about an archived merchant onto the
    live merchant it was mapped to, and re-point the restored merchants
    that named it.

    The live row is the aggregator's view of the merchant; the archive's
    row is the household's. A name a PERSON gave it (name_source 'manual')
    replaces an aggregator or derived name, and a merge or parent the
    archive recorded is set where the live row has none. Without this a
    restore would keep the destination's name and leave the merchant
    un-merged, silently undoing renames and merges the household made."""
    arch = list(mapped)
    live = [mapped[a] for a in arch]
    for col in ("merged_into", "parent_id"):
        conn.execute(
            f"""UPDATE merchants m SET {col} = v.live::uuid
                  FROM unnest(%s::text[], %s::text[]) AS v(arch, live)
                 WHERE m.{col}::text = v.arch""", (arch, live))
    for live_id, r in folded:
        if (r.get("name_source") or "") == "manual" and r.get("name"):
            conn.execute(
                """UPDATE merchants SET name = %s, name_source = 'manual',
                          updated_at = now()
                    WHERE id = %s AND name_source <> 'manual'""",
                (_descriptor(r["name"]), live_id))
        for col in ("merged_into", "parent_id"):
            target = (r.get(col) or "").strip().lower()
            target = _uuid_or_none(mapped.get(target, target))
            if target is None or target == live_id:
                continue
            conn.execute(
                f"""UPDATE merchants SET {col} = %s, updated_at = now()
                     WHERE id = %s AND {col} IS NULL""", (target, live_id))


def _terminal_survivor(merged: dict, have: set, start: str):
    """The end of `start`'s merged-into chain, or None when the chain runs
    out of the archive or loops back on itself."""
    seen = {start}
    cur = merged.get(start)
    last = None
    while cur is not None:
        if cur not in have or cur in seen or len(seen) >= _MERGE_HOPS:
            return None                  # dangling, or a cycle: no survivor
        seen.add(cur)
        last = cur
        cur = merged[cur]
    return last


def _income_document_identity(r: dict, year, form, amounts) -> tuple:
    """(the columns an archived tax document is stored and looked up by,
    a hashable key over the same values). Both are taken from the amounts
    as they are STORED — non-finite numbers read as null — so two archived
    documents that store the same are the same document, and two that
    store differently never share a key."""
    stored = _finite_only(amounts)
    ident = (year, form, r.get("payer") or None, _ein_last4_for_restore(r),
             _f(r.get("primary_amount")),
             jsonb(stored) if stored is not None else None,
             r.get("notes") or None)
    key = (*ident[:5], json.dumps(stored, sort_keys=True), ident[6])
    return ident, key


def _ein_last4_for_restore(r: dict) -> str | None:
    """The last four of a restored W-2's EIN, never the EIN.

    A pre-migration-077 export carries the full number in `ein`. Restoring
    it would put back exactly what 077 removed, so the value is reduced here
    at the door instead."""
    import re as _re
    v = (r.get("ein_last4") or "").strip()
    if v:
        return v[-4:]
    digits = _re.sub(r"\D", "", str(r.get("ein") or ""))
    return digits[-4:] if len(digits) >= 4 else None


def _unescape(v):
    # Reverse pages._csv_cell's spreadsheet-formula
    # neutralization. The export prefixes ' onto cells starting
    # = + - @ tab CR — correct for Excel, but this same ZIP is the
    # supported restore input, and without the reverse step a bank's
    # '-ACH DEBIT ...' descriptor landed as "'-ACH DEBIT" forever,
    # and exact-string merchant rules (llm_categorize joins on the
    # raw merchant) silently stopped matching after migration.
    # The exact rule lives in one place — api._csv_safe — so the two
    # sides cannot drift: undo the quote when what follows it is what
    # the export would have quoted.
    # An archive written with GUARD_MARK also guards the pieces after a
    # ';', tab or line break (api._csv_piece_safe, after the head rule) and
    # adds a quote in front of a household's own leading apostrophe, so
    # both steps there are exact inverses and lose nothing. An older
    # archive was written by the head rule alone, without that doubling:
    # it gets the old head-only reverse, and no piece step that would eat
    # an apostrophe the household typed after a ';'.
    if not isinstance(v, str) or "'" not in v:
        return v
    if not _GUARD_V2.get():
        return _unescape_head_v1(v)
    from ..web.api import _csv_piece_unsafe, _csv_safe
    v = _csv_piece_unsafe(v)
    if v[:1] == "'" and _csv_safe(v[1:]) == v:
        return v[1:]
    return v


def _unescape_head_v1(v: str) -> str:
    """The reverse of the head rule archives carried before GUARD_MARK: a
    quote put in front of = + - @ (behind optional invisible characters)
    or a leading tab/CR, with no doubling of an apostrophe already there."""
    if v[:1] != "'":
        return v
    rest = v[1:]
    lead = ("\t", "\r", "\n", "\ufeff", " ")
    head = rest.lstrip("".join(lead))
    if (rest[:1] in lead + ("=", "+", "-", "@")
            and head[:1] in ("=", "+", "-", "@")) or rest[:1] in ("\t", "\r"):
        return rest
    return v


# The longest a feed's own words about a payee can honestly be. A bank
# descriptor is a fixed-width statement field, an aggregator's merchant name
# is shorter still, and an outlet brand shorter again — the biggest real ones
# run to a couple of hundred characters. A CSV CELL, though, may be megabytes
# (_MAX_CELL is sized for a receipt photo), and these are the strings the
# identity pass at the end of a restore reads word by word, the ones the
# generated search column concatenates, and the ones every payee list shows,
# so one crafted cell is paid for on every read of that ledger from then on.
#
# It is also the difference between a restore and no restore. Several of
# these strings are btree keys — the alias map's own key, a merchant's
# lower(name), a check number — and Postgres refuses an index entry past
# about 2,700 bytes, so ONE absurd cell aborts the whole all-or-nothing
# restore rather than landing oddly. The cap is in characters and a character
# may be four bytes, so it is set well inside that limit.
#
# Clamped rather than refused: a transaction is the household's record of
# money moving, and dropping the row (or the archive) over a long descriptor
# is the worse trade. Nothing honest is ever touched.
MAX_DESCRIPTOR = 600


def _descriptor(v):
    """A restored free-text descriptor: un-escaped, and never longer than a
    real one."""
    # the cell was un-escaped once already, by _rows: a second pass would
    # take a quote off text the household typed
    return v[:MAX_DESCRIPTOR] if isinstance(v, str) else v


def _merchant_name_cells(r) -> tuple:
    """`(merchant_name, merchant_name_set_aside)` for a restored row.

    When the resolver files a charge under the merchant its bank line means,
    the aggregator's name for that one charge MOVES to
    `merchant_name_set_aside` and the row keys on the line from then on, like
    a row the aggregator never named. An archive written by the build that
    instead left the name in place and marked it with a boolean carries the
    stray name in `merchant_name` and `merchant_name_overruled` = true; the
    name is moved here on the way in, so the row means on the destination
    exactly what it meant on the source. An archive older than either
    spelling says nothing about any of this and restores unchanged.
    """
    name = _descriptor(r.get("merchant_name")) or None
    aside = _descriptor(r.get("merchant_name_set_aside")) or None
    if aside is None and _b(r.get("merchant_name_overruled")):
        name, aside = None, name
    return name, aside


# who decided a row's category, when it was a movement of money (migration 098)
_FLOW_PRIMARIES = ("TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "INCOME")
# the aggregator's facts a row carries as columns (base.enrichment_from_raw)
_ENRICHMENT_TEXT = ("check_number", "payment_channel", "payment_processor",
                    "location_city", "location_region", "location_address",
                    "location_postal", "location_store", "mcc")


# the migration that added transactions.category_source and derived it
# for the rows already stored
_CATEGORY_SOURCE_COLUMN = "098_txn_enrichment.sql"


def upgrade_archived_txn(row: dict, raw: dict, *, on_card: bool,
                         has_merchant: bool,
                         before_category_source: bool = True) -> dict:
    """A restored transaction's cells as the migrations that rewrote stored
    rows left them, applied in the order they ran, to every archived row.
    Each step is the rule the ingest applies to a new row, so on a row an
    archive written after the migration carries it changes nothing.

    `row` holds the cells by column name, `raw` the raw record as it lands;
    `on_card` says the row is on a credit card, `has_merchant` that the
    archive pointed it at a merchant row (an archive old enough to need the
    name steps was written before merchant rows existed, and a name is
    never rewritten under a merchant the household already filed it with).

      064  Plaid's own category, from raw.personal_finance_category
      075  a money-in row on a card that reads as a payment is one
      081  a Venmo row the aggregator left unnamed takes its counterparty,
      083  with the rail kept ("Venmo — …"),
      082  and a Zelle row its counterparty from the statement line
      092  the fuel suffix 091 wrote into merchant_name is taken back off,
      093  and the pump goes in merchant_outlet instead (base.fuel_arm)
      094  a proven pump is filed as fuel
      098  the aggregator's facts in raw become columns, and the row says
           who decided its category: only on an archive written before
           the column existed (`before_category_source`, from the
           archive's own age). A newer archive's empty source is the one
           its household had, and lands as it was.
      229  a Coinbase row (id `coinbase:…`, so `row` carries "id") takes
           its amount from its record in the ledger's sign

    A person's category (category_override) is never read or written, a
    category a rule decided (category_source rule_*) is left as the rule
    left it, and a row the aggregator withdrew (removed) keeps what it had,
    as the migrations left it."""
    from .flowmap import CC_PAYMENT_DETAILED, looks_like_card_payment_on_card
    out = dict(row)
    live = not out.get("removed")
    amount = out.get("amount") or 0
    line = out.get("name")
    desc = raw.get("original_description")
    desc = _descriptor(desc) if isinstance(desc, str) else None
    by_rule = str(out.get("category_source") or "").startswith("rule")

    pfc = raw.get("personal_finance_category")
    if out.get("category_plaid") is None and isinstance(pfc, dict):
        for col, key in (("category_plaid", "primary"),
                         ("category_plaid_detailed", "detailed"),
                         ("category_plaid_confidence", "confidence_level")):
            v = pfc.get(key)
            if out.get(col) is None and isinstance(v, str) and v:
                out[col] = _descriptor(v)

    name = out.get("merchant_name")
    if not has_merchant and out.get("merchant_name_set_aside") is None:
        venmo = _sync_base.venmo_counterparty(desc)
        if venmo and (name is None or name == venmo.split(" — ", 1)[1]):
            name = venmo
        zelle = _sync_base.zelle_counterparty(desc or line, amount < 0)
        if zelle and (name is None
                      or _sync_base._ZELLE_DESCRIPTOR.search(name)):
            name = zelle
        given = raw.get("merchant_name")
        if (live and name and isinstance(given, str) and given
                and name.lower() in (given.lower() + " gas",
                                     given.lower() + " fuel")):
            name = _descriptor(given)
    out["merchant_name"] = name

    if (on_card and live and amount < 0 and not by_rule
            and out.get("category_detailed") != CC_PAYMENT_DETAILED
            and looks_like_card_payment_on_card(name or line)):
        out["category_primary"] = "LOAN_PAYMENTS"
        out["category_detailed"] = CC_PAYMENT_DETAILED
        out["category_source"] = "flow"

    if out.get("merchant_outlet") is None and live and name:
        out["merchant_outlet"] = _descriptor(
            _sync_base.fuel_arm(name, desc, line))
    if (out.get("merchant_outlet") and live and not by_rule
            and out.get("category_override") is None
            and (out.get("category_primary") or "") != "TRANSPORTATION"):
        out["category_primary"] = "TRANSPORTATION"
        out["category_detailed"] = "TRANSPORTATION_GAS"
        out["category_source"] = "fuel"

    try:
        en = _sync_base.enrichment_from_raw(raw)
    except OverflowError:
        # a bare integer too large for a float (json reads one as an int)
        # is no coordinate; the row's other facts still land
        loc = {k: v for k, v in raw["location"].items()
               if k not in ("lat", "lon") or _floats(v)}
        en = _sync_base.enrichment_from_raw({**raw, "location": loc})
    for col in _ENRICHMENT_TEXT:
        if out.get(col) is None and en[col] is not None:
            out[col] = _descriptor(en[col])
    # a coordinate off the globe is no coordinate either
    for col, bound in (("location_lat", 90.0), ("location_lon", 180.0)):
        v = en[col]
        if (out.get(col) is None and v is not None and math.isfinite(v)
                and abs(v) <= bound):
            out[col] = v
    if out.get("authorized_date") is None and en["authorized_date"]:
        try:
            out["authorized_date"] = as_date(en["authorized_date"])
        except ValueError:
            pass
    # migration 223, on every archive: an inflow labelled income in another
    # case is the INCOME every exact-match income filter reads (ASCII only,
    # so no collation can read the label another way)
    cp = out.get("category_primary")
    if (isinstance(cp, str) and cp != "INCOME" and cp.isascii()
            and cp.isalpha() and cp.lower() == "income" and amount < 0):
        out["category_primary"] = "INCOME"
    # migration 229, on every archive: a Coinbase row's amount is its
    # record's native_amount in the ledger's sign (positive = money out),
    # and a transfer label follows that sign; an archive written while the
    # rows carried the wallet's sign lands corrected, a newer one as it was
    if str(out.get("id") or "").startswith("coinbase:"):
        from .coinbase_push import ledger_amount, transfer_label
        cb = ledger_amount(raw)
        if cb is not None:
            out["amount"] = cb
            if out.get("category_primary") in ("TRANSFER_IN", "TRANSFER_OUT"):
                out["category_primary"] = transfer_label(cb)
    if (before_category_source and out.get("category_source") is None
            and out.get("category_primary")):
        plaid = out.get("category_plaid")
        out["category_source"] = (
            "plaid" if plaid and out["category_primary"] == plaid
            else "rule" if plaid
            else "flow" if out["category_primary"] in _FLOW_PRIMARIES
            else "import")
    return out


def _floats(v) -> bool:
    """Whether float() of `v` gives a number rather than overflowing."""
    try:
        float(v)
    except OverflowError:
        return False
    except (TypeError, ValueError):
        pass
    return True


def _persons_names(conn, z) -> set[str]:
    """The lowered names a person gave a merchant through an alias (a
    manual one, or one an archive writes with no method, which restores
    as manual), here or in the archive: the names the cut of a machine's
    merchant name leaves as typed. Only names that hold a number matter."""
    names = {r["c"] for r in conn.execute(
        "SELECT lower(canonical) AS c FROM merchant_canonical"
        " WHERE method = 'manual' AND canonical ~ %s",
        (card_number.sql_selector(),)).fetchall()}
    for r in _rows(z, "merchant_canonical.csv"):
        canon = _descriptor(r.get("canonical"))
        if ((r.get("method") or "manual") == "manual"
                and card_number.holds_number(canon)):
            names.add(canon.lower())
    return names


def _alert_identity(kind, message) -> tuple:
    """What makes two alerts one: the kind, and the message as every door
    stores it now, card numbers cut. An older build stored some whole, so
    the household's rows and the archive's are both read through this."""
    return (kind, card_number.cut(message))


def _cut_unless_held(conn, sql: str, value):
    """`value` cut, unless a row `sql` finds by the whole `value` still
    names it so (then nothing would find that row by the cut one)."""
    if not card_number.holds_number(value):
        return value
    if conn.execute(sql, (value,)).fetchone():
        return value
    return card_number.cut(value)


_BILL_BY_PAYEE = "SELECT 1 FROM bills WHERE payee = %s"


def _mark_name(conn, entity_id, name, merchant_id, machine_cut: dict,
               persons_names: set):
    """The merchant name a restored 1099 mark lands under. A mark's name
    follows its merchant's, cut where a machine named the merchant: the
    merchant it points at, or with no pointer the live merchant of its
    name. A name no merchant carries is what a person typed on the mark,
    and stays; so does one another mark of the business already holds
    cut (the nightly fold joins the two)."""
    if not card_number.holds_number(name):
        return name
    cut = card_number.cut(name)
    if merchant_id is not None:
        m = conn.execute("SELECT name, name_source FROM merchants"
                         " WHERE id = %s", (merchant_id,)).fetchone()
        follows = bool(m) and m["name_source"] != "manual" and (
            (m["name"] or "").lower() not in persons_names)
    else:
        follows = name.lower() in machine_cut
    if not follows or conn.execute(
            "SELECT 1 FROM vendor_1099 WHERE entity_id = %s"
            " AND merchant = %s", (entity_id, cut)).fetchone():
        return name
    return cut


def _mark_merchant(conn, entity_id, name):
    """The merchant a 1099 mark that names none is for, as migration 164
    found it for the stored marks: the one live merchant of that name, when
    no other mark of the same business already holds it (that one keeps
    it, and this mark keeps its name alone, which the read side still
    finds). None when no single merchant answers to the name."""
    if not isinstance(name, str) or not name:
        return None
    hits = conn.execute(
        "SELECT id::text AS id FROM merchants"
        " WHERE merged_into IS NULL AND name = %s LIMIT 2",
        (name,)).fetchall()
    if len(hits) != 1:
        return None
    held = conn.execute(
        "SELECT 1 FROM vendor_1099 WHERE entity_id = %s AND merchant_id = %s",
        (entity_id, hits[0]["id"])).fetchone()
    return None if held else hits[0]["id"]


_RULES_FROM_OVERRIDES = "035_canonical_category_backfill.sql"
_MODEL_RULES_ON_THE_MERCHANT = "044_llm_rule_canonical_rekey.sql"
_PRINTED_DAY_NOTE = "220_ofx_rows_dated_by_the_printed_day_say_so.sql"
# the first migration that shipped with printed-day dating (210 and 211
# came with the change, migration 220's own text says); a build older than
# it dated every OFX row by its UTC day
_PRINTED_DAY_DATING = "210_payee_keyed_settings_follow_a_cut_bill_name.sql"


def _dated_by_the_utc_day(schema: str | None) -> bool:
    """Did the build that wrote an archive at `schema` date OFX rows by the
    UTC day? Every build before printed-day dating did, and an archive too
    old to say its age is read as one of them."""
    return _upgrades.older_than(schema, _PRINTED_DAY_DATING)
# No build, on any install, dated OFX lines by the day the bank printed
# before this instant. An archive's import batches keep the times its
# source wrote them, so the destination's own upgrade time says nothing
# about them; the earliest instant can only give the note to a row a
# late-upgrading source still dated by the UTC day, which then doubles on
# a re-import, and a doubled row can be removed, where a missing note can
# lose a charge.
_PRINTED_DAY_DATING_SINCE = _dt.datetime(2026, 11, 7, 17, 31,
                                         tzinfo=_dt.timezone.utc)
# a correction to one of these is a movement of money, never a spend rule
_FLOW_OVERRIDES = _FLOW_PRIMARIES + ("BANK_FEES",)


def derive_user_rules_from_overrides(conn, landed: list[str]) -> int:
    """The person's merchant rules migration 035 derived from their own
    corrections, for the merchants of the corrected rows a restore just
    wrote: a merchant (through merchant_canonical) whose every correction
    by a person agrees gets that category as their rule. A correction of a
    movement of money, or on one, derives nothing; a rule a person already
    holds is never replaced, and a machine's rule at the same key gives way
    to theirs. Only a person's corrections count (override_source 'user'):
    a bill's or a store's category is not the person teaching a merchant.
    Returns rules written."""
    if not landed:
        return 0
    return conn.execute(
        """WITH named AS (
               SELECT t.id, t.removed, t.category_override,
                      t.category_primary, t.override_source,
                      COALESCE(mc.canonical, t.merchant_name, t.name) AS canon
                 FROM transactions t
                 LEFT JOIN merchant_canonical mc
                        ON mc.raw_merchant = COALESCE(t.merchant_name, t.name)),
           touched AS (
               SELECT DISTINCT canon FROM named WHERE id = ANY(%(ids)s)),
           agreed AS (
               SELECT canon, MAX(category_override) AS cat
                 FROM named
                WHERE removed = 0
                  AND override_source = 'user'
                  AND COALESCE(category_override, '') <> ''
                  AND category_override <> ALL(%(flows)s)
                  AND COALESCE(category_primary, '') <> ALL(%(primaries)s)
                  AND canon IN (SELECT canon FROM touched)
                GROUP BY canon
               HAVING COUNT(DISTINCT category_override) = 1)
           INSERT INTO merchant_categories
                  (merchant, category_primary, source, classified_at)
           SELECT canon, cat, 'user', now() FROM agreed
            WHERE canon IS NOT NULL AND canon <> ''
           ON CONFLICT (tenant_id, merchant) DO UPDATE
              SET category_primary = EXCLUDED.category_primary,
                  source = 'user', classified_at = now()
            WHERE merchant_categories.source <> 'user'""",
        {"ids": list(landed), "flows": list(_FLOW_OVERRIDES),
         "primaries": list(_FLOW_PRIMARIES)}).rowcount


def rekey_model_rules_on_the_merchant(conn) -> int:
    """Migration 044 over the household: a model's rules keyed on a raw
    spelling of a merchant fold into one rule on the merchant
    (merchant_canonical), only where every rule resolving to that merchant
    agrees on the category and on being disabled — the condition under
    which the rules already act as one, so no transaction's category
    changes. A merchant whose rules disagree keeps them all; a person's or
    a seed rule is never moved, and a rule already at the merchant's own
    key absorbs the others. Returns the rules removed."""
    return conn.execute(
        """WITH resolved AS (
               SELECT m.merchant, m.source, m.category_primary, m.disabled,
                      m.classified_at,
                      COALESCE(mc.canonical, m.merchant) AS canon
                 FROM merchant_categories m
                 LEFT JOIN merchant_canonical mc ON mc.raw_merchant = m.merchant),
           grp AS (
               SELECT canon,
                      COUNT(*) FILTER (WHERE merchant = canon) AS at_canon,
                      COUNT(DISTINCT category_primary) AS n_cats,
                      COUNT(DISTINCT disabled) AS n_flags,
                      MIN(category_primary) AS cat,
                      BOOL_AND(disabled) AS all_disabled,
                      MAX(classified_at) FILTER (WHERE source = 'llm') AS latest
                 FROM resolved
                GROUP BY canon
               HAVING COUNT(*) FILTER (WHERE merchant <> canon
                                         AND source = 'llm') > 0),
           mergeable AS (
               SELECT * FROM grp WHERE n_cats = 1 AND n_flags = 1),
           del AS (
               DELETE FROM merchant_categories m
                USING merchant_canonical mc, mergeable g
                WHERE mc.raw_merchant = m.merchant
                  AND m.merchant <> mc.canonical
                  AND g.canon = mc.canonical
                  AND m.source = 'llm'
               RETURNING 1),
           ins AS (
               INSERT INTO merchant_categories
                      (merchant, category_primary, source, classified_at,
                       disabled)
               SELECT canon, cat, 'llm', latest, all_disabled
                 FROM mergeable WHERE at_canon = 0
               ON CONFLICT (tenant_id, merchant) DO NOTHING
               RETURNING 1)
           SELECT (SELECT COUNT(*) FROM del) AS n""").fetchone()["n"]


def _follow_moved_bills(cfg: dict, bill_payees: dict) -> None:
    """The settings keyed on a bill's payee (the bills switched off, the
    occurrence caps, the dismissed hints and health flags) renamed onto
    the payee each moved bill landed as."""
    if not bill_payees:
        return
    off = cfg.get("disabled_bills")
    if isinstance(off, list):
        cfg["disabled_bills"] = [bill_payees.get(v, v)
                                 if isinstance(v, str) else v for v in off]
    for key in ("occurrence_caps", "dismissed_hints", "dismissed_health"):
        by_payee = cfg.get(key)
        if isinstance(by_payee, dict):
            cfg[key] = {bill_payees.get(k, k): v
                        for k, v in by_payee.items()}


def _offer_cut(conn, kind, payee, evidence: dict,
               bill_payees: dict) -> tuple:
    """(payee, evidence) of a restored bill offer with every card number
    cut as the bill pass now writes them, once the bills have landed.

    An offer names its bill by payee: one naming a bill this restore moved
    (`bill_payees`, archived payee to the one it landed as) follows it. Its
    own label is the ledger's spelling of a series and is cut where no
    bill is named by it. An offer to join a merchant names the merchant by
    its stored name, and follows it only where a live merchant now holds
    the cut name and none the whole one. The stream's description in the
    evidence is always cut."""
    evidence = dict(evidence)
    joins = kind in ("attach", "merchant")
    bp = evidence.get("bill_payee")
    if joins and isinstance(bp, str):
        bp = bill_payees.get(bp, bp)
        evidence["bill_payee"] = _cut_unless_held(conn, _BILL_BY_PAYEE, bp)
    if kind != "merchant" and isinstance(payee, str):
        if not joins and payee in bill_payees:
            payee = bill_payees[payee]
        payee = _cut_unless_held(conn, _BILL_BY_PAYEE, payee)
    elif kind == "merchant" and card_number.holds_number(payee):
        cut = card_number.cut(payee)
        live = ("SELECT 1 FROM merchants WHERE merged_into IS NULL"
                " AND lower(name) = lower(%s)")
        if (conn.execute(live, (cut,)).fetchone()
                and not conn.execute(live, (payee,)).fetchone()):
            payee = cut
            if isinstance(evidence.get("merchant_name"), str):
                evidence["merchant_name"] = card_number.cut(
                    evidence["merchant_name"])
    if isinstance(evidence.get("description"), str):
        evidence["description"] = card_number.cut(evidence["description"])
    return payee, evidence


def _finite_num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _offer_evidence_typed(ev: dict) -> dict:
    """A restored bill offer's evidence with every value the Bills screen
    reads kept only in the type the detector writes it in: a price change's
    amounts as finite numbers and its day as a date, the monthly totals as
    a map of finite numbers, the named bill and merchant as text. A value
    of any other shape is dropped, so the offer reads as less evidence
    rather than a Bills screen that cannot load."""
    ev = dict(ev)
    for k in ("old", "new"):
        if k in ev and not _finite_num(ev[k]):
            del ev[k]
    if "charged_on" in ev:
        try:
            if not isinstance(ev["charged_on"], str):
                raise ValueError
            as_date(ev["charged_on"])
        except (TypeError, ValueError, OverflowError):
            del ev["charged_on"]
    mt = ev.pop("monthly_totals", None)
    if isinstance(mt, dict):
        mt = {str(k): v for k, v in mt.items() if _finite_num(v)}
        if mt:
            ev["monthly_totals"] = mt
    for k in ("hits", "n"):
        if k in ev and not (_finite_num(ev[k]) and ev[k] == int(ev[k])):
            del ev[k]
    for k in ("bill_payee", "merchant_name", "last_seen", "cycle_days"):
        v = ev.get(k)
        if v is not None and not (isinstance(v, str) or (
                k == "cycle_days" and _finite_num(v))):
            del ev[k]
    # a measured cycle past the longest a bill may repeat on is no
    # measurement: the lapse rule falls back to the offer's cadence
    c = ev.get("cycle_days")
    if _finite_num(c) and not 0 < c <= _bills.MAX_CYCLE_DAYS:
        del ev["cycle_days"]
    # the approve path hands last_seen to the bill writer as a date
    if "last_seen" in ev:
        try:
            as_date(ev["last_seen"])
        except (TypeError, ValueError, OverflowError):
            del ev["last_seen"]
    return ev


def _plan_bill_moves(conn, rows: list) -> list:
    """Where each archived bill lands, decided for all of them before any
    is written: `rows` is (archived id, payee, made by a person) per bill,
    and the answer is (id, payee, held) per row, in order, where `held` is
    the household's (id, payee) the row IS (counted present) or None.

    A detected bill whose payee spells a card number moves to the cut name
    and its id, as migration 206 moved the stored ones, and only where
    that name is free: two bills are never made one. So a row stays as it
    is when the household holds its own id (it is that bill), and keeps
    its own id and payee when another archived bill holds the cut id or
    name, when a person's bill here holds either, or when another
    archived bill cuts to the same id first (the lowest archived id moves, as 206's
    DISTINCT ON). A detected bill the household holds under the cut id or
    name, and no archived bill does, is the same bill an upgrade moved
    before this older archive came back: the row is that bill."""
    here = conn.execute(
        "SELECT id, payee, raw->>'source' IS NOT DISTINCT FROM 'manual'"
        "       AS hand_made FROM bills ORDER BY id").fetchall()
    here_ids = {h["id"] for h in here}
    here_by_payee: dict = defaultdict(list)
    for h in here:
        here_by_payee[h["payee"]].append(h)
    here_by_id = {h["id"]: h for h in here}
    plan: list = [(bid, payee, None) for bid, payee, _hm in rows]
    archived_ids = {bid for bid, _p, _hm in rows}
    archived_payees = {payee for _b, payee, _hm in rows}
    wants: dict[str, list[int]] = defaultdict(list)
    for i, (bid, payee, hand_made) in enumerate(rows):
        if (hand_made or bid in here_ids
                or not card_number.holds_number(payee)):
            continue
        cut_payee = card_number.cut(payee)
        cut_id = _bills._slug(cut_payee)
        # (the row's own payee spells the number, so it is never the cut
        # one; its own id can be, and is no other bill)
        if ((cut_id != bid and cut_id in archived_ids)
                or cut_payee in archived_payees):
            continue
        holders = ([here_by_id[cut_id]] if cut_id in here_by_id else []) + [
            h for h in here_by_payee.get(cut_payee, ()) if h["id"] != cut_id]
        if any(h["hand_made"] for h in holders):
            continue
        wants[cut_id].append(i)
        held = (holders[0]["id"], holders[0]["payee"]) if holders else None
        plan[i] = (cut_id, cut_payee, held)
    for cut_id, takers in wants.items():
        first = min(takers, key=lambda i: rows[i][0])
        for i in takers:
            if i != first:
                plan[i] = (rows[i][0], rows[i][1], None)
    return plan


def restore_zip(conn, data: bytes, *, progress=None,
                starter: str | None = None) -> dict:
    """Merge an export ZIP into this tenant. `progress`, when given, is
    called as (table, rows, done) while the members stream past — the
    background-job door uses it to keep a status row fresh so a large
    restore reports where it is instead of looking hung. `starter` is the
    credential fingerprint of the request that started the restore (the
    background job captures it before leaving the request); None reads the
    request in flight."""
    return _restore(conn, data, _Outcomes(), progress=progress,
                    starter=starter)


def restore_outcomes(conn, data: bytes) -> tuple[dict, _Outcomes]:
    """`restore_zip`, also handing back the tally behind its counts: per
    table, the rows written and each outcome of the rows that were not."""
    out = _Outcomes()
    return _restore(conn, data, out), out


def _restore(conn, data, out: _Outcomes, *, progress=None,
             starter: str | None = None) -> dict:
    token = _PROGRESS.set(progress) if progress is not None else None
    renames = _COMMA_RENAMES.set({})
    moved_txns = _MOVED_TXNS.set({})
    moved = _MOVED_ACCOUNTS.set(_plan_moved_accounts(conn))
    sessions = _START_SESSIONS.set(_starter_watch(conn, starter))
    guard = _GUARD_V2.set(False)
    columns = _COLUMNS.set({})
    nul_cells = _NUL_KEY_CELLS.set({})
    nul_sample = _NUL_KEY_SAMPLE.set({})
    nul_drops = _NUL_KEY_DROPS.set(set())
    try:
        return _restore_zip(conn, data, out)
    finally:
        _NUL_KEY_SAMPLE.reset(nul_sample)
        _NUL_KEY_DROPS.reset(nul_drops)
        _NUL_KEY_CELLS.reset(nul_cells)
        _COLUMNS.reset(columns)
        _START_SESSIONS.reset(sessions)
        _GUARD_V2.reset(guard)
        _MOVED_TXNS.reset(moved_txns)
        _COMMA_RENAMES.reset(renames)
        _MOVED_ACCOUNTS.reset(moved)
        if token is not None:
            _PROGRESS.reset(token)


def _push_fed_items(z) -> dict[str, str]:
    """The push doors' connections an archive written before migration 022
    calls 'csv', by id, with the aggregator 022 gave them. The coinbase door
    names its connections `coinbase-<label>`; the plan door's mark (its
    access token) never travels, but every account it creates is named
    `<connection>-<plan>` for one of plan_csv.PLAN_CODES, and an ordinary
    file import's accounts never are. A push door's connection is
    archived: its script sends the rows, so it stays out of the pull loop
    and the stale-connection checks."""
    from .plan_csv import PLAN_CODES
    plans = {c.lower() for c in PLAN_CODES}
    accounts: dict[str, list[str]] = defaultdict(list)
    for r in _rows(z, "accounts.csv"):
        if r.get("item_id") and r.get("id"):
            accounts[str(r["item_id"])].append(str(r["id"]))
    out: dict[str, str] = {}
    for r in _rows(z, "items.csv"):
        iid = str(r.get("id") or "")
        if not iid or (r.get("aggregator") or "") != "csv":
            continue
        if iid.startswith("coinbase-"):
            out[iid] = "coinbase"
        elif accounts.get(iid) and all(
                a.startswith(iid + "-") and a[len(iid) + 1:] in plans
                for a in accounts[iid]):
            out[iid] = "plan_csv"
    return out


def _renumber_home_ranks(conn, groups: list[str]) -> int:
    """Make home_rank unique within the given link groups, keeping the
    order the ranks already express. An archive can carry two members of
    one group at the same rank (a link edited on one side, or two archives
    merged into one tenant), and nothing on the write side checks it —
    the failover read picks "the lowest healthy rank", which on a tie is
    whichever row the planner returns first, so the household's chosen
    primary could silently swap between reads. Scoped to the groups the
    archive touched: an unscoped UPDATE row-locked every link the tenant
    has for the rest of the restore transaction, and a reorder on the
    Accounts page for an unrelated group hung behind it. Returns rows
    changed."""
    if not groups:
        return 0
    cur = conn.execute(
        """UPDATE account_links l SET home_rank = s.rn
             FROM (SELECT account_id,
                          row_number() OVER (PARTITION BY group_id
                                             ORDER BY home_rank, created_at,
                                                      account_id) - 1 AS rn
                     FROM account_links
                    WHERE group_id = ANY(%s::uuid[])) s
            WHERE l.account_id = s.account_id AND l.home_rank <> s.rn""",
        (groups,))
    return cur.rowcount


_INVITES_MEMBER = "recipient_invites.csv"
_RECIPIENT_INVITES = "074_recipient_invites.sql"
# the build that gave Today its summary face (and the setting that picks
# it) shipped with migration 090; an archive older than it never chose
_TODAY_FACES = "090_tenant_categorizer_model.sql"
_NO_INVITES = {"total": 0, "rows": 0, "accepted": 0, "declined": 0,
               "pending": 0}


def _restore_recipient_invites(conn, z, configured=()) -> dict:
    """Restore what the household's daily-email recipients ANSWERED, under
    one rule: an acceptance is a fact about a mailbox, and only this
    instance's own invite flow may create one.

    This is the half of `email_recipients` that is not config. The address
    list rides in `tenant_settings` like any other setting, but the answer
    lives in a control-plane table, and `worker._recipients_raw` mails an
    extra recipient only if they accepted. So the file is read for answers —
    but a ZIP is untrusted input. Nothing signs it, nothing proves this
    instance produced it, and the person who uploads it is the same person
    whose proposal the invite exists to check. Copying `accepted_at` out of a
    CSV therefore manufactures the one thing the recipient alone may create:
    add a line for somebody's address with a timestamp in it and that mailbox
    starts receiving a household's balances tomorrow morning, having never
    been asked. That is a mail-enrolment primitive, so the file does not get
    to write acceptances.

    What travels, and why each direction is safe:

    * A DECLINE ALWAYS TRAVELS. It is the fail-safe answer — the worst a
      forged one can do is keep mail from being sent — and carrying it is
      what stops the destination's next settings save from re-inviting
      somebody who already said no.
    * AN ACCEPTANCE TRAVELS ONLY FOR AN ADDRESS THIS INSTANCE ALREADY KNOWS
      AS ONE OF ITS OWN USERS. That is the single exception, and it holds
      because the fact is checked HERE rather than read out of the file: the
      address holds an account on this very household, so the daily mail
      tells that person nothing they cannot already read by signing in, they
      can mute themselves in Settings, and on hosted they additionally have to
      prove control of the mailbox through this instance's own verification
      (`mailguard.members_only` is the same proof `_recipients_raw` demands
      of the owner). Forging one for a non-member — the whole attack — lands
      nothing at all.
    * NOTHING ELSE TRAVELS. An unanswered invite is a question, not an
      answer, so it lands no row: the archive's copy has no live link here
      (the raw token is in a recipient's mailbox and points at the source
      instance), and a row with no token reads in Settings → Email as "invite
      sent, waiting for them" or "invite expired" — both of which describe a
      link that does not exist, and both of which leave the owner waiting for
      a confirmation that can never arrive. With no row the same screen says
      "not invited" next to an Invite button, which is true and is the way
      out. `last_sent_at`/`send_count` do not travel either: they feed
      `recipient_invites.invite`'s live resend cooldown and hourly tenant
      ceiling, so an archive full of fresh send stamps could throttle the
      invites this instance is trying to mint.

    A DECISION MADE HERE ALWAYS WINS, and only a decision counts as one. A
    `DO NOTHING` conflict clause would fire on any existing row — including
    the merely PENDING row that `mailguard.invite_added` writes on an
    ordinary settings save — so the ordinary migration shape (sign up on
    the destination, type your recipients, then restore) would throw away
    every answer the archive carried. An ANSWERED row wins; an unanswered
    one is updated in place, keeping its live token so a link already in
    somebody's mailbox is not quietly voided.

    An accepted row is not an authorization boundary and is not being
    treated as one: on hosted, `mailguard.members_only` is what keeps a
    household's balances from reaching an outside inbox, and the restored
    recipient list passes through that filter regardless of what this table
    says. This function is about consent, which is a different question.

    Runs on the ADMIN connection and after the data transaction commits:
    migration 074 grants the app role nothing here on purpose, and a
    consent row must not land for a restore that then failed.

    AN ADDRESS THE ARCHIVE LISTS BUT NEVER ASKED is a recipient from before
    invites existed: `configured` is the archive's own `email_recipients`,
    as it lands, and an archive written before migration 074 carries no
    answers at all. 074 kept those households' mail flowing by recording
    every configured address as having agreed, with no link; the restore
    does the same for each such address under the same one exception as an
    acceptance above — it holds an account here — so a household moved
    from an old install keeps mailing its own members, and an archive
    stripped of its answers cannot enrol anyone else. Every other such
    address must be invited here, and the tally says so — as it does for
    a configured address an archive from after 074 never answered for,
    which is counted pending and written nowhere.

    Returns a tally of what the household actually ends up with — `total`
    addresses in the archive, `rows` written, and how many of those addresses
    are `accepted`, `declined`, or still `pending` here — so the restore can
    say out loud who has to answer again."""
    from ..web.mailguard import split_mailboxes as mailguard_split
    from ..web.mailguard import valid_address
    seen: dict[str, dict] = {}
    for r in _rows(z, "recipient_invites.csv"):
        email = (r.get("email") or "").strip().lower()
        # one bare mailbox, the test every door that stores an address
        # applies: a row is a decision about exactly one person
        if not email or not valid_address(email):
            continue
        # one mailbox is one recipient however many times the file names it
        seen[email] = r
        if len(seen) >= _MAX_RECIPIENT_INVITES:
            break
    # the archive's recipients it holds no answer for, one bare mailbox
    # per entry (a joined entry names several people, and one consent is
    # not several people saying yes)
    unasked: list[str] = []
    # only an archive with no answers member at all is from before invites:
    # one that has it says, by leaving an address out, that it was never
    # asked, and nobody here may answer for it. The export began carrying
    # the member some time after the _RECIPIENT_INVITES migration, so an
    # archive without it whose rows show that migration or later
    # (archive_upgrades.archive_schema) held answers it did not carry: a
    # decline among them is not consent, and its
    # recipients are invited again. Recipients nobody here may answer for
    # are still counted, unwritten, as pending: Settings goes on listing
    # them while the worker mails only who accepted, so the restore says
    # who must be invited again rather than let their mail stop in silence
    enrol = (_INVITES_MEMBER not in z.namelist()
             and _upgrades.older_than(_upgrades.archive_schema(z),
                                      _RECIPIENT_INVITES))
    if not isinstance(configured, (list, tuple)):
        configured = ()
    # the owner is mailed by construction and is never invited (nor counted)
    owners = _owner_addresses(conn) if configured else set()
    for entry in configured:
        boxes, _bad = mailguard_split([entry])
        if len(boxes) != 1:
            continue
        email = boxes[0].strip().lower()
        if (email in seen or email in unasked or email in owners
                or not valid_address(email)
                or len(seen) + len(unasked) >= _MAX_RECIPIENT_INVITES):
            continue
        unasked.append(email)
    if not seen and not unasked:
        return dict(_NO_INVITES)
    cur = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS tid").fetchone()
    tid = cur["tid"] if cur else None
    if not tid:
        return dict(_NO_INVITES)
    from ..db import tenancy
    from ..web import mailguard
    written = 0
    admin = tenancy.admin_connect()
    try:
        with admin.transaction():
            # The one fact this instance can establish for itself. On hosted
            # a user row is only proof of mailbox control once verification
            # has been answered, which is exactly the test the owner has to
            # pass in `worker._recipients_raw`.
            proof = (" AND verified_at IS NOT NULL"
                     if mailguard.members_only() else "")
            members = {r["email"].lower() for r in admin.execute(
                "SELECT email FROM users WHERE tenant_id = %s" + proof,
                (tid,)).fetchall()}
            for email, r in seen.items():
                declined_at = _ts(r.get("declined_at"))
                # a decline read out of the file is believed; an acceptance
                # only for an address this household already knows as a user
                accepted_at = (None if declined_at or email not in members
                               else _ts(r.get("accepted_at")))
                if not declined_at and not accepted_at:
                    continue
                inviter = (r.get("invited_by_email") or "").strip().lower()
                c = admin.execute(
                    """INSERT INTO recipient_invites
                           (tenant_id, email, token_hash, invited_by,
                            created_at, expires_at, accepted_at,
                            declined_at, last_sent_at, send_count)
                       VALUES (%s, %s, NULL,
                               (SELECT id FROM users
                                 WHERE tenant_id = %s
                                   AND lower(email) = %s),
                               COALESCE(%s, now()), %s, %s, %s, NULL, 0)
                       ON CONFLICT (tenant_id, email) DO UPDATE
                          SET accepted_at = EXCLUDED.accepted_at,
                              declined_at = EXCLUDED.declined_at
                        WHERE recipient_invites.accepted_at IS NULL
                          AND recipient_invites.declined_at IS NULL""",
                    (tid, email, tid, inviter or None,
                     _ts(r.get("created_at")), _ts(r.get("expires_at")),
                     accepted_at, declined_at))
                written += c.rowcount
            # the consent migration 074 recorded for a recipient configured
            # before invites existed: agreed, as of now, with no link, so it
            # reads as consent that predates the invite and never had one
            for email in unasked:
                if not enrol or email not in members:
                    continue
                c = admin.execute(
                    """INSERT INTO recipient_invites
                           (tenant_id, email, accepted_at)
                       VALUES (%s, %s, now())
                       ON CONFLICT (tenant_id, email) DO UPDATE
                          SET accepted_at = EXCLUDED.accepted_at
                        WHERE recipient_invites.accepted_at IS NULL
                          AND recipient_invites.declined_at IS NULL""",
                    (tid, email))
                written += c.rowcount
            # Report the STATE, not the writes. A row the destination already
            # held answered writes nothing and is still good news; an address
            # that stayed pending writes nothing and is the thing the owner
            # has to act on. Counting inserts alone reported neither.
            final = {r["email"]: r for r in admin.execute(
                "SELECT email, accepted_at, declined_at FROM recipient_invites"
                " WHERE tenant_id = %s AND email = ANY(%s)",
                (tid, list(seen) + unasked)).fetchall()}
    finally:
        admin.close()
    listed = list(seen) + unasked
    accepted = sum(1 for e in listed
                   if (final.get(e) or {}).get("accepted_at"))
    declined = sum(1 for e in listed
                   if (final.get(e) or {}).get("declined_at"))
    return {"total": len(listed), "rows": written, "accepted": accepted,
            "declined": declined,
            "pending": len(listed) - accepted - declined}


def skipped_rows(out: _Outcomes) -> list[dict]:
    """Per table, the rows no reader could read and the restore left out:
    how many, the first few by id or line, and the sentence that says so.
    The restore's result carries them as their own field, and the
    sentences lead its notes."""
    got = []
    for table, (n, sample) in sorted(out.skipped.items()):
        more = f", and {n - len(sample)} more" if n > len(sample) else ""
        got.append({"table": table, "count": n, "sample": list(sample),
                    "sentence": f"{_plural(n, 'row', 'rows')} of {table} "
                                f"could not be read and "
                                f"{'was' if n == 1 else 'were'} skipped "
                                f"({', '.join(sample)}{more})"})
    return got


def _outcome_notes(out: _Outcomes) -> list[str]:
    """What the person restoring is told about the rows that did not come
    back as they were. `present` is said by the count alone."""
    notes = []
    orphaned = out.total("orphaned")
    if orphaned:
        # not "already present": these rows were never here and are not
        # now — the transaction, account, business or receipt they belong
        # to is not in the archive (one written before the child tables had
        # real foreign keys can carry them), or was itself left out above
        notes.append(f"{orphaned} row(s) referred to a transaction, account, "
                     f"business or receipt the archive does not carry, or "
                     f"that this restore left out, and were left out too")
    beyond_any_sum = (out.total("out_of_range")
                      - sum(out.over_bound.values()))
    if beyond_any_sum:
        notes.append(_out_of_range_note(beyond_any_sum))
    notes += _bounded_notes(out.over_bound, left="left out")
    notes += _bounded_notes(out.emptied_bound,
                            left="restored with that figure left empty")
    notes += _bounded_notes(out.misfits, left="left out")
    malformed = (out.total("malformed") - sum(out.misfits.values())
                 - sum(n for n, _ in out.skipped.values()))
    if malformed:
        notes.append(f"{malformed} row(s) were in a shape no part of the app "
                     f"writes (a required cell blank, an unreadable id or "
                     f"image, a split that does not add up) and were left out")
    over_cap = out.total("over_cap")
    if over_cap:
        notes.append(f"{over_cap} row(s) were past a limit every household "
                     f"is held to (a receipt image over the upload size, an "
                     f"account's id history, a reimbursement for a charge "
                     f"already repaid in full or from a deposit already "
                     f"used up) and were left out")
    repaired = out.total("amount_repaired")
    if repaired:
        notes.append(f"{repaired} transaction(s) had no amount in the "
                     f"archive and were restored with amount 0, keeping "
                     f"their receipts, notes and splits — edit the amount "
                     f"on each to correct it")
    trimmed = out.total("payback_trimmed")
    if trimmed:
        notes.append(f"{trimmed} partial reimbursement(s) repaid more than "
                     f"their charge cost or claimed more than their deposit "
                     f"brought in, and were cut to it, the newest first; "
                     f"the part that was cut counts as spend on its charge "
                     f"and as income on its deposit again")
    return notes


def _nul_key_notes() -> list[str]:
    """Each file and column a value was left out of for a NUL in a key."""
    drops = _NUL_KEY_DROPS.get() or set()
    return [f"a value in {member}, column {col}, had a NUL character "
            f"(U+0000) in a key, which no export writes, and was left out"
            for member, col in sorted(drops)]


PORTABILITY_REFUSAL = (
    "This is a data-portability archive, not a backup to restore; restore "
    "the export ZIP from Settings → Data (“Download export ZIP”) instead. "
    "Nothing was imported.")
# the portability archive's manifest names its format; it carries the
# household's users and control rows, so it is read under a cap of its own
_PORTABILITY_FORMAT = "oikonome-tenant-export/"
_PORTABILITY_MANIFEST_MAX = 16 * 1024 * 1024
_PORTABILITY_MEMBER = re.compile(r"^data/[^/]+\.json$")


def _refuse_portability_archive(z: zipfile.ZipFile) -> None:
    """Refuse the data-portability archive (tenant_export.build_archive)
    before anything is read or written. It is JSON under data/ for a
    person to read, not the CSVs a restore reads, so it would land nothing
    and report success. Known by its manifest's format, or, with the
    manifest gone, by its shape: data/*.json members and no CSV at the
    top."""
    names = z.namelist()
    if (any(_PORTABILITY_MEMBER.match(n) for n in names)
            and not any("/" not in n and n.endswith(".csv") for n in names)):
        raise _ArchiveRefused(PORTABILITY_REFUSAL)
    try:
        info = z.getinfo(_upgrades.MANIFEST)
    except KeyError:
        return
    if info.file_size > _PORTABILITY_MANIFEST_MAX:
        return
    try:
        doc = json.loads(z.read(info).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, zipfile.BadZipFile):
        return
    if isinstance(doc, dict) and str(doc.get("export_format") or "") \
            .startswith(_PORTABILITY_FORMAT):
        raise _ArchiveRefused(PORTABILITY_REFUSAL)


FOREIGN_ARCHIVE_REFUSAL = (
    "This ZIP isn't an Oikonome export, so there is nothing in it to "
    "restore. If it came from another budgeting app, unzip it and import "
    "the transactions CSV inside (a YNAB export: its Register.csv). "
    "Nothing was imported.")
# The export itself, zipped with the folder around it: Safari unzips a
# download, and compressing that folder puts every member one level down.
# It is the household's own file, so the sentence must not send it toward
# the transactions-CSV import, which would pour every account into one.
NESTED_EXPORT_REFUSAL = (
    "This ZIP holds an Oikonome export inside a folder, so the restore "
    "can't read it. Unzip it, open the folder, select the files in it and "
    "zip the files themselves (not the folder), then upload that ZIP. "
    "Nothing was imported.")
# the members a restore reads (every member named below; a test holds
# the two lists together), any one of which marks the household export —
# besides the manifest, which an older one lacks. transactions.csv alone
# is not enough: a zipped spending export may carry one of that name.
_EXPORT_MEMBERS = frozenset({
    "account_adoptions.csv",
    "account_links.csv",
    "account_sources.csv",
    "accounts.csv",
    "activity_log.csv",
    "alerts_log.csv",
    "amazon_matches.csv",
    "amazon_orders.csv",
    "amazon_summaries.csv",
    "bill_proposals.csv",
    "bills.csv",
    "budget_snapshots.csv",
    "business_entity.csv",
    "business_flags.csv",
    "business_txn_class.csv",
    "compliance_obligation.csv",
    "costco_matches.csv",
    "costco_receipts.csv",
    "crypto_holdings.csv",
    "entity_membership.csv",
    "equity_movement.csv",
    "forecast_nights.csv",
    "holdings.csv",
    "import_batches.csv",
    "income_annual.csv",
    "income_documents.csv",
    "income_upload_values.csv",
    "income_uploads.csv",
    "items.csv",
    "liabilities.csv",
    "manual_categories.csv",
    "merchant_canonical.csv",
    "merchant_categories.csv",
    "merchant_merge_proposals.csv",
    "merchant_renames.csv",
    "merchants.csv",
    "mileage_log.csv",
    "networth_recorded.csv",
    "networth_snapshot.csv",
    "receipt_items.csv",
    "receipts.csv",
    "recipient_invites.csv",
    "recurring.csv",
    "recurring_proposals.csv",
    "reimburse_flags.csv",
    "reimbursements.csv",
    "tenant_settings.csv",
    "transaction_notes.csv",
    "transaction_splits.csv",
    "vendor_1099.csv",
})


def _is_export(z, names: set[str], prefix: str) -> bool:
    """Do these member names (read under `prefix`) make the export?"""
    if _upgrades.MANIFEST in names or names & _EXPORT_MEMBERS:
        return True
    if "transactions.csv" in names:
        with z.open(prefix + "transactions.csv") as f:
            head = f.readline(4096).decode("utf-8-sig", "replace")
        return "account_id" in next(csv.reader([head]), [])
    return False


def _one_shared_folder(names: set[str]) -> str | None:
    """'folder/' when every member that matters sits in that one folder —
    the litter a Mac adds (__MACOSX/, .DS_Store) aside — else None."""
    tops = {n.split("/", 1)[0] + "/" for n in names
            if "/" in n and not n.startswith("__MACOSX/")}
    loose = [n for n in names if "/" not in n and n != ".DS_Store"]
    return tops.pop() if len(tops) == 1 and not loose else None


def foreign_archive_refusal(data) -> str | None:
    """The sentence refusing a ZIP that is not the household export, or
    None for one that is. Read from the members' names (and the header of
    a lone transactions.csv, which a zipped spending export may share), so
    every door asks the same question of the same file before it writes."""
    if not isinstance(data, zipfile.ZipFile):
        # bounded before anything is parsed: a central directory listing
        # millions of members costs gigabytes to open, and this runs
        # before the step-up, on every door
        problem = _upload_zip.central_directory_problem(data)
        if problem == _upload_zip.UNREADABLE:
            # damaged or not a ZIP at all: left unopened, and the restore's
            # own refusal says so, as for any archive this sniff can't read
            return None
        if problem:
            return problem
    try:
        z = (data if isinstance(data, zipfile.ZipFile)
             else _upload_zip.open_uploaded(data))
        names = set(z.namelist())
        if _is_export(z, names, ""):
            return None
        prefix = _one_shared_folder(names)
        if prefix and _is_export(z, {n[len(prefix):] for n in names
                                     if n.startswith(prefix)}, prefix):
            return NESTED_EXPORT_REFUSAL
    except Exception:
        # an encrypted, damaged or oddly compressed archive: the restore's
        # own checks say what broke, rather than this sniff failing the door
        return None
    return FOREIGN_ARCHIVE_REFUSAL


ERASED_MSG = "this household is being deleted; nothing was restored"


def _restore_zip(conn, data, out: _Outcomes) -> dict:
    # bytes (the API's small-archive door) or a seekable mapping (the
    # streamed restore door): reading a mapping through BytesIO would copy
    # the whole archive back into the heap the streaming was built to avoid
    try:
        z = _upload_zip.open_uploaded(data)
    except _upload_zip.ArchiveRefused as e:
        raise _ArchiveRefused(str(e)) from None
    _check_zip_sizes(z)          # refuse zip bombs before reading rows
    _refuse_portability_archive(z)
    refusal = foreign_archive_refusal(z)
    if refusal:
        raise _ArchiveRefused(refusal)
    # how this archive's cells were guarded; restore_zip scopes the value
    _GUARD_V2.set(archive_guard_v2(z))
    # the state the declared landers read, set as each part of it becomes
    # true (see sync/landers.py)
    from . import landers as _landers
    ctx = _landers.Ctx()

    # Validate the incoming config FIRST — the connection is autocommit,
    # so a rejection after the row loops would still leave the ZIP's
    # transactions behind. Reject at the door, before anything lands.
    # tenant_settings is a single tiny row and we index it — materialize just
    # this one member (the big tables below stay streamed via the generator)
    settings = list(_rows(z, "tenant_settings.csv"))
    cfg_in = (_j(settings[0]["config"])
              if settings and settings[0].get("config") else None)
    notes: list[str] = []
    if isinstance(cfg_in, dict):
        # The parsed cell can hold a NUL its CSV text spelled as an escape.
        # Settled here, before any key check reads it: values lose theirs,
        # so the spelling every validator sees is the one that would be
        # stored. A key holding one never gets this far: _j left the whole
        # document out (no key check may see a rewritten key), as it does
        # any JSON cell's value, and the summary says so.
        try:
            cfg_in = strip_nul(cfg_in)
        except ValueError as e:
            # nested past the walk: the file, named
            raise _not_an_export("tenant_settings.csv", str(e)) from None
        notes = _check_config(cfg_in)

    # One transaction: a mid-restore failure (bad row deep in the ZIP,
    # connection drop) leaves nothing behind.
    with conn.transaction():
        # Asked inside the restore's own transaction, before any lock or
        # row, as every push door asks: it holds the push writer lock
        # shared until the commit, so an erasure waits the restore out
        # and then deletes what it wrote, and a restore that starts after
        # the erasure began writes nothing — rows under an erased tenant
        # id are never swept again
        from .. import erasure as _erasure
        if not _erasure.push_may_write(conn, _tenant_of(conn)):
            raise HouseholdBeingDeleted(ERASED_MSG)
        # storage, pairing, identity, before any row lands: the order
        # every other writer takes them in (bulk_import)
        from ..web.bulk_import import hold_restore_write_locks
        hold_restore_write_locks(conn)
        # What the archive plans against the household as it was — the
        # account ids it renames, which archived row lands under which id —
        # read under the write locks and before any row lands, so no writer
        # moves the rows the plan was made against. The planners only read.
        ctx.prepare(conn, z, notes=notes, cfg_in=cfg_in)
        _landers.land_all(conn, z, ctx, out)
        _landers.run_step(_landers.FINALIZE, conn, ctx, out)
    return _landers.run_step(_landers.AFTER_COMMIT, conn, ctx, out)


def _settle_settings(conn, ctx, out) -> None:
    """The archive's settings merged key by key OVER the household's own,
    under the settings row lock; checked at the door (`_check_config`)."""
    cfg_in = ctx.cfg_in
    if isinstance(cfg_in, dict):
        # Belt to the netguard check above: a legit export never CARRIES
        # credential-shaped keys (the export scrubs them) — any present in
        # a ZIP are crafted. Drop them all so an injected key the
        # destination lacks (e.g. an access_url) can never land. Same for
        # policy keys (demo_mode /
        # demo_login / smtp_starttls) — a ZIP must not flip the demo
        # lockdown or downgrade SMTP to cleartext; deep-scrubbed so nested
        # shapes can't smuggle them either.
        cfg = scrub_config(cfg_in)
        for k in _CREDENTIAL_COMPANION_KEYS:
            cfg.pop(k, None)
        ctx.notes.extend(normalize_notify_shapes(cfg))
        clean_excluded_accounts(cfg)
        ctx.notes.extend(clean_budget_amounts(cfg))
        ctx.notes.extend(clean_income_scenarios(cfg))
        ctx.notes.extend(clean_savings_goals(cfg))
        ctx.notes.extend(clean_manual_assets(cfg))
        ctx.notes.extend(clean_config_shapes(cfg))
        _rename_account_keys(cfg, ctx._ready("renamed"))
        _follow_moved_bills(cfg, ctx._ready("bill_payees"))
        # The Enrich cap is the household's preference and restores, but
        # only inside the range the settings door accepts: above the
        # ceiling it would authorise spend the operator never allowed,
        # and a value that is not a number would break every Enrich call
        # until someone happened to save settings again. A malformed one
        # is dropped, which leaves this instance's own value in place.
        if "plaid_enrich_cap" in cfg:
            from ..web.api import _enrich_cap_ceiling
            try:
                cfg["plaid_enrich_cap"] = max(0, min(
                    int(cfg["plaid_enrich_cap"]), _enrich_cap_ceiling()))
            except (TypeError, ValueError, OverflowError):
                cfg.pop("plaid_enrich_cap")
        # scrub_config drops
        # secret/demo keys but NOT email_recipients — a crafted ZIP
        # could plant an outsider address that then receives the
        # household's cadence mail. Filter it to
        # tenant members on HOSTED, same boundary as the settings save;
        # a legit self-host export is untouched. This filter, not the
        # invite table, is what keeps a household's balances off an
        # outside inbox — see `_restore_recipient_invites`, which
        # carries each address's ANSWER across so the mail resumes for
        # the people who agreed to it and stays off for the ones who
        # declined.
        if cfg.get("email_recipients"):
            from ..web import mailguard
            kept = mailguard.filter_recipients(
                conn, list(cfg["email_recipients"]))
            if kept:
                cfg["email_recipients"] = kept
                ctx.archived_recipients = list(kept)
            else:
                cfg.pop("email_recipients", None)
        # Merge OVER the destination's current config: keys present
        # only in the destination — settings added since the export was
        # taken — survive; the ZIP replaces exactly the keys it carries.
        # A flat secret is safe by absence (the export scrubs
        # plaid/mx/llm/smtp keys, crafted ones were dropped above), but a
        # secret nested inside a key the ZIP DOES carry — every AI
        # backend's api_key/extra_body under `llm_backends` — would be
        # merged away with the value it lives in, so it is carried
        # forward explicitly.
        # …and take the ROW LOCK first, for the same reason
        # `budget.config_txn` exists.
        # This is the settings lost-update shape written in raw SQL:
        # read the whole document, merge, write the whole document back.
        # `ON CONFLICT DO UPDATE` locks at WRITE time, which is too late
        # — a writer that commits between this read and that write (the
        # nightly budget seed is the live one) has its keys merged out
        # of the destination document we already read. We are inside
        # `conn.transaction()`, so the lock is held until the restore
        # commits.
        cur = conn.execute(
            "SELECT config FROM tenant_settings FOR UPDATE").fetchone()
        dest = (cur["config"] if cur else {}) or {}
        # role routing may only name known roles and backends that
        # will exist after the merge (the ZIP's list if it brought one,
        # else the destination's own) — same rule as the .oikx bundle
        if "llm_roles" in cfg:
            from .connections_bundle import _clean_roles
            effective = (cfg["llm_backends"] if "llm_backends" in cfg
                         else dest.get("llm_backends") or [])
            ids = {str(b.get("id")) for b in effective
                   if isinstance(b, dict) and b.get("id")}
            cfg["llm_roles"] = _clean_roles(cfg["llm_roles"], ids)
        # a per-cadence SMS toggle only means something against a
        # number verified HERE; the ZIP never carries one, so unless the
        # destination already has a live verified number the toggle is
        # dropped rather than left armed for whatever number is verified
        # later. The owner re-ticks it after re-verifying.
        _drop_sms_toggles_without_verified_phone(cfg, dest)
        _settle_masked_urls(cfg, dest)
        merged = dict(dest)
        for k, v in _finite_only(cfg).items():
            merged[k] = (carry_forward_secrets(dest[k], v)
                         if k in dest else v)
        ctx.left_behind = (_drop_moved_credentials(dest, cfg, merged)
                           + _backend_credentials_left_behind(dest, merged))
        # an archived config without today_view chose the summary face
        # (the settings door stores that choice as no key); the face a
        # new account was seeded with is provisioning, not a choice
        if "today_view" not in cfg:
            merged.pop("today_view", None)
            # ...unless the archive is older than the choice itself:
            # before the summary face existed every household saw the
            # full detail, so that is the face its owner knew
            # (only when the archive's age is known: one that says
            # nothing about itself keeps the summary rule above)
            _age = _upgrades.archive_schema(ctx._ready("z"))
            if _age is not None and _upgrades.older_than(_age,
                                                         _TODAY_FACES):
                merged["today_view"] = "detail"
        # the destination's pause record is kept as it was (the ZIP's
        # never reaches here — scrub_config drops it), and while it is
        # armed it also covers whatever outbound setting this restore
        # brought in
        widen_armed_pause(dest, merged, conn)
        pause_if_starter_signed_out(dest, merged, conn)
        # judged on the documents as stored before and after, by the
        # settings save's own rule: the merge carries the stored key
        # onto an entry of the same id whatever URL the archive gave it
        ctx.route_moved = ai_route_moved(conn, dest, merged)
        conn.execute(
            """INSERT INTO tenant_settings (config) VALUES (%s)
               ON CONFLICT (tenant_id) DO UPDATE
               SET config = EXCLUDED.config, updated_at = now()""",
            (jsonb(merged),))
        out.wrote("tenant_settings", 1)
        # belt: this write bypasses budget.save_config — clear
        # the demoguard cache the same way so no stale flag survives
        from ..web import demoguard
        row = conn.execute(
            "SELECT current_setting('app.tenant_id', true) AS tid").fetchone()
        demoguard.invalidate(row["tid"] if row and row["tid"] else None)


def finalize(conn, ctx, out) -> None:
    """The restore's last steps inside its transaction, once every member
    has landed: what an older archive's rules and printed-day rows need,
    the settings document, and the re-keys and cuts `oikonome migrate`
    gives stored rows."""
    # an archive written before the household's rules were derived from
    # its own corrections (035) and its model rules keyed on the
    # merchant (044) gets both, over what it brought. Read from the
    # archive's own word about its age: waived, a household only keeps
    # rules in the shape it exported them in, and applied to a newer
    # archive the derivation could bring back a rule its person deleted
    schema = _upgrades.archive_schema(ctx._ready("z"))
    if _upgrades.older_than(schema, _RULES_FROM_OVERRIDES):
        derive_user_rules_from_overrides(conn, ctx._ready("overridden"))
    if _upgrades.older_than(schema, _MODEL_RULES_ON_THE_MERCHANT):
        rekey_model_rules_on_the_merchant(conn)
    if (not _dated_by_the_utc_day(schema)
            and _upgrades.older_than(schema, _PRINTED_DAY_NOTE)):
        # an archive from a build that dated OFX rows by the printed day
        # but did not yet say so: the rows its later batches wrote are
        # printed-day rows. An older build (or an archive too old to say
        # its age) dated them by the UTC day, and a note there would
        # stop a re-import moving them onto their printed day
        # only the rows this restore wrote: the instant is a fact about
        # the archive's source, and a row the household already had may
        # have been dated by a build that upgraded later than that
        from . import ofximport as _ofximport
        _ofximport.mark_printed_day_rows(
            conn, _PRINTED_DAY_DATING_SINCE, only_ids=ctx._ready("landed_ofx"))
    _settle_settings(conn, ctx, out)
    # A hand-made account an archive written before the re-key holds
    # under an id spelling a full account number is moved onto its
    # masked id here, as `oikonome migrate` moved the stored ones, once
    # its rows, lineage and settings have landed — its rows re-hashed
    # under the new id, and every adoption record naming it in full
    # cut; the adoption it leaves is what a later restore of the same
    # archive follows to it.
    rekeyed = _sync_base.rekey_numbered_accounts(conn)
    ctx.settled_accounts = {rekeyed.get(a, a)
                            for a in ctx._ready("landed_accounts")}
    # and the rows such an archive brings, each id an unkeyed hash over
    # the full number, are re-id'd under this household's own secret
    # (with every row whose id was hashed before its name was masked),
    # as `oikonome migrate` re-ids the stored ones; the secret itself
    # never travels, so another household's archive is keyed anew here
    _sync_base.key_file_import_ids(conn)
    # every hand-made account the archive brought without a
    # stored-name row (one written before the table, or one a re-key
    # just moved) gets one, as migration 207 gave the stored ones
    _account_sources.backfill(conn)
    # a detected bill's match key, and the names the log quotes, are
    # cut as migration 208 cut the stored ones; after the merchants
    # landed, because a name a live merchant holds whole stays whole
    from ..engine import activity as _activity
    _bills.cut_detected_keys(conn)
    _activity.cut_stored_names(conn)


def _untally_pruned_merchants(conn, ctx, out) -> None:
    """An archived merchant the post-commit merchant pass pruned (nothing
    here names it) was not kept: it moves from written to rebuilt, as the
    household derives a merchant again when a row names one."""
    landed = [str(m) for m in (ctx.landed_merchants or [])]
    if not landed:
        return
    try:
        here = {r["id"] for r in conn.execute(
            "SELECT id::text AS id FROM merchants WHERE id::text = ANY(%s)",
            (landed,)).fetchall()}
    except Exception:                                    # noqa: BLE001
        log.warning("restore: pruned merchants not counted", exc_info=True)
        return
    gone = len(set(landed) - here)
    if gone:
        out.written["merchants"] = out.written.get("merchants", 0) - gone
        out.refuse("merchants", "rebuilt", gone)


def after_commit(conn, ctx, out) -> dict:
    """What the restore does once its rows have committed, and its summary:
    each step its own transaction, so a failure costs that step and never
    the restore. The summary is read off the tally after the merchant
    resolve, which prunes an archived merchant nothing here names: such a
    merchant is reported rebuilt, never written."""
    notes = ctx._ready("notes")
    if ctx.route_moved:
        try:
            _tell_the_owner_the_ai_route_moved(conn)
        except Exception:                                # noqa: BLE001
            log.exception("restore: the AI route notice could not be sent")
    # Restored AFTER the same bank was linked here: the link already holds
    # each account under its new Plaid id, and the archive just brought the
    # same account back under its old one on a tokenless shell. The link
    # side cannot see this order (it adopts only accounts it does not hold
    # yet), so the restore merges them from its own side. After the commit,
    # one transaction per account, so a merge that fails costs that one
    # account its merge and never the restore.
    try:
        from .adopt import merge_restored_onto_live
        _merged, merge_notes = merge_restored_onto_live(
            conn, ctx._ready("settled_accounts"))
        notes.extend(merge_notes)
    except Exception:                                    # noqa: BLE001
        log.exception("restore: merging onto linked accounts failed")
        notes.append("accounts already linked here could not be checked "
                     "against the restored ones — some may appear twice; "
                     "check the Accounts page")
    # every restored receipt landed undecided (landers._receipt): which
    # slip each is gets decided now, by the one decider, which keeps the
    # archive's labels and every person's answer and only adds the joins
    # of the same file, a local copy beside it included
    # (specs/receipts.md)
    try:
        from ..engine import receipt_identity
        receipt_identity.decide_backlog(
            conn, passes=receipt_identity.RESTORE_PASSES)
    except Exception:                                    # noqa: BLE001
        log.exception("restore: receipt slips left for the next pass")
    # Outside the transaction above, because this one table is written over
    # the admin connection (migration 074 grants the app role nothing on
    # it) — so it lands only once the household's data has actually
    # committed.
    try:
        inv = _restore_recipient_invites(conn, ctx._ready("z"),
                                         ctx.archived_recipients)
    except Exception:                                    # noqa: BLE001
        log.exception("recipient invites could not be restored")
        inv = dict(_NO_INVITES)
        notes.append("the daily email's recipients could not be restored — "
                     "check Settings → Email, anyone listed there may need "
                     "to be invited again")
    if inv["rows"]:
        out.wrote("recipient_invites", inv["rows"])
    if inv["total"]:
        # Always said out loud, even when every answer carried: the one
        # number that matters — how many people have to be asked again
        # before their mail resumes — is reported whether it is zero or not.
        bits = []
        if inv["accepted"]:
            bits.append(f"{inv['accepted']} keep the consent this instance "
                        f"can vouch for")
        if inv["declined"]:
            bits.append(f"{inv['declined']} had declined and stay off it")
        bits.append(f"{inv['pending']} must accept a fresh invite here "
                    f"before any mail reaches them")
        notes.append(f"daily email — the archive listed {inv['total']} "
                     f"recipient(s): " + "; ".join(bits)
                     + ". Settings → Email is where you invite them")
    # Rows from an archive without merchant_id get their merchant here — the
    # ledger shows names and logos on the first load after a restore, not
    # after the next nightly
    try:
        from ..engine import merchant_identity
        merchant_identity.resolve(conn)
    except Exception:                                    # noqa: BLE001
        log.warning("post-restore merchant resolve skipped", exc_info=True)
    _untally_pruned_merchants(conn, ctx, out)
    # Counts are rows WRITTEN, each one a row its insert returned — a
    # re-restore honestly reports 0, not the ZIP's size — and every row not
    # written is under the one outcome that says why (REFUSALS).
    counts = RestoreCounts(out.counts())
    counts.credentials_dropped = dropped_kinds(ctx.left_behind)
    if counts.credentials_dropped:
        # said now, not found out when the AI or the mail later fails
        what = {"ai_key": "the AI service's key",
                "mail_password": "the mail relay's password and login"}
        notes.append(
            "the archive points somewhere other than this instance's "
            "settings, so " + " and ".join(
                what[k] for k in counts.credentials_dropped)
            + " saved here were not carried over — enter them again in "
              "Settings for the new address")
    notes.extend(_outcome_notes(out))
    notes.extend(_nul_key_notes())
    # rows in the archive that are not here, for a reason no check names:
    # said before anything else
    counts.skipped = skipped_rows(out)
    notes[:0] = [s["sentence"] for s in counts.skipped]
    # what the config check dropped, so the person restoring is told
    # rather than left to discover a missing AI backend later
    if notes:
        counts["_notes"] = notes                      # not a row count
    return counts
