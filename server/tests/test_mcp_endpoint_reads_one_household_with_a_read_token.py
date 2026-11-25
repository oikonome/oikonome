"""The MCP endpoint over HTTP: off until switched on, a read token only,
one household, and a JSON-RPC answer for everything.

What breaks if these fail: the endpoint answers on an instance that never
turned it on (or tells a stranger it exists); a push token, a sign-in
cookie or another household's token reads the books; a malformed or
oversized request takes the server down instead of getting a JSON-RPC
error; one token can call it without bound; the tools a client is shown
are not marked read-only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import mcp

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"
PATH = "/api/integrations/mcp"
ON = {"OIKONOME_MCP_ENABLED": "1"}


def _app():
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    return appmod.app


def _household(app, merchant: str, amount: float):
    owner = TestClient(app)
    owner.post("/api/signup", data={
        "email": f"mcp-{uuid.uuid4().hex[:8]}@example.dev", "password": PW})
    tid = owner.get("/api/me").json()["tenant_id"]
    conn = tenancy.tenant_connect(tid)
    try:
        seed_accounts(conn)
        write_config(conn)
        add_txn(conn, TODAY - dt.timedelta(days=2), amount, merchant,
                primary="FOOD_AND_DRINK")
    finally:
        conn.close()
    return owner, tid


def _rpc(method, params=None, rid=1):
    m = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        m["params"] = params
    return m


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        cls.owner, cls.tid = _household(cls.app, "MAPLE BAKERY", 23.0)
        cls.other_owner, cls.other_tid = _household(cls.app, "QUINCE DELI",
                                                    41.0)

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        p = mock.patch.dict(os.environ, ON)
        p.start()
        self.addCleanup(p.stop)

    def _mint(self, owner, scope="read"):
        r = owner.post("/api/tokens", json={"name": "assistant", "scope": scope,
                                            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def _client(self, token):
        c = TestClient(self.app)
        c.headers["Authorization"] = f"Bearer {token}"
        c.headers["Accept"] = "application/json, text/event-stream"
        return c

    def _call(self, c, tool, args=None, rid=7):
        r = c.post(PATH, json=_rpc("tools/call", {"name": tool,
                                                  "arguments": args or {}}, rid))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class SwitchTests(Base):

    def test_off_by_default_every_method_is_a_404_before_auth(self):
        with mock.patch.dict(os.environ, {"OIKONOME_MCP_ENABLED": ""}):
            bare = TestClient(self.app)
            unknown = "/api/integrations/no-such-door"
            for verb in ("POST", "GET", "DELETE", "PUT", "PATCH", "HEAD",
                         "OPTIONS"):
                r = bare.request(verb, PATH)
                self.assertEqual(r.status_code, 404, verb)
                self.assertEqual(r.status_code,
                                 bare.request(verb, unknown).status_code, verb)
                if verb != "HEAD":
                    self.assertEqual(r.json(), {"detail": "Not Found"})
            c = self._client(self._mint(self.owner))
            self.assertEqual(c.post(PATH, json=_rpc("ping")).status_code, 404)

    def test_off_a_cross_origin_request_is_still_a_404(self):
        with mock.patch.dict(os.environ, {"OIKONOME_MCP_ENABLED": ""}):
            bare = TestClient(self.app)
            for verb in ("POST", "PUT", "PATCH", "DELETE"):
                r = bare.request(verb, PATH,
                                 headers={"Origin": "https://elsewhere.example"})
                self.assertEqual(r.status_code, 404, verb)

    def test_on_a_cross_origin_browser_request_is_refused_in_json_rpc(self):
        c = self._client(self._mint(self.owner))
        for claim in ({"Origin": "https://elsewhere.example"},
                      {"Origin": "null"},
                      {"Referer": "https://elsewhere.example/page"}):
            r = c.post(PATH, json=_rpc("ping"), headers=claim)
            self.assertEqual(r.status_code, 403, claim)
            self.assertIn("browser", r.json()["error"]["message"])
        # the same host, or no claim at all, is no browser elsewhere
        same = c.post(PATH, json=_rpc("ping"),
                      headers={"Origin": "http://testserver"})
        self.assertEqual(same.json()["result"], {})

    def test_other_routes_keep_the_cross_origin_refusal(self):
        r = self.owner.post("/api/tokens", json={"name": "x", "scope": "read",
                                                 "password": PW},
                            headers={"Origin": "https://elsewhere.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json(), {"detail": "cross-origin write blocked"})

    def test_on_it_asks_for_a_credential_in_json_rpc(self):
        r = TestClient(self.app).post(PATH, json=_rpc("ping"))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["jsonrpc"], "2.0")
        self.assertIn("not signed in", r.json()["error"]["message"])
        self.assertTrue(r.headers.get("www-authenticate", "").startswith("Bearer"))


class CredentialTests(Base):

    def test_a_push_token_is_refused_as_on_the_read_doors(self):
        r = self._client(self._mint(self.owner, "push")).post(
            PATH, json=_rpc("ping"))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("push-scoped", r.text)

    def test_a_signed_in_session_is_refused(self):
        r = self.owner.post(PATH, json=_rpc("ping"))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("read-scoped API token", r.json()["error"]["message"])

    def test_a_revoked_token_stops(self):
        tok = self.owner.post("/api/tokens", json={
            "name": "gone", "scope": "read", "password": PW}).json()
        self.owner.post("/api/tokens/revoke", json={"id": tok["id"],
                                                    "password": PW})
        r = self._client(tok["token"]).post(PATH, json=_rpc("ping"))
        self.assertEqual(r.status_code, 401)

    def test_a_token_reads_its_own_household_and_never_another(self):
        a = self._client(self._mint(self.owner))
        b = self._client(self._mint(self.other_owner))
        got_a = self._call(a, "search_transactions")["result"]["structuredContent"]
        got_b = self._call(b, "search_transactions")["result"]["structuredContent"]
        self.assertEqual([t["payee"] for t in got_a["transactions"]],
                         ["MAPLE BAKERY"])
        self.assertEqual([t["payee"] for t in got_b["transactions"]],
                         ["QUINCE DELI"])
        self.assertEqual((got_a["totals"]["out"], got_b["totals"]["out"]),
                         (23.0, 41.0))
        # an account id from the other household names nothing here
        other_ids = [x["id"] for x in self._call(b, "get_overview")[
            "result"]["structuredContent"]["accounts"]]
        r = self._call(a, "search_transactions", {"account_ids": other_ids[:1]})
        self.assertEqual(r["error"]["code"], mcp.BAD_PARAMS)
        # and no argument can name a household
        r = self._call(a, "search_transactions", {"tenant_id": self.other_tid})
        self.assertIn("unknown argument", r["error"]["message"])


class ProtocolTests(Base):

    def setUp(self):
        super().setUp()
        self.c = self._client(self._mint(self.owner))

    def test_initialize_answers_a_known_revision_and_the_newest_otherwise(self):
        r = self.c.post(PATH, json=_rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"}}))
        res = r.json()["result"]
        self.assertEqual(res["protocolVersion"], "2025-06-18")
        self.assertEqual(res["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(res["serverInfo"]["name"], "oikonome")
        self.assertNotIn("mcp-session-id", {k.lower() for k in r.headers})
        res = self.c.post(PATH, json=_rpc("initialize", {
            "protocolVersion": "1999-01-01"})).json()["result"]
        self.assertEqual(res["protocolVersion"], mcp.PROTOCOL_VERSIONS[0])

    def test_a_notification_is_accepted_with_no_body(self):
        r = self.c.post(PATH, json={"jsonrpc": "2.0",
                                    "method": "notifications/initialized"})
        self.assertEqual((r.status_code, r.content), (202, b""))

    def test_ping(self):
        r = self.c.post(PATH, json=_rpc("ping", rid="p1")).json()
        self.assertEqual(r, {"jsonrpc": "2.0", "id": "p1", "result": {}})

    def test_tools_list_marks_every_tool_read_only(self):
        tools = self.c.post(PATH, json=_rpc("tools/list")).json()["result"]["tools"]
        self.assertEqual({t["name"] for t in tools},
                         {"get_overview", "search_transactions"})
        for t in tools:
            self.assertTrue(t["title"])
            self.assertIs(t["annotations"]["readOnlyHint"], True)
            self.assertIs(t["annotations"]["destructiveHint"], False)
            self.assertIn("positive for money out", t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_each_tool_answers_with_text_and_structured_content(self):
        for tool in ("get_overview", "search_transactions"):
            res = self._call(self.c, tool)["result"]
            self.assertIs(res["isError"], False)
            self.assertEqual(json.loads(res["content"][0]["text"]),
                             res["structuredContent"])
        ov = self._call(self.c, "get_overview")["result"]["structuredContent"]
        self.assertEqual({a["name"] for a in ov["accounts"]},
                         {"Test Checking", "Test Card"})

    def test_bad_arguments_are_a_json_rpc_error_in_words(self):
        r = self._call(self.c, "search_transactions",
                       {"start_date": "2026-05-10", "end_date": "2026-05-01"})
        self.assertEqual(r["error"], {"code": mcp.BAD_PARAMS,
                                      "message": "end_date is before start_date"})
        r = self._call(self.c, "search_transactions", {"limit": 500})
        self.assertIn("at most 100", r["error"]["message"])
        r = self._call(self.c, "no_such_tool")
        self.assertIn("unknown tool", r["error"]["message"])

    def test_an_empty_search_is_a_success(self):
        res = self._call(self.c, "search_transactions",
                         {"query": "nothing like this"})["result"]
        self.assertEqual(res["structuredContent"]["totals"]["count"], 0)

    def test_malformed_requests(self):
        r = self.c.post(PATH, content=b"{not json",
                        headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], mcp.PARSE_ERROR)
        r = self.c.post(PATH, json={"id": 1, "method": "ping"})
        self.assertEqual(r.json()["error"]["code"], mcp.INVALID_REQUEST)
        r = self.c.post(PATH, json=[_rpc("ping")])
        self.assertIn("batches", r.json()["error"]["message"])
        r = self.c.post(PATH, json=_rpc("resources/list"))
        self.assertEqual(r.json()["error"]["code"], mcp.NO_METHOD)
        r = self.c.post(PATH, json=_rpc("ping", rid=None))
        self.assertEqual(r.status_code, 400)

    def test_an_oversized_body_is_refused(self):
        big = json.dumps(_rpc("ping", {"pad": "x" * (mcp.BODY_MAX + 10)}))
        r = self.c.post(PATH, content=big.encode(),
                        headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)
        self.assertIn("larger than", r.json()["error"]["message"])

    def test_no_stream_and_no_session(self):
        self.assertEqual(self.c.get(PATH).status_code, 405)
        self.assertEqual(self.c.delete(PATH).status_code, 405)
        r = self.c.post(PATH, json=_rpc("ping"),
                        headers={"Accept": "text/event-stream"})
        self.assertEqual(r.status_code, 406)
        self.assertIn("does not stream", r.json()["error"]["message"])
        r = self.c.post(PATH, json=_rpc("ping"),
                        headers={"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(r.status_code, 400)

    def test_one_tool_call_at_a_time_per_household(self):
        # rotating tokens would escape the per-token budget; the household
        # lock does not care which token asks
        held = tenancy.tenant_connect(self.tid)
        self.addCleanup(held.close)
        held.execute("SELECT pg_advisory_lock(%s, hashtext(%s))",
                     (mcp.LOCK_CLASS, str(self.tid)))
        try:
            r = self._call(self.c, "search_transactions")
            self.assertEqual(r["error"]["code"], mcp.SERVER_ERROR)
            self.assertIn("busy", r["error"]["message"])
            # another household is not held up
            other = self._client(self._mint(self.other_owner))
            self.assertIn("result", self._call(other, "get_overview"))
            # and a ping needs no tool slot
            self.assertIn("result", self.c.post(PATH, json=_rpc("ping")).json())
        finally:
            held.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))",
                         (mcp.LOCK_CLASS, str(self.tid)))
        self.assertIn("result", self._call(self.c, "search_transactions"))

    def test_a_failing_tool_logs_no_argument_values(self):
        from oikonome.engine import ledger_read
        boom = ValueError("value 4321 and the query maple")
        with mock.patch.dict(ledger_read.TOOLS,
                             {"search_transactions": mock.Mock(side_effect=boom)}), \
                self.assertLogs("oikonome.mcp", "INFO") as logs:
            r = self._call(self.c, "search_transactions", {"query": "maple"})
        self.assertEqual(r["error"]["code"], mcp.INTERNAL)
        text = " ".join(logs.output)
        self.assertIn("ValueError", text)
        self.assertNotIn("value 4321", text)
        self.assertNotIn("maple", text)

    def test_out_of_range_arguments_are_bad_params_not_a_failure(self):
        for args in ({"start_date": "0001-01-01"}, {"min_amount": 10 ** 400},
                     {"query": "a\u0000b"}):
            r = self._call(self.c, "search_transactions", args)
            self.assertEqual(r["error"]["code"], mcp.BAD_PARAMS, args)

    def test_a_call_that_runs_too_long_says_to_narrow_it(self):
        from oikonome.engine import ledger_read

        def slow(conn, args):
            conn.execute("SELECT pg_sleep(1)")
            return {}
        with mock.patch.object(ledger_read, "STATEMENT_TIMEOUT_MS", 50), \
                mock.patch.dict(ledger_read.TOOLS, {"search_transactions": slow}):
            r = self._call(self.c, "search_transactions")
        self.assertEqual(r["error"]["code"], mcp.SERVER_ERROR)
        self.assertIn("narrow", r["error"]["message"])
        self.assertIn("result", self._call(self.c, "get_overview"))

    def test_a_tool_cannot_write(self):
        from oikonome.engine import ledger_read

        def writes(conn, args):
            conn.execute("UPDATE accounts SET name = name")
            return {}
        with mock.patch.dict(ledger_read.TOOLS, {"search_transactions": writes}):
            r = self._call(self.c, "search_transactions")
        self.assertEqual(r["error"]["code"], mcp.INTERNAL)

    def test_one_token_is_rate_limited(self):
        n, _w = mcp.RATE
        with mock.patch.object(mcp, "RATE", (3, 60.0)):
            codes = [self.c.post(PATH, json=_rpc("ping")).status_code
                     for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        last = self.c.post(PATH, json=_rpc("ping"))
        self.assertEqual(last.status_code, 200)   # back on the real budget
        self.assertGreater(n, 3)

    def test_the_log_line_carries_no_amounts_or_query_text(self):
        with self.assertLogs("oikonome.mcp", "INFO") as logs:
            self._call(self.c, "search_transactions", {"query": "maple"})
        line = " ".join(logs.output)
        self.assertIn("tool=search_transactions", line)
        self.assertNotIn("maple", line.lower())
        self.assertNotIn("23", line.split("ms=")[0])


class FirstReadTests(Base):
    """The tools answer for a household in any first-run state. The first
    read may store the household's derived starting budget, exactly as
    opening the app does; nothing inside a tool call writes."""

    def _fresh(self, *, months_closed: int, settings: dict | None):
        owner = TestClient(self.app)
        owner.post("/api/signup", data={
            "email": f"mcp-first-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        tid = owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            if settings is not None:
                conn.execute("INSERT INTO tenant_settings (config) VALUES (%s)"
                             " ON CONFLICT (tenant_id) DO UPDATE"
                             " SET config = EXCLUDED.config",
                             (json.dumps(settings),))
            first = dt.date.today().replace(day=1)
            for back in range(months_closed):
                day = (first - dt.timedelta(days=1 + 31 * back)).replace(day=10)
                add_txn(conn, day, 60.0, "BIRCH MARKET", primary="FOOD_AND_DRINK")
                add_txn(conn, day, 40.0, "ELM HARDWARE")
            if not months_closed:
                add_txn(conn, dt.date.today(), 25.0, "BIRCH MARKET",
                        primary="FOOD_AND_DRINK")
        finally:
            conn.close()
        return self._client(self._mint(owner))

    def _both(self, c):
        for tool in ("get_overview", "search_transactions"):
            r = self._call(c, tool)
            self.assertIn("result", r, f"{tool}: {r}")

    def test_empty_settings_and_two_closed_months_of_spend(self):
        self._both(self._fresh(months_closed=2, settings={}))

    def test_a_brand_new_empty_household(self):
        c = self._client(self._mint(_household_bare(self.app)))
        self._both(c)

    def test_mid_month_with_nothing_closed(self):
        self._both(self._fresh(months_closed=0, settings={}))


def _household_bare(app):
    owner = TestClient(app)
    owner.post("/api/signup", data={
        "email": f"mcp-bare-{uuid.uuid4().hex[:8]}@example.dev", "password": PW})
    return owner


if __name__ == "__main__":
    unittest.main()
