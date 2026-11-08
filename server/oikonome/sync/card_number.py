"""What a card or account number is, and how it is cut to its last four.

A household types account names, and files carry row names, merchants and
memo cells, that spell a full card or account number. Wherever the product
stores or shows such text it keeps only the last four digits, behind "…"
("Visa …0002"). This module is the one place that decides what counts as
a number; the stored-name cut, the selectors the migrations and the
re-key use to find rows worth cutting, and the SQL the migrations run are
all built from the definitions below, so Python and PostgreSQL cannot
disagree about a name.

The grammar, in prose:

* An unbroken run of eight or more digits is a number. Eight is short for
  a card, but nothing a ledger needs (an amount, a date, a check or store
  number) runs to eight digits unbroken, and an account number often does.

* A number printed in groups is one of the two layouts cards are printed
  in, and nothing else: four groups of four digits (16 digits), or four,
  six and five (the 15-digit cards). Recognising "any digits in groups"
  instead would cut a date, a statement period or what follows the
  number; fixed layouts cannot. A date always
  has a one- or two-digit day or month, a phone number has groups of three,
  "2023-2024" is two groups, and a group of seven digits is no card
  layout, so none of them is read as a number.

* Between the groups may stand one to three of: space, dash, dot, slash
  ("4000 0000 0000 0002", "4000.0000.0000.0002", "4000 / 0000 / 0000 /
  0002", double spaces), mixed freely ("4000-0000 0000-0002"). Any
  whitespace counts as a space — a tab from a spreadsheet cell, a line
  break, a no-break space from a statement — and reads as the same
  separator as a space wherever the grammar compares two. An
  account's id is the slug of its name, and a slug folds every such
  separator into one dash: the name has to be cut wherever the id would
  spell the number, or the page shows the digits the id hides. The same
  reading makes two year ranges typed side by side ("2023-2024
  2024-2025") four groups of four and so a number; the id has always read
  them so.

* Further groups of four after the fourth, joined by the same separator
  as the one before the fourth group, belong to the run, and the whole run
  is cut to its last four: a year or a short account number typed in front
  of a card with the card's own separator would otherwise leave the card's
  last eight digits in the open. A group joined differently ("…0002 -
  2025"), or a group of one to three digits after the number ("…0002 123",
  a door's "-2" collision suffix), is not part of it and stays as typed:
  shape alone cannot tell a 19-digit card from a 16-digit one followed by
  a short number, and the suffix is what keeps two accounts' ids apart.

* No digit may touch either end of a number, so a number is never read
  out of the middle of a longer run.

The cut replaces each number with "…" and its last four digits. A number
in groups is cut first, so a card is read as one number before the
unbroken-run cut could see any part of it; a "…" directly in front of a
number in groups is taken into its mark, so cutting what is already cut
never stacks marks. One pass is not always the end: "12345678 1234 1234
1234" holds no layout until its eight-digit run is cut, and then
"…5678 1234 1234 1234" does. So `cut` makes two passes, which reaches the
fixed point (cut(cut(x)) == cut(x)) — the tests prove it over the shared
fixture and random text, and the SQL makes the same two passes.

The hash input of a file row (`cut_for_hash`) is the unbroken-run cut
alone, byte for byte as it has always been: widening it would re-id every
re-imported row whose text holds a grouped number.

Every pattern here is written in the dialect Python's `re` and
PostgreSQL's advanced regular expressions share, so the same string
drives both. None of them holds a back-reference: PostgreSQL back-tracks
on one, and "joined as the fourth was" written as one
took time growing with the fourth power of the text on dense groups of
four (3,200 characters, a minute). So the joining separator is spelled
out, one alternative for each of the 84 it can be (whitespace of any
kind standing as one), and both engines stay linear.

Migrations cut in Python. A migration that has stored text to cut runs
`cut` over the rows in a Python step beside its marker
(db/migrate.py, PYTHON_STEPS), selecting them with `sql_selector`, a
pattern with no back-reference and a fixed set of alternatives. The SQL
cut (`sql_cut`) is the same cut, and linear, but slow per byte on text
dense with groups of four (about 12.5 s a megabyte); a migration never
feeds stored text to it. A migration that already calls the SQL cut
stays as written, since an applied migration is never edited.
"""

from __future__ import annotations

