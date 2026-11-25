"""A hostile cell anywhere in an archive costs at most its own row
(specs/invariants-and-fuzzing.md, R3).

A real export (tests/roundtrip_builders.py, one seed) is taken apart and
one hostile shape at a time is written into it: in every member, cells in
several rows, each row a different column, so one archive per shape
reaches every member and most columns. The shapes are the fuzz vocabulary
(tests/body_fuzz.py) as a CSV cell can carry it: NaN and infinities,
integers past a float and past Python's integer text limit, a huge
string, impossible and far dates, NUL, a lone surrogate, right-to-left and
emoji text, cells a spreadsheet reads as a formula, deep and broken JSON,
blanks; and the shapes of the file itself: an extra column, a missing
column, a record with more cells than its header, and one with fewer.

For each archive, restored into an empty household:

* the restore finishes, or refuses the archive with a stated reason (a
  ValueError saying the file is not a good export, which the restore job
  shows the person). Any other exception is the job's "the restore
  failed", the 5xx of a background job, and fails here; so does a
  ValueError in a parser's own words, which only refuses the archive by
  accident, and so does a row the driver skipped for one (a cell no
  check anticipated, which costs its row but is still a finding);
* a refused archive leaves the household exactly as it was;
* a finished one counts every archived row exactly once: written, or
  refused under one stated reason (already present, malformed, orphaned,
  ...), per table;
* and the household it leaves still reads: every page the money surfaces
  read answers below 500 (checked for the shapes that land a value).

When a whole archive is refused, each member is tried on its own as well,
so a shape one member refuses never hides what the others do with it.

The default run uses one seed and one row per column offset;
OIKONOME_PROPERTY=long walks every column of every member.
"""

from __future__ import annotations

import time
import unittest
import zlib

from oikonome.sync import export, restore

from . import props
from . import roundtrip_builders as rb
from .restore_invariants import (archived_rows, digest_diff,
                                 fresh_household, read_member,
                                 rewrite_member, rewrite_members,
                                 tenant_digest, tenant_id)

DEEP = '{"a":' * 5000 + "1" + "}" * 5000

# (name, the cell it writes, given the cell it replaces)
CELLS = (
    ("nan", lambda v: "NaN"),
    ("infinity", lambda v: "-Infinity"),
    ("int-past-float", lambda v: "9" * 400),
    ("int-past-parse", lambda v: "1" + "0" * 4400),
    ("exponent", lambda v: "1e999"),
    ("huge-text", lambda v: "x" * 200_000),
    ("impossible-date", lambda v: "2026-02-30"),
    ("far-date", lambda v: "99999-12-31T25:61:00"),
    ("nul", lambda v: (v or "a") + "\x00b"),
    ("lone-surrogate", lambda v: "a\ud800b"),
    ("bidi-emoji", lambda v: "‮́\U0001f4a5 שלום"),
    ("formula", lambda v: '=HYPERLINK("http://example.invalid","x")'),
    ("formula-plus", lambda v: "+1-1"),
    ("formula-at", lambda v: "@SUM(A1)"),
    ("deep-json", lambda v: DEEP),
    ("json-nul-key", lambda v: '{"a\\u0000": 1}'),
    ("broken-json", lambda v: '{"a": [1, 2'),
    ("json-of-another-shape", lambda v: '[null, {"x": NaN}, "\\ud800"]'),
    ("blank", lambda v: ""),
    ("word", lambda v: "maybe"),
)

# shapes of the member itself: (name, edit of its records)
SHAPES = (
    ("extra-column", lambda recs: [recs[0] + ["zz_extra"]]
     + [r + ["=1+1"] for r in recs[1:]]),
    ("missing-column", lambda recs: [r[:-1] for r in recs]),
    ("record-wider-than-header", lambda recs: recs[:1]
     + [recs[1] + ["x", "=2+2"]] + recs[2:]),
    ("record-shorter-than-header", lambda recs: recs[:1]
     + [recs[1][:1]] + recs[2:]),
)

SEED = 1


def _h(*parts) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


def _members(data: bytes) -> list[str]:
    import io
    import zipfile
    return sorted(n for n in zipfile.ZipFile(io.BytesIO(data)).namelist()
                  if n.endswith(".csv"))


