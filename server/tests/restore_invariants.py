"""Shared pieces of the restore invariants R1 to R4
(specs/invariants-and-fuzzing.md, section 2).

* `fresh_household()`: a household with nothing in it, by tenant id.
* `tenant_digest(tid)`: every row of every table carrying a `tenant_id`
  column (the household tables and the control-plane ones), whole, read
  over the admin connection. Nothing is left out as volatile: a household
  a restore did not touch is byte-identical, timestamps and serials
  included.
* `rewrite_member(data, member, edit)`: the archive with one member's
  rows edited as text (header and records), every other member as it was.
* `archived_rows(data)`: rows per member, counted the way the restore
  counts them.
"""

from __future__ import annotations

import csv
import io
import zipfile

from . import write_detector
from .test_every_table_round_trips_through_its_lander import (
    ROWS, archived_rows, fresh_household)

__all__ = ("ROWS", "archived_rows", "fresh_household", "tenant_digest",
           "digest_diff",
           "rewrite_member", "rewrite_members", "read_member",
           "tenant_id")


def tenant_id(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]


def tenant_digest(tid: str) -> dict[str, list[str]]:
    """Every row of every table with a `tenant_id` column that belongs to
    `tid`, each row whole (its JSON text) and the rows sorted. One query,
    so a test can afford one per restore."""
    admin = write_detector._admin()
    try:
        tables = write_detector._tables(admin)
        sql = " UNION ALL ".join(
            f"SELECT '{t}' AS t, row_to_json(x)::text AS r FROM \"{t}\" x"
            f" WHERE x.tenant_id::text = %(tid)s" for t in tables)
        out: dict[str, list[str]] = {}
        for row in admin.execute(sql, {"tid": str(tid)}).fetchall():
            out.setdefault(row["t"], []).append(row["r"])
        return {t: sorted(rows) for t, rows in out.items()}
    finally:
        admin.close()


def digest_diff(before: dict, after: dict) -> dict:
    """Per table, how many rows went and came (a readable failure)."""
    out = {}
    for t in sorted(set(before) | set(after)):
        b, a = set(before.get(t, ())), set(after.get(t, ()))
        if a != b:
            out[t] = {"gone": len(b - a), "new": len(a - b),
                      "example_new": sorted(a - b)[:1]}
    return out


def read_member(data: bytes, member: str) -> list[list[str]]:
    """One member's records, header first, as the csv module reads them."""
    z = zipfile.ZipFile(io.BytesIO(data))
    text = z.read(member).decode("utf-8")
    return list(csv.reader(io.StringIO(text, newline="")))


def rewrite_member(data: bytes, member: str, edit) -> bytes:
    """The archive with `member` replaced by `edit(records)`, where
    `records` is the header and rows as lists of cells; `edit` returns the
    new records, or bytes to store as the member verbatim."""
    return rewrite_members(data, {member: edit})


def rewrite_members(data: bytes, edits: dict) -> bytes:
    """`rewrite_member` for several members at once, in one pass."""
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            raw = src.read(info.filename)
            edit = edits.get(info.filename)
            if edit is not None:
                text = raw.decode("utf-8")
                got = edit(list(csv.reader(io.StringIO(text, newline=""))))
                if isinstance(got, (bytes, bytearray)):
                    raw = bytes(got)
                else:
                    s = io.StringIO(newline="")
                    csv.writer(s).writerows(got)
                    raw = s.getvalue().encode("utf-8", "surrogatepass")
            z.writestr(info.filename, raw)
    return buf.getvalue()