import itertools
import re

#: the mark a cut leaves in front of the last four digits
MARK = "…"

# what reads as a space between two groups: any whitespace a cell, a
# statement or a typed name carries, spelled out so both engines read the
# same characters (neither's `\s` is the other's)
_WS = r"[ \t\n\v\f\r\u00a0]"

#: between two groups of a number: one to three of whitespace, dash, dot,
#: slash
_SEP = r"[ \t\n\v\f\r\u00a0./-]{1,3}"

#: every separator `_SEP` matches, spelled out, with " " standing for any
#: whitespace
SEPARATORS = tuple("".join(p) for n in (1, 2, 3)
                   for p in itertools.product(" ./-", repeat=n))


def _spelled(sep: str) -> str:
    """`sep` (one of SEPARATORS) as a pattern: its space any whitespace."""
    return "".join(_WS if c == " " else "\\." if c == "." else c for c in sep)


# the fourth group's separator and the further fours joined by it, one
# alternative per separator: a separator always runs from one digit to the
# next, so exactly one alternative can match where it stands
_JOINED_AS_THE_FOURTH = "|".join(
    rf"{s}(?:\d{{4}}{s})*" for s in map(_spelled, SEPARATORS))

# four groups of four, further fours joined as the fourth was, or 4-6-5;
# the last four digits are captured (group 1 or 2) for the mark
_LAYOUTS = (
    rf"\d{{4}}{_SEP}\d{{4}}{_SEP}\d{{4}}(?:{_JOINED_AS_THE_FOURTH})(\d{{4}})"
    rf"|\d{{4}}{_SEP}\d{{6}}{_SEP}\d(\d{{4}})")

#: a number in groups, with a mark directly in front of it taken in
GROUPED_PATTERN = rf"{MARK}?(?<!\d)(?:{_LAYOUTS})(?!\d)"
GROUPED_REPLACEMENT = MARK + r"\1\2"

#: an unbroken run of eight or more digits, its last four captured
LONG_PATTERN = r"\d{4,}(\d{4})"
LONG_REPLACEMENT = MARK + r"\1"

#: an unbroken run of eight or more digits (the hash input's cut)
LONG_RUN_PATTERN = r"\d{8,}"

#: where a cut left its mark
CUT_MARK_PATTERN = MARK + r"[0-9]{4}"

# the layouts without captures: what a selector reads
_LAYOUT_SHAPES = (rf"\d{{4}}{_SEP}\d{{4}}{_SEP}\d{{4}}{_SEP}\d{{4}}"
                  rf"|\d{{4}}{_SEP}\d{{6}}{_SEP}\d{{5}}")

GROUPED_RE = re.compile(GROUPED_PATTERN)
LONG_RE = re.compile(LONG_PATTERN)
LONG_RUN_RE = re.compile(LONG_RUN_PATTERN)
_ENDS_IN_NUMBER_RE = re.compile(rf"(?:\d{{8}}|{_LAYOUT_SHAPES})$")
_DIGITS_BETWEEN_RE = re.compile(r"(?<=\d)[^0-9A-Za-z]+(?=\d)")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _pass(text: str) -> str:
    return LONG_RE.sub(LONG_REPLACEMENT,
                       GROUPED_RE.sub(GROUPED_REPLACEMENT, text))


def cut(text):
    """`text` with every number in it (see the module's grammar) cut to
    "…" and its last four digits, to a fixed point. Anything that is not
    a str is returned as it is."""
    if not isinstance(text, str):
        return text
    return _pass(_pass(text))


def cut_for_hash(text):
    """`text` with every unbroken run of eight or more digits cut to "…"
    and its last four, and nothing else: the input a file row's id is
    hashed over, which must never change (see the module docstring)."""
    if not isinstance(text, str):
        return text
    return LONG_RUN_RE.sub(lambda m: MARK + m.group()[-4:], text)


def holds_number(text) -> bool:
    """Whether `text` spells a number `cut` would cut."""
    return isinstance(text, str) and cut(text) != text


def ends_in_number(text: str) -> bool:
    """Whether `text` ends in a full number: an unbroken run of eight, or
    the tail of a card layout. What tells a door's "-N" collision suffix
    after a number from the number's own last group."""
    return bool(_ENDS_IN_NUMBER_RE.search(text or ""))


