"""How many file bytes a household holds, and the one quota on them.

A household's uploads are kept in the database: receipt images (and their
cleaned-up copies), imports parked between requests, a folder import's
files until each has run, and the categorizer model trained from its own
corrections. Rate limits bound how fast those grow, never how far; without
a quota one household could fill the disk every household writes to.

`bytes_held` adds up every such column for the calling household (the
connection is tenant-scoped, RLS does the binding), and `reserve` refuses
an upload that would take the household past its quota. Call `reserve`
inside the transaction that writes the bytes: it takes a per-household
advisory lock, so two uploads racing each other cannot both fit under room
only one of them leaves.

The quota is `OIKONOME_HOUSEHOLD_STORAGE_MB` (default `DEFAULT_MB`; 0 turns
it off). The staging area keeps its own smaller budget for temporary bytes
(db/staging.py); those bytes count here as well.
"""

from __future__ import annotations

from ..envnum import env_num

DEFAULT_MB = 2048

# Every tenant table column that holds file bytes. A test compares this
# list with the live schema, so a new one cannot be added unlisted.
BLOB_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("receipts", ("image", "image_optimized")),
    ("import_staging", ("data",)),
    ("bulk_import_files", ("data",)),
    ("categorizer_model", ("artifact",)),
)


class StorageQuotaExceeded(ValueError):
    """The upload would take the household past its storage quota. A
    ValueError, so a door that already turns ValueError into a refusal
    refuses it; a door that can should answer 413 with the message."""


def quota_bytes() -> int | None:
    """The per-household quota in bytes, or None when it is turned off."""
    try:
        mb = int(env_num("OIKONOME_HOUSEHOLD_STORAGE_MB", str(DEFAULT_MB)))
    except ValueError:
        mb = DEFAULT_MB
    if mb <= 0:
        return None
    return mb * 1024 * 1024


def _sum_sql() -> str:
    parts = []
    for table, cols in BLOB_COLUMNS:
        summed = " + ".join(f"COALESCE(octet_length({c}), 0)" for c in cols)
        parts.append(f"(SELECT COALESCE(SUM({summed}), 0) FROM {table})")
    return "SELECT (" + " + ".join(parts) + ")::bigint AS n"


_SUM_SQL = _sum_sql()


def bytes_held(conn) -> int:
    """Every file byte the calling household holds, across BLOB_COLUMNS."""
    return int(conn.execute(_SUM_SQL).fetchone()["n"] or 0)


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):,.0f} MB"


def reserve(conn, incoming: int) -> None:
    """Refuse (StorageQuotaExceeded) when `incoming` more bytes would take
    the household past its quota. Run it in the transaction that writes
    the bytes; the advisory lock it takes is held to that transaction's
    end."""
    quota = quota_bytes()
    if quota is None or incoming <= 0:
        return
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext("
        "'oikonome:storage:' || current_setting('app.tenant_id')))")
    held = bytes_held(conn)
    if held + incoming > quota:
        raise StorageQuotaExceeded(
            f"this household's file storage is full ({_mb(held)} of "
            f"{_mb(quota)} used) — delete receipts or finish the imports "
            f"in flight to make room")
