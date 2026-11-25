"""Transactions, merchants, categories and rules.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import calendar
import datetime as dt
import decimal
import functools
import json
import psycopg
import threading as _threading
import uuid
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from . import data, demoguard
from ..engine import activity as _activity
from .api_common import (_category_name, _category_why, _ck_ym, _conn,
                         _csv_safe, _csv_writer, _filename_safe, _label_field,
                         _may_edit, _merchant_name_or_400,
                         _new_category_or_400, _owner_only,
                         _recurring_stamper, router, _same_site_download,
                         _ser, _stamp_recurring, _today, _user)
from .fields import Fields, json_fields
from .schemas import documented, Ok
from .schemas.ledger import (AccountOwnerSet, Categories, CategoryRename,
                             CheckPayee, CheckPayeeSaved,
                             CategoryRenamed, MerchantCatalog,
                             MerchantDetail, MerchantMergeAnswer,
                             MerchantMerged, MerchantRenameInput,
                             MerchantRenamed, MerchantUndo, MerchantUndone,
                             Merchants, OwnerInput, Owners, PendingRetired,
                             RetirePending, RuleDeleted, RuleDisable,
                             RuleInput, RuleMerchant, RuleSet, RulesPage,
                             SplitInput, SplitSaved, TxnBulk, TxnBulkDone,
                             TxnCategory, TxnCategorySet, TxnNote,
                             TxnNoteSaved, TxnOwnerSet, TxnPage, Txn)
from .security import limit


# ---- the household activity log: the doors below write one row per act ----

def _log_bulk(conn, user, action: str, category: str, ids: list[str],
              out: dict) -> None:
    """One row for a bulk act over a hand-picked set — the count, the
    category, and the first row named so the entry reads as something."""
    n = int(out.get("applied") or 0)
    first = ids[0] if ids else ""
    label = _activity.txn_label(conn, first) if first else ""
    if action == "category":
        _activity.record(conn, user, "category", "bulk", target=None,
                         label=label,
                         detail={"after": category, "count": n, "ids": ids[:50]})
    elif action in ("biz_on", "biz_off"):
        _activity.record(conn, user, "business",
                         "flagged" if action == "biz_on" else "unflagged",
                         target=first if n == 1 else None,
                         label=label if n == 1 else f"{n} transactions")
    elif action == "reimb_flag":
        _activity.record(conn, user, "reimbursement", "flagged",
                         target=first if n == 1 else None,
                         label=label if n == 1 else f"{n} transactions")




def _log_merge(conn, user, pid: str, action: str, r: dict) -> None:
    if action == "approve":
        _activity.record(conn, user, "merchant", "merged",
                         target=r.get("into"), label=str(r.get("from") or pid),
                         detail={"before": r.get("from"), "after": r.get("into"),
                                 "rows": r.get("rows")})
        return
    names = conn.execute(
        """SELECT (SELECT name FROM merchants WHERE id = p.from_merchant_id) AS a,
                  (SELECT name FROM merchants WHERE id = p.into_merchant_id) AS b
             FROM merchant_merge_proposals p WHERE p.id=%s""", (pid,)).fetchone()
    pair = " and ".join(x for x in ((names or {}).get("a"), (names or {}).get("b")) if x)
    _activity.record(conn, user, "merchant", "kept_apart", target=pid,
                     label=pair or "two merchants")






def _decorated(conn, rows, chunk: int):
    """The ledger rows as the page renders them: serialised, with the
    category's 'why' and the bill pill. With `chunk` the rows arrive as a
    generator of slices (search_transactions' chunked form) and come back
    the same way, each slice decorated only as it is read."""
    if not chunk:
        out = [_ser(r) for r in rows]
        for o in out:
            o["category_why"] = _category_why(o)
        _stamp_recurring(conn, out)
        return out
    stamp = _recurring_stamper(conn)

    def _gen():
        for part in rows:
            out = [_ser(r) for r in part]
            for o in out:
                o["category_why"] = _category_why(o)
            stamp(out)
            yield out
    return _gen()


def _per_day(total, start, end, today: dt.date):
    """Average daily spend over a window: from `start` to `end` or today,
    whichever comes first — a month in progress divides by the days
    elapsed, a closed month by all of its days. (None, None) when there is
    no start to count from, the window has not begun, or nothing was spent.
    """
    if not total or not start:
        return None, None
    s = start if isinstance(start, dt.date) else dt.date.fromisoformat(str(start)[:10])
    e = end if isinstance(end, dt.date) else (
        dt.date.fromisoformat(str(end)[:10]) if end else today)
    days = (min(e, today) - s).days + 1
    if days <= 0:
        return None, None
    return round(total / days, 2), days


def _ledger_first(conn, *, budget_view: bool = True):
    """The ledger's first day. `budget_view` leaves out the accounts the
    budget excludes, the way the month's pace reads it; a raw search can
    list those accounts' rows too, so it counts from the first row of any
    account."""
    from ..engine import budget as _budget
    excluded = (_budget.excluded_account_ids(_budget.load_config(conn))
                if budget_view else None)
    return _budget.ledger_start(conn, excluded)


def _per_seen_day(total, windows):
    """Average daily spend over the days a ledger has seen of one or more
    months, each read through its pace window (budget.pace_window,
    specs/short-ledger-pacing.md): from the later of the 1st and the
    ledger's first day, through the day read on (today included) or month
    end once it is closed — the days the Why's "running $X/day" divides
    by. Days before the ledger are no data, not days of no spending. A
    month the ledger had not reached adds nothing. (None, None) when
    nothing was spent or no day was seen."""
    if not total:
        return None, None
    days = sum(w["seen_days"] for w in windows if w["paced_days"] > 0)
    if days <= 0:
        return None, None
    return round(total / days, 2), days


def _month_windows(first, year: int, months, today: dt.date) -> list[dict]:
    """The pace windows of the named months of `year` up to `today`: the
    month of `today` live, earlier ones closed, later ones left out."""
    from ..engine import budget as _budget
    out = []
    for m in months:
        if (year, m) > (today.year, today.month):
            continue
        closed = (year, m) < (today.year, today.month)
        out.append(_budget.pace_window_for(
            first, dt.date(year, m, 1) if closed else today, closed=closed))
    return out


def _json_default(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (uuid.UUID, set, frozenset)):
        return str(v) if isinstance(v, uuid.UUID) else sorted(v)
    raise TypeError(f"not serialisable: {type(v).__name__}")


def _flat_json(fn):
    """Serialise a handler's dict straight to JSON. FastAPI's encoder
    walks every value of a payload to make it JSON-safe; the rows the
    ledger doors hand back are already flat (`_ser` writes ISO dates),
    and that walk over a month of them cost more than the query."""
    @functools.wraps(fn)
    def wrapped(*a, **kw):
        out = fn(*a, **kw)
        if not isinstance(out, dict):
            return out
        return Response(json.dumps(out, default=_json_default, allow_nan=False),
                        media_type="application/json")
    return wrapped


@router.get("/transactions")
@_flat_json
@documented(response=TxnPage)
def transactions(user: dict = Depends(_user()), y: int = 0, m: int = 0,
                 q: str = "", acct: str = "", cat: str = "",
                 date_from: str = "", date_to: str = "", page: int = 1,
                 reimb: int = 0, owner: str = "", scope: str = "all",
                 channel: str = "", checks: int = 0, bucket: str = "",
                 as_of: str = "", counted: int = 0, spend: str = "",
                 merchant: str = "", side: str = ""):
    conn = _conn(user)
    try:
        return _transactions_payload(conn, y=y, m=m, q=q, acct=acct, cat=cat, date_from=date_from, date_to=date_to, page=page, reimb=reimb, owner=owner, scope=scope, channel=channel, checks=checks, bucket=bucket, as_of=as_of, counted=counted, spend=spend, merchant=merchant, side=side)
    finally:
        conn.close()


@router.get("/transactions/one")
@_flat_json
@documented(response=Txn)
def transaction_one(id: str, user: dict = Depends(_user())):
    """One ledger row, exactly as the list door sends it, for a screen that
    opens a transaction it was not handed (a deep link, an app restart).
    The household's connection scopes the read, so another household's id
    answers 404 like a row that does not exist. A pending hold that settled
    since the link was made answers as the posted row it became."""
    if not id or len(id) > 512:
        raise HTTPException(400, "id is required")
    conn = _conn(user)
    try:
        with conn.transaction():
            live = data.live_edit_ids(conn, [id]).get(id, id)
        rows, _total, _sum, _items, _spend, _hits = data.search_transactions(
            conn, "", ids=[live], page=1, per_page=1, scope="all")
        out = _decorated(conn, rows, 0)
        if not out:
            raise HTTPException(404, "no such transaction")
        return out[0]
    finally:
        conn.close()


# The download door answers with the page's whole row set at once; a
# household's entire history is a few tens of thousands of rows, and a
# wider answer than this is a sign of a filter that means "everything",
# which the full export (Settings) is for.
TRANSACTIONS_EXPORT_CAP = 50_000

# the columns a person reads, in the order the page shows them; the
# ledger's own keys stay out (ids are last, for anyone matching rows back)
_EXPORT_COLUMNS = (
    ("date", "date"), ("payee", "payee"), ("amount", "amount"),
    ("category", "category"), ("account", "account"), ("mask", "account_mask"),
    ("pending", "pending"), ("note", "note"), ("owner", "owner"),
    ("entity", "business"), ("reimb", "reimbursable"),
    ("payment_channel", "channel"), ("check_number", "check_number"),
    ("location_city", "city"), ("location_region", "region"),
    ("item_summary", "items"), ("recurring_bill", "bill"),
    ("bank_text", "bank_text"), ("id", "id"),
)


# How many rows a download reads, decorates and writes out at a time: big
# enough that the per-slice query is not the cost, small enough that a
# slice of the wide ledger row is a few megabytes rather than the whole
# history.
_EXPORT_CHUNK = 1_000

# Downloads being built. One per household — a second click (or CSV then
# XLSX) while the first is still being written would only build the same
# file twice — and a few for the whole process, so households exporting
# at the same moment queue at the door instead of stacking up their
# working sets in one process. A slot covers BUILDING the file only, which
# the server does at its own pace; the transfer that follows holds no slot
# and no connection, so a client that reads slowly can neither starve the
# shared connection pool nor keep other households at the door. A slot
# that outlives _EXPORT_SLOT_TTL is treated as abandoned, so a leak can
# never lock a household out.
# The gate lives in this process because the server is one process:
# `oikonome serve` starts a single uvicorn worker and the image runs
# nothing else. Running several workers would need a database-backed
# per-household lock here instead.
_EXPORT_MAX = 2
_EXPORT_SLOT_TTL = 15 * 60
_EXPORT_SLOTS: dict[object, tuple[str, float]] = {}
_EXPORT_SLOTS_LOCK = _threading.Lock()
# A built file stays in memory up to this size and spills to a temporary
# file past it, so a whole history is never held in memory at once; it is
# sent in pieces of _EXPORT_SEND_PIECE.
_EXPORT_SPOOL_MEMORY = 4 * 1024 * 1024
_EXPORT_SEND_PIECE = 64 * 1024
# Files built and not yet fully sent. The slot is given back once a file
# is built, but the file itself lives until the client has read it or
# left, and how fast that happens is up to the client — so without a
# bound of their own, readers that stall after the headers would pile up
# spooled files (4 MiB of memory each, then disk) in the one process
# every household shares. A household may hold _EXPORT_HELD_PER_HOUSEHOLD
# of them and the process _EXPORT_HELD_MAX; past that the next download
# is refused up front, before anything is built. The hold is given back
# when the send ends, however it ends, and no send may run longer than
# _EXPORT_SEND_MAX_S: a reader that takes the headers and then stalls is
# cut off and its place given back, so two stalled households cannot hold
# the door shut for everyone else. _EXPORT_HELD_TTL is only a safety net
# behind that deadline, and the process cap is several households' worth.
_EXPORT_HELD_MAX = 16
_EXPORT_HELD_PER_HOUSEHOLD = 2
_EXPORT_SEND_MAX_S = 20 * 60
_EXPORT_HELD_TTL = 2 * _EXPORT_SEND_MAX_S
_EXPORT_HELD: dict[object, tuple[str, float]] = {}


def _export_slot(tenant_id: str):
    """Take a download slot for this household, or answer 429. Returns
    the release function (safe to call more than once)."""
    import time as _time
    now = _time.monotonic()
    token = object()
    tenant_id = str(tenant_id)
    with _EXPORT_SLOTS_LOCK:
        for k, (_t, began) in list(_EXPORT_SLOTS.items()):
            if now - began > _EXPORT_SLOT_TTL:
                del _EXPORT_SLOTS[k]
        if any(t == tenant_id for t, _b in _EXPORT_SLOTS.values()):
            raise HTTPException(429, "an export is already running — wait "
                                     "for it to finish, then try again")
        if len(_EXPORT_SLOTS) >= _EXPORT_MAX:
            raise HTTPException(429, "other downloads are being prepared "
                                     "right now — try again in a minute")
        _EXPORT_SLOTS[token] = (tenant_id, now)

    def release():
        with _EXPORT_SLOTS_LOCK:
            _EXPORT_SLOTS.pop(token, None)
    return release


def _export_hold(tenant_id: str):
    """Take a place for one built-but-unsent download file, or refuse:
    429 when this household already holds its share, 503 when the
    process is full. Returns the release function (safe to call more than
    once)."""
    import time as _time
    now = _time.monotonic()
    token = object()
    tenant_id = str(tenant_id)
    with _EXPORT_SLOTS_LOCK:
        for k, (_t, began) in list(_EXPORT_HELD.items()):
            if now - began > _EXPORT_HELD_TTL:
                del _EXPORT_HELD[k]
        mine = sum(1 for t, _b in _EXPORT_HELD.values() if t == tenant_id)
        if mine >= _EXPORT_HELD_PER_HOUSEHOLD:
            raise HTTPException(
                429, "earlier downloads are still being sent — let them "
                     "finish, then try again", headers={"Retry-After": "30"})
        if len(_EXPORT_HELD) >= _EXPORT_HELD_MAX:
            raise HTTPException(
                503, "other downloads are being sent right now — try again "
                     "in a minute", headers={"Retry-After": "30"})
        _EXPORT_HELD[token] = (tenant_id, now)

    def release():
        with _EXPORT_SLOTS_LOCK:
            _EXPORT_HELD.pop(token, None)
    return release


class _ClosingStream(StreamingResponse):
    """A streamed download that closes its body generator the moment the
    response ends, however it ends.

    Starlette only stops iterating when the client leaves: the body's
    generator is left suspended at its last `yield`, and its `finally` —
    which gives back what the body holds (the built file) — would run only
    when the garbage collector reached it. So the close is done here, in
    the thread pool, shielded from the cancellation that a disconnect
    delivers."""

    def __init__(self, content, close, **kw):
        super().__init__(content, **kw)
        self._close_body = close

    async def __call__(self, scope, receive, send):
        import anyio
        try:
            # a send has a deadline: a client that stalls after the headers
            # is cut off here, and the finally below gives the file back
            with anyio.fail_after(_EXPORT_SEND_MAX_S):
                await super().__call__(scope, receive, send)
        except TimeoutError:
            import logging
            logging.getLogger("oikonome.api").info("export send cut off after %ss: the client stalled",
                                                    _EXPORT_SEND_MAX_S)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._close_body)


@router.get("/transactions/export")
def transactions_export(request: Request, user: dict = Depends(_user()),
                        format: str = "csv",
                        y: int = 0, m: int = 0, q: str = "", acct: str = "",
                        cat: str = "", date_from: str = "", date_to: str = "",
                        reimb: int = 0, owner: str = "", scope: str = "all",
                        channel: str = "", checks: int = 0, bucket: str = "",
                        as_of: str = "", counted: int = 0, spend: str = "",
                        merchant: str = "", side: str = ""):
    """The ledger page as a file: the same filters GET /api/transactions
    takes, the same rows it would list (every page of them), as CSV or a
    one-sheet .xlsx. Amounts are signed the way the page reads them —
    money in positive, money out negative.

    The filters are matched once up front (so a bad filter is still a
    plain 400), then the rows are read, decorated and written a slice at a
    time on one connection into a spooled file (memory, then disk), so a
    whole history is never held in memory at once. The connection and the
    household's download slot are given back once the file is built,
    BEFORE the first byte is sent: the connection is pooled and shared by
    every household, and how fast a download is read is up to the client.
    The built file is counted until the send ends (_export_hold), so
    readers that stall cannot pile files up in the process.

    Owner-only, like every door that writes the household's ledger to a
    file: a member or a view-only login reads the same rows on the page,
    but a copy that has left the instance cannot be recalled, so carrying
    one away is the owner's act (permissions.py, "taking the data out")."""
    _same_site_download(request)
    _owner_only(user)
    if format not in ("csv", "xlsx"):
        raise HTTPException(400, "format must be csv or xlsx")
    release = _export_slot(user["tenant_id"])
    try:
        unhold = _export_hold(user["tenant_id"])
    except BaseException:
        release()
        raise
    conn = None
    try:
        conn = _conn(user)
        payload = _transactions_payload(
            conn, y=y, m=m, q=q, acct=acct, cat=cat, date_from=date_from,
            date_to=date_to, page=1, reimb=reimb, owner=owner, scope=scope,
            channel=channel, checks=checks, bucket=bucket, as_of=as_of,
            counted=counted, spend=spend, merchant=merchant, side=side,
            per_page=TRANSACTIONS_EXPORT_CAP,
            chunk=_EXPORT_CHUNK)
    except BaseException:
        if conn is not None:
            conn.close()
        release()
        unhold()
        raise
    rows = payload.get("rows") or []
    # the month browse answers with one list; a search with slices
    batches = iter([rows]) if isinstance(rows, list) else rows
    total = payload.get("total")

    def _value(r, key):
        v = r.get(key)
        if key == "amount":
            return -v if isinstance(v, (int, float)) else v
        if key in ("pending", "reimb"):
            return "yes" if v else ""
        if isinstance(v, (int, float)) or v is None:
            return v
        return _csv_safe(str(v))

    header = [label for _k, label in _EXPORT_COLUMNS]

    def _tables():
        written = 0
        for part in batches:
            written += len(part)
            yield [[_value(r, k) for k, _l in _EXPORT_COLUMNS]
                   for r in part]
        if total is not None and total > written:
            yield [[f"{total - written} more rows were not included "
                    f"(a download holds up to {TRANSACTIONS_EXPORT_CAP:,}); "
                    "narrow the filters or use the full export"]]

    try:
        # name the file after what was asked for, the way a person would
        if payload.get("mode") == "month":
            stem = f"transactions-{payload['year']}-{payload['month']:02d}"
        else:
            bits = [b for b in (q.strip(), cat, bucket, acct and "account",
                                date_from, date_to) if b]
            stem = "transactions-" + _filename_safe("-".join(bits) or "search",
                                                    "search")
        import tempfile as _tempfile
        out = _tempfile.SpooledTemporaryFile(max_size=_EXPORT_SPOOL_MEMORY)
        try:
            if format == "csv":
                import io as _io
                buf = _io.StringIO()
                w = _csv_writer(buf)
                w.writerow(header)
                out.write(buf.getvalue().encode())
                for table in _tables():
                    buf.seek(0)
                    buf.truncate()
                    w.writerows(table)
                    out.write(buf.getvalue().encode())
                media, ext_ = "text/csv", "csv"
            else:
                from . import xlsx as _xlsx
                for part in _xlsx.stream_workbook("Transactions", header,
                                                  _tables()):
                    out.write(part)
                media, ext_ = _xlsx.MEDIA_TYPE, "xlsx"
            out.seek(0)
        except BaseException:
            out.close()
            raise
    except BaseException:
        unhold()
        raise
    finally:
        # the file is built (or the build failed): nothing the client does
        # from here holds the shared connection or the household's slot
        try:
            try:
                if hasattr(batches, "close"):
                    batches.close()
            finally:
                conn.close()
        finally:
            release()

    def _send():
        try:
            while True:
                piece = out.read(_EXPORT_SEND_PIECE)
                if not piece:
                    return
                yield piece
        finally:
            # closing a spooled file that spilled to disk removes it
            try:
                out.close()
            finally:
                unhold()
    import itertools as _it
    body = _send()
    # Start the generator here, so its cleanup is armed: a generator that
    # was never started runs no `finally` when it is discarded, and a
    # client that leaves before the first byte would otherwise leave the
    # built file open until the collector reached it.
    try:
        first = next(body)
    except BaseException:
        body.close()
        out.close()
        unhold()
        raise
    return _ClosingStream(_it.chain([first], body), body.close,
                          media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{stem}.{ext_}"',
        "Cache-Control": "no-store"})