def cut_as_slug_reads(text):
    """`cut` of `text` read the way an account id's slug reads it: any run
    of punctuation between two digits counts as one separator, as the
    slug folds it into one dash. For a name whose slug spells a number the
    grammar's separators do not ("4000_0000_0000_0002"), so the stored
    name keeps no more digits than the id."""
    if not isinstance(text, str):
        return text
    read = _DIGITS_BETWEEN_RE.sub(" ", text)
    done = cut(read)
    return done if done != read else cut(text)


def cut_slug(slug: str, width: int | None = None) -> str:
    """A slug ("visa-4000-0000-0000-0002") with every number it spells
    cut to its last four, cut to `width` characters when given. A slug
    drops the cut's "…", so the last four can join the digits after them
    into a number again ("1234-1234-1234-12345678" is "1234-1234-1234-5678"
    once cut and slugged): repeated until the slug holds none. Each round
    removes digits, so it ends."""
    body = slug
    while True:
        nxt = _SLUG_RE.sub("-", cut(body.replace("-", " ")).lower()
                           ).strip("-")
        if width is not None:
            nxt = nxt[:width].strip("-")
        if nxt == body or not holds_number(nxt):
            return nxt
        body = nxt


def last4(number: str) -> str:
    """The last four digits of `number`, whatever separates them."""
    return re.sub(r"\D", "", number or "")[-4:]


def _spans(text: str) -> list[tuple[int, int]]:
    """Where in `text` the numbers `cut` cuts stand, as (start, end)."""
    # each character of the text as it is cut, with the span of the
    # original it stands for
    chars = [(c, i, i + 1) for i, c in enumerate(text)]
    found: list[tuple[int, int]] = []
    for _ in range(2):
        for rx in (GROUPED_RE, LONG_RE):
            s = "".join(c for c, _, _ in chars)
            out, at = [], 0
            for m in rx.finditer(s):
                seg = chars[m.start():m.end()]
                lo, hi = seg[0][1], seg[-1][2]
                while (lo < hi and not text[lo].isdigit()
                       and text[lo] != MARK):
                    lo += 1
                found.append((lo, hi))
                out += chars[at:m.start()]
                out += [(c, lo, hi) for c in m.expand(
                    GROUPED_REPLACEMENT if rx is GROUPED_RE
                    else LONG_REPLACEMENT)]
                at = m.end()
            chars = out + chars[at:]
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(found):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def find(text) -> list[str]:
    """Every number in `text` that `cut` cuts, as it is spelled there."""
    if not isinstance(text, str):
        return []
    return [text[a:b].lstrip(MARK) for a, b in _spans(text)]


def mask(text) -> str | None:
    """The last four digits of the last number in `text`, the account
    mask a name spelling one implies; None when it spells none."""
    nums = find(text)
    return last4(nums[-1]) if nums else None


# ---------------------------------------------------------------- SQL

def _literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def sql_selector() -> str:
    """A pattern for `~` that every text `cut` changes matches: an
    unbroken run of eight or a card layout, any separators. Cheaper than
    the cut and wider (blind to which separator joins a fifth group, and
    to a run already cut); what it selects is handed to the cut, which
    decides exactly."""
    return rf"\d{{8}}|{_LAYOUT_SHAPES}"


def sql_cut(expr: str) -> str:
    """A SQL expression computing `cut` of the SQL expression `expr`: the
    same patterns, the same two passes. With standard_conforming_strings
    on, as every supported server has by default."""
    def one(e: str) -> str:
        grouped = (f"regexp_replace({e}, {_literal(GROUPED_PATTERN)}, "
                   f"{_literal(GROUPED_REPLACEMENT)}, 'g')")
        return (f"regexp_replace({grouped}, {_literal(LONG_PATTERN)}, "
                f"{_literal(LONG_REPLACEMENT)}, 'g')")
    return one(one(expr))


def sql_cut_function(name: str = "pg_temp.cut_numbers") -> str:
    """CREATE FUNCTION text for `name`(text) returning `sql_cut` of it,
    for a migration to define once and call per column."""
    return (f"CREATE OR REPLACE FUNCTION {name}(v text)\n"
            f"RETURNS text LANGUAGE sql IMMUTABLE AS $$\n"
            f"    SELECT {sql_cut('v')}\n$$;")
