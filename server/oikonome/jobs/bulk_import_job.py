"""A folder import runs here, on the worker, one file at a time.

The request that submits a folder plan only freezes it: the plan goes into
`bulk_import_jobs`, the staged bytes into `bulk_import_files`, and the
request answers (web/bulk_import.submit). Running a large folder inside
the request would tie up a web worker, a database connection and the
household's sync lock for the whole import, which can outlast a reverse
proxy's timeout: the client would then report a failure for an import
that is still landing, and a retry would find the upload already used.

What the job may do is exactly what the frozen plan says, and nothing a
step-up guards: an export ZIP is restored by the submitting request, after
its own step-up, and never becomes a job file. Nothing re-reads a client's
plan once the job exists; a cancel can only remove work.

Per file, in the order of the original upload:

  * the job row is re-read — gone means the household was erased under it
    (stop quietly); a cancel request settles the rest as skipped;
  * past JOB_MAX_SECONDS, or past the row budget, the rest are reported as
    not reached rather than holding the worker;
  * the household's sync lock is taken for THIS file only (waiting its
    turn behind a sync or a restore — for as long as a restore of the
    household runs, and reading a cancel on every tick) and released
    before the next, so a sync, a restore or an erasure takes turns with
    the import instead of waiting for the whole folder; a PDF then takes
    the worker's one PDF slot, never waiting for it with the lock held
    and never holding it while it waits for the lock;
  * once the turn has come, and not before: the person who started it
    must still be signed in with the same credential and still allowed
    to import — a password change or a "sign out everywhere" stops the
    job for good — and the household must still be open to writes (no
    suspension or other lockout, not read-only), as a write request
    would be;
  * the file imports in one transaction, taking the locks in the order
    every writer keeps: the sync lock, the push writer lock, the pairing
    and identity locks, then rows (web/bulk_import.hold_ledger_write_locks);
  * its outcome, the progress count and the heartbeat are written in one
    statement, and its bytes are cleared.

The arq enqueue from the submit is only a latency nudge; the
minute-by-minute sweep is the floor, and also picks up a job whose worker
died (its heartbeat went quiet). The file a lost worker was in the middle
of is reported as interrupted, never run again: under an importer that
writes as it goes it may have partly landed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
import uuid

from ..db import tenancy
from ..engine.compat import as_dict, jsonb

log = logging.getLogger(__name__)

# Wall-clock budget of one job, from its start. A folder of PDFs at the
# extraction's own per-file ceiling still finishes well inside it; what is
# left is reported as not reached, to be dropped again.
JOB_MAX_SECONDS = 45 * 60
# arq's own default (300 s) would kill a job mid-folder; the registration
# in the worker passes this instead.
ARQ_TIMEOUT = JOB_MAX_SECONDS + 300
# A running job refreshes its heartbeat every HEARTBEAT_SECONDS from a
# ticker of its own, whatever the file in hand is doing; silence longer
# than STALE_SECONDS means the worker that held it is gone, and the sweep
# may take it over.
HEARTBEAT_SECONDS = 30
STALE_SECONDS = 5 * 60
# How long a job may sit without a heartbeat before the progress door
# says it is waiting for the worker rather than going on.
WAITING_SECONDS = 10 * 60
# A finished job stays on the Import page for an hour, as a restore does
# (sync/restore_job.SETTLED_VISIBLE_SECONDS), and its rows a week.
SETTLED_VISIBLE_SECONDS = 60 * 60
KEEP_DAYS = 7
# A file waits for the household's sync lock in steps of
# LOCK_RETRY_SECONDS, and is skipped after LOCK_GIVE_UP_SECONDS.
LOCK_RETRY_SECONDS = 5
LOCK_GIVE_UP_SECONDS = 10 * 60
# Process-wide: at most two folder imports run at once in a worker, and
# only one of them may be extracting a PDF — each extraction is a child
# process allowed several hundred megabytes. These are the worker's own;
# the web process's two PDF slots (sync/pdf_sandbox) are never taken here.
BULK_JOBS_AT_ONCE = 2
BULK_PDF_AT_ONCE = 1
_job_slots = threading.BoundedSemaphore(BULK_JOBS_AT_ONCE)
_pdf_slot = threading.BoundedSemaphore(BULK_PDF_AT_ONCE)

# a file's warnings as its status carries them: enough for any statement,
# bounded so one malformed file cannot swell every poll of the job
WARNINGS_PER_FILE = 20

LIVE = ("queued", "running", "cancelling")
SETTLED = ("cancelled", "stopped", "done", "error")

PENDING_NOTE = ("still importing in the background — open Import to see "
                "when it finishes")
CANCELLED_NOTE = "cancelled"
NOT_REACHED_NOTE = ("not reached — the import's time ran out; drop these "
                    "files again")
INTERRUPTED_NOTE = "interrupted — drop this file again"
BUSY_NOTE = "the account stayed busy — drop this file again"
EXPIRED_NOTE = ("not reached — the import waited too long for the worker; "
                "drop these files again")
STOP_NOTES = {
    "signed_out": "not imported — whoever started this import was signed "
                  "out; drop these files again",
    "role_changed": "not imported — whoever started this import may no "
                    "longer import; drop these files again",
    "household_deleted": "not imported — this household is being deleted",
    "locked_out": "not imported — this account is locked right now; drop "
                  "these files again once it is open",
    "read_only": "not imported — this account is read-only right now; "
                 "drop these files again once it can import",
    "second_factor": "not imported — enroll a second factor (authenticator "
                     "or passkey), then drop these files again",
    "unchecked": "not imported — the account's standing could not be "
                 "checked; drop these files again",
}
RESTORE_WAIT_NOTE = "waiting for the restore to finish"


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _uuid(job_id) -> str | None:
    try:
        return str(uuid.UUID(str(job_id)))
    except (TypeError, ValueError, AttributeError):
        return None


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def find_by_token(conn, token: str) -> str | None:
    """The job a staged folder was submitted as, if it was."""
    row = conn.execute(
        "SELECT id FROM bulk_import_jobs WHERE stage_token_hash = %s",
        (token_hash(token),)).fetchone()
    return str(row["id"]) if row else None


# ---- what the clients read --------------------------------------------------

def _iso(v):
    return v.isoformat() if v is not None else None


def status(conn, job_id) -> dict | None:
    """The job as the clients show it; None for an unknown id (or another
    household's — RLS makes those the same)."""
    jid = _uuid(job_id)
    if jid is None:
        return None
    job = conn.execute(
        """SELECT id, state, stop_reason, total, done, notes, created_at,
                  started_at, heartbeat_at, finished_at,
                  extract(epoch FROM now() - coalesce(heartbeat_at,
                                                      created_at)) AS quiet
             FROM bulk_import_jobs WHERE id = %s""", (jid,)).fetchone()
    if job is None:
        return None
    files = []
    for r in conn.execute(
            """SELECT file_id, filename, state, result FROM bulk_import_files
                WHERE job_id = %s ORDER BY file_id""", (jid,)).fetchall():
        row = {"index": r["file_id"], "name": r["filename"] or "",
               "state": r["state"]}
        res = as_dict(r["result"]) or {}
        for k in ("ok", "imported", "error", "account_id", "warnings",
                  "corrected", "skipped_older"):
            if k in res:
                row[k] = res[k]
        files.append(row)
    notes = [n for n in (job["notes"] or []) if isinstance(n, dict)]
    for n in notes:
        files.append(dict(n, state="done" if n.get("ok") else "failed"))
    files.sort(key=lambda f: f["index"])
    out = {"job_id": str(job["id"]), "state": job["state"],
           "stop_reason": job["stop_reason"],
           "total": (job["total"] or 0) + len(notes),
           "done": (job["done"] or 0) + len(notes),
           "started_at": _iso(job["started_at"]),
           "updated_at": _iso(max(t for t in (
               job["created_at"], job["started_at"], job["heartbeat_at"],
               job["finished_at"]) if t is not None)),
           "files": files}
    if any(n.get("restore_started") for n in notes):
        out["restore_started"] = True
    # A job nobody has picked up, or whose worker went quiet, is waiting —
    # for the queue, or for the sweep to take it over — not failing.
    if job["state"] in LIVE and (job["quiet"] or 0) > WAITING_SECONDS:
        out["waiting"] = True
        out["note"] = ("waiting for the worker" if not _restore_running(conn)
                       else RESTORE_WAIT_NOTE)
    return out


def _restore_running(conn) -> bool:
    """Whether a restore of this household is under way (its progress row
    reads running and its thread has not gone quiet). A read that fails
    says no: the file then waits as it would behind any busy lock."""
    from ..sync import restore_job
    try:
        with conn.transaction():
            return restore_job.status(conn)["state"] == "running"
    except Exception:                                    # noqa: BLE001
        log.debug("folder import could not read the restore", exc_info=True)
        return False


def current(conn) -> dict:
    """The household's live job, else the one that settled within the
    last hour, else idle — so a reload or a second device picks the job
    up again, and an old outcome does not greet every visit."""
    row = conn.execute(
        """SELECT id FROM bulk_import_jobs
            WHERE state IN ('queued', 'running', 'cancelling')
               OR finished_at > now() - make_interval(secs => %s)
            ORDER BY (state IN ('queued', 'running', 'cancelling')) DESC,
                     created_at DESC
            LIMIT 1""", (SETTLED_VISIBLE_SECONDS,)).fetchone()
    st = status(conn, row["id"]) if row else None
    return st or {"state": "idle"}


def compat_results(st: dict | None) -> list[dict]:
    """The per-file rows the run door answered with before the job existed,
    for builds that still read `results`. A file not settled yet is
    reported as pending, with a note saying where to look."""
    out = []
    for f in (st or {}).get("files") or []:
        row = {"index": f["index"], "name": f["name"]}
        if f["state"] in ("pending", "running"):
            row.update(ok=False, pending=True, error=PENDING_NOTE)
        else:
            row.update({k: v for k, v in f.items()
                        if k not in ("index", "name", "state")})
            row.setdefault("ok", False)
        out.append(row)
    return out


def cancel(conn, job_id) -> dict | None:
    """Ask a live job to stop. A queued job is settled here and now; a
    running one finishes the file in hand and skips the rest. Cancelling
    only removes work, so it asks no step-up."""
    jid = _uuid(job_id)
    if jid is None:
        return None
    if conn.execute(
            """UPDATE bulk_import_jobs SET state = 'cancelled',
                      cancel_requested_at = now(), finished_at = now()
                WHERE id = %s AND state = 'queued' RETURNING id""",
            (jid,)).fetchone():
        _settle_rest(conn, jid, CANCELLED_NOTE)
    else:
        conn.execute(
            """UPDATE bulk_import_jobs SET state = 'cancelling',
                      cancel_requested_at = coalesce(cancel_requested_at,
                                                     now())
                WHERE id = %s AND state = 'running'""", (jid,))
    return status(conn, jid)


# ---- the queue ---------------------------------------------------------------

def nudge(tenant_id: str, job_id: str) -> None:
    """Ask the worker to start the job now, without making the submit wait
    on Redis. A queue that is down is logged; the sweep runs the job
    within a minute regardless."""
    async def _enqueue():
        from arq import create_pool
        from .worker import _redis_settings
        pool = await create_pool(_redis_settings())
        try:
            await pool.enqueue_job("bulk_import_run", str(tenant_id),
                                   str(job_id))
        finally:
            await pool.close()

    def _run():
        try:
            asyncio.run(_enqueue())
        except Exception as e:                               # noqa: BLE001
            log.warning("could not queue folder import %s: %s", job_id, e)

    threading.Thread(target=_run, name=f"bulk-import-nudge-{job_id}",
                     daemon=True).start()


async def bulk_import_run(ctx, tenant_id: str, job_id: str):
    """arq entry point for the submit's nudge."""
    return await asyncio.to_thread(run_job, tenant_id, job_id)


async def bulk_import_sweep(ctx):
    """Every minute: run the queued jobs and take over the ones whose
    worker went quiet. As many lanes as there are slots, each running the
    waiting jobs one after another on its own thread; a lane that finds
    the slots taken (a nudged job holds one) stops, and the next sweep
    picks up what is left."""
    try:
        waiting = await asyncio.to_thread(_candidates, None)
        ran = 0

        async def lane():
            nonlocal ran
            while waiting:
                t, j = waiting.pop(0)
                got = await asyncio.to_thread(run_job, t, j)
                if got == "busy":
                    waiting.insert(0, (t, j))
                    return
                ran += got == "ran"

        await asyncio.gather(*(lane() for _ in range(BULK_JOBS_AT_ONCE)))
        return ran
    except Exception as e:                               # noqa: BLE001
        log.error("folder import sweep failed: %s", e)
        return f"error:{type(e).__name__}"


def _candidates(tenant_id: str | None) -> list[tuple[str, str]]:
    admin = tenancy.admin_connect()
    try:
        rows = admin.execute(
            """SELECT tenant_id, id FROM bulk_import_jobs
                WHERE (state = 'queued'
                       OR (state IN ('running', 'cancelling')
                           AND (heartbeat_at IS NULL OR heartbeat_at
                                < now() - make_interval(secs => %s))))
                  AND (%s::uuid IS NULL OR tenant_id = %s::uuid)
                ORDER BY created_at LIMIT 20""",
            (STALE_SECONDS, tenant_id, tenant_id)).fetchall()
    finally:
        admin.close()
    return [(str(r["tenant_id"]), str(r["id"])) for r in rows]


def sweep(tenant_id: str | None = None) -> int:
    """The sweep's work on the calling thread, one job after another (the
    cron runs them side by side). `tenant_id` narrows it to one
    household. Returns how many jobs ran."""
    return sum(1 for t, j in _candidates(tenant_id)
               if run_job(t, j) == "ran")


def expire(admin) -> int:
    """For the hourly staging sweep, on the owner connection: settle a
    live job that is far past any run it could still be having, clear the
    bytes of every settled job, and forget jobs a week old. Returns how
    many jobs were settled here."""
    with admin.transaction():
        gone = admin.execute(
            # 'stopped' with its reason, like every other deliberate end
            # the clients explain: 'error' reads as a crash and hides why
            """UPDATE bulk_import_jobs SET state = 'stopped',
                      stop_reason = 'expired', finished_at = now()
                WHERE state IN ('queued', 'running', 'cancelling')
                  AND coalesce(started_at, created_at)
                      < now() - make_interval(secs => %s)
                RETURNING tenant_id, id""",
            (JOB_MAX_SECONDS + 3600,)).fetchall()
        for r in gone:
            admin.execute(
                """UPDATE bulk_import_files SET state = 'skipped',
                          result = %s, data = NULL
                    WHERE tenant_id = %s AND job_id = %s
                      AND state IN ('pending', 'running')""",
                (jsonb({"ok": False, "error": EXPIRED_NOTE}),
                 r["tenant_id"], r["id"]))
        admin.execute(
            """UPDATE bulk_import_files f SET data = NULL
                 FROM bulk_import_jobs j
                WHERE f.data IS NOT NULL AND j.tenant_id = f.tenant_id
                  AND j.id = f.job_id
                  AND j.state IN ('cancelled', 'stopped', 'done', 'error')""")
        admin.execute(
            """DELETE FROM bulk_import_jobs
                WHERE state IN ('cancelled', 'stopped', 'done', 'error')
                  AND created_at < now() - make_interval(days => %s)""",
            (KEEP_DAYS,))
    return len(gone)


# ---- running a job -----------------------------------------------------------

def run_job(tenant_id: str, job_id: str) -> str:
    """Claim the job and run it to its end on this thread. "ran" when it
    ran; otherwise why not ("busy": no slot free in this process — the
    sweep tries again; "restoring": a restore of the household is under
    way, and the sweep tries again once a minute; "not claimed": queued
    nowhere, or another worker's heartbeat is still fresh)."""
    if not _job_slots.acquire(blocking=False):
        return "busy"
    try:
        conn = tenancy.tenant_connect(tenant_id)
        try:
            # A folder with an export ZIP starts its restore before its
            # own files, and the restore holds the household's sync lock
            # for the whole merge — minutes, for a real archive. Claimed
            # now, the job would sit on a worker slot and spend its time
            # budget waiting; left queued, it starts once the restore is
            # over, its clock starting then.
            if _restore_running(conn):
                return "restoring"
            job = _claim(conn, job_id)
            if job is None:
                return "not claimed"
            try:
                _run(conn, str(tenant_id), job)
            except Exception:                            # noqa: BLE001
                log.exception("folder import %s failed", job_id)
                try:
                    _finish(conn, str(job["id"]), "error")
                except Exception:                        # noqa: BLE001
                    log.warning("could not record folder import failure",
                                exc_info=True)
            return "ran"
        finally:
            conn.close()
    finally:
        _job_slots.release()


def _claim(conn, job_id) -> dict | None:
    """Take the job: a queued one, or a live one whose heartbeat went
    quiet (its worker is gone). One conditional UPDATE, so two workers
    never both run it."""
    jid = _uuid(job_id)
    if jid is None:
        return None
    return conn.execute(
        """UPDATE bulk_import_jobs
              SET state = CASE WHEN state = 'queued' THEN 'running'
                               ELSE state END,
                  started_at = coalesce(started_at, now()),
                  heartbeat_at = now()
            WHERE id = %s
              AND (state = 'queued'
                   OR (state IN ('running', 'cancelling')
                       AND (heartbeat_at IS NULL OR heartbeat_at
                            < now() - make_interval(secs => %s))))
            RETURNING id, decisions, starter_via""",
        (jid, STALE_SECONDS)).fetchone()


class _Heartbeat(threading.Thread):
    """Refreshes the job's heartbeat on a connection of its own while the
    job runs, so a long file never reads as a dead worker."""

    def __init__(self, tenant_id: str, job_id: str):
        super().__init__(name=f"bulk-import-heartbeat-{job_id}",
                         daemon=True)
        self.tenant_id, self.job_id = tenant_id, job_id
        self.stopped = threading.Event()

    def run(self):
        try:
            conn = tenancy.tenant_connect(self.tenant_id)
        except Exception:                                # noqa: BLE001
            log.warning("folder import heartbeat could not connect",
                        exc_info=True)
            return
        try:
            while not self.stopped.wait(HEARTBEAT_SECONDS):
                try:
                    conn.execute(
                        "UPDATE bulk_import_jobs SET heartbeat_at = now()"
                        " WHERE id = %s AND state IN ('running',"
                        " 'cancelling')", (self.job_id,))
                except Exception:                        # noqa: BLE001
                    log.debug("folder import heartbeat failed",
                              exc_info=True)
        finally:
            conn.close()


def _job_now(conn, job_id: str):
    return conn.execute(
        """SELECT state, cancel_requested_at, rows_total, starter_user_id,
                  starter_cred, starter_via,
                  extract(epoch FROM now() - started_at) AS age
             FROM bulk_import_jobs WHERE id = %s""", (job_id,)).fetchone()


def _run(conn, tenant_id: str, job: dict) -> None:
    from ..web import bulk_import
    job_id = str(job["id"])
    via = job["starter_via"] or "app"
    plan = {int(d["file_id"]): d
            for d in (as_dict(job["decisions"]) or {}).get("files") or []
            if isinstance(d, dict) and "file_id" in d}
    # the file a lost worker was in the middle of: reported, never re-run
    _settle_files(conn, job_id, "failed",
                  {"ok": False, "error": INTERRUPTED_NOTE},
                  states=("running",))
    ticker = _Heartbeat(tenant_id, job_id)
    ticker.start()
    work = tenancy.tenant_connect(tenant_id)
    imported = False
    ran = 0
    try:
        while True:
            now = _job_now(conn, job_id)
            if now is None:
                # erased under the job: nothing left to report to
                return
            nxt = conn.execute(
                """SELECT file_id, filename FROM bulk_import_files
                    WHERE job_id = %s AND state = 'pending'
                    ORDER BY file_id LIMIT 1""", (job_id,)).fetchone()
            if nxt is None:
                break
            if now["cancel_requested_at"] is not None:
                _settle_rest(conn, job_id, CANCELLED_NOTE)
                break
            if ran and (now["age"] or 0) >= JOB_MAX_SECONDS:
                _settle_rest(conn, job_id, NOT_REACHED_NOTE)
                break
            fid, name = nxt["file_id"], nxt["filename"] or ""
            if (now["rows_total"] or 0) >= bulk_import.MAX_TOTAL_ROWS:
                _settle_file(conn, job_id, fid, "skipped", {
                    "ok": False,
                    "error": f"batch row budget reached (max "
                             f"{bulk_import.MAX_TOTAL_ROWS:,} rows per run)"
                             f" — run this file in a new batch"})
                continue
            result = _run_file(conn, work, tenant_id, job_id, fid, name,
                               plan.get(fid) or {"file_id": fid}, via)
            if result is None:
                # the job row went (erasure) while the file waited its turn
                return
            ran += 1
            stop = result.pop("stop", None)
            rows = result.pop("rows", 0) or 0
            if result.get("ok"):
                imported = True
            state = ("done" if result.get("ok") else
                     "skipped" if result.pop("skipped", False) else "failed")
            _settle_file(conn, job_id, fid, state, result, rows=rows)
            if stop:
                _stop(conn, job_id, stop)
                return
        _finish(conn, job_id, "done")
    finally:
        ticker.stopped.set()
        work.close()
        if imported:
            from ..web.api import _categorize_after_import
            _categorize_after_import(tenant_id)


def _run_file(conn, work, tenant_id: str, job_id: str, fid: int, name: str,
              decision: dict, via: str) -> dict | None:
    """Wait for this file's turn, check that the job may still go on, import
    it, and let the turn go. None when the job vanished meanwhile.

    The turn is the household's sync lock and then, for a PDF, the
    worker's one PDF slot, taken without waiting while the lock is held:
    a slot that is busy lets the lock go and both are tried again. So a
    household's lock is never held while its file waits for another
    household's extraction, and the slot every household shares is never
    held while one household's file waits out its own sync or restore."""
    from ..sync.base import tenant_sync_lock
    pdf = name.lower().endswith(".pdf")
    deadline = time.monotonic() + LOCK_GIVE_UP_SECONDS
    while True:
        now = _job_now(conn, job_id)
        if now is None or now["state"] not in LIVE:
            # gone, or settled under it (an expiry, a cancel that landed):
            # nothing to wait for, and the slot it holds goes back
            return None
        if now["cancel_requested_at"] is not None:
            # read on every tick, so "Cancel the rest" reaches a file still
            # waiting its turn at once rather than when the wait gives up
            return {"ok": False, "skipped": True, "error": CANCELLED_NOTE}
        slot = False
        with tenant_sync_lock(work, tenant_id) as held:
            if held:
                slot = not pdf or _pdf_slot.acquire(blocking=False)
                if slot:
                    try:
                        return _run_turn(conn, work, tenant_id, job_id, fid,
                                         name, decision, via)
                    finally:
                        if pdf:
                            _pdf_slot.release()
        if held:
            # the household is free; only another household's extraction
            # is in the way, which the job's own time budget bounds
            deadline = time.monotonic() + LOCK_GIVE_UP_SECONDS
            if (now["age"] or 0) >= JOB_MAX_SECONDS:
                return {"ok": False, "skipped": True,
                        "error": NOT_REACHED_NOTE}
        elif _restore_running(conn):
            # a restore of this household is merging — often one the same
            # folder started — and it ends on its own: giving the file up
            # meanwhile would throw away its place in the plan. The job's
            # own time budget still binds, or a household that keeps
            # restoring would pin a worker slot for as long as it liked
            if (now["age"] or 0) >= JOB_MAX_SECONDS:
                return {"ok": False, "skipped": True,
                        "error": NOT_REACHED_NOTE}
            deadline = time.monotonic() + LOCK_GIVE_UP_SECONDS
        elif time.monotonic() >= deadline:
            return {"ok": False, "skipped": True, "error": BUSY_NOTE}
        _sleep(LOCK_RETRY_SECONDS)


def _run_turn(conn, work, tenant_id: str, job_id: str, fid: int, name: str,
              decision: dict, via: str) -> dict | None:
    """The file's turn has come (the caller holds the sync lock, and the
    PDF slot for a PDF): check again, now, everything that may have
    changed while it waited — a rotation or a suspension landing in the
    wait must not let the file in — then claim the file and import it."""
    now = _job_now(conn, job_id)
    if now is None:
        return None
    if now["cancel_requested_at"] is not None:
        return {"ok": False, "skipped": True, "error": CANCELLED_NOTE}
    reason = _starter_ok(conn, now) or _standing(tenant_id, now)
    if reason:
        return {"ok": False, "skipped": True,
                "error": STOP_NOTES.get(reason, reason), "stop": reason}
    row = conn.execute(
        """UPDATE bulk_import_files SET state = 'running'
            WHERE job_id = %s AND file_id = %s AND state = 'pending'
        RETURNING data""", (job_id, fid)).fetchone()
    if row is None:
        # settled meanwhile (the job expired or was erased)
        return None
    return _run_one(work, tenant_id, decision, name,
                    bytes(row["data"] or b""), via=via)


class _Refused(Exception):
    """A file the import declined; its transaction is rolled back, so a
    refused file lands nothing — not even the account it would have
    created."""

    def __init__(self, message: str, stop: str | None = None):
        super().__init__(message)
        self.stop = stop


def _run_one(work, tenant_id: str, decision: dict, name: str, data: bytes,
             *, via: str = "app") -> dict:
    """Import one file of the frozen plan, in one transaction, under the
    sync lock the caller holds. The outcome as the clients show it, plus
    `rows` (what the row budget counts) and `stop` (a reason the whole job
    must end)."""
    from fastapi import HTTPException

    from .. import erasure
    from ..sync import heartbeat
    from ..web import pages
    from ..web.bulk_import import _ensure_account, hold_ledger_write_locks
    _watch = heartbeat.AUTO_WATCH.set(via == "token")
    _via = heartbeat.PUSH_VIA.set(via)
    sign = str(decision.get("amount_sign") or "bank")
    try:
        # an oversized file is staged as empty bytes, whatever the plan
        # says about it
        if not data:
            return {"ok": False, "error": "file was too large to import"}
        try:
            with work.transaction():
                if not erasure.push_may_write(work, tenant_id):
                    raise _Refused(STOP_NOTES["household_deleted"],
                                   stop="household_deleted")
                if not name.lower().endswith((".pdf", ".ofx", ".qfx")):
                    # A PDF or OFX/QFX file takes them inside its importer,
                    # after its slow sandboxed read (a wait for a parse
                    # slot, then a child, up to a minute each) and before
                    # its first ledger row: taken here, every edit in the
                    # household would wait out each statement's read.
                    hold_ledger_write_locks(work)
                account_id = decision.get("account_id")
                if not account_id:
                    new_name = str(decision.get("new_account_name") or "")
                    if not new_name.strip():
                        raise _Refused("needs an account")
                    account_id = _ensure_account(work, new_name.strip(),
                                                 sign)
                result, mapping = pages._dispatch_import_unlocked(
                    work, account_id, sign, name, data, role="")
                if mapping is not None:
                    raise _Refused("CSV columns not recognized — import it "
                                   "alone to map them")
                if isinstance(result, dict) and result.get("error"):
                    raise _Refused(str(result["error"]))
        except _Refused as e:
            out = {"ok": False, "error": str(e)}
            if e.stop:
                out["stop"] = e.stop
            return out
        # the importer's warnings (lines whose direction was read from
        # their wording) and its count of rows an earlier import read
        # differently are what the person checks after a statement import;
        # a folder import shows them per file, as a single upload does
        warnings = [str(w) for w in (result.get("warnings") or [])
                    if w][:WARNINGS_PER_FILE]
        return {"ok": True, "imported": result.get("imported", 0),
                "account_id": account_id,
                "warnings": warnings,
                "corrected": int(result.get("corrected") or 0),
                # rows the household's history window left out, shown per
                # file as a single upload shows them
                "skipped_older": int(result.get("skipped_older") or 0),
                "rows": (result.get("rows") or 0)
                + (result.get("skipped_duplicates") or 0)}
    except HTTPException as e:
        return {"ok": False, "error": str(e.detail)}
    except ValueError as e:
        # the importers' own refusals are worded for the person and say
        # what to do instead
        return {"ok": False, "error": str(e)}
    except Exception as e:                               # noqa: BLE001
        # an unexpected failure's text can carry server internals (paths,
        # SQL): the log gets it, the person gets its kind and the advice
        logging.getLogger("oikonome.import").exception(
            "bulk import of one file failed")
        return {"ok": False,
                "error": f"import failed ({type(e).__name__}) — upload this "
                         f"file again to retry"}
    finally:
        heartbeat.AUTO_WATCH.reset(_watch)
        heartbeat.PUSH_VIA.reset(_via)


# ---- who started it ------------------------------------------------------------

def starter_credentials(conn, fingerprint: str, via: str = "app") -> frozenset:
    """Token hashes of the household's live credentials that begin with
    `fingerprint`: sessions and device tokens (sync.restore's own reading
    of them, which a password change or a sign-out-everywhere empties),
    and, for a job a script token started, that token while unrevoked."""
    from ..sync import restore
    live = restore._live_credentials(conn, fingerprint)
    if live or via != "token" or not fingerprint:
        return live
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]
    cc = tenancy.control_connect()
    try:
        return frozenset(r["h"] for r in cc.execute(
            "SELECT token_hash AS h FROM api_tokens WHERE tenant_id = %s"
            " AND revoked_at IS NULL AND scope = 'push'"
            " AND left(token_hash, %s) = %s",
            (uuid.UUID(str(tid)), len(fingerprint), fingerprint)).fetchall())
    finally:
        cc.close()