def _transactions_payload(conn, *, y: int = 0, m: int = 0, q: str = "",
                          acct: str = "", cat: str = "", date_from: str = "",
                          date_to: str = "", page: int = 1, reimb: int = 0,
                          owner: str = "", scope: str = "all",
                          channel: str = "", checks: int = 0,
                          bucket: str = "", as_of: str = "", counted: int = 0,
                          spend: str = "", merchant: str = "", side: str = "",
                          per_page: int = 100, chunk: int = 0) -> dict:
    """The ledger page's answer for one set of filters: month browse, a
    search, a bucket's or a counted category's row set. The page reads it
    through GET /api/transactions a page at a time; the download door
    reads the same answer with `per_page` wide enough for the whole set,
    so what is exported is exactly what the page would list. `chunk`
    turns a search's "rows" into a generator of row slices read on
    `conn` (see search_transactions); the month browse, one month of
    rows, stays a list either way."""
    # bound like _ck_ym bounds y/m: an unbounded page multiplies into an
    # OFFSET past bigint inside the driver (NumericValueOutOfRange → 500)
    page = min(max(1, page), 10_000_000)
    # which rows the ledger lists: household + business (default, business
    # rows labelled by entity), household only, or business only
    if scope not in ("all", "personal", "business"):
        raise HTTPException(400, "scope must be all, personal or business")
    if channel and channel not in ("in store", "online", "other"):
        raise HTTPException(400, "channel must be in store, online or other")
    today = _today(conn)
    _ck_ym(y or today.year, m or today.month)
    for _label, _v in (("date_from", date_from), ("date_to", date_to)):
        if _v:
            # as_date, not fromisoformat: a well-formed year the ledger
            # cannot read (0001, 9999) must be a 400 here, not a 500 in
            # the search below
            from ..engine.compat import as_date as _as_date
            try:
                _as_date(_v[:10])
            except ValueError:
                raise HTTPException(400, f"{_label} must be YYYY-MM-DD")
    yy, mm = y or today.year, m or today.month
    # `bucket` opens the rows behind ONE of the month verdict's own
    # numbers — Food, Everything else, or a custom carve-out. The set
    # cannot be expressed as a category filter (Food is a category OR
    # two override strings, minus every bill-shaped row, plus envelope
    # overflow; a carve-out is a plan label matched by merchant AND
    # category rules), so the engine names the rows and the search
    # surface renders them. Every other filter stands down: this is a
    # listing of a number, not a query.
    if bucket:
        from ..engine import budget as _budget
        from . import lenses as _lenses
        # a year's bucket — the year lens's money map — is `y` with no
        # `m`: the union of that bucket over the year's elapsed budgeted
        # months, the months the map summed. Its rows run to today.
        year_scope = bool(y) and not m
        read_on = today
        if year_scope:
            if as_of:
                raise HTTPException(400, "as_of belongs to a month's bucket")
            info = _lenses.year_bucket_ledger(conn, bucket, yy, today)
        else:
            # the month lens' own state: a closed month is judged against
            # its frozen snapshot there, so the listing matches the tile
            # that month actually shows, not today's budget applied back.
            # `as_of` is the day a Today page was read on: a past day of
            # the month shows that day's figures, so its rows stop there.
            if as_of:
                try:
                    read_on = dt.date.fromisoformat(as_of[:10])
                except ValueError:
                    raise HTTPException(400, "as_of must be YYYY-MM-DD")
                if (read_on.year, read_on.month) != (yy, mm) or read_on > today:
                    raise HTTPException(
                        400, "as_of must be a day of that month, not after today")
            mst = _lenses.month_state(conn, yy, mm, read_on)
            info = _budget.bucket_ledger(mst, bucket)
        if info is None:
            raise HTTPException(400, "unknown bucket")
        rows, total, amount_sum, amazon_items, spend, account_hits = \
            data.search_transactions(conn, "", ids=info["txn_ids"],
                                     page=max(1, page), per_page=per_page, scope="all",
                                     chunk=chunk)
        out = _decorated(conn, rows, chunk)
        avg = (round(spend["sum"] / spend["count"], 2)
               if spend["count"] else None)
        # A bucket is one month's (or one year's) number, and its daily
        # pace is THAT number — the figure the label wore, reimbursements
        # netted and envelope overflow counted only past the envelope —
        # over the days it was counted across, up to the day it was read
        # on. The listed rows' raw sum is neither: an overflowing
        # purchase is listed whole. A year's figure sums only its
        # budgeted months, so its days are those months' days, not every
        # day since January 1. Either way the days are the ones the
        # ledger has seen: a ledger that began on the 10th divides from
        # the 10th, the way the Why's "running $X/day" does.
        if year_scope:
            per_day, per_day_days = _per_seen_day(
                info["amount"], _month_windows(
                    _ledger_first(conn), yy, info.get("months") or [],
                    read_on))
        else:
            per_day, per_day_days = _per_seen_day(info["amount"],
                                                  [mst["pace"]])
        return {"mode": "search", "rows": out,
                "total": total, "amount_sum": amount_sum,
                "amazon_items": amazon_items, "page": page,
                "spend_count": spend["count"], "spend_sum": spend["sum"],
                "spend_avg": avg, "biz_count": spend["biz_count"],
                "day_spend": spend.get("by_day") or {},
                "per_day": per_day, "per_day_days": per_day_days,
                "account_hits": account_hits,
                # what the chip over the list says it is showing, and
                # the figure the label wore: reimbursements netted and
                # envelope overflow counted the way the plan counts
                # them, which the listed rows' raw sum is not
                "bucket": info["bucket"], "bucket_label": info["label"],
                "bucket_amount": info["amount"],
                "bucket_scope": "year" if year_scope else "month",
                "ranges": _ranges(conn, today, chunk)}
    # `spend` is a Spending tab figure's link: the rows one of its figures
    # summed over one range — a category (`cat`), a merchant (`merchant`)
    # or the window's whole spend — on the window's current or previous
    # side. The engine names the rows by the tab's own predicate
    # (reporting.spending_window_ledger) and every other filter stands
    # down, like a bucket; the total is the figure itself.
    if spend:
        from ..engine import reporting as _reporting
        try:
            info = _reporting.spending_window_ledger(
                conn, today, spend, category=cat or None,
                merchant=merchant or None, side=side or "current")
        except ValueError as e:
            raise HTTPException(400, str(e))
        rows, total, _gross, amazon_items, sp, account_hits = \
            data.search_transactions(conn, "", ids=info["txn_ids"],
                                     page=max(1, page), per_page=per_page,
                                     scope="all", chunk=chunk)
        out = _decorated(conn, rows, chunk)
        # the list's sum, average and day dividers are the FIGURE's, not
        # the rows' whole amounts: a category counts a split charge for its
        # part, a partly repaid one for what was kept
        n = len(info["txn_ids"])
        avg = round(info["amount"] / n, 2) if n and info["amount"] else None
        # the figure per day over the window's days the ledger has seen
        from ..engine.compat import as_date as _as_date
        first = _ledger_first(conn)
        dstart = _as_date(info["from"])
        if first and first > dstart:
            dstart = first
        per_day, per_day_days = _per_day(info["amount"], dstart,
                                         info["to"], today)
        return {"mode": "search", "rows": out,
                "total": total, "amount_sum": info["amount"],
                "amazon_items": amazon_items, "page": page,
                "spend_count": n, "spend_sum": info["amount"],
                "spend_avg": avg, "biz_count": sp["biz_count"],
                "day_spend": info["by_day"],
                "per_day": per_day, "per_day_days": per_day_days,
                "account_hits": account_hits, "counted": True,
                "drill_label": info["label"],
                "ranges": _ranges(conn, today, chunk)}
    # `counted` is a lens CATEGORY label's link: the rows that label
    # summed (personal spend, money out, reimbursements netted) for one
    # month or one whole year — not every row filed under the category.
    # Like a bucket, the engine names the rows and the other lenses
    # stand down, and the total is the label's own number.
    if counted and cat:
        from . import lenses as _lenses
        if date_from or date_to:
            yr = date_from[:4]
            if not (yr.isdigit() and date_from[:10] == f"{yr}-01-01"
                    and date_to[:10] == f"{yr}-12-31"):
                raise HTTPException(
                    400, "a counted category is one month or one whole year")
            info = _lenses.year_category_ledger(conn, cat, int(yr))
        else:
            mst = _lenses.month_state(conn, yy, mm, today)
            info = _lenses.month_category_ledger(mst, cat)
        rows, total, _gross, amazon_items, spend, account_hits = \
            data.search_transactions(conn, "", ids=info["txn_ids"],
                                     page=max(1, page), per_page=per_page, scope="all",
                                     chunk=chunk)
        out = _decorated(conn, rows, chunk)
        avg = (round(spend["sum"] / spend["count"], 2)
               if spend["count"] else None)
        # the label's own number per day over the days of the month or
        # year it counts that the ledger has seen
        if date_from:
            windows = _month_windows(_ledger_first(conn), int(yr),
                                     range(1, 13), today)
        else:
            windows = [mst["pace"]]
        per_day, per_day_days = _per_seen_day(info["amount"], windows)
        return {"mode": "search", "rows": out,
                "total": total, "amount_sum": info["amount"],
                "amazon_items": amazon_items, "page": page,
                "spend_count": spend["count"], "spend_sum": spend["sum"],
                "spend_avg": avg, "biz_count": spend["biz_count"],
                "day_spend": spend.get("by_day") or {},
                "per_day": per_day, "per_day_days": per_day_days,
                "account_hits": account_hits, "counted": True,
                "ranges": _ranges(conn, today, chunk)}
    # a channel or checks-only lens is a search (the month browse has
    # no way to express it), like a category or an account is
    searching = bool(q.strip() or acct or cat or date_from or date_to
                     or channel or checks)
    # A month link ("Food, July") sends y/m beside the filter, but a
    # search reads neither, so the same link would open a category's
    # ALL-TIME history. The month's span is derived server-side, so every
    # client gets the month it asked for. An explicit range
    # still wins: it is the more specific instruction.
    if searching and (y or m) and not (date_from or date_to):
        date_from = dt.date(yy, mm, 1).isoformat()
        date_to = dt.date(yy, mm,
                          calendar.monthrange(yy, mm)[1]).isoformat()
    if searching:
        rows, total, amount_sum, amazon_items, spend, account_hits = \
            data.search_transactions(
                conn, q, account_id=acct or None, category=cat or None,
                date_from=date_from or None, date_to=date_to or None,
                reimb_only=bool(reimb), page=max(1, page), per_page=per_page, scope=scope,
                channel=channel or None, checks_only=bool(checks),
                chunk=chunk, flows=not chunk)
        out = _decorated(conn, rows, chunk)
        # A total answers "how much have I given them"; the average
        # answers "what does a visit cost", which is the question a
        # merchant search is usually really asking. Sent only when
        # there is spending to average — a search matching nothing but
        # income or transfers has no average, and 0 would read as one.
        avg = round(spend["sum"] / spend["count"], 2) if spend["count"] else None
        # the average per DAY answers "what is this costing me" over the
        # window asked for — a month's category, a date range. A search
        # with no start date has no window to divide by. The window starts
        # no earlier than the ledger: days before its first row are no
        # data, not days of no spending (the pace window's boundary rule).
        start = date_from or None
        if start and spend["sum"]:
            from ..engine.compat import as_date as _as_date
            first = _ledger_first(conn, budget_view=False)
            if first and first > _as_date(start[:10]):
                start = first
        per_day, per_day_days = _per_day(spend["sum"], start,
                                         date_to or None, today)
        return {"mode": "search", "rows": out,
                "total": total, "amount_sum": amount_sum,
                "amazon_items": amazon_items, "page": page,
                "spend_count": spend["count"], "spend_sum": spend["sum"],
                "spend_avg": avg,
                "per_day": per_day, "per_day_days": per_day_days,
                # each listed day's spending over the whole result set,
                # pages not yet loaded included (the day dividers)
                "day_spend": spend.get("by_day") or {},
                # matched rows that are a business's (not in amount_sum
                # unless scope=business)
                "biz_count": spend["biz_count"],
                # accounts the term matched by name — via_bank marks a
                # renamed account found through the bank's own name, so
                # the page can explain a result that shows neither word
                "account_hits": account_hits,
                # the headline: money out / in / net over every matched
                # row by Cash Flow's rules (reporting.flows), never the
                # raw sum of the rows, which counts a card payment on both
                # sides and a transfer as money made or spent
                "flow": _flow_shape(spend.get("flows")),
                "ranges": _ranges(conn, today, chunk)}
    rows = data.transactions(conn, y or today.year, m or today.month,
                             reimb_only=bool(reimb), owner=owner or None,
                             scope=scope)
    out = [_ser(r) for r in rows]
    for o in out:
        o["category_why"] = _category_why(o)
    _stamp_recurring(conn, out)
    # The month header states the month's CASH FLOW — income in, spend
    # out, net — by the same definitions the Cash Flow page uses, not a
    # raw sum of the listed rows. Raw sums would count a credit-card
    # payment as money out on top of the card swipes it paid for, and
    # count transfers between own accounts on both sides, so the
    # header would show an "overspent" month that was actually
    # positive. Transfers, card payments, linked
    # shadow rows and business-entity money are all excluded; the
    # owner/reimb quick-filters narrow the LIST but the headline stays
    # the month's — a display filter must not change what the month
    # cost. `biz_count` says how many listed rows are a business's.
    # In and net are null for a month before the bank feed starts:
    # its income is unknown (the Month lens says the same), and $0 in
    # would read the month's whole spend as a loss.
    from ..engine import entities as _ent, reporting as _reporting
    combined = _ent.is_combined(conn)
    flows = _reporting.month_flows(conn, y or today.year,
                                   m or today.month)
    # per day over the days of the month the ledger has seen, the way the
    # month's pace counts them (from the ledger's first day when it began
    # inside the month)
    per_day, per_day_days = _per_seen_day(
        flows["out"], _month_windows(_ledger_first(conn), yy, [mm], today))
    # the current month's income still to come — the paychecks due before
    # month end that have not landed — so the header's "in" can say what
    # the month will be, the same figure the Budget plan counts. Like
    # in_sum it is the household's month, whatever owner or scope narrows
    # the list. The download (`chunk`) has no header, so it skips the walk.
    # "in" and "net" stay the ledger's figure (month_flows), the one the
    # Month lens hero, the Year cells, Cash Flow and the monthly email all
    # count by date: a paycheck dated the 1st that the plan counts in the
    # month before (a weekend payday) would otherwise sit in two different
    # months on two pages. "expected" is the plan's still-due half.
    in_expected = None
    in_sum, net_sum = flows["in"], flows["net"]
    if (yy, mm) == (today.year, today.month) and not chunk:
        from ..engine import budget as _budget
        in_expected = _budget.expected_income_remaining(
            conn, _budget.load_config(conn), today)
    listed = data.listed_spend(conn, out)
    return {"mode": "month", "year": y or today.year,
            "month": m or today.month, "rows": out,
            "out_sum": flows["out"], "in_sum": in_sum,
            "in_expected": in_expected,
            "net_sum": net_sum,
            # the month's spending per day so far (every day, once closed)
            "per_day": per_day, "per_day_days": per_day_days,
            # what the LISTED rows spent, by the rule the day totals and
            # the verdict use (each row's spend_amount: card payments,
            # transfers and money in count nothing) — the line a month
            # narrowed by a lens (whose money, business, reimbursements)
            # prints beside its count
            # — a business's spending instead when every listed row is a
            # business's (data.listed_spend; `listed_scope` says which)
            "listed_spend": listed["amount"],
            "listed_scope": listed["scope"],
            # each day's spending over ALL of that day's listed rows, so a
            # day divider "show more" has cut reads the whole day
            "day_spend": data.day_spend(out),
            # zero under the combined toggle: the rows ARE household
            # money then, and the label the client hangs on this says
            # otherwise
            "biz_count": 0 if combined else
                         sum(1 for r in out if r.get("entity")),
            "ranges": _ranges(conn, today, chunk)}


