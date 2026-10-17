"""A thorough model that keeps asking for data gets one answer-only turn
instead of a canned apology.

Asked "coffee shops by month this year", a model that does not notice
spend_lookup already returns the months calls it once per month, spends the
hop budget holding six results, and the person reads "I wasn't able to
finish answering that". Pinned here: the hop after the budget is sent with
tools withheld (tool_choice none) so the model answers from what it has;
a tool call it tries to return anyway is ignored; and spend_lookup's one
call carries the month-by-month figures that make the extra hops pointless.
"""

import json
import unittest

import httpx

from oikonome.engine import assistant

from .util import add_txn, make_db

_BE = {"url": "http://fake-llm.local", "model": "m", "api_key": "",
       "extra_body": "", "source": "env"}


def _call(cid="c1"):
    return {"content": "", "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": "net_worth", "arguments": "{}"}}]}


class HopsRunOutTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.seen = []

    def tearDown(self):
        self.conn.close()

    def _transport(self, final_reply):
        def handler(request):
            body = json.loads(request.content)
            self.seen.append(body.get("tool_choice"))
            if body.get("tool_choice") == "none":
                return httpx.Response(200, json={"choices": [{"message": final_reply}]})
            return httpx.Response(200, json={"choices": [{"message": _call()}]})
        return httpx.MockTransport(handler)

    def test_last_turn_withholds_tools_and_answers(self):
        t = self._transport({"content": "About $0 so far.", "tool_calls": []})
        r = assistant.ask(self.conn, "by month?", transport=t, backend=_BE)
        self.assertEqual(r["answer"], "About $0 so far.")
        self.assertEqual(self.seen, ["auto"] * assistant.MAX_TOOL_HOPS + ["none"])
        self.assertEqual(len(r["tools_used"]), assistant.MAX_TOOL_HOPS)

    def test_a_tool_call_on_the_final_turn_is_not_run(self):
        t = self._transport({"content": "", "tool_calls": _call("zz")["tool_calls"]})
        r = assistant.ask(self.conn, "by month?", transport=t, backend=_BE)
        self.assertIn("wasn't able to finish", r["answer"])
        self.assertEqual(len(r["tools_used"]), assistant.MAX_TOOL_HOPS)
        self.assertEqual(self.seen[-1], "none")


class SpendLookupByMonthTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_one_lookup_carries_the_months(self):
        add_txn(self.conn, "2025-01-03", 4.5, "BEAN HAUS",
                merchant="Bean Haus", primary="FOOD_AND_DRINK")
        add_txn(self.conn, "2025-01-20", 5.5, "BEAN HAUS",
                merchant="Bean Haus", primary="FOOD_AND_DRINK")
        add_txn(self.conn, "2025-03-02", 6.0, "BEAN HAUS",
                merchant="Bean Haus", primary="FOOD_AND_DRINK")
        out = assistant._run_tool(self.conn, "spend_lookup",
                                  {"merchant": "bean", "year": 2025})
        self.assertEqual(out["by_month"], [["2025-01", 10.0, 2], ["2025-03", 6.0, 1]])
        self.assertEqual(out["total"], 16.0)


if __name__ == "__main__":
    unittest.main()
