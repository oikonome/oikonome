"""sqlite→Postgres porting shims (THE PATTERN — see CLAUDE.md).

The engine's SQL was written against sqlite3, where dates are TEXT and raw
JSON is TEXT. On Postgres (DATE / JSONB via psycopg dict_row) the same
values arrive as datetime.date and dict. These coercers make the ported code
accept both, so the port stays mechanical.

Rules replicated across every ported module:
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


def jsonb(v: dict) -> Jsonb:
    return Jsonb(v)