def _flow_shape(f: dict | None) -> dict | None:
    """reporting.flows' figures under the names TxnPage.flow carries."""
    if f is None:
        return None
    return {"money_out": f["out"], "money_in": f["in"], "net": f["net"]}


def _ranges(conn, today, chunk: int) -> dict:
    """The range buttons' dates (reporting.ledger_ranges); a download has
    no buttons, so it skips them."""
    if chunk:
        return {}
    from ..engine import reporting as _reporting
    return _reporting.ledger_ranges(conn, today)


# Every door naming a transaction takes its id as a `path` segment: a
# file-import or bridge id carries the source's own reference, which can
# hold a '/', and the server decodes the clients' %2F before it matches a
# route — a plain segment would stop at the slash and 404 the request.
@router.post("/transactions/{txn_id:path}/category")
@documented(response=TxnCategorySet, body=TxnCategory)
def set_category(txn_id: str, user: dict = Depends(_user()),
                 body: Fields = Depends(json_fields)):
    category = _category_name(body, "category", user=user)
    # "one" (default) corrects THIS row only and writes no rule; "all" also
    # teaches the merchant, which is what the Rules page then shows. An
    # unconditional "all" would let one correction on a generic payee
    # ("Check Paid") claim every row sharing it.
    scope = body.text("scope", default="one").lower()
    if scope not in ("one", "all"):
        raise HTTPException(400, "scope must be 'one' or 'all'")
    if scope == "all":
        # "all" teaches the merchant — the same class of merchant-wide
        # rewrite as the history page's bulk set and the rules editor, and
        # demo-denied with them (shared tenant, hourly reset; a write
        # reshaping a whole merchant's history for every visitor in
        # between is the kind the instance does not host). The single-row
        # correction stays open: demo is fully editable row by row.
        demoguard.deny(user)
    conn = _conn(user)
    try:
        # the row is checked and written under the shared pairing lock, so
        # a settlement cannot retire it in between (see _live_txn_for_edit)
        with conn.transaction():
            txn_id = _live_txn_for_edit(conn, txn_id)
            exists = txn_id and conn.execute(
                "SELECT COALESCE(category_override, category_primary) AS cat "
                "FROM transactions WHERE id=%s", (txn_id,)).fetchone()
            if not exists:
                raise HTTPException(404, "transaction not found")
            res = None
            if category:
                res = data.set_category(conn, txn_id, category, scope=scope)
            else:
                data.clear_category(conn, txn_id)
        _activity.record(
            conn, user, "category", "set" if category else "cleared",
            target=txn_id, label=_activity.txn_label(conn, txn_id),
            detail={"before": exists["cat"], "after": category or None,
                    "scope": scope,
                    "merchant": (res or {}).get("merchant") if res else None})
        # `rule_written` reports what happened: an "all" on a flow category or
        # a merchant-less row writes nothing, so `scope == "all"` alone would
        # claim a write that never happened. When a rule IS
        # written, `undo` is the same opaque reference the history page's
        # bulk write returns — POST it back to
        # /bills/merchant-category/undo to reverse the teach (the per-row
        # override itself stays; it was the user's direct answer).
        return {"ok": True, "category": category or None,
                "scope": scope, "rule_written": bool(res),
                "undo": (res or {}).get("undo")}
    finally:
        conn.close()


