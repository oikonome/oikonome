"""One row cap for every file importer.

The upload path caps BYTES (MAX_UPLOAD, 50 MB), but rows are what cost:
each one is parsed into memory, run through the O(candidates) dedup scan,
and then upserted with its own synchronous INSERT inside the request
handler — no background job, no chunking. Tiny rows (`1/1/24,1,a\\n` is
~12 bytes) fit ~4 million to a 50 MB file, so without a row cap one
authenticated POST could hold a worker and a DB connection for hours, and
on a multi-tenant instance that worker is shared.

The script-token collector doors (`plan_csv.py`, `coinbase_push.py`) carry
their own `MAX_ROWS`, and `restore.py` has MAX_ROWS_PER_MEMBER. This cap is
higher on purpose: a collector pushes one account's recent activity, while
a person migrating off Mint or Quicken legitimately arrives with a decade
of history. 50k rows is far beyond any real export and still bounds the
work.
"""

MAX_IMPORT_ROWS = 50_000


def capped(rows, *, limit: int | None = None, what: str = "rows"):
    """Yield `rows`, refusing past the cap.

    A generator on purpose: it must not materialize the input to count it,
    or the guard would itself do the thing it exists to prevent.
    """
    limit = limit or MAX_IMPORT_ROWS
    for i, row in enumerate(rows, 1):
        if i > limit:
            raise ValueError(
                f"too many {what} in one file (max {limit:,}). Split the "
                f"export into smaller files and import them one at a time.")
        yield row


def check_len(rows, *, limit: int | None = None, what: str = "rows"):
    """Same cap for parsers that hand back a materialized list (OFX/QIF),
    where refusing before the DB work is still worth doing."""
    limit = limit or MAX_IMPORT_ROWS
    if len(rows) > limit:
        raise ValueError(
            f"too many {what} in one file (max {limit:,}). Split the "
            f"export into smaller files and import them one at a time.")
    return rows


# Aggregator pulls funnel through base.upsert_transactions, which needs its
# own row ceiling: the file importers above cap what a user uploads, but a
# provider payload is otherwise unbounded. That matters because one of
# the upstreams is tenant-supplied: a SimpleFIN access URL points at
# whatever bridge the tenant configured, so a hostile bridge can stream
# an arbitrarily long transaction list into the synchronous upsert loop.
# Far above any legitimate pull (a 2-year MX backfill of a busy household
# is a few tens of thousands of rows; Plaid pages at ~500) so only an
# implausible payload refuses.
MAX_SYNC_ROWS = 250_000


def check_sync_len(rows, *, limit: int | None = None,
                   what: str = "transactions"):
    limit = limit or MAX_SYNC_ROWS
    if len(rows) > limit:
        raise ValueError(
            f"refusing to store {len(rows):,} {what} from one sync batch "
            f"(max {limit:,}) — the provider payload is implausibly large")
    return rows
