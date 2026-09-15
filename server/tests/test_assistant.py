"""local ledger assistant — the model routes a plain-language
question to READ-ONLY tools, then answers from what the code computes.
Tested with a mock LLM backend (no real model needed)."""

import json
import unittest

import httpx

from oikonome.engine import assistant

from .util import add_txn, make_db

_BE = {"url": "http://fake-llm.local", "model": "m", "api_key": "",
       "extra_body": "", "source": "env"}


def _seq_transport(replies):
    """MockTransport returning `replies` (OpenAI message dicts) in order."""
    state = {"i": 0}

    def handler(request):
        i = min(state["i"], len(replies) - 1)
        state["i"] += 1
        return httpx.Response(200, json={"choices": [{"message": replies[i]}]})

    return httpx.MockTransport(handler)


def _tool_call(name, cid="c1"):
    return {"content": "", "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": name, "arguments": "{}"}}]}


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_unavailable_without_backend(self):
        with self.assertRaises(assistant.AssistantUnavailable):
            assistant.ask(self.conn, "hi", backend={**_BE, "url": ""})

    def test_direct_answer_no_tools(self):
        t = _seq_transport([{"content": "Hello!", "tool_calls": []}])
        r = assistant.ask(self.conn, "hi", transport=t, backend=_BE)
        self.assertEqual(r["answer"], "Hello!")
        self.assertEqual(r["tools_used"], [])

    def test_routes_tool_then_answers(self):
        t = _seq_transport([
            _tool_call("net_worth"),
            {"content": "Your net worth is about $0.", "tool_calls": []},
        ])
        r = assistant.ask(self.conn, "what's my net worth?",
                          transport=t, backend=_BE)
        self.assertIn("net worth", r["answer"].lower())
        self.assertEqual(r["tools_used"], ["net_worth"])

    def test_unknown_tool_does_not_crash(self):
        t = _seq_transport([
            _tool_call("definitely_not_a_tool"),
            {"content": "I can't answer that.", "tool_calls": []},
        ])
        r = assistant.ask(self.conn, "?", transport=t, backend=_BE)
        self.assertEqual(r["answer"], "I can't answer that.")
        self.assertEqual(r["tools_used"], ["definitely_not_a_tool"])

    def test_all_tools_run_read_only(self):
        # each tool must return a dict (never raise out) on an empty ledger
        for name in assistant._TOOLS_IMPL:
            out = assistant._run_tool(self.conn, name, {})
            self.assertIsInstance(out, dict)

    def test_spend_lookup_filters_by_merchant_and_year(self):
        # A named merchant in a named year returns that money and nothing
        # else — the tool the "how much at X" questions depend on.
        add_txn(self.conn, "2025-03-01", 100.0, "HARBOR REHAB VALLEY",
                merchant="Harbor Rehab", primary="MEDICAL")
        add_txn(self.conn, "2025-04-01", 50.0, "HARBOR REHAB DOWNTOWN",
                merchant="Harbor Rehab", primary="MEDICAL")
        add_txn(self.conn, "2024-03-01", 75.0, "HARBOR REHAB VALLEY",
                merchant="Harbor Rehab", primary="MEDICAL")
        add_txn(self.conn, "2025-03-05", 20.0, "SAFEWAY",
                merchant="Safeway", primary="FOOD_AND_DRINK")
        out = assistant._run_tool(self.conn, "spend_lookup",
                                  {"merchant": "harbor", "year": 2025})
        self.assertEqual(out["total"], 150.0)
        self.assertEqual(out["transactions"], 2)
        out = assistant._run_tool(self.conn, "spend_lookup",
                                  {"category": "food and drink"})
        self.assertEqual(out["total"], 20.0)
        # year alone is a valid filter; nothing at all is a refusal, not a
        # ledger-wide dump
        self.assertEqual(assistant._run_tool(
            self.conn, "spend_lookup", {"year": "2024"})["total"], 75.0)
        self.assertIn("error", assistant._run_tool(
            self.conn, "spend_lookup", {}))

    def test_spending_summary_labels_its_windows(self):
        # the model answers from the KEY names; a trailing-12-month figure
        # labelled "alltime" produced confidently wrong "this year" answers
        out = assistant._run_tool(self.conn, "spending_summary", {})
        self.assertIn("by_category_trailing_12_months", out)
        self.assertIn("by_year_and_category", out)
        self.assertNotIn("by_category_alltime", out)

    def test_tool_result_is_json_serializable(self):
        for name in assistant._TOOLS_IMPL:
            out = assistant._run_tool(self.conn, name, {})
            json.dumps(out, default=str)   # must not raise