def _split_parts(body: Fields) -> list | None:
    """A split's parts, read through the typed accessors and handed to
    splits.validate as the plain [{category, amount}] it checks.

    The engine owns every rule (two parts at least, cents adding up, no
    flow category) and says which part broke one; the reader here only
    keeps a wrong type from reaching it. An amount is a JSON number, as
    both clients send it: a string is refused as the engine always
    refused it, rather than parsed as the money reader would."""
    from ..engine import splits as _splits
    parts = body.list_of(
        "parts", Fields, _splits.MAX_PARTS,
        msg=f"parts must be a list of at most {_splits.MAX_PARTS} parts, "
            f"each an object")
    if parts is None:
        return None
    out = []
    for i, p in enumerate(parts, 1):
        need = f"part {i} needs an amount"
        try:
            as_text = p.text("amount", strip=False)
        except HTTPException:
            as_text = None      # not a string: money() reads or refuses it
        if as_text:
            raise HTTPException(400, need)
        out.append({
            "category": p.text("category", strip=False,
                               msg=f"part {i} needs a category"),
            "amount": p.money("amount", msg=need)})
    return out


# one charge, several categories — the parts a person wrote for a single
# ledger row (engine/splits.py holds the rules). PUT replaces, DELETE
# removes; the ledger row carries the result as `split`, so a client that
# refetches its list sees it, and the category rollups read the parts.
@router.put("/transactions/{txn_id:path}/split")
@documented(response=SplitSaved, body=SplitInput)
def set_split_api(txn_id: str, user: dict = Depends(_user()),
                  body: Fields = Depends(json_fields)):
    _may_edit(user)
    from ..engine import splits as _splits
    parts = _split_parts(body)
    conn = _conn(user)
    try:
        try:
            # under the pairing lock, onto the live row (live_edit_ids)
            with conn.transaction():
                txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id,
                                                                 txn_id)
                clean = _splits.set_split(conn, txn_id, parts)
        except LookupError:
            raise HTTPException(404, "transaction not found")
        except _splits.SplitError as e:
            raise HTTPException(400, str(e))
        _activity.record(conn, user, "split", "set", target=txn_id,
                         label=_activity.txn_label(conn, txn_id),
                         detail={"parts": clean})
        return {"ok": True, "split": clean}
    finally:
        conn.close()


