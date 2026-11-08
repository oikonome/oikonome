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
