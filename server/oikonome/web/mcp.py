"""The MCP endpoint: the read tools over HTTP, for an assistant that
speaks the Model Context Protocol.

One route, `POST /api/integrations/mcp`, answering JSON-RPC 2.0 in the
protocol's stateless JSON form: `initialize`, `ping`, `tools/list`,
`tools/call`, and notifications (202, no body). No event stream, no
session, nothing the server starts on its own. The tools are
engine/ledger_read.py's; the stdio server in integrations/mcp/ is a
separate client of the /api/integrations read doors, with tools of its
own.

Off unless OIKONOME_MCP_ENABLED is set: until then every method here
answers 404 before a credential is read, the same as a path that does not
exist. The credential is the household's READ-scoped API token, resolved
by the same `current_user` as every read door (one exact POST entry in
app.READ_TOKEN_EXACT lets it in); any other credential is refused.

Design and limits: specs/mcp-endpoint.md.
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback

import psycopg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ..db import tenancy
from ..engine import budget, ledger_read
from ..envnum import env_flag
from . import security, session_user

router = APIRouter()

PATH = "/api/integrations/mcp"
FLAG = "OIKONOME_MCP_ENABLED"
# the newest revision first; initialize echoes the client's when it is one
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")
BODY_MAX = 64 * 1024
RATE = (60, 60.0)            # requests per token, per window (seconds)
# the first key of the per-household advisory lock a tool call holds (the
# second is the household); one tool call per household at a time, so
# rotating tokens cannot multiply the cost a household can ask for
LOCK_CLASS = 0x4D4350        # "MCP"

PARSE_ERROR, INVALID_REQUEST, NO_METHOD, BAD_PARAMS, INTERNAL = (
    -32700, -32600, -32601, -32602, -32603)
SERVER_ERROR = -32000

_log = logging.getLogger("oikonome.mcp")

_ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False,
                "idempotentHint": True, "openWorldHint": False}

TOOLS = [
    {"name": "get_overview", "title": "Accounts and coverage",
     "description": (
         "The household's accounts: name, institution, type, last four "
         "digits, balance and when it was last updated, the first and last "
         "transaction date each account holds, its owner, and the owner "
         "labels in use. Call it first: its account ids are what "
         "search_transactions takes, and its dates say how far back each "
         "account's history goes. " + ledger_read.SIGN_CONVENTION + " "
         + ledger_read.UNTRUSTED)},
    {"name": "search_transactions", "title": "Search transactions",
     "description": (
         "Find transactions by dates (inclusive), text, category, merchant, "
         "accounts, owner, size, direction or spending only. Returns one "
         "page of rows (newest first) plus totals over EVERY match — count, "
         "money out, money in and net by the app's Cash Flow rules (card "
         "payments and transfers between own accounts are neither) — and "
         "the same matches by month, spending by category and the top "
         "merchants. A coverage_note says when the period starts before an "
         "account's history. " + ledger_read.SIGN_CONVENTION + " "
         + ledger_read.UNTRUSTED)},
]
for _t in TOOLS:
    _t["inputSchema"] = ledger_read.input_schema(_t["name"])
    _t["annotations"] = dict(_ANNOTATIONS)

INSTRUCTIONS = (
    "Read-only access to one household's books in Oikonome. Nothing here "
    "can change anything. Call get_overview first. "
    + ledger_read.SIGN_CONVENTION + " " + ledger_read.UNTRUSTED)

NOT_FOUND = {"detail": "Not Found"}


def _enabled() -> bool:
    return env_flag(FLAG)


def _err(code: int, message: str, rid=None, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid,
                         "error": {"code": code, "message": message}},
                        status_code=status)


def _ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _fail(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def _accepts_json(accept: str) -> bool:
    if not accept.strip():
        return True
    kinds = {p.split(";")[0].strip().lower() for p in accept.split(",")}
    return bool(kinds & {"application/json", "application/*", "*/*"})


async def _body(request: Request) -> bytes | None:
    """The request body, or None once it passes BODY_MAX."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > BODY_MAX:
        return None
    got = bytearray()
    async for chunk in request.stream():
        got += chunk
        if len(got) > BODY_MAX:
            return None
    return bytes(got)


@router.api_route(PATH, methods=["GET", "DELETE", "PUT", "PATCH", "HEAD",
                                 "OPTIONS"], include_in_schema=False)
def mcp_no_stream(request: Request):
    """Every method but POST. The protocol's GET (open a server-to-client
    stream) and DELETE (end a session) exist in neither form here; the
    rest are registered so that, switched off, every method answers the
    404 an unknown path does."""
    if not _enabled():
        return JSONResponse(NOT_FOUND, status_code=404)
    return JSONResponse(
        {"detail": "this MCP endpoint does not stream and keeps no "
                   "session: POST each JSON-RPC request and read the JSON "
                   "reply"},
        status_code=405, headers={"Allow": "POST"})


