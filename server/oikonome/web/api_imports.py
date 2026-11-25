"""The import hub, tax documents, onboarding and collector pushes.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import contextlib
from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import demoguard, permissions
from .. import ext
from ..db import tenancy
from ..jobs.sync_row import SYNC_LOCK_HELD_SQL, SYNC_STALE_AFTER
from .api_common import (_budgets_set, _conn, _CONTROL_CHARS_RE,
                         _literal_true, _owner_only, router, _scalar_text,
                         _ser, _setup_mode_of, _today, _user)
from .fields import (Fields, json_fields,
                     json_fields_or_empty as _json_fields_or_empty)
from .schemas import documented, Ok
from .schemas.imports import (AmazonPush, AmazonPushed, BackfillStatus,
                              BulkAnalysis, BulkJob, BulkJobCurrent, BulkRun,
                              BulkRunDone, CoinbaseLink, CoinbaseLinked,
                              CoinbasePushed, CollectorPush, CostcoPush,
                              CostcoPushed, EnrichLimit, EnrichRun,
                              EnrichStatus, HistoryCoverage, ImportBatches,
                              ImportOutcome, ImportResultReply, MappedImport,
                              Onboarding, PlanActivityPush,
                              PlanActivityPushed, RestoreProgress, Rollback,
                              RolledBack, StoredDocEffect, StoredDocRemoved,
                              StoredDocPayerBody, StoredDocs, StoredDocSeenBody,
                              StoredW2s,
                              TaxdocCommit,
                              TaxdocCommitted, TaxdocPlan, W2Owner, W2Seen,
                              YnabAccountsAnswer)
from .security import limit


# ---- import hub (SPA Import page) -------------------------------------------

def _categorize_after_import(tenant_id: str) -> None:
    """File imports land rows with no category, and the sync-time
    categorize pass only runs after aggregator syncs — so a
    files-only first run would reach the wizard's budgets step with everything
    uncategorized (no food/other split to suggest from). Run the same
    best-effort pass here, in a thread so the import response doesn't wait
    on the LLM; a categorize failure must never fail the import."""
    import logging
    import threading

    def _run() -> None:
        conn = None
        try:
            from ..engine import llm_categorize
            from ..sync import base as sync_base
            conn = tenancy.tenant_connect(tenant_id)
            # the same single-flight lock as the pass after a pull — which
            # is also what an account erasure waits on, so a pass in flight
            # finishes before the rows go and none starts during the wipe
            # a files-only household's first import creates its checking
            # row: with one, the forecast's anchor is pinned here, as a
            # bank's first pull pins it
            try:
                from ..engine import forecast
                forecast.auto_pin_primary_checking(conn)
            except Exception as e:                          # noqa: BLE001
                logging.getLogger("oikonome.import").warning(
                    "post-import primary checking pick failed tenant=%s: %s",
                    tenant_id, e)
            with sync_base.categorize_lock(conn, tenant_id) as held:
                if held:
                    llm_categorize.categorize_new(conn)
        except Exception as e:                              # noqa: BLE001
            logging.getLogger("oikonome.import").warning(
                "post-import categorize failed tenant=%s: %s", tenant_id, e)
        finally:
            if conn is not None:
                conn.close()

    threading.Thread(target=_run, daemon=True).start()


# Rate limits on the upload doors, bounding the PARSE and the work after it.
# They do not bound the ingest, and must not be read as if they did: FastAPI
# solves a route's dependencies only after it has called `request.form()`, so
# by the time this limiter answers, Starlette has already received, spooled and
# parsed the whole multipart. The bytes are bounded a layer out, before any of
# that runs — security.BodySizeLimitMiddleware caps the body, the per-IP ingest
# window and the in-flight cap beside it.
#
# This one is the most expensive of the set (up to 200 files and the whole
# tenant staging budget in a single request), so it gets the tightest budget —
# still far above any real folder import, where a person analyzes a batch,
# edits the plan and runs it.
@router.post("/import/bulk/analyze",
             dependencies=[Depends(limit("import_analyze", 20, 3600))])
@documented(response=BulkAnalysis)
async def import_bulk_analyze(user: dict = Depends(_user()),
                              files: list[UploadFile] = File(...)):
    """Stage a whole folder of files and return the per-file
    plan (type, destination recommendation, sign guess) for review.
    Nothing imports here."""
    # the demo flag can be a tenant checkout and a config read
    await run_in_threadpool(demoguard.deny, user)
    from .pages import MAX_UPLOAD, read_capped

    from . import bulk_import
    if len(files) > bulk_import.MAX_FILES:
        raise HTTPException(400, f"too many files (max "
                                 f"{bulk_import.MAX_FILES})")
    from ..db.staging import TENANT_BUDGET
    budget = min(TENANT_BUDGET, bulk_import.MAX_BATCH_BYTES)
    staged: list[tuple[str, bytes]] = []
    total = 0
    for f in files:
        # read_capped streams and bails past MAX_UPLOAD instead of buffering
        # a multi-GB file into RAM first
        data = await read_capped(f)
        total += len(data or b"")
        # The per-FILE cap alone would let 200 × 50 MB sit in this worker's
        # RAM — refuse the batch outright once the AGGREGATE blows the
        # batch budget (sized to fit inside the body ceiling, so this
        # message is reachable rather than pre-empted at the edge)
        if total > budget:
            raise HTTPException(413, bulk_import.BATCH_TOO_LARGE)
        staged.append((f.filename or "?", data if data is not None else b""))

    # the parse (sniff + decode + stage) is CPU- and DB-bound: it must not
    # run on the event loop, where a big batch would stall every other
    # request in the process
    def _analyze():
        conn = _conn(user)
        try:
            try:
                plan = bulk_import.analyze(conn, staged)
            except ValueError as e:
                raise HTTPException(400, str(e))
            # oversized files were blanked above — mark them skipped AND
            # flag them so the UI can keep the import checkbox disabled;
            # size 0 would mislead, so report the cap instead
            cap_mb = MAX_UPLOAD // (1024 * 1024)
            from .pages import RESTORE_MAX_UPLOAD
            restore_mb = RESTORE_MAX_UPLOAD // (1024 * 1024)
            for entry, (name, data) in zip(plan["files"], staged):
                if not data and entry.get("kind") != "unsupported":
                    entry.update(action="skip", oversized=True,
                                 note=f"file too large (over {cap_mb} MB)")
                    if name.lower().endswith(".zip"):
                        # not a dead end: the restore door takes the
                        # archive alone, streamed to disk, ten times larger
                        entry.update(
                            restore_door=bulk_import.RESTORE_DOOR,
                            note=f"an export ZIP over {cap_mb} MB is "
                                 f"restored on its own (up to "
                                 f"{restore_mb} MB) — upload it by itself")
            # one export ZIP and nothing else is a restore, whatever its
            # size: say which door takes it
            # — unless the plan already refused it as not an export, which
            # the clients would otherwise hand to the restore (and its
            # step-up) anyway
            if (len(staged) == 1
                    and staged[0][0].lower().endswith(".zip")
                    and not any(e.get("kind") == "unsupported"
                                for e in plan.get("files", []))):
                plan["restore_door"] = bulk_import.RESTORE_DOOR
            return plan
        finally:
            conn.close()

    return await run_in_threadpool(_analyze)


# The run door SUBMITS a folder plan: it freezes the plan as a background
# job (jobs/bulk_import_job) and answers at once, so its cost is the
# submit, and the analyze limit already bounds the jobs that do work.
# This bounds the posts that are refused before spending a stage (no
# step-up yet, another folder still importing) and the retries that only
# read a job back.
@router.post("/import/bulk/run",
             dependencies=[Depends(limit("import_bulk_run", 60, 3600))])
@documented(response=BulkRunDone, body=BulkRun)
def import_bulk_run(user: dict = Depends(_user()),
                    body: Fields = Depends(json_fields)):
    """Submit an analyzed folder's plan. A client that sends
    `background: true` gets 202 and the job's status at once, and polls
    the job doors below; an older build, which reads `results`, gets 200
    after a short wait with one row per file, those still importing marked
    pending. Posting the same token again answers the same job, whatever
    plan it carries."""
    demoguard.deny(user)
    from . import bulk_import
    # the plan is handed whole to the submit, which checks each decision
    # itself; a scalar there is a malformed body (400), never a TypeError
    # escaping as a 500, and a plan past the folder limit is refused before
    # any of it is read, in the submit's own words
    files = body.opaque_rows("files", bulk_import.MAX_FILES,
                             blank_ok=True) or []
    token = body.text("token", strip=False)
    background = _literal_true(body, "background")

    def step_up():
        # a plan that restores an export ZIP owes the step-up the single
        # upload door asks for (the archive repoints mail and AI
        # endpoints), asked by this request before the stage is spent, so
        # the client's retry after elevating finds its batch — and the
        # shared demonstration login, whose step-up proves nothing, may
        # not land an archive's endpoints at all
        demoguard.deny_shared_grant(user)
        from .app import _require_elevation
        # read as the step-up has always read it: the text of any scalar
        _require_elevation(user, **{
            k: _scalar_text(body, k)
            for k in ("password", "totp_code", "recovery_code")})

    conn = _conn(user)
    try:
        try:
            out = bulk_import.submit(conn, token, list(files), user,
                                     step_up=step_up)
        except bulk_import.Busy as e:
            raise HTTPException(409, str(e))
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        # given back before any wait below: the tenant pool is shared by
        # every household on the instance
        conn.close()
    existing = out.pop("existing", False)
    if background:
        from fastapi.responses import JSONResponse
        return JSONResponse(out, status_code=202)
    from ..jobs import bulk_import_job
    # Only the post that created the job waits for it; a repost of the
    # token reads it back as it stands, so reposting cannot buy a second
    # wait.
    st = out if existing else (bulk_import.wait_for(
        user["tenant_id"], out["job_id"],
        bulk_import.COMPAT_WAIT_SECONDS) or out)
    return {"job_id": st.get("job_id"), "state": st.get("state"),
            "results": bulk_import_job.compat_results(st)}


@router.get("/import/bulk/jobs/current")
@documented(response=BulkJobCurrent)
def import_bulk_job_current(user: dict = Depends(_user())):
    """The household's folder import in progress, or the one that finished
    in the last hour, or `{state: "idle"}` — read by the Import page on
    load so a reload or a second device finds the job again."""
    from ..jobs import bulk_import_job
    conn = _conn(user)
    try:
        return bulk_import_job.current(conn)
    finally:
        conn.close()


@router.get("/import/bulk/jobs/{job_id}")
@documented(response=BulkJob)
def import_bulk_job(job_id: str, user: dict = Depends(_user())):
    """One folder import's progress: N of M files and each file's outcome
    as it settles."""
    from ..jobs import bulk_import_job
    conn = _conn(user)
    try:
        st = bulk_import_job.status(conn, job_id)
    finally:
        conn.close()
    if st is None:
        raise HTTPException(404, "no such import")
    return st


# Cancelling only removes work (the file in hand finishes, the rest are
# skipped), so it asks no step-up; it is bounded like the run door.
@router.post("/import/bulk/jobs/{job_id}/cancel",
             dependencies=[Depends(limit("import_bulk_cancel", 60, 3600))])
@documented(response=BulkJob)
def import_bulk_job_cancel(job_id: str, user: dict = Depends(_user())):
    demoguard.deny(user)
    from ..jobs import bulk_import_job
    conn = _conn(user)
    try:
        st = bulk_import_job.cancel(conn, job_id)
    finally:
        conn.close()
    if st is None:
        raise HTTPException(404, "no such import")
    return st


@router.get("/restore/progress")
@documented(response=RestoreProgress)
def restore_progress(user: dict = Depends(_user())):
    """Where the background restore has got to. Polled by the Import page
    from the moment the upload returns until the job settles."""
    from ..sync import restore_job
    conn = _conn(user)
    try:
        return restore_job.status(conn)
    finally:
        conn.close()


# The hub takes one 50 MB file per call. Collector tokens push through it
# hourly and already answer to the central script-token budget, so this
# matches that number rather than undercutting it.
@router.post("/import",
             dependencies=[Depends(limit("import_upload", 60, 3600))])
@documented(response=ImportOutcome)
async def import_api(request: Request,
                     user: dict = Depends(_user()),
                     account_id: str = Form(""),
                     amount_sign: str = Form("bank"),
                     new_account_name: str = Form(""),
                     file: UploadFile = File(...),
                     password: str = Form(""),
                     totp_code: str = Form(""),
                     recovery_code: str = Form(""),
                     ynab_choice: str = Form("")):
    # the demo flag can be a tenant checkout and a config read
    await run_in_threadpool(demoguard.deny, user)
    from ..sync import heartbeat
    from .pages import (OVERSIZE_MSG, _dispatch_import_unlocked,
                        read_capped)
    # a client that can ask where each account of a multi-account YNAB
    # register goes says so; one that does not is refused such a file by
    # name rather than handed a card it cannot draw. Passed only when set,
    # so the dispatcher's other callers keep their exact call.
    choice = {"ynab_choice": True} if ynab_choice.strip() in (
        "1", "true", "yes") else {}
    filename = file.filename or ""
    # the same fork pages.import_submit makes: a whole-ledger archive is
    # streamed to disk under the restore cap, everything else is one
    # statement file read under the ordinary one
    if filename.lower().endswith(".zip") and not user.get("script_token"):
        # the role check BEFORE the read: a member may import but not
        # restore, and must not get to spool 500 MB onto a door that then
        # refuses them
        if not permissions.may_restore(user["role"]):
            raise HTTPException(403, permissions.RESTORE_DENIED)
        # a restore merges the archive's mail relay and AI endpoints over
        # the household's own — the destinations a Settings save may only
        # repoint after a step-up — so it owes the same proof, asked here
        # because the job it starts cannot refuse once it is running. The
        # shared demonstration login's step-up proves nothing, so it is
        # refused outright, as the Settings save refuses it a new endpoint.
        await run_in_threadpool(demoguard.deny_shared_grant, user)
        from .pages import RESTORE_OVERSIZE_MSG, read_restore_capped
        data = await read_restore_capped(file)
        if data is None:
            return {"result": {"error": RESTORE_OVERSIZE_MSG}, "mapping_needed": None}
        # a ZIP that is not an Oikonome export (a budget app's export) is
        # told so BEFORE the proof is asked: the step-up can spend a
        # one-time recovery code, and a file the restore refuses anyway
        # must not cost one
        from ..sync import restore as _restore
        refusal = await run_in_threadpool(_restore.foreign_archive_refusal,
                                          data)
        if refusal:
            return {"result": {"error": refusal}, "mapping_needed": None}
        from .app import _require_elevation
        # off the event loop: the legacy password proof hashes (argon2)
        # and waits on the hasher gate, and this handler is async
        await run_in_threadpool(
            lambda: _require_elevation(user, password=password,
                                       totp_code=totp_code,
                                       recovery_code=recovery_code,
                                       as_of_arrival=request))
    else:
        data = await read_capped(file)
        if data is None:
            return {"result": {"error": OVERSIZE_MSG}, "mapping_needed": None}

    # A ZIP here is a whole-ledger restore, and that is the one import that
    # routinely outlives the edge timeout in front of a hosted instance —
    # so it starts a background job and the client polls /restore/progress
    # instead of holding the POST open (see sync/restore_job). Script-token
    # pushes are NOT sent down this path: dispatch_import owes them a 403,
    # and starting a job for a principal that may not restore would answer
    # the wrong question first.
    if filename.lower().endswith(".zip") and not user.get("script_token"):
        # authorization is unchanged by going async: viewers are already
        # refused every write by the gate in app.py, and this endpoint
        # never asked for more than that. A MEMBER, though, passes that
        # gate — importing is theirs — so the restore half is refused
        # here, where the filename is what separates the two.
        if not permissions.may_restore(user["role"]):
            raise HTTPException(403, permissions.RESTORE_DENIED)
        from ..sync import restore, restore_job
        # a ZIP that is not an Oikonome export (a budget app's export) is
        # told so now, not by a job that starts and merges nothing
        # off the event loop: a central directory of millions of entries
        # takes seconds to parse
        refusal = await run_in_threadpool(restore.foreign_archive_refusal,
                                          data)
        if refusal:
            return {"result": {"error": refusal}, "mapping_needed": None}
        started = await run_in_threadpool(
            restore_job.start, str(user["tenant_id"]), data)
        if started.get("erased"):
            # what every other file import answers a household being
            # deleted (pages.import_write)
            from .pages import IMPORT_ERASED_MSG
            raise HTTPException(409, IMPORT_ERASED_MSG)
        return {"result": started, "mapping_needed": None}

    # parse + upsert are CPU- and DB-bound (a 50 MB OFX/ZIP takes seconds
    # to minutes): off the event loop, or every other request in the
    # process waits behind this one. The heartbeat contextvars are set
    # inside the worker so they scope to exactly this import.
    def _run():
        _watch = heartbeat.AUTO_WATCH.set(bool(user.get("script_token")))
        _via = heartbeat.PUSH_VIA.set(
            "token" if user.get("script_token") else "app")
        conn = _conn(user)
        try:
            # the collector scripts push statements through this door too,
            # so it joins the push writer lock like the other push doors:
            # an erasure that began after the request authenticated must
            # not have the file's accounts and rows written back under it
            #
            # The sync lock is taken OUTSIDE the push transaction: it is a
            # session lock, and releasing it inside the transaction would
            # free it (and queue a nudged sync) before the rows commit, so
            # that sync would filter its pull against a ledger without the
            # file and insert rows the file already covers.
            # pages.import_write is that frame, shared by every file-import
            # door; a household being deleted answers 409 there.
            from .pages import (IMPORT_SYNC_BUSY_MSG, import_write,
                                takes_ledger_locks_first)
            with import_write(conn, user["tenant_id"],
                              ledger_locks=takes_ledger_locks_first(
                                  filename)) as held:
                if not held:
                    result, mapping_needed = (
                        {"error": IMPORT_SYNC_BUSY_MSG}, None)
                else:
                    dest = account_id
                    refusal = None
                    if dest:
                        # a script pushing by the id an account had
                        # before a migrate moved it off a full number
                        # follows the move's record when it leads to
                        # one account; anything else is answered as
                        # the id always was
                        from .bulk_import import moved_account
                        found = moved_account(conn, dest)
                        if len(found) == 1:
                            dest = found[0]
                        elif not found:
                            raise HTTPException(
                                404, "that account no longer exists — "
                                     "pick another")
                        else:
                            raise HTTPException(
                                409, "that account moved to several — "
                                     "pick one of them")
                    # the single-file page can create the destination
                    # account inline, like bulk already could — the same
                    # helper mints an opaque id once, so two different
                    # "Chase" accounts never merge
                    def run(aid):
                        return _dispatch_import_unlocked(
                            conn, aid, amount_sign, filename, data,
                            role=str(user.get("role") or ""), **choice)
                    ran = None
                    if not dest and new_account_name.strip():
                        from .bulk_import import (AccountChoiceNeeded,
                                                  import_named)
                        # the name becomes the account's id and label:
                        # _ensure_account cuts it to the folder import's
                        # bound; a control character is refused as the
                        # account-add and rename doors refuse it
                        if _CONTROL_CHARS_RE.search(new_account_name):
                            raise HTTPException(
                                400, "name holds control characters")
                        typed_name = new_account_name
                        # the sign convention also types the account
                        # (card / investment), as the bulk path does
                        # (or the earlier account that already holds
                        # this file's rows: bulk_import.import_named)
                        try:
                            dest, ran = import_named(
                                conn, typed_name.strip(), amount_sign,
                                run)
                        except AccountChoiceNeeded as e:
                            # two cards share the last four the name
                            # leads to: the person picks one, as the
                            # folder import asks per file
                            # each described, as the two can
                            # read the same and the pick is kept
                            # — but only to the person: a push token
                            # may import, never read, and the cards'
                            # balances and dates are a read
                            from .bulk_import import _describe
                            refusal = {"error": str(e)}
                            if not user.get("script_token"):
                                refusal["choices"] = _describe(
                                    conn, e.choices)
                    if refusal:
                        result, mapping_needed = refusal, None
                    else:
                        result, mapping_needed = ran or run(dest)
                        if account_id and mapping_needed:
                            # parked to be mapped or placed: the pick
                            # is remembered when the finish lands it
                            from .bulk_import import _stem
                            from .pages import remember_when_landed
                            remember_when_landed(
                                conn, mapping_needed,
                                {new_account_name, _stem(filename)})
                    if account_id and not mapping_needed and not (
                            isinstance(result, dict)
                            and result.get("error")):
                        # an account picked for a name two cards share
                        # is remembered once its file has landed, as
                        # the folder import does, so the next file of
                        # that card is not asked again: under the typed
                        # name and the file's own. A file still to be
                        # mapped, or refused, settles nothing.
                        from .bulk_import import _stem, remember_choice
                        for typed in {new_account_name.strip(),
                                      _stem(filename)} - {""}:
                            remember_choice(conn, typed, dest)
            if result and not mapping_needed and not (
                    isinstance(result, dict) and result.get("error")):
                _categorize_after_import(str(user["tenant_id"]))
            return {"result": result or None,
                    "mapping_needed": mapping_needed}
        finally:
            heartbeat.AUTO_WATCH.reset(_watch)
            heartbeat.PUSH_VIA.reset(_via)
            conn.close()

    return await run_in_threadpool(_run)


@router.post("/import/mapped")
@documented(response=ImportResultReply, body=MappedImport)
def import_mapped_api(user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    # The token and the column names reach SQL and the CSV engine. Anything
    # but text is a malformed request; refused here, before the staged
    # upload is touched, so a bad body never consumes it. A column name is
    # kept exactly as sent (a header may carry a space), and only the
    # mapping keys are handed on: the engine reads nothing else.
    token = body.ident("token", msg="token must be a string")
    cols = {k: body.text(k, strip=False)
            for k in ("date_col", "name_col", "amount_col", "debit_col",
                      "credit_col", "merchant_col", "category_col")}
    from ..sync import heartbeat
    from .pages import run_mapped_import
    _watch = heartbeat.AUTO_WATCH.set(bool(user.get("script_token")))
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    conn = _conn(user)
    try:
        result = run_mapped_import(conn, token, cols)
        if not (isinstance(result, dict) and result.get("error")):
            _categorize_after_import(str(user["tenant_id"]))
        return {"result": result}
    finally:
        heartbeat.AUTO_WATCH.reset(_watch)
        heartbeat.PUSH_VIA.reset(_via)
        conn.close()


@router.post("/import/ynab-accounts")
@documented(response=ImportResultReply, body=YnabAccountsAnswer)
def import_ynab_accounts_api(user: dict = Depends(_user()),
                             body: Fields = Depends(json_fields)):
    """Finish a multi-account YNAB register /api/import parked: the person's
    destination for each YNAB account (an account id, "skip", or
    {"new": name}), and for each, whether rows an earlier import of the
    whole register left on the picked account are to be removed.

    The same gates as the upload door it finishes: the demo refusal, the
    role gate (app.py, by path), the sync lock outside the push writer
    transaction, the erasure 409, the categorize pass after."""
    demoguard.deny(user)
    _accounts_msg = ("accounts must map each YNAB account to "
                     "an account id, \"skip\", {\"new\": name} "
                     "or {\"id\": id, \"typed\": name}")
    _remove_msg = "remove_earlier must map YNAB accounts to true or false"
    # malformed is refused before the parked upload is touched; the token
    # is kept as sent (it is a lookup key), and the two maps are checked
    # value by value below
    token = body.text("token", strip=False, msg="token must be a string")
    if not token:
        raise HTTPException(400, "token must be a string")
    accounts = body.opaque("accounts", msg=_accounts_msg)
    remove_earlier = body.opaque("remove_earlier", msg=_remove_msg)
    if not isinstance(accounts, dict) or not all(
            isinstance(k, str) and (isinstance(v, str) or (
                isinstance(v, dict) and (
                    isinstance(v.get("new"), str)
                    or (isinstance(v.get("id"), str)
                        and isinstance(v.get("typed", ""), str)))))
            for k, v in accounts.items()):
        # {"id", "typed"}: an account picked from those a typed new-account
        # name could mean, with the name, so the pick is kept under it
        raise HTTPException(400, _accounts_msg)
    if remove_earlier is None:
        remove_earlier = {}
    if not all(isinstance(v, bool) for v in remove_earlier.values()):
        raise HTTPException(400, _remove_msg)
    if user.get("script_token"):
        raise HTTPException(403, "script tokens cannot import a YNAB "
                                 "register of several accounts")
    from ..sync import heartbeat
    from .pages import run_ynab_import
    _watch = heartbeat.AUTO_WATCH.set(False)
    _via = heartbeat.PUSH_VIA.set("app")
    conn = _conn(user)
    try:
        # the sync lock, the erasure check and the ledger locks in one
        # transaction, as every file-import door takes them
        # (pages.import_write)
        result = run_ynab_import(conn, token, accounts, remove_earlier)
        if not result.get("error"):
            _categorize_after_import(str(user["tenant_id"]))
        return {"result": result}
    finally:
        heartbeat.AUTO_WATCH.reset(_watch)
        heartbeat.PUSH_VIA.reset(_via)
        conn.close()


# A tax document goes through OCR and, for a 1040, a model — the most
# CPU-expensive parse in the product for a single upload.
@router.post("/import/taxdoc/analyze",
             dependencies=[Depends(limit("taxdoc_analyze", 20, 3600))])
@documented(response=TaxdocPlan)
async def taxdoc_analyze_api(user: dict = Depends(_user()),
                             file: UploadFile = File(...)):
    """Detect + parse a tax/income document, return the per-year
    preview rows behind a commit token."""
    # the demo flag can be a tenant checkout and a config read
    await run_in_threadpool(demoguard.deny, user)
    from ..engine import taxdocs
    from .pages import read_capped
    data = await read_capped(file)
    if data is None:
        raise HTTPException(413, "file too large (max 50 MB)")
    filename, ctype = file.filename or "", file.content_type or ""

    # PDF/OCR/LLM parse is slow and blocking — keep it off the event loop,
    # and hold no pooled connection while the model reads: the household's
    # settings are read first and the connection returned, the parse runs
    # on its own, and a second connection stages what it found
    def _analyze():
        import httpx

        from ..engine.llm_categorize import LLMError
        conn = _conn(user)
        try:
            ctx = taxdocs.read_context(conn)
        finally:
            conn.close()
        conn = None
        try:
            try:
                kind, rows = taxdocs.parse(ctx, filename, data, ctype)
                conn = _conn(user)
                plan = taxdocs.stage(conn, kind, rows, filename)
                # who the preview's whose-picker may offer
                plan["w2_people"] = taxdocs.w2_people(conn)
                return plan
            except ValueError as e:
                raise HTTPException(400, str(e))
            # The W-2 image and 1040 paths ask the AI backend, and a backend
            # that is down, names a model it does not have, or sends back
            # something unusable is a fault in Settings → AI, not in the
            # document. Say so, naming the kind of failure (never the reply),
            # instead of the bare 500 an uncaught error becomes.
            except httpx.TransportError as e:
                raise HTTPException(
                    503, "the AI backend in Settings → AI could not be "
                         f"reached ({type(e).__name__}) — check that the "
                         "endpoint is up")
            except (httpx.HTTPStatusError, LLMError, KeyError,
                    IndexError) as e:
                cause = (f"HTTP {e.response.status_code}"
                         if isinstance(e, httpx.HTTPStatusError)
                         else type(e).__name__)
                raise HTTPException(
                    502, "the AI backend in Settings → AI could not read "
                         f"this document ({cause}) — check the vision model "
                         "and that the endpoint is up")
        finally:
            if conn is not None:
                conn.close()

    return await run_in_threadpool(_analyze)


@router.post("/import/taxdoc/commit")
@documented(response=TaxdocCommitted, body=TaxdocCommit)
def taxdoc_commit_api(user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    from ..engine import taxdocs
    # the reviewed rows go whole to the commit, which checks each one; the
    # token is a lookup key, kept as sent
    rows = body.opaque_rows("rows", taxdocs.MAX_ROWS, blank_ok=True) or []
    token = body.text("token", strip=False)
    conn = _conn(user)
    try:
        try:
            out = taxdocs.commit(conn, token, list(rows),
                                 stored_by=user.get("user_id"))
        except taxdocs.HouseholdErasing as e:
            raise HTTPException(409, str(e))
        except taxdocs.W2Unanswered as e:
            # the lookalikes ride the refusal, per committed row, so the
            # preview can ask about them where they first appear
            return JSONResponse(status_code=409, content={
                "detail": str(e), "matches": e.matches})
        except ValueError as e:
            raise HTTPException(400, str(e))
        return out
    finally:
        conn.close()


@router.get("/import/taxdoc/w2s")
@documented(response=StoredW2s)
def taxdoc_w2s_api(user: dict = Depends(_user())):
    """The stored W-2s: year, employer, whose, box 1."""
    from ..engine import taxdocs
    conn = _conn(user)
    try:
        return {"w2s": taxdocs.list_w2s(conn)}
    finally:
        conn.close()


@router.delete("/import/taxdoc/w2s/{doc_id}")
@documented(response=Ok, body=W2Seen)
def taxdoc_w2_delete_api(doc_id: int, user: dict = Depends(_user()),
                         body: Fields = Depends(_json_fields_or_empty)):
    """Remove one stored W-2 — the one the list showed (`seen`, in the
    body: a person's wages never ride a URL into an access log); its
    year's W-2 wages are summed again."""
    demoguard.deny(user)
    from ..engine import taxdocs
    seen = body.opaque("seen", blank_ok=True)
    conn = _conn(user)
    try:
        # a number past BIGINT is no document's id, not a database error
        try:
            gone = 0 < doc_id < 2 ** 63 and taxdocs.delete_w2(
                conn, doc_id, seen)
        except taxdocs.HouseholdErasing as e:
            raise HTTPException(409, str(e))
        except taxdocs.W2Changed as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not gone:
            raise HTTPException(404, "no such W-2")
        return {"ok": True}
    finally:
        conn.close()


@router.post("/import/taxdoc/w2s/{doc_id}/owner")
@documented(response=Ok, body=W2Owner)
def taxdoc_w2_owner_api(doc_id: int, user: dict = Depends(_user()),
                        body: Fields = Depends(json_fields)):
    """Say whose a stored W-2 is (primary or spouse) — the one the list
    showed (`seen`: its year and box 1)."""
    demoguard.deny(user)
    from ..engine import taxdocs
    owner = body.text("owner")
    seen = body.opaque("seen", blank_ok=True)
    conn = _conn(user)
    try:
        try:
            if (not 0 < doc_id < 2 ** 63
                    or not taxdocs.set_w2_owner(conn, doc_id, owner, seen)):
                raise HTTPException(404, "no such W-2")
        except taxdocs.HouseholdErasing as e:
            raise HTTPException(409, str(e))
        except taxdocs.W2Changed as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}
    finally:
        conn.close()


