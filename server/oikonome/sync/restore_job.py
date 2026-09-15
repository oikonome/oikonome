"""Restoring a whole export runs as a BACKGROUND JOB, not inside the upload.

A full household archive — tens of thousands of rows and megabytes of
receipts — takes minutes to merge. Held open inside the POST, that is
longer than the edge in front of a hosted instance will wait: Cloudflare
cuts the response at 100 s and hands the browser an error page while the
restore goes on to finish normally on the server. The person doing the one
migration this product promises then reads "it failed" about work that
succeeded, and their instinct is to upload it again.

So the upload's job is only to accept the bytes and start the work.
Progress lands in `job_progress` under the id below — the same row shape
the sync job uses — and the client polls it to completion. That also
means the page can say WHERE the restore is instead of spinning, which is
the difference between a slow operation and a hung one.

The claim is per tenant and atomic: a second restore while one is running
is refused rather than queued, because two merges of overlapping archives
racing each other is not a state worth reasoning about.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

from ..db import tenancy
from ..engine.compat import as_dict, jsonb

log = logging.getLogger(__name__)

JOB_ID = "restore"
# A restore holds no aggregator quota and touches only this tenant, so the
# only reason to declare a claim dead is that the worker thread itself is
# gone (container restart mid-restore). Every tick refreshes updated_at,
# so this window is measured against SILENCE, not against total runtime —
# it must be longer than the gap between ticks, not longer than a restore.
STALE_SECONDS = 10 * 60
STALE_AFTER = f"{STALE_SECONDS} seconds"
# How long a finished restore stays on screen. The import page shows this
# state whenever it is opened — not only right after the upload that
# started the job, because the job outlives the page and a reload must not
# lose it. But the row is never deleted, so without a window the outcome
# of a restore from months ago would greet every visit to the page
# forever. An hour is long enough to come back to a result and find it,
# and long past the end of even a slow restore.
SETTLED_VISIBLE_SECONDS = 60 * 60


def status(conn) -> dict:
    """The tenant's restore job as the client should see it."""
    row = conn.execute(
        "SELECT state, progress, started_at, updated_at FROM job_progress "
        "WHERE id=%s", (JOB_ID,)).fetchone()
    if not row:
        return {"state": "idle", "progress": {}}
    prog = as_dict(row["progress"]) or {}
    state = row["state"]
    age = dt.datetime.now(row["updated_at"].tzinfo) - row["updated_at"]
    if state == "running":
        if age.total_seconds() > STALE_SECONDS:
            # the thread died without settling its own row; saying
            # "running" forever would leave the page polling a ghost
            state = "error"
            prog = dict(prog, error="the restore stopped unexpectedly — "
                                    "reload and check before uploading again")
    elif age.total_seconds() > SETTLED_VISIBLE_SECONDS:
        # a finished (or failed) restore is history after a while, and
        # history is not something to show on every visit to the page
        return {"state": "idle", "progress": {}}
    return {"state": state, "progress": prog,
            "started_at": row["started_at"].isoformat() if row["started_at"]
            else None}


def start(tenant_id: str, data: bytes) -> dict:
    """Claim the job row and run the restore on a worker thread.

    Returns the import hub's `result` dict — either the started marker the
    client polls on, or an error when a restore is already in flight.
    """
    conn = tenancy.tenant_connect(tenant_id)
    try:
        claimed = conn.execute(
            """INSERT INTO job_progress (id, state, progress)
               VALUES (%s, 'running', %s::jsonb)
               ON CONFLICT (tenant_id, id) DO UPDATE SET
                   state='running', progress=EXCLUDED.progress,
                   started_at=now(), updated_at=now()
               WHERE job_progress.state != 'running'
                  OR job_progress.updated_at < now() - %s::interval
               RETURNING id""",
            (JOB_ID, jsonb({"table": None, "rows": 0, "tables_done": 0}),
             STALE_AFTER)).fetchone()
    finally:
        conn.close()
    if claimed is None:
        return {"error": "a restore is already running on this account — "
                         "wait for it to finish"}

    threading.Thread(target=_run, args=(tenant_id, data),
                     name="oikonome-restore", daemon=True).start()
    return {"restore_started": True, "job": JOB_ID}


def _run(tenant_id: str, data: bytes) -> None:
    from ..web.pages import restore_result
    from . import restore

    conn = tenancy.tenant_connect(tenant_id)
    # the ticker writes on its OWN connection: the restore holds one
    # transaction from first row to last, so a progress write sharing it
    # would either be rolled back with a failure or, worse, keep the
    # transaction alive for reasons unrelated to the data
    prog_conn = tenancy.tenant_connect(tenant_id)
    seen: set[str] = set()

    def tick(table: str, rows: int, done: bool) -> None:
        if done:
            seen.add(table)
        try:
            prog_conn.execute(
                "UPDATE job_progress SET progress=%s::jsonb, updated_at=now() "
                "WHERE id=%s",
                (jsonb({"table": table, "rows": rows,
                        "tables_done": len(seen)}), JOB_ID))
        except Exception:                                # noqa: BLE001
            log.debug("restore progress write failed", exc_info=True)

    try:
        # The same per-tenant lock sync, reap and the purge paths contend
        # on. purge_scheduled_deletions and the console's immediate delete
        # promise "whichever of purge/restore takes the lock first wins" —
        # that was true of the old synchronous restore and was silently
        # dropped when this moved to a background thread: a purge running
        # beside an uncommitted restore deleted only the rows that already
        # existed, and the restore's later commit resurrected a tenant
        # whose owner had been told it was erased. Session-scoped:
        # explicitly unlocked in finally, exactly like sync_tenant — a
        # pooled connection handed back still holding it would lock the
        # tenant out of syncing until the backend died.
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:sync:{tenant_id}",)).fetchone()["ok"]
        if not locked:
            raise ValueError("the account is busy (a sync or delete is "
                             "running) — try the restore again in a minute")
        counts = restore.restore_zip(conn, data, progress=tick)
        result = restore_result(counts)
        prog_conn.execute(
            "UPDATE job_progress SET state='done', progress=%s::jsonb, "
            "updated_at=now() WHERE id=%s", (jsonb({"result": result}), JOB_ID))
        # the inline path ran this after every successful import; going
        # async must not quietly drop it. An archive normally carries its
        # own categories, so this is usually a no-op — but a partial or
        # hand-built one does not, and the wizard's budgets step reads
        # categories that nothing else would fill in.
        from ..web.api import _categorize_after_import
        _categorize_after_import(tenant_id)
    except Exception as e:                               # noqa: BLE001
        # ValueError is a bad-file message meant for the person; anything
        # else is ours, and its text is not theirs to read
        msg = (str(e) if isinstance(e, ValueError)
               else "the restore failed — the file was not applied")
        log.warning("restore job failed", exc_info=True)
        try:
            prog_conn.execute(
                "UPDATE job_progress SET state='error', progress=%s::jsonb, "
                "updated_at=now() WHERE id=%s",
                (jsonb({"error": msg}), JOB_ID))
        except Exception:                                # noqa: BLE001
            log.warning("could not record restore failure", exc_info=True)
    finally:
        # a failed unlock drops the connection rather than pooling it
        # with the lock attached (tenancy.release_lock)
        tenancy.release_lock(conn, f"oikonome:sync:{tenant_id}")
        conn.close()
        prog_conn.close()