def _starter_ok(conn, job) -> str | None:
    """None while whoever started the job may still run it; otherwise the
    stop reason. Read at every file's turn, after its wait. A job
    submitted outside any request (no credential, no user) has nobody to
    watch.

    Fails closed: a credential that cannot be read counts as gone, and
    the person drops the files again."""
    from ..web import permissions
    via = job["starter_via"] or "app"
    cred = job["starter_cred"] or ""
    try:
        if cred and not starter_credentials(conn, cred, via):
            return "signed_out"
        uid = job["starter_user_id"]
        if uid and via != "token":
            cc = tenancy.control_connect()
            try:
                row = cc.execute(
                    "SELECT role FROM users WHERE id = %s",
                    (uuid.UUID(str(uid)),)).fetchone()
            finally:
                cc.close()
            if row is None:
                return "signed_out"
            if not permissions.can_write(str(row["role"] or ""),
                                         "/api/import/bulk/run"):
                return "role_changed"
    except Exception:                                    # noqa: BLE001
        log.warning("folder import could not read its starter",
                    exc_info=True)
        return "signed_out"
    return None


def _standing(tenant_id: str, job) -> str | None:
    """None while the household may still be written to; otherwise the
    stop reason. The gates are the ones every write request meets in
    current_user (web.app.write_refusal: a lockout such as a suspension,
    the read-only state an installed gate sets when billing lapses, a
    hosted account with no second factor), called rather than restated so
    the job cannot drift from the app on what a household in that state
    may do — the request that submitted the plan was checked once, and a
    job runs for up to JOB_MAX_SECONDS after it.

    Fails closed: a standing that cannot be read stops the job."""
    from ..web.app import lockout_for, write_refusal
    uid = job["starter_user_id"]
    try:
        cc = tenancy.control_connect()
        try:
            row = cc.execute(
                """SELECT coalesce(t.status, 'active') AS tenant_status,
                          u.id AS user_id,
                          (u.totp_secret IS NOT NULL) AS has_totp,
                          coalesce(u.second_factor_waived, false)
                              AS second_factor_waived
                     FROM tenants t
                     LEFT JOIN users u ON u.id = %s AND u.tenant_id = t.id
                    WHERE t.id = %s""",
                (uuid.UUID(str(uid)) if uid else None,
                 uuid.UUID(str(tenant_id)))).fetchone()
        finally:
            cc.close()
        if row is None:
            return "household_deleted"
        sess = dict(row, tenant_id=str(tenant_id))
        if sess["user_id"] is None:
            # a job started outside any request has no person to hold to
            # a second factor; the household's own standing still applies
            sess["second_factor_waived"] = True
        if lockout_for(sess["tenant_status"]) is not None:
            return ("household_deleted"
                    if sess["tenant_status"] == "pending_delete"
                    else "locked_out")
        refused = write_refusal(sess)
    except Exception:                                    # noqa: BLE001
        log.warning("folder import could not read its household's standing",
                    exc_info=True)
        return "unchecked"
    if refused is None:
        return None
    return "read_only" if refused[0] == 402 else "second_factor"