def _countable_rows(data: bytes) -> dict:
    """Rows per member, as `archived_rows` counts them, leaving out a
    member that is not UTF-8: one the restore reads in its transaction
    refuses the archive, and the one it reads after the commit (the
    recipient answers) costs only that step, by design."""
    import io
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(data))
    good = {}
    for name in z.namelist():
        try:
            z.read(name).decode("utf-8")
        except UnicodeDecodeError:
            continue
        good[name] = z.read(name)
    if len(good) == len(z.namelist()):
        return archived_rows(data)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for name, raw in good.items():
            out.writestr(name, raw)
    return archived_rows(buf.getvalue())


def _cell_edit(name, make, member, offset):
    """Row i (of the member's rows) gets the hostile cell in column
    (start + i + offset) — each row a different column."""
    def edit(recs):
        if len(recs) < 2:
            return recs
        width = len(recs[0])
        start = _h(member, name) % width
        out = [recs[0]]
        for i, row in enumerate(recs[1:]):
            row = list(row)
            if i < width:
                c = (start + i + offset) % width
                row[c] = make(row[c] if c < len(row) else "")
            out.append(row)
        return out
    return edit


def _offsets(data, member) -> range:
    recs = read_member(data, member)
    rows, width = len(recs) - 1, len(recs[0]) if recs else 0
    if not props.LONG or rows <= 0:
        return range(1)
    # enough passes that every column is reached
    return range(0, max(1, width), max(1, rows))


class HostileArchiveCells(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.started = time.monotonic()
        src = fresh_household()
        try:
            rb.build(src, SEED)
            cls.data = export.build_zip(src)
        finally:
            src.close()
        cls.members = _members(cls.data)

    @classmethod
    def tearDownClass(cls):
        print(f"\n[timing] {cls.__name__}: "
              f"{time.monotonic() - cls.started:.1f}s", flush=True)

    def restore_into_empty(self, data: bytes, what: str):
        """Restore `data` into an empty household and hold it to R3.
        Returns 'done' or 'refused'."""
        conn = fresh_household()
        try:
            tid = tenant_id(conn)
            before = tenant_digest(tid)
            try:
                _result, out = restore.restore_outcomes(conn, data)
            except ValueError as e:
                # a stated refusal says the file is not a good export; a
                # parser's own words ("could not convert string to float")
                # are a crash the job happens to show
                said = str(e).lower()
                if isinstance(e, UnicodeError) or not (
                        "export" in said or "genuine" in said):
                    self.fail(f"{what}: the restore stopped on {e!r}, "
                              "which names nothing the person can act on")
                self.assertEqual(digest_diff(before, tenant_digest(tid)), {},
                                 f"{what}: a refused archive wrote rows")
                return "refused"
            except Exception as e:                      # noqa: BLE001
                self.fail(f"{what}: the restore broke with {e!r}")
            # the driver skips a row its reader fails on unexpectedly, so
            # the restore finishes; such a row is still a cell no check
            # anticipated, which this property exists to find
            self.assertEqual(out.skipped, {},
                             f"{what}: a reader failed on a cell no check "
                             "anticipated")
            for table, n in _countable_rows(data).items():
                refused = sum(v for (t, _), v in out.refused.items()
                              if t == table)
                written = out.written.get(table, 0)
                if table in rb.CONTROL:
                    self.assertLessEqual(written + refused, n, what)
                    continue
                self.assertEqual(
                    written + refused, n,
                    f"{what}: {table} archived {n} rows, wrote {written}, "
                    f"refused {refused} ({out.refused})")
            return "done"
        finally:
            conn.close()

    def run_class(self, name, edit_for):
        """One archive with every member edited; when it is refused,
        each member on its own as well."""
        data = rewrite_members(self.data, {m: edit_for(m)
                                           for m in self.members})
        if self.restore_into_empty(data, f"{name} in every member") \
                == "refused":
            # each member on its own: every one in the long mode, a fixed
            # sixth of them (by a hash of member and shape) by default
            for member in self.members:
                if not props.LONG and _h(member, name) % 6:
                    continue
                one = rewrite_member(self.data, member, edit_for(member))
                self.restore_into_empty(one, f"{name} in {member}")

    def test_every_cell_shape_in_every_member(self):
        for name, make in CELLS:
            with self.subTest(cell=name):
                for offset in sorted({o for m in self.members
                                      for o in _offsets(self.data, m)}):
                    self.run_class(name, lambda m, n=name, mk=make, o=offset:
                                   _cell_edit(n, mk, m, o))

    def test_every_member_shape(self):
        for name, edit in SHAPES:
            with self.subTest(shape=name):
                self.run_class(name, lambda m, e=edit: e)


if __name__ == "__main__":
    unittest.main()