if __name__ == "__main__":
    unittest.main()


class AssistantBoundsTests(unittest.TestCase):
    """The assistant reads an untrusted backend and holds a web thread for
    minutes: the reply size, the tool fan-out per hop and the number of
    concurrent asks are all bounded."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_oversized_reply_is_refused_not_buffered(self):
        from oikonome.engine import llm_categorize as llm
        big = "x" * (llm.MAX_RESPONSE_BYTES + 1024)

        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {
                "content": big, "tool_calls": []}}]})
        with self.assertRaises(llm.LLMError):
            assistant.ask(self.conn, "hi",
                          transport=httpx.MockTransport(handler), backend=_BE)

    def test_tool_calls_per_hop_are_capped(self):
        many = {"content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "net_worth", "arguments": "{}"}}
            for i in range(20)]}
        t = _seq_transport([many, {"content": "done", "tool_calls": []}])
        r = assistant.ask(self.conn, "?", transport=t, backend=_BE)
        self.assertEqual(len(r["tools_used"]), assistant.MAX_CALLS_PER_HOP)

    def test_in_flight_cap_refuses_instead_of_queueing(self):
        held = 0
        while assistant._IN_FLIGHT.acquire(blocking=False):
            held += 1
        try:
            t = _seq_transport([{"content": "Hello!", "tool_calls": []}])
            with self.assertRaises(assistant.AssistantBusy):
                assistant.ask(self.conn, "hi", transport=t, backend=_BE)
        finally:
            for _ in range(held):
                assistant._IN_FLIGHT.release()
        r = assistant.ask(self.conn, "hi", transport=t, backend=_BE)
        self.assertEqual(r["answer"], "Hello!")


class AssistantLocalityTests(unittest.TestCase):
    """The status the UI shows must say where answers go: 'local' only
    when the model runs beside this server, otherwise the host."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _with_backend(self, be):
        from unittest import mock
        return mock.patch.object(assistant._llm, "_backend",
                                 return_value=be)

    def test_no_backend_is_unavailable(self):
        with self._with_backend({**_BE, "url": ""}):
            self.assertEqual(assistant.status(self.conn),
                             {"available": False, "local": False,
                              "host": ""})

    def test_env_backend_is_local(self):
        with self._with_backend({**_BE, "url": "http://ollama:11434"}):
            st = assistant.status(self.conn)
        self.assertTrue(st["available"] and st["local"])

    def test_tenant_loopback_and_compose_hosts_are_local(self):
        for url in ("http://127.0.0.1:11434", "http://localhost:8080/v1",
                    "http://ollama:11434", "http://[::1]:11434"):
            with self._with_backend({**_BE, "url": url, "source": "tenant"}):
                st = assistant.status(self.conn)
            self.assertTrue(st["local"], url)
            self.assertEqual(st["host"], "")

    def test_tenant_remote_backend_is_not_local_and_names_the_host(self):
        with self._with_backend({**_BE, "url": "https://api.example.com/v1",
                                 "source": "tenant"}):
            st = assistant.status(self.conn)
        self.assertEqual(st, {"available": True, "local": False,
                              "host": "api.example.com"})