# ---- bookkeeping -----------------------------------------------------------------

def _settle_file(conn, job_id: str, fid: int, state: str, result: dict,
                 rows: int = 0) -> None:
    """One file's outcome, the progress count and the heartbeat, in one
    statement; the file's bytes go with it."""
    conn.execute(
        """WITH f AS (
               UPDATE bulk_import_files SET state = %s, result = %s,
                      data = NULL
                WHERE job_id = %s AND file_id = %s RETURNING 1)
           UPDATE bulk_import_jobs
              SET done = done + (SELECT count(*) FROM f),
                  rows_total = rows_total + %s, heartbeat_at = now()
            WHERE id = %s""",
        (state, jsonb(result), job_id, fid, rows, job_id))


def _settle_files(conn, job_id: str, state: str, result: dict,
                  states=("pending",)) -> None:
    conn.execute(
        """WITH f AS (
               UPDATE bulk_import_files SET state = %s, result = %s,
                      data = NULL
                WHERE job_id = %s AND state = ANY(%s) RETURNING 1)
           UPDATE bulk_import_jobs
              SET done = done + (SELECT count(*) FROM f),
                  heartbeat_at = now()
            WHERE id = %s""",
        (state, jsonb(result), job_id, list(states), job_id))


def _settle_rest(conn, job_id: str, note: str) -> None:
    _settle_files(conn, job_id, "skipped", {"ok": False, "error": note})


def _finish(conn, job_id: str, state: str) -> None:
    """Settle the job (a cancel that arrived during the last file still
    reads as cancelled) and clear every byte it still holds."""
    conn.execute(
        """UPDATE bulk_import_jobs
              SET state = CASE WHEN state = 'cancelling' THEN 'cancelled'
                               ELSE %s END,
                  finished_at = now(), heartbeat_at = now()
            WHERE id = %s AND state IN ('queued', 'running', 'cancelling')""",
        (state, job_id))
    conn.execute("UPDATE bulk_import_files SET data = NULL WHERE job_id = %s"
                 " AND data IS NOT NULL", (job_id,))


def _stop(conn, job_id: str, reason: str) -> None:
    """End the job for good: what it had not reached is skipped, with the
    reason, and its bytes cleared. Continuing means a new upload."""
    _settle_rest(conn, job_id, STOP_NOTES.get(reason, reason))
    conn.execute(
        """UPDATE bulk_import_jobs SET state = 'stopped', stop_reason = %s,
                  finished_at = now()
            WHERE id = %s AND state IN ('queued', 'running', 'cancelling')""",
        (reason, job_id))
    _finish(conn, job_id, "stopped")
