"""Receipts.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import datetime as dt
import uuid
from fastapi import (Depends, File, Form, HTTPException, Query, Request,
                     UploadFile)
from starlette.concurrency import run_in_threadpool

from . import data, demoguard
from ..db import tenancy
from ..engine import activity as _activity, storage_quota
from .api_common import (_ck_ym, _conn, _csv_safe, _csv_writer,
                         _filename_safe, _owner_only, _same_site_download,
                         _ser, _today, _user, router)
from .fields import Fields, json_fields
from .schemas import documented, Ok
from .schemas.receipts import (ReceiptAttached, ReceiptDetails,
                               ReceiptDetailsSaved, ReceiptItems,
                               ReceiptMatch, ReceiptMatched, ReceiptOther,
                               ReceiptParsing, ReceiptReport,
                               ReceiptSlipAnswered, ReceiptSnapped,
                               ReceiptTag, TxnReceipts, WaitingReceipts)
from .security import limit


# ---- receipts ------------------------------------------------------

def _receipt_id_or_404(rid: str) -> str:
    """A receipt id that is not even a uuid names no receipt: 404, before
    any lock or query. Checked up front so the doors below need no blanket
    except — a lock wait that timed out or a database fault then surfaces as
    the server error it is, instead of telling the person the receipt they
    are looking at does not exist."""
    try:
        return str(uuid.UUID(str(rid)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(404, "no such receipt")


def _parse_receipt_async(tenant_id: str, receipt_id: str) -> None:
    """The vision parse takes 30s+ on CPU — never inside the
    upload/parse request. The receipt is stamped 'parsing' before the
    thread starts; the UI polls status for its progress bar."""
    import logging
    import threading

    def _run() -> None:
        from ..engine import receipts
        conn = None
        # by household: a freed turn goes to whoever holds the fewest, so one
        # household's batch cannot hold every read from the others
        turn = receipts.wait_for_parse_turn(tenant_id)
        try:
            conn = tenancy.tenant_connect(tenant_id)
            if turn:
                # claimed=True: the route claim()ed before spawning us
                receipts.parse_one(conn, receipt_id, claimed=True)
            else:
                receipts.fail_busy(conn, receipt_id)
        except Exception as e:                    # noqa: BLE001
            # class only at WARNING; the text can carry receipt contents
            # or a backend URL, so it stays out of the feedback log ring
            rlog = logging.getLogger("oikonome.receipts")
            rlog.warning("background receipt parse failed tenant=%s: %s",
                         tenant_id, type(e).__name__)
            rlog.debug("background receipt parse failure detail: %s", e,
                       extra={"no_capture": True})
        finally:
            if conn is not None:
                conn.close()
            if turn:
                receipts.end_parse_turn(tenant_id)

    threading.Thread(target=_run, daemon=True).start()


@router.post("/transactions/{txn_id:path}/receipt",
             dependencies=[Depends(limit("receipt", 60, 3600))])
@documented(response=ReceiptAttached)
async def receipt_upload(txn_id: str, user: dict = Depends(_user()),
                         file: UploadFile = File(...),
                         kind: str = Form("receipt")):
    """Attach a receipt (kind='receipt') or a check image (kind='check');
    kicks the vision parse in the background when an LLM is configured
    (failure leaves 'failed' with a reason — the nightly sweep and the
    retry button exist). A parsed check also feeds its payee to the
    categorizer."""
    # the demo flag can be a tenant checkout and a config read
    await run_in_threadpool(demoguard.deny, user)
    from ..engine import receipts
    from .pages import read_capped
    # Stream with a hard ceiling — an unbounded read() would let an
    # authenticated user OOM the (shared, on hosted) host with a multi-GB
    # upload. read_capped stops buffering the moment it blows the cap.
    # The cap is the RECEIPT limit — buffering 50 MB only for
    # receipts.add to refuse at 5 MB would be pointless exposure.
    data = await read_capped(file, cap=receipts.MAX_IMAGE)
    if data is None:
        raise HTTPException(413, "receipt too large (5 MB max)")
    mime = file.content_type or "application/octet-stream"
    # Everything below touches the database (the pooled checkout, the
    # image-optimize pass in add, the log, the claim, the read-back): one
    # threadpool call, so neither a starved pool nor a large upload stalls
    # the event loop the whole worker serves.
    return await run_in_threadpool(
        _receipt_attach, user, txn_id, data, mime, kind)


def _receipt_attach(user: dict, txn_id: str, data: bytes, mime: str,
                    kind: str) -> dict:
    from ..engine import llm_categorize, receipts
    conn = _conn(user)
    try:
        try:
            rid = receipts.add(conn, txn_id, data, mime, kind)
        except storage_quota.StorageQuotaExceeded as e:
            raise HTTPException(413, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        # add attaches to the row the charge stands as NOW: a pending hold
        # that settled since the client read it hands its receipt to the
        # posted row. The log, the answer and the id the clients re-key on
        # all name that row, or the upload reads back empty from the
        # retired hold and invites a second, duplicate upload.
        landed = conn.execute("SELECT txn_id FROM receipts WHERE id = %s",
                              (rid,)).fetchone()
        if landed and landed["txn_id"]:
            txn_id = landed["txn_id"]
        _activity.record(conn, user, "receipt", "attached", target=txn_id,
                         label=_activity.txn_label(conn, txn_id),
                         detail={"receipt_kind": kind, "receipt_id": rid})
        if llm_categorize.configured(conn, role="vision") and receipts.claim(conn, rid):
            # claim before responding so the first poll already shows
            # 'parsing' — and so a racing retry can't double-parse
            _parse_receipt_async(str(user["tenant_id"]), rid)
        return {"id": rid, "txn_id": txn_id,
                "receipts": receipts.for_txn(conn, txn_id)}
    finally:
        conn.close()


@router.post("/receipts",
             dependencies=[Depends(limit("receipt", 60, 3600))])
@documented(response=ReceiptSnapped)
async def receipt_snap(user: dict = Depends(_user()),
                       file: UploadFile = File(...)):
    """Snap a receipt before its transaction reaches the ledger. Stored as
    WAITING (no transaction), parsed in the background like any receipt,
    and paired with its charge by engine/receipt_match — when the parse
    finishes if the charge is already here, else when a sync brings it.
    Same limits, same rate bucket and the same demo refusal as attaching a
    receipt to a transaction."""
    # the demo flag can be a tenant checkout and a config read
    await run_in_threadpool(demoguard.deny, user)
    from ..engine import receipts
    from .pages import read_capped
    data = await read_capped(file, cap=receipts.MAX_IMAGE)
    if data is None:
        raise HTTPException(413, "receipt too large (5 MB max)")
    mime = file.content_type or "application/octet-stream"
    # every database step in one threadpool call, as the attach door does
    return await run_in_threadpool(_receipt_snap_store, user, data, mime)


def _receipt_snap_store(user: dict, data: bytes, mime: str) -> dict:
    from ..engine import llm_categorize, receipt_match, receipts
    conn = _conn(user)
    try:
        try:
            rid = receipts.add(conn, None, data, mime, "receipt")
        except storage_quota.StorageQuotaExceeded as e:
            raise HTTPException(413, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        _activity.record(conn, user, "receipt", "snapped",
                         label="a waiting receipt",
                         detail={"receipt_kind": "receipt",
                                 "receipt_id": rid})
        if llm_categorize.configured(conn, role="vision") and receipts.claim(conn, rid):
            _parse_receipt_async(str(user["tenant_id"]), rid)
        return {"id": rid,
                **_ser(receipt_match.waiting(conn, today=_today(conn)))}
    finally:
        conn.close()


@router.get("/receipts/waiting")
@documented(response=WaitingReceipts)
def receipt_waiting(user: dict = Depends(_user()),
                    limit: int | None = Query(None, ge=1),
                    offset: int = Query(0, ge=0, le=1_000_000)):
    """Receipts waiting for their transaction, each with what was read off
    it and the charges it could be; plus what the matcher paired recently.
    One page at a time: `limit` (capped at the matcher's page size, which
    is also the default) and `offset` reach the rest, and `count` is every
    receipt waiting, not just this page."""
    from ..engine import receipt_match
    conn = _conn(user)
    try:
        return _ser(receipt_match.waiting(
            conn, today=_today(conn),
            limit=min(limit or receipt_match.PAGE_LIMIT,
                      receipt_match.PAGE_LIMIT),
            offset=offset))
    finally:
        conn.close()


@router.post("/receipts/{rid}/match")
@documented(response=ReceiptMatched, body=ReceiptMatch)
def receipt_match_by_hand(rid: str, user: dict = Depends(_user()),
                          body: Fields = Depends(json_fields)):
    """Pair a waiting receipt with a transaction a person chose."""
    from ..engine import receipt_match
    rid = _receipt_id_or_404(rid)
    txn_id = body.text("txn_id")
    if not txn_id:
        raise HTTPException(400, "txn_id required")
    conn = _conn(user)
    try:
        # A charge offered while pending may have posted since: the pair
        # goes on the posted row the hold became, resolved under the
        # pairing lock the match takes (exclusive up front, as it does),
        # so no settlement can retire the row between the two.
        from ..engine import entities
        with conn.transaction():
            receipt_match._lock(conn)
            txn_id = entities.live_txn_id(conn, txn_id) or txn_id
            ok = receipt_match.match_by_hand(conn, rid, txn_id)
        if not ok:
            raise HTTPException(
                404, "no such waiting receipt, or that transaction is gone")
        _activity.record(conn, user, "receipt", "matched", target=txn_id,
                         label=_activity.txn_label(conn, txn_id),
                         detail={"receipt_kind": "receipt",
                                 "receipt_id": rid})
        return {"txn_id": txn_id,
                **_ser(receipt_match.waiting(conn, today=_today(conn)))}
    finally:
        conn.close()


@router.post("/receipts/{rid}/unmatch")
@documented(response=ReceiptMatched)
def receipt_unmatch(rid: str, user: dict = Depends(_user())):
    """Take a receipt off its transaction and put it back to waiting. The
    matcher never pairs it with that transaction again."""
    from ..engine import receipt_match
    rid = _receipt_id_or_404(rid)
    conn = _conn(user)
    try:
        was = receipt_match.unmatch(conn, rid)
        if was is None:
            raise HTTPException(404, "no such matched receipt")
        _activity.record(conn, user, "receipt", "unmatched", target=was,
                         label=_activity.txn_label(conn, was),
                         detail={"receipt_kind": "receipt",
                                 "receipt_id": rid})
        return {"txn_id": was,
                **_ser(receipt_match.waiting(conn, today=_today(conn)))}
    finally:
        conn.close()


def _slip_answer(rid: str, user: dict, other: str | None,
                 same: bool) -> dict:
    from ..engine import receipt_identity
    rid = _receipt_id_or_404(rid)
    if not other:
        raise HTTPException(400, "other required")
    other = _receipt_id_or_404(other)
    conn = _conn(user)
    try:
        released: list[str] = []
        try:
            if same:
                receipt_identity.same_slip(conn, rid, other)
            else:
                released = receipt_identity.two_purchases(conn, rid, other)
        except LookupError:
            raise HTTPException(404, "no such receipt")
        except receipt_identity.NotAPair as e:
            raise HTTPException(400, str(e))
        _activity.record(conn, user, "receipt",
                         "same_slip" if same else "two_purchases",
                         detail={"receipt_kind": "receipt",
                                 "receipt_id": rid, "other": other,
                                 "released": released})
        return {"id": rid, "other": other, "same": same,
                "released": released}
    finally:
        conn.close()


@router.post("/receipts/{rid}/same-slip")
@documented(response=ReceiptSlipAnswered, body=ReceiptOther)
def receipt_same_slip(rid: str, user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    """A person says two receipts are the same receipt: one slip, its lines
    counted once. Stored, and never asked again."""
    return _slip_answer(rid, user, body.text("other"), True)


@router.post("/receipts/{rid}/two-purchases")
@documented(response=ReceiptSlipAnswered, body=ReceiptOther)
def receipt_two_purchases(rid: str, user: dict = Depends(_user()),
                          body: Fields = Depends(json_fields)):
    """A person says two receipts are two purchases: each counted, and no
    rule ever makes them one slip again."""
    return _slip_answer(rid, user, body.text("other"), False)


@router.post("/receipts/{rid}/details")
@documented(response=ReceiptDetailsSaved, body=ReceiptDetails)
def receipt_details(rid: str, user: dict = Depends(_user()),
                    body: Fields = Depends(json_fields)):
    """Type in a receipt's total, date and (optionally) store — for an
    instance with no AI to read it, a parse that failed, or a total read
    wrong. Stored as the receipt's parsed fields, marked as entered by hand
    so no later parse writes over them; a waiting receipt then looks for
    its charge at once. Saving the same details again changes nothing."""
    demoguard.deny(user)
    from ..engine import receipt_match, receipts
    total = body.money("total")
    if total is None:
        raise HTTPException(400, "total required")
    total = round(total, 2)
    if total <= 0:
        raise HTTPException(400, "total must be more than zero")
    # the matcher reads a total past its ceiling as unknown; a person
    # typing one would save details that silently never match
    if total > receipt_match.MAX_TOTAL:
        raise HTTPException(400, "total is too large")
    day = body.date("date", msg="date must be YYYY-MM-DD")
    if day is None:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    # absent or null keeps the store already read; "" clears it, so the
    # two are told apart here (the text reader reads both as blank)
    merchant = body.text("merchant", strip=False, msg="merchant must be text")
    if body.json_type("merchant") in (None, "null"):
        merchant = None
    rid = _receipt_id_or_404(rid)
    conn = _conn(user)
    try:
        row = conn.execute(
            "SELECT created_at, parsed FROM receipts WHERE id = %s::uuid"
            " AND kind = 'receipt'", (rid,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such receipt")
        # the store the form was filled with, sent back untouched, is kept
        # as stored — one read before the parse kept no more than
        # STORE_MAX may be longer, and the person never typed it
        was = row["parsed"] if isinstance(row["parsed"], dict) else {}
        if merchant and merchant.strip() == str(
                was.get("merchant") or "").strip():
            merchant = None
        if merchant and len(merchant.strip()) > receipts.STORE_MAX:
            raise HTTPException(
                400, f"merchant is {receipts.STORE_MAX} characters at most")
        # the matcher only trusts a date on the receipt from the year
        # before it was snapped up to the day after; refuse the rest here
        # rather than store a date that would silently be ignored
        snapped = row["created_at"].date()
        if day > snapped + dt.timedelta(days=1):
            raise HTTPException(400, "that date is after the receipt "
                                     "was snapped")
        if day < snapped - dt.timedelta(days=366):
            raise HTTPException(400, "that date is more than a year "
                                     "before the receipt was snapped")
        got = receipts.set_details(
            conn, rid, total=total, date=day,
            merchant=merchant if merchant is not None else None,
            keep_merchant=merchant is None)
        if got is None:
            raise HTTPException(404, "no such receipt")
        txn_id = got["txn_id"]
        if txn_id is None:
            txn_id = receipt_match.match_receipt(conn, rid)
        _activity.record(conn, user, "receipt", "details", target=txn_id,
                         label=(_activity.txn_label(conn, txn_id)
                                if txn_id else "a waiting receipt"),
                         detail={"receipt_kind": "receipt",
                                 "receipt_id": rid, "total": total})
        return {"id": rid, "txn_id": txn_id,
                **_ser(receipt_match.waiting(conn, today=_today(conn)))}
    finally:
        conn.close()


@router.get("/transactions/{txn_id:path}/receipts")
@documented(response=TxnReceipts)
def receipt_list(txn_id: str, user: dict = Depends(_user())):
    import math

    from ..engine import receipts
    conn = _conn(user)
    try:
        # a pending hold that settled since the client read it is shown as
        # the posted row it became, which is where its receipts went; read
        # from the retired hold, a refetch after an upload would come back
        # empty
        with conn.transaction():
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
        # the charge's own amount and date ride along: the panel ranks the
        # waiting receipts against them, so the one that fits this charge is
        # offered first however many are waiting
        t = conn.execute(
            "SELECT amount, date FROM transactions WHERE id = %s"
            " AND removed = 0", (txn_id,)).fetchone()
        # a row can carry no usable amount (a restore keeps what the file
        # held); then there is nothing to rank against, and the panel gets
        # the charge as unknown — which both clients already handle —
        # rather than the whole panel failing on a NULL
        amount = t["amount"] if t else None
        known = (amount is not None and t["date"] is not None
                 and math.isfinite(float(amount)))
        return {"receipts": [_ser(r) for r in receipts.for_txn(conn, txn_id)],
                "txn": ({"amount": round(float(amount), 2),
                         "date": str(t["date"])[:10]} if known else None)}
    finally:
        conn.close()


@router.get("/receipts/{rid}/image")
def receipt_image(rid: str, request: Request, user: dict = Depends(_user()),
                  variant: str = "original"):
    _same_site_download(request)
    from fastapi.responses import Response as _Resp

    from ..engine import receipts
    conn = _conn(user)
    try:
        r = receipts.image(conn, rid, variant=variant)
    finally:
        conn.close()
    if r is None:
        raise HTTPException(404, "no such receipt")
    # The stored mime is data, not policy: restore paths and old uploads
    # could carry anything. Real image types render inline (the receipt
    # panel opens them in a tab); everything else — PDFs included, whose
    # viewers run script — is a named download. Sniffing off either way,
    # so an attacker-shaped "image" can never render as origin-hosted HTML.
    mime = str(r["mime"] or "")
    if mime in receipts.IMAGE_MIMES:
        disp = "inline"
    else:
        ext = "pdf" if mime == "application/pdf" else "bin"
        mime = mime if mime == "application/pdf" else "application/octet-stream"
        disp = f'attachment; filename="receipt-{str(rid)[:8]}.{ext}"'
    return _Resp(bytes(r["image"]), media_type=mime,
                 headers={"Content-Disposition": disp,
                          "X-Content-Type-Options": "nosniff"})


@router.delete("/receipts/{rid}")
@documented(response=Ok)
def receipt_delete(rid: str, user: dict = Depends(_user())):
    # Refused on the demo household: the upload that could put a receipt
    # back is refused there too, so a deleted seeded receipt would be gone
    # for every visitor until the reset. Pairing and unpairing stay open —
    # they are row-level and a visitor can undo them from the same screen.
    demoguard.deny(user)
    from ..engine import receipts
    rid = _receipt_id_or_404(rid)
    conn = _conn(user)
    try:
        # the receipt's row before it goes: the log names the charge
        was = conn.execute("SELECT txn_id, kind FROM receipts "
                           "WHERE id = %s::uuid", (rid,)).fetchone()
        if not receipts.delete(conn, rid):
            raise HTTPException(404, "no such receipt")
        if was:
            _activity.record(conn, user, "receipt", "removed",
                             target=was["txn_id"],
                             label=(_activity.txn_label(conn, was["txn_id"])
                                    if was["txn_id"]
                                    else "the waiting list"),
                             detail={"receipt_kind": was["kind"],
                                     "receipt_id": rid})
        return {"ok": True}
    finally:
        conn.close()


@router.post("/receipts/{rid}/parse",
             dependencies=[Depends(limit("receipt_parse", 60, 3600))])
@documented(response=ReceiptParsing)
def receipt_parse(rid: str, user: dict = Depends(_user())):
    """Kick a (re)parse in the background; the UI polls receipt status.
    Rate-limited like upload: each call spawns a decode+vision thread."""
    demoguard.deny(user)
    from ..engine import llm_categorize, receipts
    conn = _conn(user)
    try:
        if receipts.image(conn, rid) is None:
            raise HTTPException(404, "no such receipt")
        if not llm_categorize.configured(conn, role="vision"):
            raise HTTPException(
                400, "no LLM configured — Settings → AI")
        if receipts.is_manual(conn, rid):
            raise HTTPException(
                409, "its total and date were entered by hand")
        if not receipts.claim(conn, rid):
            # A parse is already in flight — don't start a second
            # thread to fight it over the line items; the poll loop the
            # caller is about to enter will see the live one's result
            return {"status": "parsing"}
    finally:
        conn.close()
    _parse_receipt_async(str(user["tenant_id"]), rid)
    return {"status": "parsing"}


@router.get("/receipts/items")
@documented(response=ReceiptItems)
def receipt_items_api(user: dict = Depends(_user()), q: str = "",
                      group: str = "item"):
    """Search + group line items across every parsed receipt."""
    from ..engine import receipts
    conn = _conn(user)
    try:
        out = receipts.search_items(conn, q=q, group=group)
    finally:
        conn.close()
    return {"rows": [_ser(r) for r in out["rows"]],
            "groups": [_ser(g) for g in out["groups"]],
            "truncated": out["truncated"]}


@router.post("/receipts/{rid}/items/{line}/tag")
@documented(response=Ok, body=ReceiptTag)
def receipt_tag(rid: str, line: int, user: dict = Depends(_user()),
                body: Fields = Depends(json_fields)):
    from ..engine import receipts
    tag = body.text("tag", strip=False)          # "" or null clears it
    conn = _conn(user)
    try:
        if not receipts.set_tag(conn, rid, line, tag):
            raise HTTPException(404, "no such line item")
        return {"ok": True}
    finally:
        conn.close()


@router.get("/receipts/report")
@documented(response=ReceiptReport)
def receipt_report(request: Request, user: dict = Depends(_user()),
                   tag: str = "business",
                   y: int = 0, m: int = 0, format: str = "json"):
    _same_site_download(request)
    import io as _io

    from ..engine import receipts
    if format == "csv":
        # the expense file is a download of the household's books, the
        # owner's to take like every other ledger file
        _owner_only(user)
    conn = _conn(user)
    try:
        today = _today(conn)
        y, m = y or today.year, m or today.month
        _ck_ym(y, m)
        rep = receipts.report(conn, tag, y, m)
    finally:
        conn.close()
    if format == "csv":
        from fastapi.responses import Response as _Resp
        buf = _io.StringIO()
        w = _csv_writer(buf)
        w.writerow(["date", "payee", "description", "qty", "amount", "tag",
                    "txn_id", "receipt_id"])
        for r in rep["rows"]:
            w.writerow([r["date"], _csv_safe(r["payee"]),
                        _csv_safe(r["description"]), r["qty"], r["amount"],
                        _csv_safe(r["tag"]), r["txn_id"], r["receipt_id"]])
        w.writerow([])
        w.writerow(["total", "", "", "", rep["total"]])
        return _Resp(buf.getvalue(), media_type="text/csv", headers={
            "Content-Disposition":
            f'attachment; filename="expenses-{_filename_safe(tag, "all")}-{y}-{m:02d}.csv"'})
    rep["rows"] = [_ser(r) for r in rep["rows"]]
    return rep
