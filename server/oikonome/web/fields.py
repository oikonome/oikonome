"""Typed reads of a JSON request body.

A JSON body is whatever the client sent. Read as a plain dict, every door
has to decide for every field whether `{"payee": 7}`, `{"cap": [1]}` or
`{"adjust": "x"}` is acceptable, and each field somebody forgot to check
surfaces as an opaque 500, or as a list or an int reaching SQL or the
engine. `Fields` wraps the body and exposes only typed accessors. Each one
answers a wrong type with a 400 that names the field, and there is
deliberately no `.get` and no indexing, so a door reading through it cannot
read a field untyped.

A door declares `body: Fields = Depends(json_fields)` instead of
`body: dict = Body(...)`. Present/absent semantics stay the door's own:
`present(key)` says whether the key was sent at all (null included), and
every accessor returns its `default` for a key that is absent or null —
except `money_given`, which refuses a null; `json_type(key)` tells a null
from a blank where a door has always treated the two differently. A
typed reader refuses a value of the wrong kind, but never takes away a
meaning a door gave a value on purpose: where a field has always read ""
as "not given" (a form's empty box, a script clearing a setting), its
accessor is called with `blank_ok=True` and "" reads as absent.

The money, date and short-text readers the doors used before the typed
reader live here too, so the accessors and the older call sites share one
rule each.
"""

from __future__ import annotations

import datetime as dt
import math

from fastapi import Body, HTTPException

# The largest money figure a person may type. Far past anything a
# household holds and far inside what a double carries to the cent — and,
# unlike the float ceiling, small enough that any realistic number of such
# values still SUM to a finite total. Two balances near 1e308 are each
# finite, but net worth, the runway and every other total over them
# overflow to infinity (a 500 under allow_nan=False, or Postgres's float8
# "value out of range") until someone finds and edits the balance.
MONEY_MAX = 1e12


def typed_money(raw) -> float:
    """A hand-typed amount as a number; blank reads as 0. Either separator
    convention is read the way the CSV importer reads it ("1,234.56",
    "1.234,56", "12,50"): stripping every comma would store a decimal-comma
    "12,50" as 1250 and "1.234,56" as 1.23456, with nothing to say so.
    Raises ValueError for anything that is not an amount."""
    from ..sync.csvimport import parse_money_text
    s = str(raw).strip()
    return parse_money_text(s) if s else 0.0


def money_float(raw, field: str) -> float:
    """Parse a user money string; reject NaN/Infinity — they parse fine
    but poison DOUBLE PRECISION columns and then permanently 500 every
    JSON response containing the row (allow_nan=False) — and anything
    past MONEY_MAX, which is finite alone but overflows the totals."""
    try:
        v = typed_money(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be a number")
    if not math.isfinite(v):
        raise HTTPException(400, f"{field} must be a real number")
    if abs(v) > MONEY_MAX:
        raise HTTPException(400, f"{field} is too large")
    return v


def iso_date(v: str, msg: str) -> dt.date:
    """A caller's ISO date, parsed strictly AND inside the years the app
    can read (1900-2100). `date.fromisoformat` alone takes 0001-01-01 and
    9999-12-31: stored, they poison every later reader (goal pacing, the
    receipt matcher's day window, `as_date`), and a day stepped past either
    end of the calendar raises OverflowError — a 500, or a write that
    commits half way. Refused here as a 400."""
    try:
        d = dt.date.fromisoformat(v)
    except (TypeError, ValueError):
        raise HTTPException(400, msg)
    if not 1900 <= d.year <= 2100:
        raise HTTPException(400, msg)
    return d


def body_text(body: dict, *keys: str, default: str = "") -> str:
    """Read a short human-text field (payee, category, name…) from a JSON
    body, safely.

    Clients send whatever JSON they like: `{"display": 123}` would reach
    `.strip()` on an int and surface as an opaque 500. A number or composite
    value is never a usable name here, and str()-coercing one would write
    "123" or "{'a': 1}" into the ledger — so anything that is not a string
    (or null) is a 400. Keys are tried in order (some endpoints accept
    aliases like old/from); blank and missing values fall through to
    `default`."""
    for key in keys:
        v = body.get(key)
        if v is None:
            continue
        if not isinstance(v, str):
            raise HTTPException(400, f"{key} must be a string")
        v = v.strip()
        if v:
            return v
    return default


def _has_control(s: str) -> bool:
    return any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in s)


