"""Date and JSON coercion shims (THE PATTERN — see DEVELOPMENT.md → SQL / psycopg conventions).

Engine code may receive dates and raw JSON as TEXT (restores, fixtures,
client input) or, from Postgres (DATE / JSONB via psycopg dict_row), as
datetime.date and dict. These coercers make the engine accept both.

Conventions every engine module follows:
  * `?` placeholders  → `%s`
  * `json.loads(row["raw"])`         → `as_dict(row["raw"])`
  * `dt.date.fromisoformat(row[c])`  → `as_date(row[c])`
  * date params: pass datetime.date objects, never .isoformat() strings
  * `with conn:`  (sqlite = transaction)  → `with conn.transaction():`
      (psycopg `with conn:` CLOSES the connection — silent breakage)
  * `INSERT OR REPLACE` → `INSERT ... ON CONFLICT (tenant_id, id) DO UPDATE`
  * `INSERT OR IGNORE`  → `INSERT ... ON CONFLICT DO NOTHING`
  * sqlite `instr(x,y)` → `strpos(x,y)`; `datetime('now')` → `now()`
  * JSON writes: wrap dicts in psycopg Jsonb (see `jsonb`)
  * tenant_id: NEVER referenced in engine SQL — ambient
    `app.tenant_id` + column DEFAULT + RLS handle it (db/tenancy.py)
"""

import datetime as dt
import json

from psycopg.types.json import Jsonb


# Dates a person can plausibly mean. `date` itself goes to year 9999, and
# code that walks FORWARD from a stored date (the compliance calendar steps
# year by year from an entity's formation month) hits `year 10000 is out of
# range` and 500s on a value that was accepted years earlier. Bound it at the
# door instead: nobody forms an LLC in 3271, and if they do, they can file
# their own annual report.
_YEAR_MIN, _YEAR_MAX = 1800, 2200


# The largest amount any money field may hold: far past any charge a
# household ledger sees, and far inside what a double rounds to exact
# cents. Finite alone is not a bound — 1e308 is finite, and the first
# conversion of it to cents overflows.
MAX_AMOUNT = 1e12


def as_date(v) -> dt.date | None:
    if v is None or isinstance(v, dt.date):
        d = v
    else:
        d = dt.date.fromisoformat(str(v)[:10])
    if d is not None and not (_YEAR_MIN <= d.year <= _YEAR_MAX):
        raise ValueError(
            f"year {d.year} is outside the supported range "
            f"({_YEAR_MIN}-{_YEAR_MAX})")
    return d


def req_date(v) -> dt.date:
    """A required date input, parsed strictly. Raises ValueError if missing or
    malformed (so a bad request becomes a 400, not a DB 500)."""
    d = as_date(v)
    if d is None:
        raise ValueError("a valid date (YYYY-MM-DD) is required")
    return d


def as_dict(v) -> dict:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    return json.loads(v)


def strip_nul(v):
    """`v` with every U+0000 removed from its strings, nested dicts and
    lists included. Postgres text and jsonb cannot hold the character, so a
    value carrying one fails its whole write; it means nothing in a name or
    a memo, and it arrives in honest input (a UTF-16 file decoded as UTF-8
    has one after every ASCII letter).

    A dict KEY holding one is refused with ValueError, never rewritten.
    Dropping the NUL would turn "limit\\x00" into "limit" and replace the real
    key's value, after every check that compares keys exactly (a policy
    denylist, a guarded destination, a network check on a URL) had already
    waved the stranger through. No honest document needs one in a key."""
    if isinstance(v, str):
        return v.replace("\x00", "") if "\x00" in v else v
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if isinstance(k, str) and "\x00" in k:
                raise ValueError(
                    "a key holds a NUL character (U+0000), which no genuine "
                    "document carries")
            out[k] = strip_nul(x)
        return out
    if isinstance(v, (list, tuple)):
        return [strip_nul(x) for x in v]
    return v


def _dumps_without_nul(v) -> str:
    s = json.dumps(v)
    # JSON spells U+0000 as the escape \u0000, which jsonb refuses. The
    # escape's text can also appear without one (an escaped backslash
    # followed by "u0000"), so a hit only means "look closer": the value is
    # stripped and dumped again, and an honest backslash survives that. A
    # NUL in a key raises there instead (see strip_nul).
    if "\\u0000" in s:
        s = json.dumps(strip_nul(v))
    return s


def jsonb(v: dict) -> Jsonb:
    # Every jsonb write in the app goes through here, so no document that
    # carries a NUL in a value from a feed, a file or a push can fail its
    # write. The document is serialised now rather than when the statement
    # binds it, so a NUL in a key surfaces as ValueError at the caller,
    # which every import door already answers as a bad file or payload.
    s = _dumps_without_nul(v)
    return Jsonb(v, dumps=lambda _obj: s)
