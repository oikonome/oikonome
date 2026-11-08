"""Restoring a whole export runs as a BACKGROUND JOB, not inside the upload.

A full household archive — tens of thousands of rows and megabytes of
receipts — takes minutes to merge. Held open inside the POST, that is
longer than a typical reverse proxy will hold a request open: the browser
gets an error page while the restore goes on to finish normally on the
server. The person migrating then reads "it failed" about work that
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


def _restores_at_once() -> int:
    from ..envnum import env_int
    return env_int("OIKONOME_RESTORES_AT_ONCE", 2, lo=1, hi=64)


# Restores run on threads inside the WEB process, each holding a tenant
# connection for its whole transaction and doing CPU-bound merging under
# the GIL. The per-tenant claim stops a household racing itself; this cap
# stops several households at once starving every other request on the
# instance of the pooled connections it serves them from. Taken without
# waiting: a restore past the cap is refused, never queued behind a lock
# a request thread would then sit on.
RESTORES_AT_ONCE = _restores_at_once()
_slots = threading.BoundedSemaphore(RESTORES_AT_ONCE)
BUSY_MSG = ("another restore is running on this server — try again in a "
            "few minutes")


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
    Blocking (a pooled connection and an INSERT): async doors call it
    through run_in_threadpool.
    """
    if not _slots.acquire(blocking=False):
        return {"error": BUSY_MSG}
    try:
        return _claim_and_spawn(tenant_id, data)
    except BaseException:
        _slots.release()
        raise


def _claim_and_spawn(tenant_id: str, data: bytes) -> dict:
    """Entered holding a slot. A return that does not hand the slot to a
    running worker gives it back (an exception is start's to release)."""
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
        _slots.release()
        return {"error": "a restore is already running on this account — "
                         "wait for it to finish"}

    # the worker thread runs outside the request, so the credential that
    # started the restore is captured here, where the request still is
    from ..web import security
    starter = security.current_credential()
    threading.Thread(target=_run_holding_slot,
                     args=(tenant_id, data, starter), name="oikonome-restore", daemon=True).start()
    return {"restore_started": True, "job": JOB_ID}


def _run_holding_slot(tenant_id: str, data: bytes, starter: str) -> None:
    try:
        _run(tenant_id, data, starter)
    finally:
        _slots.release()


def _write_progress(tenant_id: str, sql: str, params: tuple) -> None:
    """One job_progress write on a connection borrowed for that write only.
    Not the restore's own connection: it holds one transaction from first
    row to last, so a progress write sharing it would be rolled back with a
    failure or keep the transaction alive for reasons unrelated to the
    data. Nor a second connection held for the whole restore: that doubles
    what each restore takes from the pool every request is served from."""
    c = tenancy.tenant_connect(tenant_id)
    try:
        c.execute(sql, params)
    finally:
        c.close()


# a tick borrows a pooled connection, so ticks are spaced in time as well as
# in rows; the stale window (minutes) is all they must stay inside
_TICK_MIN_SECONDS = 1.0


def _run(tenant_id: str, data: bytes, starter: str = "") -> None:
    import time

    from ..web.pages import restore_result
    from . import restore

    conn = tenancy.tenant_connect(tenant_id)
    seen: set[str] = set()
    locked = False
    last_write = [0.0]

    def tick(table: str, rows: int, done: bool) -> None:
        if done:
            seen.add(table)
        now = time.monotonic()
        if now - last_write[0] < _TICK_MIN_SECONDS:
            return
        last_write[0] = now
        try:
            _write_progress(
                tenant_id,
                "UPDATE job_progress SET progress=%s::jsonb, updated_at=now() "
                "WHERE id=%s",
                (jsonb({"table": table, "rows": rows,
                        "tables_done": len(seen)}), JOB_ID))
        except Exception:                                # noqa: BLE001
            log.debug("restore progress write failed", exc_info=True)

    try:
        # The restore holds the tenant's sync lock — the same per-tenant
        # lock sync, reap and the purge paths contend on — so a purge and a
        # restore serialize: whichever takes the lock first wins. Without
        # it a purge running beside an uncommitted restore deletes only the
        # rows that already exist, and the restore's later commit resurrects
        # a tenant whose owner was told it is erased. Session-scoped:
        # explicitly unlocked in finally, exactly like sync_tenant — a
        # pooled connection handed back still holding it would lock the
        # tenant out of syncing until the backend died.
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:sync:{tenant_id}",)).fetchone()["ok"]
        if not locked:
            raise ValueError("the account is busy (a sync or delete is "
                             "running) — try the restore again in a minute")
        counts = restore.restore_zip(conn, data, progress=tick,
                                     starter=starter)
        result = restore_result(counts)
        # the merge's transaction is over, so the job's own connection
        # settles the row — no second borrow from a pool that may be busy
        conn.execute(
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
            _write_progress(
                tenant_id,
                "UPDATE job_progress SET state='error', progress=%s::jsonb, "
                "updated_at=now() WHERE id=%s",
                (jsonb({"error": msg}), JOB_ID))
        except Exception:                                # noqa: BLE001
            log.warning("could not record restore failure", exc_info=True)
    finally:
        # A failed unlock drops the connection rather than pooling it with
        # the lock attached (tenancy.release_lock), and a dropped handle can
        # run nothing more. A sync turned away during the restore left a
        # nudge; the restore pulls no bank itself, so it queues that sync
        # now. Only a restore that actually held the lock may answer it — a
        # refused one would consume the request meant for the real holder,
        # which then lets go without queueing anything.
        if locked and not tenancy.release_lock(
                conn, f"oikonome:sync:{tenant_id}"):
            from ..jobs import worker
            worker.answer_sync_nudge(conn, tenant_id)
        conn.close()
