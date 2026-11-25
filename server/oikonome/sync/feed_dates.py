"""The calendar day a file or bank feed means by a timestamp.

A transaction belongs to the day its source says it happened. A stamp
that carries its own zone is read in that zone: the day is the one the
source wrote down, never the UTC day the instant falls on. Converting to
UTC first moves a midnight row of a bank east of Greenwich a day early
and an evening row of a bank west of it a day late, and at a month end
into the wrong month's spend and budget.

Every importer and feed that turns a provider timestamp into a stored
date goes through here, so the rule is spelled once.
"""

from __future__ import annotations

import datetime as dt
import re

# OFX 3.2.8.2: YYYYMMDD, optionally HHMM[SS[.XXX]], optionally a
# [offset:name] zone. The printed day is the first eight digits.
_OFX_STAMP = re.compile(
    r"^\s*(\d{4})(\d{2})(\d{2})(?:\d{4}(?:\d{2}(?:\.\d{1,3})?)?)?"
    r"(?:\[[^\]]*\])?\s*$")

# How far the UTC day of a zoned stamp can sit from the day it prints
# (zone offsets run from -12 to +14 hours). Rows imported before stamps
# were read in their own zone may be stored this far off.
UTC_DAY_DRIFT = 1

# The note the OFX importer writes into every row's raw: the row is dated
# by the day its line printed. A row an earlier build stored on a stamp's
# UTC day has no such note (or, restored from an older archive, the UTC
# word below), which is the only thing that tells it apart from a
# neighbouring charge this build stored a day earlier.
PRINTED_DAY_KEY, PRINTED_DAY_VALUE = "dated", "printed"
# The other word the note can hold: a row known to have been dated by its
# stamp's UTC day. Only a restore can know that (the archive says which
# build wrote it), and a row that says so is never given the printed-day
# note by a later pass: it stays movable until an import moves it.
UTC_DAY_VALUE = "utc"

# The OFX stamps whose day the importer stores: a row's posting date and
# the statement's own window.
OFX_DAY_TAGS = ("DTPOSTED", "DTSTART", "DTEND")


def ofx_printed_day(stamp: str | None) -> dt.date | None:
    """The day an OFX date-time stamp prints, in the file's own zone, or
    None when the text is not an OFX stamp (the parser then refuses it as
    it always has)."""
    m = _OFX_STAMP.match(stamp or "")
    if not m:
        return None
    try:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def ofx_utc_day(stamp: str | None) -> dt.date | None:
    """The UTC day of an OFX stamp's instant when it is not the day the
    stamp prints, else None.

    An earlier build stored a row on this day: it let the converter read
    the stamp as an instant and took the UTC date. Only a stamp with a
    clock time and a zone can land on another day that way (a stamp with
    no zone is read as UTC already), so every other stamp gives None. The
    converter is the one that build used, so the day is the one it wrote."""
    printed = ofx_printed_day(stamp)
    if printed is None or "[" not in stamp:
        return None
    from ofxtools.Types import DateTime
    try:
        day = DateTime().convert(stamp.strip()).date()
    except Exception:            # noqa: BLE001 — a stamp it cannot read
        return None
    return day if day != printed else None


def ofx_utc_days(root) -> dict[tuple, list[dt.date | None]]:
    """For each posted line of a parsed OFX element tree, before its
    stamps are pinned: (FITID, printed day, cents) -> the `ofx_utc_day`
    of each line with that key, in file order. A line whose key the
    converted statement cannot rebuild is simply never asked for."""
    out: dict[tuple, list[dt.date | None]] = {}
    for line in root.iter("STMTTRN"):
        stamp = line.findtext("DTPOSTED")
        day = ofx_printed_day(stamp)
        try:
            cents = round(float((line.findtext("TRNAMT") or "").strip())
                          * 100)
        except (ValueError, OverflowError):
            continue
        if day is None:
            continue
        fitid = (line.findtext("FITID") or "").strip() or None
        out.setdefault((fitid, day, cents), []).append(ofx_utc_day(stamp))
    return out


def pin_ofx_days(root) -> None:
    """Rewrite each stamp the importer dates by to its bare printed day,
    in a parsed OFX element tree, before the tree is converted.

    The converter normalises a zoned stamp to UTC, which loses the day
    the bank wrote; a bare YYYYMMDD converts to midnight UTC of that same
    day, so the date read back afterwards is the printed one. Only the
    day of these stamps is ever used, so dropping the clock time loses
    nothing. A stamp that is not well-formed is left as it is, for the
    converter to refuse."""
    for tag in OFX_DAY_TAGS:
        for el in root.iter(tag):
            day = ofx_printed_day(el.text)
            if day is not None:
                el.text = f"{day.year:04d}{day.month:02d}{day.day:02d}"


def epoch_day(seconds) -> dt.date:
    """The day a bare epoch timestamp (SimpleFIN's `posted`) means.

    The feed sends seconds since the epoch and no zone — neither the
    transaction nor the account says where the bank keeps its books — so
    the instant is read on the UTC calendar. Any other reading would be a
    guess about a zone the feed never named. Raises ValueError,
    OverflowError or OSError for a value the platform cannot convert."""
    return dt.datetime.fromtimestamp(int(seconds), dt.timezone.utc).date()


# The migrations that shipped with OFX lines dated by their printed day:
# the earliest time either was applied is the moment this install first
# ran that dating (a fresh install records them before its first row).
PRINTED_DAY_DATING_MIGRATIONS = (
    "210_payee_keyed_settings_follow_a_cut_bill_name.sql",
    "211_a_folder_import_job_names_the_worker_that_holds_it.sql")


def printed_day_dating_began(conn):
    """When this install first dated OFX lines by their printed day, or
    None when the ledger does not say. schema_migrations is readable by
    the app role (written by migrate alone), so a household's own
    connection can ask."""
    row = conn.execute(
        "SELECT min(applied_at) AS t FROM schema_migrations "
        "WHERE name = ANY(%s)", (list(PRINTED_DAY_DATING_MIGRATIONS),)
    ).fetchone()
    return row["t"] if row else None