_KINDS = {str: "strings", int: "whole numbers"}


class Fields:
    """A JSON object read only through typed accessors (see the module)."""

    __slots__ = ("_d",)

    def __init__(self, data):
        if not isinstance(data, dict):
            raise HTTPException(400, "the body must be a JSON object")
        self._d = data

    def _raw(self, key: str, blank_ok: bool = False):
        v = self._d.get(key)
        # `blank_ok`: a door whose field has always read "" as "not given"
        # (a form's empty box, a script clearing a value) keeps that
        # reading; any other wrong kind is still refused
        return None if blank_ok and isinstance(v, str) and v == "" else v

    @staticmethod
    def _no(msg: str | None, fallback: str):
        raise HTTPException(400, msg or fallback)

    def present(self, key: str) -> bool:
        """Whether the key was sent at all — null counts as sent, which is
        how a client clears a value it is allowed to clear."""
        return key in self._d

    def text(self, key: str, max_len: int | None = None, default: str = "",
             *, strip: bool = True, msg: str | None = None) -> str:
        """A string field. Absent, null and (when stripped) blank read as
        `default`; anything that is not a string is a 400, as is one past
        `max_len`. `strip=False` keeps a lookup key exactly as stored."""
        v = self._raw(key)
        if v is None:
            return default
        if not isinstance(v, str):
            self._no(msg, f"{key} must be a string")
        if strip:
            v = v.strip()
        if max_len is not None and len(v) > max_len:
            self._no(msg, f"{key} too long ({max_len} chars max)")
        return v if v else default

    def ident(self, key: str, max_len: int = 200,
              *, msg: str | None = None) -> str:
        """An id or name a door looks a row up by: a bounded string with
        no control characters, "" when absent or blank."""
        v = self.text(key, max_len, msg=msg)
        if _has_control(v):
            self._no(msg, f"{key} contains control characters")
        return v

    def flag(self, key: str, default: bool | None = None,
             *, blank_ok: bool = False,
             msg: str | None = None) -> bool | None:
        """A JSON true/false. Absent or null is `default`; a string or a
        number is a 400 — "false" read as truthy is the classic flip."""
        v = self._raw(key, blank_ok)
        if v is None:
            return default
        if not isinstance(v, bool):
            self._no(msg, f"{key} must be true or false")
        return v

    def money(self, key: str, default: float | None = None,
              *, msg: str | None = None) -> float | None:
        """An amount, typed by hand ("1,234.56") or sent as a number.
        Absent, null and "" are `default`."""
        v = self._raw(key)
        if v is None or v == "":
            return default
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            self._no(msg, f"{key} must be a number")
        if msg is None:
            return money_float(v, key)
        try:
            return money_float(v, key)
        except HTTPException:
            self._no(msg, "")

    def number(self, key: str, *, msg: str | None = None) -> float | None:
        """A plain number, or the text of one as float() reads it ("12.5",
        " 3 ", "1e3") — a door that has always taken either. Absent, null
        and "" are None; a bool, a list, an object or text float() cannot
        read is a 400. Range and finiteness are the door's to judge."""
        v = self._raw(key, blank_ok=True)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            self._no(msg, f"{key} must be a number")
        try:
            return float(v)
        except (ValueError, OverflowError):
            self._no(msg, f"{key} must be a number")

    def int_in(self, key: str, lo: int | None = None, hi: int | None = None,
               default: int | None = None, *, text_ok: bool = False,
               truncate: bool = False, blank_ok: bool = False,
               msg: str | None = None) -> int | None:
        """A whole number in [lo, hi] (either end open when None). Only a
        JSON integer, unless `text_ok` also takes digits in a string (a
        field the clients send from a text input) and `truncate` takes a
        finite fractional number cut to its whole part (a field a client
        builds with Number(text), so "3.5" arrives as 3.5 and the door has
        always read it as 3). Absent and null are `default`, as is "" when
        `text_ok`."""
        v = self._raw(key, blank_ok)
        if v is None:
            return default
        if truncate and isinstance(v, float) and math.isfinite(v):
            v = int(v)
        if text_ok and isinstance(v, str):
            if not v.strip():
                return default
            if len(v) <= 32:
                try:
                    v = int(v.strip())
                except ValueError:
                    pass
        if isinstance(v, bool) or not isinstance(v, int):
            self._no(msg, f"{key} must be an integer")
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            span = (f"between {lo} and {hi}" if lo is not None
                    and hi is not None else
                    f"at least {lo}" if lo is not None else f"at most {hi}")
            self._no(msg, f"{key} must be {span}")
        return v

    def date(self, key: str, default: dt.date | None = None,
             *, msg: str | None = None) -> dt.date | None:
        """An ISO date inside the years the app reads (`iso_date`).
        Absent, null and blank are `default`."""
        v = self.text(key, 10, msg=msg or f"{key} must be an ISO date")
        if not v:
            return default
        return iso_date(v, msg or f"{key} must be an ISO date")

    def one_of(self, key: str, choices, default: str | None = None,
               *, required: bool = False,
               msg: str | None = None) -> str | None:
        """One of a fixed set of strings. Absent, null and "" are `default`
        (a 400 when `required`)."""
        v = self._raw(key)
        if v is None or v == "":
            if required:
                self._no(msg, f"{key} is required")
            return default
        if not isinstance(v, str) or v not in choices:
            self._no(msg, f"{key} must be one of {', '.join(choices)}")
        return v

    def list_of(self, key: str, kind, max_n: int,
                *, blank_ok: bool = False,
                msg: str | None = None) -> list | None:
        """A JSON array of at most `max_n` items, each a `kind`: `str`,
        `int` (never a bool) or `Fields` (each item an object, wrapped).
        Absent or null is None, so a door can tell "not sent" from "sent
        empty"."""
        v = self._raw(key, blank_ok)
        if v is None:
            return None
        noun = "objects" if kind is Fields else _KINDS[kind]
        fallback = f"{key} must be a list of up to {max_n} {noun}"
        if not isinstance(v, list) or len(v) > max_n:
            self._no(msg, fallback)
        out = []
        for item in v:
            if kind is Fields:
                if not isinstance(item, dict):
                    self._no(msg, fallback)
                out.append(Fields(item))
            elif kind is int:
                if isinstance(item, bool) or not isinstance(item, int):
                    self._no(msg, fallback)
                out.append(item)
            else:
                if not isinstance(item, str):
                    self._no(msg, fallback)
                out.append(item)
        return out

    def sub(self, key: str, *, blank_ok: bool = False,
            msg: str | None = None) -> Fields | None:
        """A nested object, read through its own typed accessors. Absent or
        null is None."""
        v = self._raw(key, blank_ok)
        if v is None:
            return None
        if not isinstance(v, dict):
            self._no(msg, f"{key} must be an object")
        return Fields(v)

    def opaque(self, key: str, *, blank_ok: bool = False,
               msg: str | None = None) -> dict | None:
        """A nested object handed WHOLE to an engine reader that checks
        every value it uses itself (an undo snapshot the server issued).
        Only its type is checked here. Absent or null is None."""
        v = self._raw(key, blank_ok)
        if v is None:
            return None
        if not isinstance(v, dict):
            self._no(msg, f"{key} must be an object")
        return v

    def opaque_rows(self, key: str, max_n: int,
                    *, blank_ok: bool = False,
                    msg: str | None = None) -> list | None:
        """A JSON array of at most `max_n` items handed WHOLE to an engine
        reader that skips or checks each item itself (a collector's rows,
        a folder import's plan). Only the list and its length are checked
        here; the items are exactly what was sent. Absent or null is None.
        The two refusals read "<key> must be a list" and "too many <key>
        (max N)", the words the engine readers use for the same faults."""
        v = self._raw(key, blank_ok)
        if v is None:
            return None
        if not isinstance(v, list):
            self._no(msg, f"{key} must be a list")
        if len(v) > max_n:
            self._no(msg, f"too many {key} (max {max_n})")
        return v

    def text_or_list(self, key: str, *, msg: str | None = None):
        """A field a person types as one string and a client may also send
        as a list (addresses, account masks): the string or the list
        exactly as sent, for the reader that splits and checks each entry.
        Absent or null is None; any other kind is a 400."""
        v = self._raw(key)
        if v is None or isinstance(v, (str, list)):
            return v
        self._no(msg, f"{key} must be a string or a list")

    def without(self, *keys: str) -> Fields:
        """The same body with `keys` left out, for a door that lets one
        field stand in for another (a change to one address in place of
        the whole list)."""
        return Fields({k: v for k, v in self._d.items() if k not in keys})

    def with_value(self, key: str, value) -> Fields:
        """The same body with `key` set to a value the door computed from
        what was sent (an entry merged back that the caller was not shown);
        read back through the same typed accessors."""
        return Fields({**self._d, key: value})

    def json_type(self, key: str) -> str | None:
        """The JSON type the key was sent as — "null", "bool", "number",
        "string", "list" or "object" — or None when it was not sent. The
        value itself is never handed out. For the doors that have always
        told a null from a blank (a store kept versus a store cleared), and
        for the few that read a key one way when it has one type and leave
        it alone when it has another."""
        if key not in self._d:
            return None
        v = self._d[key]
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, (int, float)):
            return "number"
        if isinstance(v, str):
            return "string"
        return "list" if isinstance(v, list) else "object"

    def money_given(self, key: str, blank: float = 0.0,
                    *, label: str | None = None) -> float:
        """An amount the caller sent and must state: a text box's string
        ("1,234.56", "" included) or a number. A blank box is `blank`.
        A null is refused, never read as the blank: it is what a client's
        NaN becomes on the wire, so a figure the person typed and the
        client could not read would otherwise be stored as 0 without a
        word. Absent is refused too — a door asks `present` first when a
        key may be left out. Refusals name `label` (default the key)."""
        name = label or key
        v = self._raw(key)
        if isinstance(v, str) and not v.strip():
            return blank
        if v is None or isinstance(v, bool) or not isinstance(
                v, (str, int, float)):
            raise HTTPException(400, f"{name} must be a number")
        try:
            n = typed_money(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{name} must be a number")
        if not math.isfinite(n):
            raise HTTPException(400, f"{name} must be a finite number")
        if abs(n) > MONEY_MAX:
            raise HTTPException(400, f"{name} is too large")
        return n

    def text_or_int(self, key: str, default: str | None = None,
                    *, strip: bool = True,
                    msg: str | None = None) -> str | None:
        """A string, or a whole number read as its digits — a field a
        person types as text that a script of their own may send as a
        bare JSON number (the last digits of a tax id). Absent, null and
        (when stripped) blank are `default`; a bool, a fraction, a list or
        an object is a 400."""
        v = self._raw(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return str(v)
        try:
            return self.text(key, default=default, strip=strip)
        except HTTPException:
            self._no(msg, f"{key} must be a string or a whole number")

    def as_dict(self) -> dict:
        """The whole body as a plain dict, for a door that hands ALL of it
        to one engine reader which checks every key it uses (a collector's
        normalized payload). A shallow copy: what the reader does to its
        top level is not seen by this door's other reads."""
        return dict(self._d)


def json_fields(body: dict = Body(...)) -> Fields:
    """The dependency a door takes its JSON body through."""
    return Fields(body)


def json_fields_or_empty(body: dict | None = Body(default=None)) -> Fields:
    """`json_fields` for a door whose whole body is optional: a POST that
    sends none, or sends a JSON null, reads as `{}`, as it always has."""
    return Fields(body if body is not None else {})