@router.post(PATH, include_in_schema=False)
async def mcp_post(request: Request):
    if not _enabled():
        return JSONResponse(NOT_FOUND, status_code=404)
    if security.cross_origin_write(request):
        # the Origin check every other write door gets from the middleware,
        # here after the switch so an instance that never turned this on
        # answers 404, and in the protocol's shape: a bearer token held by
        # a web page is not a client this endpoint serves
        return _err(SERVER_ERROR,
                    "cross-origin browser requests are refused: connect "
                    "from an assistant or a server-side client, not a web "
                    "page", status=403)
    started = time.monotonic()
    try:
        user = await run_in_threadpool(session_user.current_user, request)
    except HTTPException as e:
        # the read doors' refusal, in the protocol's shape
        reply = _err(SERVER_ERROR, str(e.detail), status=e.status_code)
        if e.status_code == 401:
            reply.headers["WWW-Authenticate"] = "Bearer"
        return reply
    if user.get("token_scope") != "read":
        return _err(SERVER_ERROR,
                    "the MCP endpoint takes a read-scoped API token "
                    "(Settings → Integrations), not a sign-in", status=403)
    token_id = str(user.get("token_id") or "-")
    try:
        security.account_limit_or_429(("mcp", token_id), *RATE)
    except HTTPException as e:
        return _err(SERVER_ERROR, str(e.detail), status=429)
    if not _accepts_json(request.headers.get("accept", "")):
        return _err(INVALID_REQUEST,
                    "this endpoint answers application/json only; it does "
                    "not stream (text/event-stream)", status=406)
    asked = request.headers.get("mcp-protocol-version")
    if asked and asked not in PROTOCOL_VERSIONS:
        return _err(INVALID_REQUEST,
                    f"unsupported MCP-Protocol-Version; this server speaks "
                    f"{', '.join(PROTOCOL_VERSIONS)}", status=400)
    raw = await _body(request)
    if raw is None:
        return _err(INVALID_REQUEST,
                    f"the request body is larger than {BODY_MAX // 1024} KiB",
                    status=413)
    try:
        msg = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _err(PARSE_ERROR, "the request body is not valid JSON",
                    status=400)
    if isinstance(msg, list):
        return _err(INVALID_REQUEST, "batches are not accepted: send one "
                                     "JSON-RPC message per request", status=400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(INVALID_REQUEST, 'not a JSON-RPC 2.0 message '
                                     '(expected {"jsonrpc": "2.0", ...})',
                    status=400)
    method = msg.get("method")
    if method is None and ("result" in msg or "error" in msg):
        # a reply to a request this server never sends
        return Response(status_code=202)
    if not isinstance(method, str):
        return _err(INVALID_REQUEST, "method must be a string", status=400)
    if "id" not in msg:
        # a notification (notifications/initialized and the like): nothing
        # to answer
        _done(method, None, 202, started, token_id)
        return Response(status_code=202)
    rid = msg["id"]
    if isinstance(rid, bool) or not isinstance(rid, (str, int)):
        return _err(INVALID_REQUEST, "id must be a string or an integer",
                    status=400)
    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _err(BAD_PARAMS, "params must be an object", rid)
    reply, tool = await run_in_threadpool(_dispatch, user, method, params, rid)
    _done(method, tool, "error" if "error" in reply else "ok", started,
          token_id)
    return JSONResponse(reply)


def _done(method: str, tool: str | None, status, started: float,
          token_id: str) -> None:
    # what was asked and how it went — never the arguments, an amount or a
    # name (logsafe flattens any line break regardless)
    _log.info("mcp method=%s tool=%s status=%s ms=%d token=%s",
              method[:40], tool or "-", status,
              int((time.monotonic() - started) * 1000), token_id)


def _dispatch(user: dict, method: str, params: dict, rid):
    """(reply, tool name or None) for one request."""
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _ok(rid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "oikonome", "title": "Oikonome",
                           "version": os.environ.get("OIKONOME_VERSION") or "dev"},
            "instructions": INSTRUCTIONS}), None
    if method == "ping":
        return _ok(rid, {}), None
    if method == "tools/list":
        return _ok(rid, {"tools": TOOLS}), None
    if method != "tools/call":
        return _fail(rid, NO_METHOD, f"method not found: {method[:60]}"), None
    name = params.get("name")
    if not isinstance(name, str) or name not in ledger_read.TOOLS:
        return _fail(rid, BAD_PARAMS, f"unknown tool: {str(name)[:60]} "
                                      "(tools/list names them)"), None
    try:
        args = ledger_read.parse_args(name, params.get("arguments"))
    except ledger_read.ToolInputError as e:
        return _fail(rid, BAD_PARAMS, str(e)), name
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        # the household's settings, read once BEFORE the read-only
        # snapshot: a first read may store the derived starting budget
        # (budget.load_config seeds it once spending has closed a month),
        # the same write opening the app makes, and it cannot be made
        # inside a READ ONLY transaction. Every read inside then finds it.
        budget.load_config(conn)
        with ledger_read.one_snapshot(conn):
            # held to the end of this transaction, so it cannot outlive
            # the call on a pooled connection
            if not conn.execute(
                    "SELECT pg_try_advisory_xact_lock(%s, hashtext(%s)) AS ok",
                    (LOCK_CLASS, str(user["tenant_id"]))).fetchone()["ok"]:
                return _fail(rid, SERVER_ERROR,
                             "busy: another request for this household is "
                             "still running — retry in a moment"), name
            result = ledger_read.TOOLS[name](conn, args)
    except ledger_read.ToolInputError as e:
        return _fail(rid, BAD_PARAMS, str(e)), name
    except psycopg.errors.QueryCanceled:
        _log.warning("mcp tool %s passed the statement time limit", name)
        return _fail(rid, SERVER_ERROR,
                     "the search took too long: narrow it (a shorter date "
                     "range, an account or a category) and ask again"), name
    except Exception as e:                               # noqa: BLE001
        # the class and where it was raised, never the message or the
        # traceback's locals: either can carry an argument or a figure
        where = traceback.extract_tb(e.__traceback__)[-1:]
        _log.error("mcp tool %s failed: %s at %s", name, type(e).__name__,
                   (f"{os.path.basename(where[0].filename)}:{where[0].lineno}"
                    if where else "?"))
        return _fail(rid, INTERNAL, "the tool failed on the server; nothing "
                                    "was changed — try again"), name
    finally:
        conn.close()
    return _ok(rid, {"content": [{"type": "text",
                                  "text": json.dumps(result)}],
                     "structuredContent": result, "isError": False}), name
