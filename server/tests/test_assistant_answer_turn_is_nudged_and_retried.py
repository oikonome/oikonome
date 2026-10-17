"""The answer-only turn tells the model in words that the lookups are over,
and an empty reply to it earns one more such turn before the apology.

Withholding tools with tool_choice "none" is not enough on its own: a model
still reaching for a lookup spends that turn on a tool call the endpoint
drops and returns an empty message, and the person reads "I wasn't able to
finish answering that" for a question the results already answered. Pinned
here: the final turn carries a user message saying no more lookups are
available; an empty final reply is asked once more; a second empty reply
is the apology, not a third request; and a tool result that would exceed
the result cap reaches the model as JSON that still parses, shortened
rather than cut.
"""

import json
import unittest

import httpx

from oikonome.engine import assistant

from .util import make_db

_BE = {"url": "http://fake-llm.local", "model": "m", "api_key": "",
       "extra_body": "", "source": "env"}


def _call(cid="c1"):
    return {"content": "", "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": "net_worth", "arguments": "{}"}}]}


class AnswerTurnTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.bodies = []

    def tearDown(self):
        self.conn.close()

    def _transport(self, final_replies):
        finals = list(final_replies)

        def handler(request):
            body = json.loads(request.content)
            self.bodies.append(body)
            if body.get("tool_choice") == "none":
                reply = finals.pop(0)
                return httpx.Response(200, json={"choices": [{"message": reply}]})
            return httpx.Response(200, json={"choices": [{"message": _call()}]})
        return httpx.MockTransport(handler)

    def test_the_answer_turn_says_the_lookups_are_over(self):
        t = self._transport([{"content": "About $0 so far.", "tool_calls": []}])
        assistant.ask(self.conn, "by month?", transport=t, backend=_BE)
        last = self.bodies[-1]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(last["content"], assistant._ANSWER_NOW)
        # the tool results stay in front of the nudge
        self.assertEqual(self.bodies[-1]["messages"][-2]["role"], "tool")

    def test_an_empty_answer_turn_is_asked_once_more(self):
        t = self._transport([{"content": ""},
                             {"content": "About $0 so far.", "tool_calls": []}])
        r = assistant.ask(self.conn, "by month?", transport=t, backend=_BE)
        self.assertEqual(r["answer"], "About $0 so far.")
        choices = [b.get("tool_choice") for b in self.bodies]
        self.assertEqual(choices, ["auto"] * assistant.MAX_TOOL_HOPS + ["none", "none"])
        self.assertEqual(len(r["tools_used"]), assistant.MAX_TOOL_HOPS)

    def test_a_second_empty_reply_is_the_apology_not_a_third_turn(self):
        t = self._transport([{"content": ""}, {"content": ""}, {"content": "never"}])
        r = assistant.ask(self.conn, "by month?", transport=t, backend=_BE)
        self.assertIn("wasn't able to finish", r["answer"])
        self.assertEqual(len(self.bodies), assistant.MAX_TOOL_HOPS + 2)

    def test_a_list_content_is_not_an_answer(self):
        t = self._transport([{"content": [{"type": "text", "text": "x"}]},
                             {"content": "Plain."}])
        r = assistant.ask(self.conn, "by month?", transport=t, backend=_BE)
        self.assertEqual(r["answer"], "Plain.")


class ToolResultFitTests(unittest.TestCase):
    def test_a_long_result_is_shortened_not_cut(self):
        big = {"total": 1, "by_month": [[f"2025-{m:02d}", m, 1] for m in range(1, 13)] * 3,
               "top_merchants": [[f"shop {i}", 100 - i, 1] for i in range(40)],
               "by_year_and_category": {f"cat {i}": {"2025": i} for i in range(60)}}
        out = assistant._fit_result(big, cap=600)
        self.assertLessEqual(len(out), 600)
        parsed = json.loads(out)              # still JSON
        self.assertTrue(parsed["truncated"])
        self.assertEqual(parsed["total"], 1)
        # a series keeps its most recent entries, a ranking its leaders
        self.assertEqual(parsed["by_month"][-1], ["2025-12", 12, 1])
        self.assertEqual(parsed["top_merchants"][0], ["shop 0", 100, 1])
        self.assertIn("cat 0", parsed["by_year_and_category"])

    def test_a_short_result_is_untouched(self):
        r = {"total": 5, "by_month": [["2025-01", 5, 1]]}
        self.assertEqual(json.loads(assistant._fit_result(r)), r)

    def test_the_spending_summary_reaches_the_model_as_json(self):
        conn = make_db()
        try:
            big = {"by_year_and_category": {f"category {i}": {str(y): i * 7
                                             for y in range(2015, 2027)}
                                            for i in range(80)}}
            assistant._TOOLS_IMPL["_test_big"] = lambda c, a: big
            try:
                seen = []

                def handler(request):
                    body = json.loads(request.content)
                    seen.append(body)
                    if body.get("tool_choice") == "none":
                        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
                    return httpx.Response(200, json={"choices": [{"message": {
                        "content": "", "tool_calls": [{"id": "b1", "type": "function",
                        "function": {"name": "_test_big", "arguments": "{}"}}]}}]})
                assistant.ask(conn, "q", transport=httpx.MockTransport(handler), backend=_BE)
            finally:
                del assistant._TOOLS_IMPL["_test_big"]
            tool_msgs = [m for m in seen[-1]["messages"] if m["role"] == "tool"]
            self.assertTrue(tool_msgs)
            for m in tool_msgs:
                self.assertLessEqual(len(m["content"]), assistant._TOOL_RESULT_CAP)
                self.assertTrue(json.loads(m["content"])["truncated"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
