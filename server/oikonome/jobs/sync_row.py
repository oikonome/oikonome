"""What the 'sync' row in job_progress means.

A process killed mid-sync never reaches the `finally` that settles the row, so
it stays state='running' with no owner until the next sweep rewrites it. The
readers of the row and the claim that replaces it have to agree on when it
stops meaning anything; this is their one rule. Kept apart from jobs/worker
because the web process reads it on every poll and has no other reason to load
the scheduler.

Silence alone does not end a run: nothing writes the row inside a phase, and a
categorize pass can go quiet for minutes while it works. What does end it is
its session. sync_tenant holds the advisory lock on one connection for the
whole run, and the server drops that lock the moment the session goes, wherever
the worker ran. So a row that has gone quiet is dead only once nobody holds the
lock.
"""
from __future__ import annotations

import datetime as dt

SYNC_STALE_SECONDS = 180
SYNC_STALE_AFTER = f"{SYNC_STALE_SECONDS} seconds"

# A read, never a try-lock: a try-then-release, however brief, could turn away
# a real sync starting in that instant, because sync_tenant only tries once.
# The bigint key is hashtext's signed int4 widened, which pg_locks reports as
# two unsigned halves. pg_locks spans the cluster, so the database is named, and
# it is readable by every role. It takes the lock manager's partition locks, so
# ask it only about a row that has already gone quiet. For a connection scoped
# to a tenant; takes no parameters, so it can sit inside a larger statement.
SYNC_LOCK_HELD_SQL = """EXISTS (
    SELECT 1 FROM pg_locks
    WHERE locktype = 'advisory' AND granted AND objsubid = 1
      AND database = (SELECT oid FROM pg_database
                      WHERE datname = current_database())
      AND ((classid::bigint << 32) | objid::bigint)
          = hashtext('oikonome:sync:' || current_setting('app.tenant_id'))::bigint
)"""


def sync_row_live(conn, row) -> bool:
    """Does this job_progress row (state, updated_at) mean a sync is running?
    `conn` is scoped to the row's tenant."""
    if row["state"] != "running":
        return False
    age = dt.datetime.now(row["updated_at"].tzinfo) - row["updated_at"]
    if age.total_seconds() < SYNC_STALE_SECONDS:
        return True
    return conn.execute(
        f"SELECT {SYNC_LOCK_HELD_SQL} AS held").fetchone()["held"]