@router.get("/import/taxdoc/stored")
@documented(response=StoredDocs)
def taxdoc_stored_api(user: dict = Depends(_user())):
    """Every stored tax and income document, newest first, in the
    server's words (specs/tax-documents.md, "Stored documents"); one
    removed in the last 30 days is listed with its put-back."""
    from ..engine import taxdocs
    conn = _conn(user)
    try:
        rows = taxdocs.list_stored(conn)
        return {"documents": rows, "groups": taxdocs.stored_groups(rows),
                "w2_people": taxdocs.w2_people(conn)}
    finally:
        conn.close()


def _upload_id_ok(upload_id: int) -> bool:
    # a number past BIGINT is no document's id, not a database error
    return 0 < upload_id < 2 ** 63


@router.get("/import/taxdoc/stored/{upload_id}/effect")
@documented(response=StoredDocEffect)
def taxdoc_stored_effect_api(upload_id: int, user: dict = Depends(_user())):
    """What removing one stored document does to the figures, in words —
    the confirm's text — and whether removing it asks for a step-up."""
    from ..engine import taxdocs
    if not _upload_id_ok(upload_id):
        raise HTTPException(404, "no such document")
    conn = _conn(user)
    try:
        try:
            line = taxdocs.removal_effect(conn, upload_id)
        except taxdocs.UploadChanged:
            raise HTTPException(404, "no such document")
        return {"effect_line": line,
                "step_up": taxdocs.needs_step_up(conn, upload_id)}
    finally:
        conn.close()


