"""Archives in the shape an older install wrote them
(specs/invariants-and-fuzzing.md, R7).

An install that had applied migrations up to some file E exported only the
tables and columns it had then. That shape is read off the migration files
themselves: a table a later file creates was not there, a column a later
file adds was not there, and the two renames and the one dropped column in
the history are put back the way the older install spelled them:

* before 040 the bill tables were `recurring` and `recurring_proposals`;
* from 136 up to 137 a charge whose aggregator name was set aside carried
  the name in `merchant_name` and `merchant_name_overruled` = true; before
  136 the name simply stood in `merchant_name`.

`era_archive(data, E)` rewrites a current export into that shape, with a
manifest naming E. The rows' values are the current household's: an older
install wrote the same kinds of values, and what the restore does about a
value's shape (an id with a comma, a raw record) has its own tests.

`shape_changes()` lists the migrations that changed the archive's shape,
the era boundaries worth sampling. `test_an_archive_from_an_older_schema_
restores_whole` proves the parser sees every column `FLOOR_COLUMNS` names,
so a migration written in a shape the parser misses fails there instead of
leaving an era unshaped.
"""

from __future__ import annotations

import csv
import functools
import io
import json
import re
import zipfile

from oikonome.sync import archive_upgrades as au

_ALTER = re.compile(r"alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?"
                    r"\"?([a-z0-9_]+)\"?(.*?);", re.I | re.S)
_ADD = re.compile(r"\badd\s+column\s+(?:if\s+not\s+exists\s+)?\"?"
                  r"([a-z0-9_]+)", re.I)
_CREATE = re.compile(r"create\s+table\s+(?:if\s+not\s+exists\s+)?\"?"
                     r"([a-z0-9_]+)", re.I)

BILLS_RENAME = "040_bills_rename.sql"
OVERRULED_ADDED = "136_merchant_name_overruled.sql"
SET_ASIDE = "137_merchant_name_set_aside.sql"
SPLIT_SOURCE = "184_split_source.sql"

# the bill tables' names before 040, read as the tables they became
_RENAMED = {"recurring": "bills", "recurring_proposals": "bill_proposals"}

# Tables schema.sql creates with no migration file of their own, whose
# rows point into a table a migration created: they cannot be older.
BORN_WITH = {"merchant_merge_proposals": "097_merchants.sql"}

# Rows an install before a migration could not have written: before 184
# every split was the person's (a store's carved parts came with it).
ERA_ROWS = {("transaction_splits", SPLIT_SOURCE):
            lambda row: row.get("source", "user") == "user"}


@functools.lru_cache(maxsize=1)
def history() -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """(table -> the migration that created it, (table, column) -> the
    migration that added it), each the FIRST file to do so."""
    created: dict[str, str] = {}
    added: dict[tuple[str, str], str] = {}
    for name in au.migration_names():
        sql = re.sub(r"--[^\n]*", "", (au.MIGRATIONS / name).read_text())
        for t in _CREATE.findall(sql):
            created.setdefault(_RENAMED.get(t.lower(), t.lower()), name)
        for m in _ALTER.finditer(sql):
            t = _RENAMED.get(m.group(1).lower(), m.group(1).lower())
            for c in _ADD.findall(m.group(2)):
                added.setdefault((t, c.lower()), name)
    # the overruled flag was dropped again by 137; it is put back by hand
    added.pop(("transactions", "merchant_name_overruled"), None)
    created.update(BORN_WITH)
    return created, added


def shape_changes(members: set[str]) -> list[str]:
    """The migrations that changed the shape of an archive carrying
    `members` (table names): created one of its tables, added one of its
    columns, or renamed or dropped one."""
    created, added = history()
    out = {m for t, m in created.items() if t in members}
    out |= {m for (t, _c), m in added.items() if t in members}
    out |= {BILLS_RENAME, OVERRULED_ADDED, SET_ASIDE}
    return sorted(out, key=au._order)


def before(migration: str) -> str | None:
    """The era just before `migration`: the file applied last before it,
    or None for the very first."""
    names = au.migration_names()
    i = names.index(migration)
    return names[i - 1] if i else None


def _older(era: str, migration: str) -> bool:
    return au._order(era) < au._order(migration)


def _records(raw: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(raw.decode("utf-8"), newline="")))


def _write(records) -> bytes:
    s = io.StringIO(newline="")
    csv.writer(s).writerows(records)
    return s.getvalue().encode("utf-8", "surrogatepass")


def _set_aside(era: str, records):
    """The set-aside name spelled as an install before 137 spelled it."""
    head = records[0]
    if "merchant_name_set_aside" not in head:
        return records
    i_aside = head.index("merchant_name_set_aside")
    i_name = head.index("merchant_name")
    flag = not _older(era, OVERRULED_ADDED)
    out = [[c for j, c in enumerate(head) if j != i_aside]
           + (["merchant_name_overruled"] if flag else [])]
    for r in records[1:]:
        aside = r[i_aside] if i_aside < len(r) else ""
        row = list(r)
        if aside:
            row[i_name] = aside
        row = [c for j, c in enumerate(row) if j != i_aside]
        out.append(row + (["true" if aside else "false"] if flag else []))
    return out


def era_columns(table: str, header: list[str], era: str) -> list[str]:
    """The columns of `header` an install at `era` had on `table`."""
    _created, added = history()
    return [c for c in header
            if (table, c) not in added or not _older(era, added[(table, c)])]


def era_archive(data: bytes, era: str) -> bytes:
    """The export `data` as an install whose last migration was `era`
    would have written it."""
    created, _added = history()
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            name = info.filename
            raw = src.read(name)
            if name == au.MANIFEST:
                doc = json.loads(raw)
                doc["schema"] = era
                z.writestr(name, json.dumps(doc))
                continue
            if not name.endswith(".csv"):
                z.writestr(name, raw)
                continue
            table = name[:-4]
            if table in created and _older(era, created[table]):
                continue                     # the table did not exist yet
            records = _records(raw)
            if records:
                for (t, mig), keep_row in ERA_ROWS.items():
                    if t == table and _older(era, mig):
                        head = records[0]
                        records = [head] + [
                            r for r in records[1:]
                            if keep_row(dict(zip(head, r)))]
                if table == "transactions" and _older(era, SET_ASIDE):
                    records = _set_aside(era, records)
                head = records[0]
                keep = [i for i, c in enumerate(head)
                        if c in era_columns(table, head, era)
                        or c == "merchant_name_overruled"]
                records = [[r[i] for i in keep if i < len(r)]
                           for r in records]
            if _older(era, BILLS_RENAME) and table in ("bills",
                                                       "bill_proposals"):
                name = ("recurring.csv" if table == "bills"
                        else "recurring_proposals.csv")
            z.writestr(name, _write(records))
    return buf.getvalue()


def era_members(data: bytes) -> set[str]:
    return {n[:-4] for n in zipfile.ZipFile(io.BytesIO(data)).namelist()
            if n.endswith(".csv")}
