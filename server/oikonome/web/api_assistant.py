"""The assistant.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import uuid
from fastapi import Depends, HTTPException, Query
from fastapi.responses import Response

from . import demoguard
from ..engine import assistant
from .api_common import (_BODY_ROWS_MAX, _conn, _literal_true, _today, _user,
                         router)
from .fields import Fields, json_fields
from .schemas import documented
from .schemas.assistant import (AssistantAnswer, AssistantAsk,
                                AssistantCleared, AssistantForgotten,
                                AssistantHistory, AssistantReport,
                                AssistantStatus)
from .security import limit


# local conversational assistant over your own ledger. Tool-routed, and
# every tool only reads the ledger; the one row an ask writes is its own
# entry in the household's question log (assistant.record_turn, trimmed
# per asker so no one asker can push out another's). Runs on the
# household's configured LLM. Hidden when no LLM is configured.

@router.get("/assistant")
@documented(response=AssistantStatus)
def assistant_status_api(user: dict = Depends(_user())):
    from ..engine import assistant
    conn = _conn(user)
    try:
        # status plus, per task, where its data goes — the one answer every
        # "stays on this instance" line in the clients reads — from one load
        return assistant.status_view(conn)
    finally:
        conn.close()


@router.post("/assistant",
             dependencies=[Depends(limit("assistant", 30, 3600))])
@documented(response=AssistantAnswer, body=AssistantAsk)
def assistant_ask_api(user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    """Answer a question; `thread_id` in the body makes it a follow-up of
    that conversation (404 when the household has none such, 400 when it
    is not a uuid).

    The reply is the answer (`answer`, `blocks`, `tools_used`) plus what
    became of it in the log:
      `id`        the kept turn's id, or null when nothing was kept;
      `thread_id` the conversation it belongs to — its own id for a new
                  question — or null when no conversation holds it;
      `saved`     true when the turn was kept;
      `removed`   true when this was a follow-up and its conversation was
                  deleted while the answer was being written: the answer
                  still comes back (it was waited for), nothing is kept,
                  and `thread_id` is null, so a client starts the next
                  question as a new conversation rather than following
                  up on one that is gone.
    An answer that could not be kept for any other reason has `saved`
    false, and `thread_id` null on a new question (there is no
    conversation to follow up on) or the conversation's id on a
    follow-up (that conversation is still there)."""
    from ..engine import assistant
    question = body.text("question")
    if not question:
        raise HTTPException(400, "question required")
    # a follow-up names the conversation it continues; the model then
    # sees that thread's earlier turns — and only that thread's
    thread_id = body.text("thread_id", default=None, strip=False,
                          msg="bad thread id")
    if thread_id is not None:
        try:
            thread_id = str(uuid.UUID(thread_id))
        except ValueError:
            raise HTTPException(400, "bad thread id")
    conn = _conn(user)
    try:
        context: list[dict] = []
        if thread_id:
            # a view-only login continues only its own conversations: the
            # household's others are not shown to it, and the answer the
            # same question would get for an unknown thread says nothing
            # about whether one exists. Its check and its context are one
            # read, so a turn landing between them cannot slip in.
            context = (assistant.own_thread_context(
                           conn, thread_id, user.get("user_id"))
                       if _own_log_only(user)
                       else assistant.thread_context(conn, thread_id))
            if not context:
                raise HTTPException(404, "no such conversation")
        out = assistant.ask(conn, question, context=context)
        # an answered question joins the household's log; a reply that
        # could not be kept still reaches the asker, and says so — a
        # silent null thread would turn the next "follow-up" into a fresh
        # question with no context and nothing telling anyone why
        removed = False
        try:
            out["id"] = assistant.record_turn(conn, user, question, out,
                                              thread_id=thread_id)
        except assistant.ThreadRemoved:
            conn.rollback()
            out["id"], removed = None, True
        except Exception as e:                            # noqa: BLE001
            import logging
            conn.rollback()
            logging.getLogger("oikonome.api").warning(
                "assistant: answer not kept: %s", type(e).__name__)
            out["id"] = None
        out["saved"] = out["id"] is not None
        out["removed"] = removed
        out["thread_id"] = None if removed else (thread_id or out["id"])
        return out
    except HTTPException:
        raise
    except assistant.AssistantUnavailable:
        raise HTTPException(503, "the assistant needs a local LLM configured")
    except assistant.AssistantBusy:
        raise HTTPException(503, "the assistant is busy — try again in a "
                                 "moment")
    except Exception as e:                # LLM/network failure → friendly 502
        raise HTTPException(502, f"assistant error: {type(e).__name__}")
    finally:
        conn.close()


# a saved conversation keeps its newest turns: this many, and this many
# blocks in all (each block is at most a 6,000-character paragraph or a
# bounded chart), so one request cannot ask for megabytes of SVG
REPORT_TURNS = 50
REPORT_BLOCKS = 120
REPORT_TURNS_SENT_MAX = _BODY_ROWS_MAX


def _report_blocks(turn: Fields) -> list[dict]:
    """A turn's `blocks`, re-validated by assistant_blocks.clean, which
    reads the first few, keeps a text block's text and redraws every other
    kind from its spec. A `blocks` that is not a list has nothing in it to
    draw: no blocks, so the turn is left out, never refused."""
    from ..engine import assistant_blocks
    if turn.json_type("blocks") != "list":
        return []
    return assistant_blocks.clean(
        turn.opaque_rows("blocks", REPORT_TURNS_SENT_MAX))


@router.post("/assistant/report",
             dependencies=[Depends(limit("assistant_report", 60, 3600))])
@documented(body=AssistantReport)
def assistant_report_api(user: dict = Depends(_user()),
                         body: Fields = Depends(json_fields)):
    """One answer — or, with `turns` (a list of {question, blocks}), a
    whole conversation — as a self-contained HTML page (inline SVG +
    CSS, no script) to save or share. The client sends the turns back —
    nothing is stored server-side — and every block is re-validated
    here, so a client can no more smuggle markup into the file than the
    model can. A turn with nothing valid in it is left out; a report
    with none is a 400. A conversation has no turn limit, so the page
    keeps the NEWEST turns — the ones a person saving a long conversation
    came for — up to REPORT_TURNS of them and REPORT_BLOCKS blocks in
    all, and says how many earlier questions it left out; `partial` is
    the client saying the conversation's start was not loaded on its
    side either."""
    from ..engine import assistant, assistant_blocks
    # the cheap refusal first: nothing is parsed or drawn for an instance
    # with no assistant
    conn = _conn(user)
    try:
        if not assistant.available(conn):
            raise HTTPException(503, "the assistant needs a local LLM configured")
        today = _today(conn)
    finally:
        conn.close()
    turns: list[tuple[str, list[dict]]] = []
    omitted = 0
    if body.json_type("turns") == "list":
        # Only a list of turns is a conversation; anything else in `turns`
        # leaves the single-answer body below to speak, as it always has.
        # The turns are the client's own copy of a conversation with no
        # turn limit, so the bound is only the body's: the page keeps the
        # newest REPORT_TURNS of them.
        valid: list[tuple[str, list[dict]]] = []
        for t in body.opaque_rows("turns", REPORT_TURNS_SENT_MAX):
            # a turn that is not an object is skipped, never refused
            if not isinstance(t, dict):
                continue
            turn = Fields(t)
            blks = _report_blocks(turn)
            if blks:
                valid.append((turn.text("question"), blks))
        if not valid:
            raise HTTPException(400, "nothing to report")
        kept = valid[-REPORT_TURNS:]
        while len(kept) > 1 and sum(len(b) for _, b in kept) > REPORT_BLOCKS:
            kept = kept[1:]
        omitted = len(valid) - len(kept)
        turns = kept
        question = turns[0][0]
    else:
        question = body.text("question")
        blocks = _report_blocks(body)
        if not blocks:
            raise HTTPException(400, "nothing to report")
        turns.append((question, blocks))
    # only a literal true says the conversation's start was not loaded
    partial = _literal_true(body, "partial")
    note = None
    if omitted and partial:
        note = (f"{omitted} earlier question{'s' if omitted != 1 else ''} in this "
                "conversation not included, and the ones before them were not loaded")
    elif omitted:
        note = (f"{omitted} earlier question{'s' if omitted != 1 else ''} in this "
                "conversation not included")
    elif partial:
        note = "earlier questions in this conversation not included (its start was not loaded)"
    page = assistant_blocks.report_html(question, turns[0][1], today.isoformat(),
                                        turns=turns, note=note)
    what = "conversation" if len(turns) > 1 else "answer"
    name = f"oikonome-{what}-{today.isoformat()}.html"
    return Response(page, media_type="text/html; charset=utf-8",
                    headers={"content-disposition":
                             f'attachment; filename="{name}"',
                             "cache-control": "no-store"})


def _own_log_only(user: dict) -> bool:
    """Is this login shown only its own conversations? A view-only login:
    it reads the household's money, not what the household asked about
    it. The owner and a member read the whole log."""
    return user.get("role") == "viewer"


@router.get("/assistant/history")
@documented(response=AssistantHistory)
def assistant_history_api(user: dict = Depends(_user()),
                          limit: int = Query(assistant.HISTORY_PAGE),
                          before: str | None = Query(None),
                          before_id: str | None = Query(None)):
    """The household's answered questions, newest first — the owner and
    every member read the same log, so a chart drawn on the phone is on
    the web too; a view-only login is shown its own questions only.
    The shared demonstration login is shown its own address only: every
    visitor holds it, so who asked what would hand each of them the
    household's people."""
    conn = _conn(user)
    try:
        out = assistant.history(
            conn, limit=limit, before=before, before_id=before_id,
            asked_by_id=(user.get("user_id") if _own_log_only(user)
                         else None))
    except ValueError:
        raise HTTPException(400, "bad cursor")
    finally:
        conn.close()
    if demoguard.people_hidden(user):
        for t in out["turns"]:
            t["asked_by"] = demoguard.own_address_only(user, t["asked_by"])
    return out