@router.delete("/import/taxdoc/stored/{upload_id}")
@documented(response=StoredDocRemoved, body=StoredDocSeenBody)
def taxdoc_stored_delete_api(upload_id: int, user: dict = Depends(_user()),
                             body: Fields = Depends(_json_fields_or_empty)):
    """Remove one stored document — the one the list showed (`seen`) —
    and work out every year it wrote again. It can be put back for 30
    days. An earnings record, or a document that wrote more than one
    year, asks for the step-up window first."""
    demoguard.deny(user)
    from ..engine import taxdocs
    if not _upload_id_ok(upload_id):
        raise HTTPException(404, "no such document")
    seen = body.opaque("seen", blank_ok=True)
    conn = _conn(user)
    try:
        from .app import _require_elevation
        if taxdocs.needs_step_up(conn, upload_id):
            _require_elevation(user)
        try:
            # asked again under the removal's locks, over what it removes
            years = taxdocs.remove_upload(
                conn, upload_id, seen, by=user.get("user_id"),
                step_up=lambda: _require_elevation(user))
        except taxdocs.HouseholdErasing as e:
            raise HTTPException(409, str(e))
        except taxdocs.UploadChanged as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if years is None:
            raise HTTPException(404, "no such document")
        _log_taxdoc(conn, user, "tax_document_removed", seen, years)
        return {"ok": True, "years": years}
    finally:
        conn.close()


