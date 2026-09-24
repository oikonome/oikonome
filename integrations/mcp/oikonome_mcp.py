#!/usr/bin/env python3
"""A local MCP server over your Oikonome ledger — read-only.

Run it on the machine where your assistant runs (any MCP client that
speaks stdio). It needs nothing but Python 3.10+:
no packages, no build. It talks to your instance over HTTP with a READ
scoped script token (Settings → Integrations → Integration tokens), which
can reach the summary and query endpoints and nothing else — it cannot
import a row, change a setting or read a credential.

    OIKONOME_URL=https://money.example.lan OIKONOME_TOKEN=oik_… \\
        python3 oikonome_mcp.py

Tools (each one is one GET against `/api/integrations/…`):

    summary                the verdict, left today, month pace, balances,
                           bills due, alerts, connection freshness
    accounts               every account with its balance
    transactions           search the ledger (text, dates, account,
                           category), newest first, paged
    spending               spend by category and by month
    bills                  unpaid bills due in the next N days + the catalogue
    alerts                 the active alerts
    net_worth              today's net worth and the recorded series

The protocol is JSON-RPC 2.0, one message per line on stdin/stdout, the
MCP subset a tools-only server needs: initialize, ping, tools/list,
tools/call, and the resources/prompts listings answered empty so a client
that asks for them is not confused. Logging goes to stderr; stdout is the
protocol channel and nothing else is ever written to it.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "oikonome", "version": "1.0"}
TIMEOUT = 30

TOOLS = [
    {
        "name": "summary",
        "description": (
            "The household's money at a glance: the month verdict, dollars "
            "left to spend today and this month, balances per account, bills "
            "due in the next week, active alerts, and how fresh each bank "
            "connection is. Start here."),
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
        "path": "/api/integrations/summary",
    },
    {
        "name": "accounts",
        "description": "Every account with its current balance, type and "
                       "institution. counted=false marks the second copy of "
                       "a linked account: skip it when adding balances up.",
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
        "path": "/api/integrations/accounts",
    },
    {
        "name": "transactions",
        "description": (
            "Search the ledger. Free text matches the merchant, the bank's "
            "description, notes and Amazon order items; dates are ISO "
            "(YYYY-MM-DD). Newest first, paged. Amounts are positive for "
            "money out, negative for money in."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "free-text search"},
                "since": {"type": "string", "description": "earliest date, YYYY-MM-DD"},
                "until": {"type": "string", "description": "latest date, YYYY-MM-DD"},
                "account": {"type": "string", "description": "account id (from accounts)"},
                "category": {"type": "string",
                             "description": "category key as the spending "
                                            "tool returns it, e.g. "
                                            "FOOD_AND_DRINK; ? for rows "
                                            "with no category"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                          "description": "rows per page (default 50)"},
                "page": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False},
        "path": "/api/integrations/transactions",
    },
    {
        "name": "spending",
        "description": "Spend by category and by month over the last N "
                       "months, as the Spending page shows it (no transfers "
                       "or card payments; a split charge counts in each of "
                       "its categories). Each category key can be passed to "
                       "transactions to list the rows behind it.",
        "inputSchema": {
            "type": "object",
            "properties": {"months": {"type": "integer", "minimum": 1,
                                      "maximum": 24,
                                      "description": "default 3"}},
            "additionalProperties": False},
        "path": "/api/integrations/spending",
    },
    {
        "name": "bills",
        "description": "Unpaid bills due in the next N days, and the bill "
                       "catalogue (payee, amount, cadence, next due).",
        "inputSchema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1,
                                    "maximum": 366, "description": "default 30"}},
            "additionalProperties": False},
        "path": "/api/integrations/bills",
    },
    {
        "name": "alerts",
        "description": "The active, undismissed alerts as the Today page "
                       "shows them.",
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
        "path": "/api/integrations/alerts",
    },
    {
        "name": "net_worth",
        "description": "Today's net worth (financial accounts, and with "
                       "property) plus the nightly recorded series over the "
                       "last N months.",
        "inputSchema": {
            "type": "object",
            "properties": {"months": {"type": "integer", "minimum": 1,
                                      "maximum": 120,
                                      "description": "default 12"}},
            "additionalProperties": False},
        "path": "/api/integrations/networth",
    },
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def log(msg: str) -> None:
    sys.stderr.write(f"oikonome-mcp: {msg}\n")
    sys.stderr.flush()


class Instance:
    """The HTTP side: one GET per tool call, bearer token, JSON back."""

    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def get(self, path: str, params: dict | None = None):
        qs = urlencode({k: v for k, v in (params or {}).items()
                        if v not in (None, "")})
        req = Request(
            self.url + path + (f"?{qs}" if qs else ""),
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/json",
                     "User-Agent": "oikonome-mcp/1.0"})
        try:
            with urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310
                return json.loads(r.read().decode())
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"HTTP {e.code} from the instance: {body}")
        except OSError as e:
            # unreachable host, refused connection, timeout, bad TLS
            raise RuntimeError(f"could not reach {self.url}: {e}")


class ToolError(Exception):
    pass


class Server:
    """The JSON-RPC side. `fetch(path, params)` is injected so the tests
    can drive it without a network."""

    def __init__(self, fetch):
        self.fetch = fetch
        self.initialized = False

    # -- dispatch ------------------------------------------------------------

    def handle(self, msg: dict) -> dict | None:
        """One request → one response dict, or None for a notification."""
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method is None:
            return None                                  # a response; ignore
        try:
            if method == "initialize":
                result = self.initialize(params)
            elif method == "notifications/initialized":
                self.initialized = True
                return None
            elif method.startswith("notifications/"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{k: t[k] for k in
                                     ("name", "description", "inputSchema")}
                                    for t in TOOLS]}
            elif method == "tools/call":
                result = self.call(params.get("name"),
                                   params.get("arguments") or {})
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            else:
                return self._error(mid, -32601, f"method not found: {method}")
        except ToolError as e:
            # a tool's own failure is a RESULT with isError, per MCP: the
            # model reads the words and can try something else
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": str(e)}],
                               "isError": True}}
        except Exception as e:                                # noqa: BLE001
            return self._error(mid, -32603, f"{type(e).__name__}: {e}")
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _error(mid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": code, "message": message}}

    def initialize(self, params: dict) -> dict:
        return {"protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "Read-only access to one household's Oikonome ledger. "
                    "Call `summary` first; amounts are US dollars, dates "
                    "are ISO. Spend is positive, income negative.")}

    def call(self, name, args: dict) -> dict:
        tool = _BY_NAME.get(name or "")
        if tool is None:
            raise ToolError(f"unknown tool: {name}")
        allowed = set(tool["inputSchema"].get("properties") or {})
        extra = sorted(set(args) - allowed)
        if extra:
            raise ToolError(f"{name} does not take: {', '.join(extra)}")
        try:
            data = self.fetch(tool["path"], args)
        except RuntimeError as e:
            raise ToolError(str(e))
        out = {"content": [{"type": "text",
                            "text": json.dumps(data, indent=1,
                                               sort_keys=True)}]}
        if isinstance(data, dict):
            out["structuredContent"] = data
        return out


def serve(server: Server, inp=None, out=None) -> None:
    """Read one JSON-RPC message per line until stdin closes."""
    inp = inp or sys.stdin
    out = out or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = Server._error(None, -32700, "parse error")
        else:
            if isinstance(msg, list):                        # a batch
                resps = [r for r in (server.handle(m) for m in msg
                                     if isinstance(m, dict)) if r]
                if resps:
                    out.write(json.dumps(resps) + "\n")
                    out.flush()
                continue
            resp = server.handle(msg) if isinstance(msg, dict) else \
                Server._error(None, -32600, "invalid request")
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()


def main() -> int:
    url = os.environ.get("OIKONOME_URL", "").strip()
    token = os.environ.get("OIKONOME_TOKEN", "").strip()
    if not url or not token:
        log("set OIKONOME_URL (your instance) and OIKONOME_TOKEN (a read "
            "scoped script token from Settings → Integrations)")
        return 2
    inst = Instance(url, token)
    log(f"serving {url}")
    serve(Server(inst.get))
    return 0


if __name__ == "__main__":
    sys.exit(main())