@router.delete("/transactions/{txn_id:path}/split")
@documented(response=SplitSaved)
def clear_split_api(txn_id: str, user: dict = Depends(_user())):
    _may_edit(user)
    from ..engine import splits as _splits
    conn = _conn(user)
    try:
        # under the pairing lock, onto the live row (live_edit_ids)
        with conn.transaction():
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id)
            if not txn_id:
                raise HTTPException(404, "transaction not found")
            had = _splits.clear_split(conn, txn_id)
            if had and had != "user":
                # the parts were a store receipt's reading, and the store
                # matcher writes them again on its next run unless the row
                # reads as answered: pin the row's category as the person's
                # (the whole charge under it), exactly what removing a
                # split means. Clearing that pin later brings the parts back.
                cat = conn.execute(
                    "SELECT COALESCE(category_override, category_primary) AS c "
                    "FROM transactions WHERE id = %s", (txn_id,)).fetchone()
                if cat and cat["c"]:
                    data.set_category(conn, txn_id, cat["c"])
        if had:
            _activity.record(conn, user, "split", "cleared", target=txn_id,
                             label=_activity.txn_label(conn, txn_id))
        return {"ok": True, "split": None}
    finally:
        conn.close()


# household ownership attribution — set an account's default owner or
# a per-transaction override, and list the owners in use for the filter axis.
# Owner-only (a write). Attribution only — never touches the verdict.