@router.post("/import/taxdoc/stored/{upload_id}/restore")
@documented(response=StoredDocRemoved, body=StoredDocSeenBody)
def taxdoc_stored_restore_api(upload_id: int, user: dict = Depends(_user()),
                              body: Fields = Depends(json_fields)):
    """Put back a stored document removed in the last 30 days — the one
    the list showed (`seen`)."""
    demoguard.deny(user)
    from ..engine import taxdocs
    if not _upload_id_ok(upload_id):
        raise HTTPException(404, "no such document")
    seen = body.opaque("seen", blank_ok=True)
    conn = _conn(user)
    try:
        try:
            years = taxdocs.restore_upload(conn, upload_id, seen)
        except taxdocs.HouseholdErasing as e:
            raise HTTPException(409, str(e))
        except taxdocs.UploadChanged as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if years is None:
            raise HTTPException(404, "nothing to put back")
        _log_taxdoc(conn, user, "tax_document_restored", seen, years)
        return {"ok": True, "years": years}
    finally:
        conn.close()


@router.post("/import/taxdoc/stored/{upload_id}/payer")
@documented(response=StoredDocRemoved, body=StoredDocPayerBody)
def taxdoc_stored_payer_api(upload_id: int, user: dict = Depends(_user()),
                            body: Fields = Depends(json_fields)):
    """Rename the payer on a stored document — the one the list showed
    (`seen`), on its forms under the `payer` it listed. No step-up: a
    rename loses nothing."""
    demoguard.deny(user)
    from ..engine import taxdocs
    if not _upload_id_ok(upload_id):
        raise HTTPException(404, "no such document")
    seen = body.opaque("seen", blank_ok=True)
    payer = body.text("payer", max_len=1000, strip=False)
    name = body.ident("name", max_len=taxdocs.PAYER_MAX)
    conn = _conn(user)
    try:
        try:
            years = taxdocs.rename_payer(conn, upload_id, seen, payer, name)
        except taxdocs.HouseholdErasing as e:
            raise HTTPException(409, str(e))
        except taxdocs.UploadChanged as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if years is None:
            raise HTTPException(404, "no such document")
        _log_taxdoc(conn, user, "tax_document_payer_renamed", seen, years)
        return {"ok": True, "years": years}
    finally:
        conn.close()


