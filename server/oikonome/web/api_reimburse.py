"""Reimbursement pairing.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from . import data
from ..engine import activity as _activity
from .api_common import _conn, _ser, _user, router
from .fields import (Fields, json_fields,
                     json_fields_or_empty as _json_fields_or_empty)
from .schemas import documented
from .schemas.reimburse import (ReimbCandidates, ReimbFlag, ReimbFlagged,
                                ReimbLink, ReimbLinked, ReimbPairsPage,
                                ReimbPending, ReimbSelection, ReimbUnflagged,
                                ReimbUnlink, ReimbUnlinked)
from .security import limit


@router.get("/reimburse/pending")
@documented(response=ReimbPending)
def reimburse_pending(user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        from ..engine import alerts
        rows = [_ser(r) for r in data.pending_reimbursements(conn)]
        today = data.household_today(conn)
        for r in rows:
            r["received_pct"] = alerts.reimb_received_pct(
                r["amount"], r.get("expected"), r.get("received"))
            r["waited_label"] = data.waited_label(r["date"], today)
            # what the matcher counts a selection against before the
            # candidates door answers: the figure that door states
            r["left_amount"] = data.reimb_left(conn, r)
        # the page's headline: the figure the Today strip and the email
        # state (alerts.reimb_pending), and how long the oldest has waited
        oldest = min((r["date"] for r in rows), default=None)
        return {"pending": rows, "owed": alerts.reimb_owed(conn),
                "oldest_label": (data.waited_label(oldest, today)
                                 if oldest and oldest < today.isoformat()
                                 else None)}
    finally:
        conn.close()


_PAIRS_PAGE = 50


@router.get("/reimburse/pairs")
@documented(response=ReimbPairsPage)
def reimburse_pairs(user: dict = Depends(_user()), page: int = 1,
                    q: str = ""):
    """Linked pairs, newest charge first — what the undo button acts on.
    Each pair says what it nets (`amount_label`), and a deposit dated
    before its charge says how far ahead it came (`deposit_note`).

    `page` (from 1) reaches further back and `q` finds a pair by either
    side's merchant, so an older wrong match can still be undone; `more`
    says whether a further page exists."""
    page = max(1, min(int(page), 10_000))
    conn = _conn(user)
    try:
        rows = data.recent_reimbursement_pairs(
            conn, limit=_PAIRS_PAGE + 1, offset=(page - 1) * _PAIRS_PAGE,
            q=q[:200])
        return {"pairs": [{**_ser(p),
                           "amount_label": data.pair_amount_label(p),
                           "deposit_note": data.pair_deposit_note(p)}
                          for p in rows[:_PAIRS_PAGE]],
                "more": len(rows) > _PAIRS_PAGE, "page": page}
    finally:
        conn.close()


@router.get("/reimburse/{txn_id:path}/candidates")
@documented(response=ReimbCandidates)
def reimburse_candidates(txn_id: str, user: dict = Depends(_user()),
                         q: str = ""):
    conn = _conn(user)
    try:
        # a hold that settled since the list was read is answered as the
        # posted row it became (anchor.id says which, for the client)
        with conn.transaction():
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
        anchor, cands = data.reimbursement_candidates(conn, txn_id, q=q)
        if anchor is None:
            raise HTTPException(404, "transaction not found")
        pairs = data.reimbursement_pairs(conn, txn_id)
        paired = {p["other_id"] for p in pairs}
        return {"anchor": _ser(anchor),
                "pairs": [_ser(p) for p in pairs],
                "candidates": [_ser(c) for c in cands
                               if c["id"] not in paired]}
    finally:
        conn.close()


@router.post("/reimburse/link",
             dependencies=[Depends(limit("reimburse_link", 60, 3600))])
@documented(response=ReimbLinked, body=ReimbLink)
def reimburse_link(user: dict = Depends(_user()),
                   body: Fields = Depends(json_fields)):
    # ids exactly as the lists gave them (a file-import id is the source's
    # own reference); every id fans out into per-row DB work on this one
    # held connection, so the client-controlled list gets the same cap and
    # throttle as the sibling bulk endpoint
    txn_id = body.text("txn_id", strip=False)
    other_ids = body.list_of("other_ids", str, 1000, blank_ok=True,
                             msg="other_ids must be a list of at most 1000")
    partial = body.flag("partial", default=False)
    if not txn_id or not other_ids:
        raise HTTPException(400, "txn_id and other_ids are required")
    conn = _conn(user)
    try:
        out = data.link_reimbursements(conn, txn_id, other_ids,
                                       partial=partial)
        # the log says what happened, not what was asked: a request whose
        # every pair was refused linked nothing, and a full link the server
        # made partial is recorded as partial
        # the anchor the links went on: a hold that settled since the list
        # was read is linked as its posted row, and named so in the answer
        with conn.transaction():
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
        out["txn_id"] = txn_id
        if out["linked"] > 0:
            _activity.record(conn, user, "reimbursement", "linked",
                             target=txn_id,
                             label=_activity.txn_label(conn, txn_id),
                             detail={"count": out["linked"],
                                     "partial": partial
                                     or bool(out.get("partial"))})
        return out
    finally:
        conn.close()


# The selection door tries the real link under the household's pairing
# lock and rolls it back, so each call costs what a Pair press costs.
# A person ticks deposits by hand: past this many the matcher's line is
# not worked out at all (the answer carries no line, and both clients
# print none), so a long list cannot hold the lock for the whole request.
SELECTION_MAX = 50
# Per member, beside the per-IP figure: one member looping the door from
# many addresses would otherwise hold the household's pairing lock against
# every real link. Both clients wait for a pause in the ticking before
# asking, so a person matching deposits for an hour asks a few dozen
# times; 240 an hour (one every 15 seconds, sustained) is out of an
# honest reach and still a quarter of what the per-IP figure allows.
SELECTION_PER_USER = (240, 3600)


@router.post("/reimburse/selection",
             dependencies=[Depends(limit("reimburse_selection", 1200, 3600))])
@documented(response=ReimbSelection, body=ReimbLink)
def reimburse_selection(user: dict = Depends(_user()),
                        body: Fields = Depends(json_fields)):
    """The matcher's line beside Pair for the rows ticked so far: the
    link tried and rolled back, so the figures are the ones Pair makes.
    More than 50 rows ticked is answered with no line (`line` null)."""
    from .security import account_limit_or_429
    account_limit_or_429(("reimburse_selection", str(user["user_id"])),
                         *SELECTION_PER_USER)
    txn_id = body.text("txn_id", strip=False)
    other_ids = body.list_of("other_ids", str, 1000, blank_ok=True,
                             msg="other_ids must be a list of at most 1000")
    partial = body.flag("partial", default=False)
    if not txn_id or not other_ids:
        raise HTTPException(400, "txn_id and other_ids are required")
    if len(dict.fromkeys(other_ids)) > SELECTION_MAX:
        return {"selected": None, "need": None, "line": None}
    conn = _conn(user)
    try:
        out = data.reimbursement_selection(conn, txn_id, other_ids,
                                           partial=partial)
        if not out:
            raise HTTPException(404, "transaction not found")
        return out
    finally:
        conn.close()


@router.post("/reimburse/unlink")
@documented(response=ReimbUnlinked, body=ReimbUnlink)
def reimburse_unlink(user: dict = Depends(_user()),
                     body: Fields = Depends(json_fields)):
    expense_id = body.text("expense_id", strip=False)
    reimburse_id = body.text("reimburse_id", strip=False)
    conn = _conn(user)
    try:
        # A settlement moves a pending hold's pairs onto its posted row
        # under the pairing lock, so a list read before it names the hold.
        # Resolved under that same lock (exclusive, as the unlink writes),
        # each side follows its hold to the row the pair now stands on;
        # with the hold's id the unlink would find nothing and the pair
        # would quietly stay.
        with conn.transaction():
            from ..sync.reanchor import hold_pairing_lock_for_request
            hold_pairing_lock_for_request(conn)
            live = data.live_edit_ids(conn, [expense_id, reimburse_id])
            expense_id = live.get(expense_id, expense_id)
            reimburse_id = live.get(reimburse_id, reimburse_id)
            removed = data._unlink_locked(conn, expense_id, reimburse_id)
        # logged only when a pair really went: a second tap, the other
        # device or a retry finds nothing to remove and records nothing
        if removed:
            _activity.record(conn, user, "reimbursement", "unlinked",
                             target=expense_id,
                             label=_activity.txn_label(conn, expense_id))
        return {"ok": True, "removed": bool(removed),
                "expense_id": expense_id, "reimburse_id": reimburse_id}
    finally:
        conn.close()


@router.post("/reimburse/{txn_id:path}/flag")
@documented(response=ReimbFlagged, body=ReimbFlag)
def reimburse_flag(txn_id: str, user: dict = Depends(_user()),
                   body: Fields = Depends(_json_fields_or_empty)):
    # a number or the text of one, as float() always read it; null and ""
    # are no expected amount (an integer too wide for a double is refused)
    expected = body.number("expected", msg="expected must be a number")
    # both clients send true/false; "" and null are not partial
    partial = bool(body.flag("partial", default=False, blank_ok=True))
    # NaN/Infinity parse fine and slip past the <=0 check (all NaN comparisons
    # are False), then poison the DOUBLE column and 500 every later
    # /reimburse/pending response (allow_nan=False) — reject them.
    import math
    if expected is not None and not math.isfinite(expected):
        raise HTTPException(400, "expected must be a finite number")
    from ..engine.compat import MAX_AMOUNT
    if expected is not None and expected > MAX_AMOUNT:
        raise HTTPException(400, "expected is too large")
    if expected is not None and expected <= 0:
        raise HTTPException(400, "expected must be positive")
    conn = _conn(user)
    try:
        # a hold that settled since the list was read is flagged on the
        # posted row it became, resolved under the pairing lock the
        # settlement holds (exclusive up front: the flag takes it so)
        with conn.transaction():
            from ..sync.reanchor import hold_pairing_lock_for_request
            hold_pairing_lock_for_request(conn)
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
            refused = data.flag_reimbursement(
                conn, txn_id, partial=partial, expected=expected)
        if refused == data.NO_SUCH_FLAG_TXN:
            raise HTTPException(404, refused)
        if refused:
            raise HTTPException(409, refused)
        _activity.record(conn, user, "reimbursement", "flagged", target=txn_id,
                         label=_activity.txn_label(conn, txn_id),
                         detail={"expected": expected, "partial": partial})
        return {"ok": True, "id": txn_id}
    finally:
        conn.close()


@router.post("/reimburse/{txn_id:path}/unflag")
@documented(response=ReimbUnflagged)
def reimburse_unflag(txn_id: str, user: dict = Depends(_user())):
    conn = _conn(user)
    try:
        # A settlement moves a hold's flag onto its posted row under the
        # pairing lock; resolved under it, the unflag follows the hold
        # there instead of deleting nothing and leaving the flag counted.
        with conn.transaction():
            from ..sync.reanchor import hold_pairing_lock_for_request
            hold_pairing_lock_for_request(conn)
            txn_id = data.live_edit_ids(conn, [txn_id]).get(txn_id, txn_id)
            dropped = data.unflag_reimbursement(conn, txn_id)
        # logged only when a flag really went, as unlink is: a second tap
        # or the other device finds nothing to drop and records nothing
        if dropped:
            _activity.record(conn, user, "reimbursement", "unflagged",
                             target=txn_id,
                             label=_activity.txn_label(conn, txn_id))
        return {"ok": True, "removed": bool(dropped), "id": txn_id}
    finally:
        conn.close()