@router.get("/owners")
@documented(response=Owners)
def owners_api(user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        return {"owners": data.list_owners(conn)}
    finally:
        conn.close()


@router.post("/accounts/{account_id}/owner")
@documented(response=AccountOwnerSet, body=OwnerInput)
def set_account_owner_api(account_id: str, user: dict = Depends(_user()),
                          body: Fields = Depends(json_fields)):
    _may_edit(user)
    owner = _label_field(body, "owner")
    conn = _conn(user)
    try:
        n = data.set_account_owner(conn, account_id, owner or None)
        if not n:
            raise HTTPException(404, "account not found")
        return {"ok": True, "owner": owner or None}
    finally:
        conn.close()


@router.post("/transactions/bulk",
             dependencies=[Depends(limit("txn_bulk", 60, 3600))])
@documented(response=TxnBulkDone, body=TxnBulk)
def transactions_bulk(user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    """One action across an explicitly-selected set of transactions.

    Distinct from the merchant-wide write on purpose. That one infers a rule
    from a single correction and therefore refuses to touch transfers and
    income — a rule that reclassified money movement as spending would
    double-count it everywhere. Here the user picked the rows, which is
    consent to exactly those rows, and no rule is written, so nothing
    propagates forward or onto the Rules page.

    The response says what actually happened rather than reporting success:
    how many moved, how many ids no longer exist, and how many were flow rows
    — that last so the caller can point out that a transfer just became
    spending, which is a real consequence of a legitimate request.
    """
    demoguard.deny(user)
    _may_edit(user)
    # "Select all shown" on an unpaged view (a merchant's whole history, a
    # month shown in full) routinely selects well over a thousand rows, so
    # the cap sits far above what a screen shows rather than at it.
    ids = body.list_of("ids", str, BULK_MAX_IDS, blank_ok=True,
                       msg=f"ids must be a list of at most "
                           f"{BULK_MAX_IDS}") or []
    action = body.text("action", strip=False)
    category = _category_name(body, "category", user=user) \
        if action == "category" else ""
    ids = list(dict.fromkeys(ids))
    conn = _conn(user)
    try:
        # A bulk set offers only names that already exist: the standard
        # set and the household's own categories (a new custom name is made
        # on one row, through the row's own picker). Ten thousand rows of a
        # name nobody picked from a list is a typo or a flood, either way
        # not something to stamp across a selection.
        if category:
            from ..engine import categories as _cat
            if category not in _cat.PLAID_PRIMARIES \
                    and category not in data.all_categories(conn):
                raise HTTPException(
                    400, "not a known category — pick one of the standard "
                         "categories or one of your own")
        out = {"applied": 0, "missing": 0, "flow": 0}
        try:
            # Worked through in slices so no single statement carries an
            # unbounded id array, but inside ONE transaction: the person
            # chose one act over one selection, and a failure part-way
            # must leave none of it applied rather than an arbitrary prefix.
            with conn.transaction():
                # each id resolved under the pairing lock to the row it now
                # stands as: a pending hold a settlement retired is edited
                # through its posted row, and none lands on a hidden hold
                if action in ("reimb_flag", "reimb_unflag"):
                    # flagging takes the pairing lock exclusively per row;
                    # take it exclusive up front, because upgrading the
                    # shared hold below deadlocks two concurrent bulk flags
                    # (each holds it shared and waits for the other)
                    from ..sync.reanchor import hold_pairing_lock_for_request
                    hold_pairing_lock_for_request(conn)
                live = data.live_edit_ids(conn, ids)
                ids = list(dict.fromkeys(live.get(i, i) for i in ids))
                # Sliced in the database's id order, not the client's: each
                # slice locks its rows by id, and the other writers over many
                # rows do too, so only a selection walked in that one order
                # across its slices waits for them instead of crossing them.
                # Sorted by Postgres, not Python: ids are mixed case and the
                # two collations disagree.
                ids = [r["u"] for r in conn.execute(
                    "SELECT u FROM unnest(%s::text[]) AS u ORDER BY u",
                    (ids,)).fetchall()]
                for at in range(0, len(ids), _BULK_SLICE):
                    part = data.bulk_apply(conn, ids[at:at + _BULK_SLICE],
                                           action, category)
                    for k in out:
                        out[k] += int(part.get(k) or 0)
                if not ids:
                    # still validates the action and category
                    data.bulk_apply(conn, [], action, category)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except psycopg.errors.DeadlockDetected:
            # another edit crossed this selection and Postgres picked this
            # side to roll back; nothing was applied and a retry succeeds
            raise HTTPException(409, "another edit was changing some of "
                                     "these rows — nothing was changed, "
                                     "try again")
        if out.get("applied"):
            _log_bulk(conn, user, action, category, ids, out)
        return {"ok": True, **out}
    finally:
        conn.close()


BULK_MAX_IDS = 10_000
_BULK_SLICE = 1_000


@router.post("/transactions/retire-pending")
@documented(response=PendingRetired, body=RetirePending)
def retire_pending(user: dict = Depends(_user()),
                   body: Fields = Depends(json_fields)):
    """Retire a pending authorization the bank abandoned.

    The connectors retire the holds their own source stops carrying; a hold
    on a file-imported account, or on a connection that has since died, has
    nobody to do that and counts as spend forever. This is the person's
    answer to that row — and only to that row: it refuses anything already
    posted, and anything still inside the two-week window, because a hold
    that is merely slow would come back as a duplicate once it settled.
    """
    demoguard.deny(user)
    _may_edit(user)
    txn_id = body.text("id")
    if not txn_id:
        raise HTTPException(400, "id is required")
    conn = _conn(user)
    try:
        try:
            data.retire_pending(conn, txn_id)
        except LookupError:
            raise HTTPException(404, "transaction not found")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "id": txn_id}
    finally:
        conn.close()


# ---- merchants: the one place a person can correct merchant identity ----
# Every naming rule in the app is deliberately conservative, because a wrong
# answer the person looking at it cannot fix is worse than a missed merge.
# These three endpoints are what let the rules relax: a correction here
# outranks every automatic layer and is itself undoable.


@router.get("/merchants/catalog")
@documented(response=MerchantCatalog)
def merchants_catalog(user: dict = Depends(_user()), q: str = "",
                      limit: int = 200, order: str = "count",
                      sort: str | None = None, filter: str = "all",
                      offset: int = 0):
    """Merchants as the ledger shows them, with the raw strings underneath.

    Distinct from GET /merchants, which is the bill picker's flat list of
    known names — this one carries the counts, totals and variants the
    merchants screen needs.

    The variants are the point: a merchant that looks wrong is usually
    several source strings that should be one, or one that should be
    several.

    `order` is "count" (busiest first) or "alpha". The list is capped, and
    under count-order the cap cuts exactly the long-tail merchants most
    likely to need correction, so the screen needs a way to walk the tail."""
    from ..engine import merchant_dedup, merchant_merge
    # Validated here rather than letting the engine's ValueError escape: an
    # unknown sort is a bad request, and unhandled it would have been a 500.
    if order not in ("count", "alpha"):
        raise HTTPException(400, "order must be 'count' or 'alpha'")
    conn = _conn(user)
    try:
        try:
            # filter and sort apply before the cap, so a filtered view
            # reaches the long tail the cap would otherwise cut
            rows = merchant_dedup.listing(conn, q, min(500, max(1, limit)),
                                          order=order, sort=sort,
                                          filter=filter, offset=offset)
        except ValueError as e:
            raise HTTPException(400, str(e))
        recent = conn.execute(
            "SELECT id, raw_merchant, from_canonical, to_canonical, at "
            "FROM merchant_renames ORDER BY at DESC LIMIT 20").fetchall()
        return {"merchants": [merchant_dedup.listing_words(r)
                              for r in rows],
                "recent": [{**r, **merchant_dedup.rename_words(r)}
                           for r in recent],
                # merchants the ledger thinks are one business, for the
                # person to merge or keep apart — never merged unasked
                "merges": merchant_merge.pending(conn)}
    finally:
        conn.close()


@router.post("/merchants/merge")
@documented(response=MerchantMerged, body=MerchantMergeAnswer)
def merchant_merge_api(user: dict = Depends(_user()),
                       body: Fields = Depends(json_fields)):
    """Decide a merge proposal: approve merges through the same journalled
    rename the Merchants page uses (so it shows under Recent changes and
    undo works unchanged); reject keeps the two apart and the pair is never
    offered again. `bulk` is "Merge all": an offer an earlier approve in
    the batch settled or re-pointed into a pair that only shares a word
    answers 409, for the batch to skip. `into_id` is the survivor the
    person was shown: an offer a sync turned since then answers 409, for
    the client to list it again. Without it (an older app) an offer never
    turned is decided as stored and a turned one answers 409, because
    which way it was shown cannot be told."""
    from ..engine import merchant_merge
    demoguard.deny(user)
    _may_edit(user)
    bulk = bool(body.flag("bulk", False))
    action = body.text("action")
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    pid = body.text("pid")
    if not pid:
        raise HTTPException(400, "pid required")
    shown_into = body.text("into_id", max_len=64) or None
    conn = _conn(user)
    try:
        r = merchant_merge.decide(conn, pid, action, bulk=bulk,
                                  shown_into=shown_into)
        if not r.get("error"):
            _log_merge(conn, user, pid, action, r)
    finally:
        conn.close()
    if r.get("error"):
        raise HTTPException(409 if r.get("conflict") else 400, r["error"])
    return {"ok": True, **r}










@router.get("/merchants/detail")
@documented(response=MerchantDetail)
def merchants_detail(display: str, user: dict = Depends(_user())):
    """Everything about one merchant as the ledger shows it — the
    expandable row on the merchants screen (facts, category, places)."""
    from ..engine import merchant_dedup
    # read path: no length cap — see _merchant_name_or_400
    _merchant_name_or_400("display", display, max_len=None)
    conn = _conn(user)
    try:
        d = merchant_dedup.detail(conn, display)
        if d is None:
            raise HTTPException(404, f"no merchant displaying as {display!r}")
        return d
    finally:
        conn.close()


@router.post("/merchants/rename")
@documented(response=MerchantRenamed, body=MerchantRenameInput)
def merchant_rename(user: dict = Depends(_user()),
                    body: Fields = Depends(json_fields)):
    """Rename a merchant, or merge it into another by naming that one.

    One endpoint for both because they are one act: pointing the source
    strings at a name. Whether that name already exists is a fact about the
    rest of the ledger, not a different operation."""
    from ..engine import merchant_dedup
    demoguard.deny(user)
    _may_edit(user)
    display = body.text("display")
    to = body.text("to")
    if not display or not to:
        raise HTTPException(400, "display and to are both required")
    # The source is read the way the detail read reads it — uncapped: an
    # imported descriptor over the cap is a real name the catalog lists and
    # opens, and the long ones are exactly those that most need renaming.
    # An unknown source is a 404 below. The TARGET is written into
    # merchant_canonical for every variant, journalled once per variant and
    # rendered on every surface, so a new name keeps the cap; a target the
    # ledger already displays writes nothing new (a merge into it), so its
    # length is not held against it.
    _merchant_name_or_400("display", display, max_len=None)
    _merchant_name_or_400("to", to, max_len=None)
    conn = _conn(user)
    try:
        if len(to) > 200 and not merchant_dedup.raw_strings_for(conn, to):
            _merchant_name_or_400("to", to)
        # a rename onto a name the ledger already shows is a merge: read
        # that before the act, for the sentence
        _into_exists = conn.execute(
            "SELECT 1 FROM merchants WHERE name=%s AND merged_into IS NULL "
            "LIMIT 1", (to,)).fetchone() is not None
        with conn.transaction():
            result = merchant_dedup.rename(conn, display, to)
        if not result["raws"]:
            raise HTTPException(404, f"no merchant displaying as {display!r}")
        _activity.record(conn, user, "merchant",
                         "merged" if _into_exists and to != display else "renamed",
                         target=to, label=display,
                         detail={"before": display, "after": to,
                                 "rows": result.get("rows")})
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/merchants/undo")
@documented(response=MerchantUndone, body=MerchantUndo)
def merchant_undo(user: dict = Depends(_user()),
                  body: Fields = Depends(json_fields)):
    """Replay one journalled naming change backwards."""
    from ..engine import merchant_dedup
    demoguard.deny(user)
    _may_edit(user)
    # the clients echo the id the catalog's `recent` gave them, a JSON
    # number; digits in a string and a whole-valued float have always been
    # read as that number too
    change_id = body.int_in("id", text_ok=True, truncate=True,
                            msg="id is required")
    if change_id is None:
        raise HTTPException(400, "id is required")
    # journal ids are bigserial; a JSON number past bigint would only blow
    # up inside the driver as an out-of-range bind — an opaque 500 — so
    # bound it here where it can be a 400
    if not 1 <= change_id <= 2 ** 62:
        raise HTTPException(400, "id out of range")
    conn = _conn(user)
    try:
        with conn.transaction():
            out = merchant_dedup.undo(conn, change_id)
        _activity.record(conn, user, "merchant", "undone",
                         target=str(out.get("restored") or ""),
                         label=str(out.get("restored") or out.get("raw_merchant")
                                   or f"change {change_id}"))
        return out
    except LookupError as e:
        raise HTTPException(404, str(e))
    finally:
        conn.close()


# The pairing lock in SHARED mode: person-written edits on one row run side
# by side, and wait only for a writer holding it exclusively — a feed
# settling a pending hold into its posted row, which carries the hold's
# note and category across and retires it in one commit.
def _live_txn_for_edit(conn, txn_id: str) -> str | None:
    """Inside the caller's transaction: take the shared pairing lock, then
    name the live row an edit of `txn_id` belongs on — the row itself, or,
    when a settlement has just retired it, the posted row that names it as
    its pending authorization. None when there is neither.

    Checked and written under the lock: read before it, a settlement could
    commit between the check and the write, and the note or category would
    land on the retired hold after its carry had already run — hidden, with
    the posted row showing nothing the person wrote."""
    return data.live_edit_ids(conn, [txn_id]).get(txn_id)


@router.post("/transactions/{txn_id:path}/note")
@documented(response=TxnNoteSaved, body=TxnNote)
def set_txn_note(txn_id: str, user: dict = Depends(_user()),
                 body: Fields = Depends(json_fields)):
    """Set (or clear, when empty) a free-text note on a transaction."""
    demoguard.deny(user)
    _may_edit(user)
    # set_note trims and caps it
    note_in = body.text("note", strip=False)
    conn = _conn(user)
    try:
        with conn.transaction():
            txn_id = _live_txn_for_edit(conn, txn_id)
            if not txn_id:
                raise HTTPException(404, "transaction not found")
            prev = conn.execute(
                "SELECT note FROM transaction_notes WHERE txn_id=%s",
                (txn_id,)).fetchone()
            prev = prev["note"] if prev else ""
            note = data.set_note(conn, txn_id, note_in)
        if note != prev:
            _activity.record(
                conn, user, "note",
                "cleared" if not note else ("edited" if prev else "added"),
                target=txn_id, label=_activity.txn_label(conn, txn_id),
                detail={"before": prev or None, "after": note or None})
        return {"ok": True, "note": note}
    finally:
        conn.close()


@router.post("/transactions/{txn_id:path}/check-payee")
@documented(response=CheckPayeeSaved, body=CheckPayee)
def set_check_payee(txn_id: str, user: dict = Depends(_user()),
                    body: Fields = Depends(json_fields)):
    """The answer to a check's "who was this to?": written as the first
    line of the row's note ("paid to …", ahead of any note it already
    had; a first line this door wrote before is replaced), then run through the check-image machinery, which files the
    check under the payee's category when a rule or a model can say it.
    The note is what keeps the answer: every categorization pass files the
    check again from it (specs/ledger-figures.md)."""
    demoguard.deny(user)
    _may_edit(user)
    payee = body.text("payee", 120)
    if not payee:
        raise HTTPException(400, "payee is required")
    conn = _conn(user)
    try:
        with conn.transaction():
            txn_id = _live_txn_for_edit(conn, txn_id)
            row = txn_id and conn.execute(
                "SELECT check_number FROM transactions WHERE id=%s",
                (txn_id,)).fetchone()
            if not row:
                raise HTTPException(404, "transaction not found")
            if row["check_number"] is None:
                raise HTTPException(400, "not a check")
            # the note row held for the read-and-prepend: a note door
            # saving at the same moment is either seen here or waits —
            # never overwritten by an answer built from what it replaced.
            # A row inserted for the hold is overwritten below.
            # one statement both creates the row if absent and locks the
            # one that is there, so a note cleared in between cannot leave
            # nothing to read
            before = conn.execute(
                "INSERT INTO transaction_notes (txn_id, note) VALUES (%s,'') "
                "ON CONFLICT (tenant_id, txn_id) "
                "DO UPDATE SET note = transaction_notes.note "
                "RETURNING note", (txn_id,)).fetchone()["note"]
            from ..engine import llm_categorize as _llm
            note = _llm.with_answer(before, payee)
            if len(note) > data.NOTE_MAX:
                raise HTTPException(
                    400, "the check's note is too long to add the answer "
                         "to — shorten the note first")
            note = data.set_note(conn, txn_id, note)
        if note != before:
            _activity.record(
                conn, user, "note", "edited" if before else "added",
                target=txn_id, label=_activity.txn_label(conn, txn_id),
                detail={"before": before or None, "after": note})
        # Outside the note's transaction, so a sync may re-stamp the row
        # in between; categorize_check_payee files a re-stamped check too,
        # and the next pass re-derives it from the note either way.
        category = _llm.categorize_check_payee(conn, txn_id, payee)
        # a later answer may have landed while this one was being classified
        # (another tab, a retry): the note that stands decides, so file the
        # check by it again, from the rule cache, if it names someone else
        cur = conn.execute(
            "SELECT note FROM transaction_notes WHERE txn_id=%s",
            (txn_id,)).fetchone()
        standing = _llm.answered_payee((cur or {}).get("note"))
        if standing and standing != payee:
            category = _llm.categorize_check_payee(conn, txn_id, standing,
                                                   classify=False)
        return {"ok": True, "note": note, "category": category}
    finally:
        conn.close()


@router.post("/transactions/{txn_id:path}/owner")
@documented(response=TxnOwnerSet, body=OwnerInput)
def set_txn_owner_api(txn_id: str, user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    _may_edit(user)
    owner = _label_field(body, "owner")
    conn = _conn(user)
    try:
        # under the pairing lock, onto the live row: a settlement retiring
        # the hold between the check and the write would strand the owner
        # on the hidden hold (see _live_txn_for_edit)
        with conn.transaction():
            txn_id = _live_txn_for_edit(conn, txn_id)
            n = txn_id and data.set_txn_owner(conn, txn_id, owner or None)
            if not n:
                raise HTTPException(404, "transaction not found")
        return {"ok": True, "owner": owner or None, "id": txn_id}
    finally:
        conn.close()


@router.get("/categories")
@documented(response=Categories)
def categories(user: dict = Depends(_user())):
    """``categories`` = what this tenant actually uses (filter dropdowns);
    ``plaid_spend``/``plaid_flow`` = the standard Plaid primaries, for the
    pickers that SET a category rather than filter by one."""
    from ..engine import categories as cat
    conn = _conn(user)
    try:
        return {"categories": data.all_categories(conn),
                "plaid_spend": list(cat.PLAID_SPEND),
                "plaid_flow": list(cat.FLOW)}
    finally:
        conn.close()


@router.get("/merchants")
@documented(response=Merchants)
def merchants_list_api(user: dict = Depends(_user()), limit: int = 1000):
    """Distinct display-merchant names the tenant has transactions for
    (canonical-resolved via merchant_canonical), most-frequent first. Powers
    the recurring "Add a bill" merchant picker so a manual bill's match key
    lines up with the ledger's real merchants and matches future txns.
    Tenant-scoped by RLS on the _conn."""
    from ..engine import merchant_sql
    limit = max(1, min(int(limit or 1000), 2000))
    conn = _conn(user)
    try:
        rows = conn.execute(
            # The ledger's own display names, through the shared display
            # join: the bill door checks a chip against the merchants
            # table, so the picker must offer the same names, never a raw
            # string the resolver has cleaned. Keyed on the identity key
            # (outlet first) like every other surface — retyped from
            # merchant_name it offered a fuel arm's rows under the parent
            # brand, a name the bill would then never match.
            f"""SELECT {merchant_sql.DISPLAY_MERCHANT} AS merchant,
                       count(*) AS n
                 FROM transactions t
                 {merchant_sql.MC_JOIN}
                WHERE t.removed = 0
                  AND {merchant_sql.RAW_KEY} IS NOT NULL
                  AND {merchant_sql.RAW_KEY} <> ''
                GROUP BY 1
                ORDER BY n DESC, merchant
                LIMIT %s""", (limit,)).fetchall()
        return {"merchants": [r["merchant"] for r in rows]}
    finally:
        conn.close()


# the learned-rules surface — see every categorization rule the
# system applies + its provenance, and correct / disable / delete any of
# them (never hand-author). Viewing is read for anyone; edits are owner-only.

@router.get("/rules")
@documented(response=RulesPage)
def rules_list_api(user: dict = Depends(_user()), source: str = "",
                   q: str = "", page: int = 1):
    """Provenance-grouped, lazily-loaded rules — filter by source
    (user/llm/seed/model), search merchant+category, ~50/page. `counts` carries
    per-source totals for the collapsed group headers."""
    if source and source not in data.RULE_SOURCES:
        raise HTTPException(400, "source must be user, llm, seed, or model")
    # bound like _ck_ym bounds y/m: an unbounded page multiplies into an
    # OFFSET past bigint inside the driver (NumericValueOutOfRange → 500)
    page = min(max(1, page), 10_000_000)
    conn = _conn(user)
    try:
        return data.list_rules(conn, source=source or None, q=q, page=page)
    finally:
        conn.close()




@router.post("/rules/set")
@documented(response=RuleSet, body=RuleInput)
def rules_set_api(user: dict = Depends(_user()),
                  body: Fields = Depends(json_fields)):
    # the rules editor's set IS set_merchant_category — the same
    # merchant-wide rewrite the /bills/merchant-category doors deny on the
    # demo instance, so it is denied here too (an open spelling of a
    # closed door is no guard at all)
    demoguard.deny(user)
    _may_edit(user)
    merchant = body.text("merchant")
    category = _category_name(body, "category", user=user)
    if not merchant or not category:
        raise HTTPException(400, "merchant and category required")
    conn = _conn(user)
    try:
        # this one rule, not the history page's token family: the key is
        # the rule's stored merchant, whose word match can reach other
        # merchants' rows and teach them too
        out = data.set_merchant_category(conn, merchant, category,
                                         family=False)
        _activity.record(conn, user, "rule", "set",
                         target=out.get("merchant") or merchant,
                         label=out.get("merchant") or merchant,
                         detail={"after": category, "count": out.get("count")})
        return out
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/categories/rename")
@documented(response=CategoryRenamed, body=CategoryRename)
def categories_rename_api(user: dict = Depends(_user()),
                          body: Fields = Depends(json_fields)):
    """Rename a free-form / custom category everywhere it appears.

    Owner-only. Rewrites transaction overrides + primaries, merchant rules,
    manual_categories, and budget custom_buckets that use the old string.
    Standard Plaid primaries cannot be the *old* name (custom labels only).
    """
    demoguard.deny(user)
    _may_edit(user)
    # `from`/`to` are older spellings of `old`/`new`, read when the newer
    # key is absent or blank
    old = body.text("old") or body.text("from")
    new = _new_category_or_400(body.text("new") or body.text("to"), user)
    if not old or not new:
        raise HTTPException(400, "old and new names are required")
    conn = _conn(user)
    try:
        out = data.rename_category(conn, old, new)
        _activity.record(conn, user, "category", "renamed", target=new,
                         label=new, detail={"before": old, "after": new})
        return out
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/rules/disable")
@documented(response=Ok, body=RuleDisable)
def rules_disable_api(user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    _may_edit(user)
    # same class of tenant-wide rule rewrite as /rules/set — the shared
    # demo login must not corrupt every visitor's rules until the reset
    demoguard.deny(user)
    merchant = body.text("merchant")
    if not merchant:
        raise HTTPException(400, "merchant required")
    # absent or null re-enables, as it always has
    disabled = bool(body.flag("disabled", default=False))
    conn = _conn(user)
    try:
        data.set_rule_disabled(conn, merchant, disabled)
        _activity.record(conn, user, "rule",
                         "disabled" if disabled else "enabled",
                         target=merchant, label=merchant)
        return {"ok": True}
    finally:
        conn.close()


@router.post("/rules/delete")
@documented(response=RuleDeleted, body=RuleMerchant)
def rules_delete_api(user: dict = Depends(_user()),
                     body: Fields = Depends(json_fields)):
    _may_edit(user)
    demoguard.deny(user)  # see /rules/disable
    merchant = body.text("merchant")
    if not merchant:
        raise HTTPException(400, "merchant required")
    conn = _conn(user)
    try:
        n = data.delete_rule(conn, merchant)
        if n:
            _activity.record(conn, user, "rule", "deleted",
                             target=merchant, label=merchant)
        return {"ok": True, "deleted": n}
    finally:
        conn.close()