def _log_taxdoc(conn, user, action: str, seen, years) -> None:
    """The activity log's row for a removal, a put-back or a payer rename:
    the kind and years, never the file name."""
    from ..engine import activity as _activity, taxdocs
    kind = seen.get("kind") if isinstance(seen, dict) else None
    label = (f"{taxdocs._KIND_LABEL.get(kind, 'tax document')} "
             f"{taxdocs._years_label(years)}").strip()
    _activity.record(conn, user, "settings", action, label=label)


@router.get("/import/batches")
@documented(response=ImportBatches)
def import_batches_api(user: dict = Depends(_user())):
    from ..sync import batches
    conn = _conn(user)
    try:
        return {"batches": [_ser(b) for b in batches.recent(conn)],
                "older_imports_line": batches.older_imports_line(conn)}
    finally:
        conn.close()


def kept_rows_refusal(user: dict, e) -> JSONResponse:
    """The 409 an undo answers when it would keep rows held only by a note
    naming no feed account. Only the owner may delete those rows
    (force_kept), so only the owner is asked "undo anyway?" — error
    `kept_rows_unconfirmed`. Anyone else gets error `kept_rows`: the same
    count, and a note that says what happened and who can finish it, with
    no question attached, because the retry it would invite is refused."""
    if user.get("role") == "owner" and not user.get("script_token"):
        return JSONResponse(status_code=409, content={
            "detail": str(e), "error": "kept_rows_unconfirmed",
            "kept_transactions": e.kept})
    rows = f"{e.kept} imported row{'s' if e.kept != 1 else ''}"
    return JSONResponse(status_code=409, content={
        "detail": (f"Nothing was undone. {rows} in this import may stand "
                   f"in for charges an older bank feed set aside, so "
                   f"{'they are' if e.kept != 1 else 'it is'} kept for now. "
                   f"Only the household's owner can undo this import."),
        "error": "kept_rows", "kept_transactions": e.kept})