@router.delete("/assistant/history/{turn_id}")
@documented(response=AssistantForgotten)
def assistant_forget_api(turn_id: str, user: dict = Depends(_user())):
    """Drop one turn — the question and answer with this id — and nothing
    else of its conversation. The apps already installed send a turn's
    id here to remove that one answer, so this door keeps that meaning;
    a whole conversation goes through /assistant/threads/{thread_id}.
    A turn already gone answers 200 with deleted 0, never 404: the
    installed apps read a 404 here as "could not remove it" and leave the
    row on screen, when the row is in fact gone. 400 for a bad id.

    A conversation's OPENING turn (its id is the thread's) goes only as
    the conversation's last row: while follow-ups remain, the door keeps
    it and answers 200 {ok, deleted: 0, reason}. Refused rather than
    widened to the whole conversation — the installed apps mean one answer
    by this id and expect exactly {ok, deleted}, so taking the follow-ups
    with it would remove rows nobody chose; and deleting the opening alone
    would leave those follow-ups headless."""
    # the shared demonstration login is every visitor at once: deleting
    # from the log as it would remove what other visitors asked
    demoguard.deny_shared_log(user)
    try:
        turn_id = str(uuid.UUID(str(turn_id)))
    except ValueError:
        raise HTTPException(400, "bad id")
    conn = _conn(user)
    try:
        out = assistant.forget_turn(conn, turn_id)
    finally:
        conn.close()
    return {"ok": True, **out}


@router.delete("/assistant/threads/{thread_id}")
@documented(response=AssistantCleared)
def assistant_forget_thread_api(thread_id: str,
                                user: dict = Depends(_user())):
    """Drop one conversation: every turn of the thread named, whoever in
    the household asked it — the log is the household's, read alike by
    every member, and a member may tidy it (a viewer may not: the role
    gate refuses the write). 404 when the household has no such
    conversation, 400 when the id is not a uuid."""
    demoguard.deny_shared_log(user)    # see assistant_forget_api
    try:
        thread_id = str(uuid.UUID(str(thread_id)))
    except ValueError:
        raise HTTPException(400, "bad id")
    conn = _conn(user)
    try:
        gone = assistant.forget_thread(conn, thread_id)
    finally:
        conn.close()
    if not gone:
        raise HTTPException(404, "no such conversation")
    return {"ok": True, "deleted": gone}


@router.delete("/assistant/history")
@documented(response=AssistantCleared)
def assistant_clear_api(user: dict = Depends(_user())):
    demoguard.deny_shared_log(user)    # see assistant_forget_api
    conn = _conn(user)
    try:
        return {"ok": True, "deleted": assistant.forget(conn)}
    finally:
        conn.close()