@router.post("/import/rollback")
@documented(response=RolledBack, body=Rollback)
def import_rollback_api(user: dict = Depends(_user()),
                        body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    from ..sync import batches
    from ..sync.base import SyncRunning
    # an id that is not a short string can name no batch: it undoes
    # nothing, as it always has, rather than being refused
    try:
        batch_id = body.ident("batch_id")
    except HTTPException:
        batch_id = ""
    # Rows held only by a note naming no feed account may be the only copy
    # of charges an older bank feed set aside; nothing can check whether a
    # feed still relies on them, so the undo asks first (409, with the
    # count) and deletes them only on the owner's literal `force_kept:
    # true` — the account-remove door's contract, for the same rows.
    force_kept = _literal_true(body, "force_kept")
    if force_kept and (user.get("role") != "owner" or user.get("script_token")):
        # the option, not the door, is the owner's: a member may still undo
        raise HTTPException(403, "owner only")
    conn = _conn(user)
    try:
        # An archived business is read-only and its rows are retention
        # records: every door that moves or deletes them refuses, and only
        # the owner's delete-forever may destroy them. Undoing an import
        # hard-deletes the rows it alone owns — with their Schedule C
        # classes — so the rollback refuses while any of those rows
        # belongs to an archived business, looking under its own lock so
        # an archive landing after this request began cannot slip past.
        n = batches.rollback(conn, batch_id, force_kept=force_kept,
                             ask_first=not force_kept)
    except batches.KeptRowsUnconfirmed as e:
        # nothing was changed: the owner is told the count and may answer
        # with force_kept; anyone else is told plainly, with no offer they
        # could not take up
        return kept_rows_refusal(user, e)
    except (SyncRunning, batches.ArchivedBusinessRows) as e:
        # nothing was deleted: the client retries once the sync is done, or
        # once the business is restored
        raise HTTPException(409, str(e))
    finally:
        conn.close()
    return {"ok": True, "deleted": int(n),
            "warning": getattr(n, "warning", None)}


@router.get("/onboarding")
@documented(response=Onboarding)
def onboarding_api(user: dict = Depends(_user())):
    """Fresh-instance progress for the SPA's guided checklist."""
    from ..engine import budget
    from ..sync.base import history_months_of
    conn = _conn(user)
    try:
        counts = conn.execute(
            """SELECT (SELECT COUNT(*) FROM accounts) AS accounts,
                      (SELECT COUNT(*) FROM transactions WHERE removed=0)
                          AS transactions,
                      (SELECT COUNT(*) FROM bills WHERE active=1)
                          AS bills,
                      (SELECT COUNT(*) FROM items
                       WHERE aggregator IN ('plaid','simplefin','mx')
                         AND COALESCE(status,'') NOT IN ('archived','restored'))
                          AS connections""").fetchone()
        # the offers the Bills page lists (bills.pending_proposals): one it
        # leaves out waits unanswered, and the wizard's Bills step, which
        # finishes when none is left, must not wait on what no page shows
        from ..engine import bills
        pending = len(bills.pending_proposals(conn))
        cfg = budget.load_config(conn)
        budgets_set = _budgets_set(conn)
        # who saved it: the stored first-run seed is a plan too, to every
        # reader of the row, but not one the household made
        saved_by_user = budget.budgets_saved_by_user(conn)
        # the accounts the household is asked to choose between: one is
        # pinned by itself, two or more are a question the wizard and the
        # Today checklist ask (closed imports without a balance are not
        # offered next to a live account)
        from ..engine import forecast
        candidates = forecast.pickable_checking_ids(conn, cfg)
        # the pin as the forecast honours it (forecast.pin_is_live): one on
        # a disconnected bank's account reads as unset, so the checklist
        # asks for a new one
        pin_live = forecast.pin_is_live(conn, cfg)
    finally:
        conn.close()
    # Setup is complete when the household has what every step sets up
    # (accounts, transactions, bills, budgets, a live checking pin when
    # there is a choice), whether or not the wizard's final step was
    # pressed. Same steps as the Today checklist.
    setup_complete = bool(cfg.get("wizard_done")) or (
        counts["accounts"] > 0 and counts["transactions"] > 0
        and counts["bills"] > 0 and bool(budgets_set)
        and (pin_live or not candidates))
    return {"accounts": counts["accounts"],
            "transactions": counts["transactions"],
            "setup_complete": setup_complete,
            "bills": counts["bills"],
            # top-level connections (the simplefin bridge counts as
            # one; its per-bank children don't inflate this) + the pending
            # recurring-proposal queue for the wizard's Recurring step
            "connections": counts["connections"],
            "pending_proposals": pending,
            "budgets_set": budgets_set,
            "budgets_saved_by_user": saved_by_user,
            "primary_checking_set": pin_live,
            "primary_checking_id": (cfg.get("checking_account_id") or None)
            if pin_live else None,
            "checking_candidates": candidates,
            # the guided wizard is offered until every step is
            # done or the user dismisses it for good
            "wizard_done": bool(cfg.get("wizard_done")),
            # explicit per-step done/skipped marks (strict flow)
            "wizard_steps": cfg.get("wizard_steps") or {},
            # the setup choices, read-only here, for the wizard's summary
            # (written through the settings save)
            "history_months": history_months_of(cfg),
            "setup_mode": _setup_mode_of(cfg),
            "bill_detection_auto": cfg.get("bill_detection_auto") is not False}


# The historical window Plaid backfills after a link, in days — the
# denominator of the wizard's import bar for an Item that predates the
# per-Item record of the window it asked for (those all asked for two years).
BACKFILL_WINDOW_DAYS = 730


def _backfill_status(conn) -> tuple[dict, list]:
    """Where each Plaid connection stands in its one-time historical
    backfill — the read half shared by the status GET and the nudge POST.
    Returns (payload, waiting_rows): the client-facing shape plus the raw
    rows of items still backfilling (unfinished, unbroken).

    An item is ready when /transactions/sync has reported
    HISTORICAL_UPDATE_COMPLETE; items that predate the tx_update_status
    column count as ready once they are a day old (their backfill finished
    long ago)."""
    rows = conn.execute(
        """SELECT i.id, i.institution_name, i.status, i.tx_update_status,
                  CASE WHEN jsonb_typeof(i.raw->'days_requested') = 'number'
                       THEN (i.raw->>'days_requested')::numeric END
                      AS days_requested,
                  (i.tx_update_status IS NULL
                   AND i.linked_at < now() - interval '24 hours')
                      AS legacy_done,
                  (SELECT COUNT(*) FROM transactions t
                   JOIN accounts a ON a.id = t.account_id
                   WHERE a.item_id = i.id AND COALESCE(t.removed,0)=0)
                      AS transactions,
                  (SELECT MIN(t.date) FROM transactions t
                   JOIN accounts a ON a.id = t.account_id
                   WHERE a.item_id = i.id AND COALESCE(t.removed,0)=0)
                      AS earliest,
                  (SELECT MAX(ran_at) FROM sync_log s
                   WHERE s.item_id = i.id
                     AND s.ran_at > now() - interval '45 seconds')
                      AS recent_attempt,
                  EXISTS (SELECT 1 FROM sync_log s WHERE s.item_id = i.id)
                      AS ever_pulled
           FROM items i
           WHERE i.aggregator = 'plaid'
             AND COALESCE(i.status,'') != 'archived'
           ORDER BY i.linked_at""").fetchall()
    running = conn.execute(
        f"""SELECT 1 FROM job_progress WHERE id='sync'
            AND state='running'
            AND (updated_at > now() - %s::interval OR {SYNC_LOCK_HELD_SQL})""",
        (SYNC_STALE_AFTER,)).fetchone() is not None
    items = []
    today = _today(conn)
    first_pending = False
    for r in rows:
        # Each Item fills against the window ITS link asked Plaid for, not
        # the household's current choice: the window cannot be changed on
        # an Item once it is made, so an Item linked for two years keeps
        # backfilling two years after the household picks three months,
        # and measured against three months it would read full long before
        # its history is in.
        window_days = (float(r["days_requested"])
                       if r["days_requested"] and r["days_requested"] > 0
                       else BACKFILL_WINDOW_DAYS)
        stalled = (r["status"] or "").startswith("error")
        ready = (r["tx_update_status"] == "HISTORICAL_UPDATE_COMPLETE"
                 or bool(r["legacy_done"]))
        # no pull has finished yet: the Item's first sync is queued or
        # running on the worker (the link saved it and queued the pull),
        # which writes nothing a reader can see until it ends — so the
        # household counts as syncing, and the nudge does not start another
        waits_first = (not r["ever_pulled"] and not r["transactions"]
                       and not ready and not stalled)
        first_pending = first_pending or waits_first
        # A bar the client can draw. Plaid never says how many rows are
        # coming, only that the two-year window is complete, so the fill
        # is how much of that window the oldest landed row already
        # covers — capped short of full until the bank says complete,
        # because a bar parked at 100% while still importing reads as
        # hung. A window that reaches further back than the two years
        # (a bank that keeps more) is simply full.
        if ready:
            progress = 1.0
        elif r["earliest"]:
            days = (today - r["earliest"]).days
            progress = max(0.02, min(0.95, days / window_days))
        else:
            progress = 0.0
        items.append({
            "id": r["id"],
            "institution": r["institution_name"],
            "transactions": int(r["transactions"] or 0),
            # how far back the history reaches so far — the one
            # progress signal a backfill can honestly show (Plaid
            # never says how many rows are coming, only that the
            # window is complete)
            "earliest": r["earliest"].isoformat() if r["earliest"] else None,
            "progress": round(progress, 3),
            # a pull touched this connection within the last 45 seconds:
            # the client shows "pulling from your bank" against the
            # quieter "waiting for the bank to send more" — the
            # difference between working and stuck, from the outside
            "active": (r["recent_attempt"] is not None or waits_first)
                      and not ready,
            "ready": ready,
            # a broken connection will not finish its backfill —
            # surfaced so the UI can say "needs attention" instead of
            # waiting forever, and excluded from the all_ready gate
            "stalled": stalled,
        })
    waiting = [r for r, it in zip(rows, items)
               if not it["ready"] and not it["stalled"]]
    all_ready = all(it["ready"] or it["stalled"] for it in items)
    return ({"items": items, "all_ready": all_ready,
             "syncing": running or first_pending},
            waiting)


@router.get("/onboarding/backfill")
@documented(response=BackfillStatus)
def onboarding_backfill(user: dict = Depends(_user())):
    """Where each Plaid connection stands in its one-time historical
    backfill. READ ONLY — two clients poll this every few seconds and a
    family viewer (or a cross-site top-level GET, which still carries a
    SameSite=Lax cookie) may reach it, so it must never start a sync. The
    pull lives behind POST /onboarding/backfill/nudge.

    Plaid delivers the two-year window ASYNCHRONOUSLY: the first sync
    lands ~30 days and the rest follows over minutes. The wizard's
    bills-&-income step waits on this — detection over a sliver of
    history proposes almost nothing."""
    conn = _conn(user)
    try:
        payload, _waiting = _backfill_status(conn)
    finally:
        conn.close()
    return payload


@router.post("/onboarding/backfill/nudge",
             dependencies=[Depends(limit("backfill_nudge", 30, 600))])
@documented(response=BackfillStatus)
def onboarding_backfill_nudge(user: dict = Depends(_user())):
    """The self-driving half of the backfill wait: when an unfinished item
    has not been pulled recently and no sync is running, queue a pull of it
    on the worker. Hosted installs hear HISTORICAL_UPDATE by webhook, but a
    self-host without a public webhook URL would otherwise sit until the
    hourly cron. Owner-only and demo-denied like every other door that
    starts a sync (it pulls new transactions, moves cursors and spends
    aggregator quota). Returns the same status shape as the GET.

    Never pulls in the web process: a first two-year pull can hold a
    pooled web connection and the sync lock for minutes, which is why the
    link poll hands that work to the worker. An Item whose first pull has
    not finished gets that first-sync job queued again under its one job
    id (a no-op while it is queued or running; a re-queue if Redis lost
    it); any other waiting Item gets one pull per minute at most."""
    from ..jobs import worker

    demoguard.deny(user)
    if user["role"] != "owner":
        raise HTTPException(403, "owner only")
    tid = str(user["tenant_id"])
    conn = _conn(user)
    try:
        payload, waiting = _backfill_status(conn)
    finally:
        conn.close()
    first = [r["id"] for r in waiting
             if not r["ever_pulled"] and not r["transactions"]]
    for iid in first:
        worker.queue_first_sync(tid, iid)
    # 45 s, not 90: the wizard re-nudges every 30 s while it waits, and a
    # 90 s window would decline the first two nudges after a pull and leave
    # the history minutes away; the bank's window is usually ready within
    # a minute of the link
    later = [r for r in waiting
             if r["ever_pulled"] or r["transactions"]]
    if later and not first and not payload["syncing"] \
            and all(r["recent_attempt"] is None for r in later):
        for r in later:
            worker.queue_backfill_sync(tid, r["id"])
        payload["syncing"] = True
    return payload


@router.get("/history/coverage")
@documented(response=HistoryCoverage)
def history_coverage(user: dict = Depends(_user())):
    """Per-account transaction-history bounds — the wizard's gap-aware
    Import step ("Chase checking ← Apr 12, 2026 — 90 days"). Shadow
    accounts (linked non-primaries) are excluded like every other
    aggregate."""
    from ..engine import links
    conn = _conn(user)
    try:
        shadows = set(links.shadow_ids(conn))
        rows = conn.execute(
            """SELECT a.id, COALESCE(a.display_name, a.name) AS name,
                      a.type, MIN(t.date) AS earliest, MAX(t.date) AS latest,
                      COUNT(t.id) AS transactions
               FROM accounts a
               LEFT JOIN transactions t
                 ON t.account_id = a.id AND t.removed = 0
               GROUP BY a.id, COALESCE(a.display_name, a.name), a.type
               ORDER BY 2""").fetchall()
    finally:
        conn.close()
    return {"accounts": [
        {"id": r["id"], "name": r["name"], "type": r["type"],
         "earliest": r["earliest"].isoformat() if r["earliest"] else None,
         "latest": r["latest"].isoformat() if r["latest"] else None,
         "transactions": r["transactions"]}
        for r in rows if r["id"] not in shadows]}


@router.post("/accounts/coinbase/link")
@documented(response=CoinbaseLinked, body=CoinbaseLink)
def coinbase_link_api(user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    from ..sync import coinbase
    # the label becomes an item and account id, so it is read as one; the
    # key name and the PEM are kept exactly as pasted (a PEM's line breaks
    # are part of it)
    sent = {"label": body.ident("label"),
            "key_name": body.text("key_name", strip=False),
            "private_key": body.text("private_key", strip=False)}
    for k, v in sent.items():
        if not v:
            raise HTTPException(400, f"{k} required")
    # the label becomes the account id, which may not hold a comma (the
    # link would succeed and every sync after it fail)
    if "," in sent["label"]:
        raise HTTPException(400, "label may not contain a comma")
    conn = _conn(user)
    try:
        item_id = coinbase.link(conn, sent["label"], sent["key_name"],
                                sent["private_key"])
        return {"ok": True, "item_id": item_id}
    finally:
        conn.close()


def _pin_kind(body: Fields):
    """Collectors declare how their collection half works
    (api | scrape) — pin it for the heartbeat stamps this request makes.
    A `kind` of any other value or type pins nothing and is never refused:
    a push is never turned away over the label on its heartbeat."""
    kind = (body.text("kind", strip=False)
            if body.json_type("kind") == "string" else "")
    return _pin_kind_text(kind)


def _push_rows(body: Fields, key: str, max_n: int) -> list:
    """A collector's rows, handed whole to its importer, which skips any
    row it cannot read. Anything but a list reaches the importer as no rows,
    so it is refused in the importer's own words ("payload needs a non-empty
    … list"), and a list past the importer's limit is refused here as the
    importer refuses it — the answers a collector has always had. Read
    inside the push transaction, after the erasure check, where the
    importer read them."""
    if body.json_type(key) != "list":
        return []
    return body.opaque_rows(
        key, max_n, msg=f"bad payload: too many {key} (max {max_n})")


def _pin_kind_text(kind: str):
    """`_pin_kind` for a door that read `kind` through `Fields` (before it
    set anything it must reset). Any string other than the two kinds pins
    nothing, as it always has."""
    from ..sync import heartbeat
    return heartbeat.PUSH_KIND.set(
        kind if kind in ("api", "scrape") else None)


def _enrich_awaits_confirm(conn) -> bool:
    """Whether Enrich waits on the owner confirming their address. It bills
    per row on the instance's Plaid account exactly as a bank link does, so
    it waits on the same condition the add-a-bank door does. Asked through
    ext.gate.enrich_awaits_confirm, the fail-closed form of
    bank_awaits_confirm: the display's form swallows an error to False, and
    a spending door must not inherit that. A household's own instance
    never waits."""
    try:
        return bool(ext.gate.enrich_awaits_confirm(conn))
    except Exception:                                    # noqa: BLE001
        # a gate that cannot answer refuses: this door spends money
        return True


@router.get("/import/enrich")
@documented(response=EnrichStatus)
def enrich_status_api(user: dict = Depends(_user())):
    """How much imported history Plaid Enrich could still describe, and
    this month's cap/usage — the Settings card's numbers."""
    # the run is the owner's (it is billed); so are its numbers — both
    # clients already show the card to the owner only
    _owner_only(user)
    from ..sync import plaid as _plaid
    conn = _conn(user)
    try:
        try:
            _plaid.credentials(conn)
            configured = True
        except _plaid.PlaidError:
            configured = False
        # the connector owns the definition of a candidate — a count computed
        # separately here would drift from what a click would actually send
        out = {"candidates": _plaid.enrich_candidate_count(conn),
               "plaid_configured": configured,
               **_plaid.enrich_usage(conn),
               "awaits_confirm": _enrich_awaits_confirm(conn)}
        if out["awaits_confirm"]:
            # the run refuses until the address is confirmed, so the card
            # must not count rows it could send
            out["remaining"] = 0
        return out
    finally:
        conn.close()


@router.post("/import/enrich",
             dependencies=[Depends(limit("import-enrich", 20, 3600.0))])
@documented(response=EnrichRun, body=EnrichLimit)
def enrich_run_api(user: dict = Depends(_user()),
                   body: Fields = Depends(_json_fields_or_empty)):
    """Send a batch of imported rows to Plaid Enrich (owner, on request —
    billed per row, so never automatic; capped per month). Refused to the
    shared demonstration login: its password is handed out, and every run
    bills the Plaid account behind the instance."""
    demoguard.deny_shared_credentials(user)
    _owner_only(user)
    from ..sync import plaid as _plaid
    # digits typed into a field and a fractional count are read as they
    # always were; 0 and absent both mean the default batch
    limit_n = body.int_in("limit", text_ok=True, truncate=True,
                          msg="limit must be an integer") \
        or _plaid.ENRICH_BATCH
    limit_n = max(1, min(limit_n, 500))
    conn = _conn(user)
    try:
        # Enrich is billed per row on the operator's Plaid account, so the
        # plan is consulted BEFORE the instance configuration — a tenant
        # whose plan does not carry it should be told that, not invited to
        # set Plaid up first. Self-host is ungated (effective_tier None):
        # the operator brings their own Plaid keys and pays their own bill.
        if not ext.gate.tier_allows(conn, "enrich"):
            raise HTTPException(
                402, "Enriching imported history isn't enabled for this account.")
        if _enrich_awaits_confirm(conn):
            raise HTTPException(
                403, "Confirm your email address first — describing "
                     "imported history waits on it, as connecting a bank "
                     "does.")
        try:
            _plaid.credentials(conn)
        except _plaid.PlaidError:
            raise HTTPException(400, "Plaid is not configured on this instance")
        return _plaid.enrich_rows(conn, limit=limit_n)
    finally:
        conn.close()


@contextlib.contextmanager
def _push_write(conn, user: dict):
    """The write transaction of a collector push door. The push was
    authenticated before it got here, and an account erasure flips the
    household's status and then wipes its rows: a push landing after that
    would write the rows back under a tenant id nothing ever sweeps again.
    Holding the push writer lock for the transaction makes an erasure that
    starts now wait for this push (and then delete it); one already under
    way answers 409 and nothing is written."""
    from .. import erasure
    with conn.transaction():
        if not erasure.push_may_write(conn, user["tenant_id"]):
            raise HTTPException(
                409, "this household is being deleted; nothing was imported")
        yield


@router.post("/import/coinbase")
@documented(response=CoinbasePushed, body=CollectorPush)
def coinbase_import_api(user: dict = Depends(_user()),
                        body: Fields = Depends(json_fields)):
    """The Coinbase collector's push door (script-token
    friendly) — normalized items/accounts/transactions payload; rows the
    source stopped reporting are retired inside the pushed prefix."""
    demoguard.deny(user)
    from ..sync import coinbase_push, heartbeat
    _watch = heartbeat.AUTO_WATCH.set(bool(user.get("script_token")))
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    _kind = _pin_kind(body)
    conn = _conn(user)
    try:
        try:
            with _push_write(conn, user):
                # the normalized payload goes whole to the importer, which
                # checks every key it reads and ignores the rest
                return coinbase_push.import_payload(conn, body.as_dict())
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, f"bad payload: {e}")
    finally:
        heartbeat.AUTO_WATCH.reset(_watch)
        heartbeat.PUSH_VIA.reset(_via)
        heartbeat.PUSH_KIND.reset(_kind)
        conn.close()


@router.post("/import/amazon")
@documented(response=AmazonPushed, body=AmazonPush)
def amazon_import_api(user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    """The Amazon-orders push door (script-token friendly) —
    orders upsert + drift refresh + the app's own order↔txn matcher."""
    demoguard.deny(user)
    from ..sync import amazon_orders, heartbeat
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    _kind = _pin_kind(body)
    conn = _conn(user)
    try:
        try:
            with _push_write(conn, user):
                return amazon_orders.import_orders(conn, _push_rows(
                    body, "orders", amazon_orders.MAX_ORDERS))
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, f"bad payload: {e}")
    finally:
        heartbeat.PUSH_VIA.reset(_via)
        heartbeat.PUSH_KIND.reset(_kind)
        conn.close()


@router.post("/import/costco")
@documented(response=CostcoPushed, body=CostcoPush)
def costco_import_api(user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    """The Costco-receipts push door (script-token friendly) —
    receipts upsert/refresh + the app's own receipt↔txn matcher."""
    demoguard.deny(user)
    from ..sync import costco_receipts, heartbeat
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    _kind = _pin_kind(body)
    conn = _conn(user)
    try:
        try:
            with _push_write(conn, user):
                return costco_receipts.import_receipts(conn, _push_rows(
                    body, "receipts", costco_receipts.MAX_RECEIPTS))
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, f"bad payload: {e}")
    finally:
        heartbeat.PUSH_VIA.reset(_via)
        heartbeat.PUSH_KIND.reset(_kind)
        conn.close()


@router.post("/import/plan-activity")
@documented(response=PlanActivityPushed, body=PlanActivityPush)
def plan_activity_import_api(user: dict = Depends(_user()),
                             body: Fields = Depends(json_fields)):
    """A workplace plan's activity CSV, pushed by a host-side collector:
    `{plan, csv, balance?, mask?, provider?, institution?}`. `provider` is
    the collector's slug for the plan administrator (its item and id
    namespace), `institution` the name shown for it."""
    demoguard.deny(user)
    from ..sync import heartbeat, plan_csv
    plan = body.text("plan").upper()
    if plan not in plan_csv.PLAN_CODES:
        raise HTTPException(400, f"plan must be one of {sorted(plan_csv.PLAN_CODES)}")
    # The collector posts whatever it scraped. A non-string csv would reach
    # the parser's string methods, and a non-numeric balance would reach a
    # float column — both would surface as an opaque 500, which tells a scripted
    # caller nothing about which field it got wrong.
    csv_text = body.text("csv", strip=False, msg="csv must be a string")
    if not csv_text:
        raise HTTPException(400, "csv required")
    try:
        provider = plan_csv.provider_slug(body.text("provider"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    institution = body.text("institution")[:80]
    # a number or the text of one; absent, null and "" are no balance. The
    # reader refuses what is not finite, and anything past the ceiling two
    # of which would still add up to infinity in every net-worth total
    try:
        balance = body.money("balance")
    except HTTPException:
        # The money reader parses a number through its text, and a float
        # that large is written with an exponent it does not read
        # ("1.7e+308"). A finite float is a real number, only too large to
        # carry; an integer too wide for a double is not a number here.
        raw = body.as_dict().get("balance")
        if isinstance(raw, float) or (
                isinstance(raw, str) and raw.strip().lower().lstrip("+-")
                in ("nan", "inf", "infinity")):
            import math
            if not (isinstance(raw, float) and math.isfinite(raw)):
                raise HTTPException(400, "balance must be a finite number")
        whole = body.int_in("balance", truncate=True,
                            msg="balance must be a number")
        try:
            float(whole)
        except (TypeError, OverflowError):
            raise HTTPException(400, "balance must be a number")
        raise HTTPException(400, "balance is too large")
    # the plan number's trailing digits, as an aggregator would report
    # them (Plaid's `mask`) — optional, and refused rather than stored
    # when it is not a short digit string, since it lands on the account
    # row and drives the account-link suggestions
    # the digits sent as a JSON number, by a script of the household's own,
    # are read as their text, as they always were
    mask = body.text_or_int("mask", msg="mask must be 2-8 digits")
    if mask is not None and not (2 <= len(mask) <= 8 and mask.isdigit()):
        raise HTTPException(400, "mask must be 2-8 digits")
    kind = body.text("kind", strip=False)
    _watch = heartbeat.AUTO_WATCH.set(bool(user.get("script_token")))
    _via = heartbeat.PUSH_VIA.set(
        "token" if user.get("script_token") else "app")
    _kind = _pin_kind_text(kind)
    conn = _conn(user)
    try:
        try:
            with _push_write(conn, user):
                r = plan_csv.import_csv(conn, plan, csv_text,
                                        balance=balance, mask=mask,
                                        provider=provider,
                                        institution=institution)
        except (KeyError, TypeError, ValueError) as e:
            # a CSV nothing parses out of, or one past the row ceiling, is a
            # statement about the POSTed document — same 400 the coinbase
            # door gives for the same class of thing
            raise HTTPException(400, str(e))
        return {"ok": True, **r}
    finally:
        heartbeat.AUTO_WATCH.reset(_watch)
        heartbeat.PUSH_VIA.reset(_via)
        heartbeat.PUSH_KIND.reset(_kind)
        conn.close()
