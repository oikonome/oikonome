"""Requests to Anthropic's OpenAI-compatible endpoint carry only what it
accepts. The endpoint answers 400 to `temperature` and to a
`response_format` that is not a strict JSON schema, and its model listing
needs the native API's version header; a pasted SDK base URL ends in /v1,
which every call would otherwise double. Any other OpenAI-shaped server
keeps the full body."""
import json
import unittest

import httpx

from oikonome.engine import llm_categorize as llm

MESSAGES = [{"role": "user", "content": "x"}]


def capture(reply: dict):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": reply}]})
    return seen, httpx.MockTransport(handler)


def backend(url: str) -> dict:
    return {"url": url, "model": "m", "api_key": "k", "extra_body": "",
            "vision_model": "", "source": "env"}


class AnthropicRequestShapeTests(unittest.TestCase):
    def test_chat_drops_the_fields_the_endpoint_refuses(self):
        seen, t = capture({"content": '{"ok": true}'})
        llm._chat(MESSAGES, max_tokens=40, transport=t,
                  backend=backend("https://api.anthropic.com/v1"))
        req = seen[0]
        self.assertEqual(str(req.url),
                         "https://api.anthropic.com/v1/chat/completions")
        body = json.loads(req.content)
        self.assertNotIn("temperature", body)
        self.assertNotIn("response_format", body)
        # the answer-sized cap would be spent on thinking the endpoint
        # never returns; the ceiling is lifted, the answer stays short
        self.assertEqual(body["max_tokens"], llm.ANTHROPIC_MIN_MAX_TOKENS)
        self.assertEqual(req.headers["authorization"], "Bearer k")
        self.assertEqual(req.headers["anthropic-version"],
                         llm.ANTHROPIC_VERSION)

    def test_tools_call_drops_temperature_keeps_tools(self):
        seen, t = capture({"content": "", "tool_calls": []})
        tools = [{"type": "function", "function": {"name": "f"}}]
        llm.chat_tools(MESSAGES, tools, max_tokens=40, transport=t,
                       backend=backend("https://api.anthropic.com"))
        body = json.loads(seen[0].content)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["max_tokens"], llm.ANTHROPIC_MIN_MAX_TOKENS)
        self.assertEqual(body["tools"], tools)
        self.assertEqual(body["tool_choice"], "auto")

    def test_other_servers_keep_the_full_body(self):
        seen, t = capture({"content": "{}"})
        llm._chat(MESSAGES, max_tokens=40, transport=t,
                  backend=backend("http://llm.test/v1/"))
        req = seen[0]
        self.assertEqual(str(req.url), "http://llm.test/v1/chat/completions")
        body = json.loads(req.content)
        self.assertEqual(body["temperature"], 0.1)
        self.assertEqual(body["max_tokens"], 40)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("anthropic-version", req.headers)

    def test_extra_body_can_put_a_dropped_field_back(self):
        seen, t = capture({"content": "{}"})
        be = backend("https://api.anthropic.com")
        be["extra_body"] = '{"temperature": 1}'
        llm._chat(MESSAGES, max_tokens=40, transport=t, backend=be)
        self.assertEqual(json.loads(seen[0].content)["temperature"], 1)

    def test_base_url_folds_a_trailing_v1(self):
        self.assertEqual(llm.base_url("https://api.openai.com/v1"),
                         "https://api.openai.com")
        self.assertEqual(llm.base_url("http://ollama:11434/V1/"),
                         "http://ollama:11434")
        self.assertEqual(llm.base_url("http://ollama:11434"),
                         "http://ollama:11434")
        self.assertEqual(llm.base_url(None), "")


if __name__ == "__main__":
    unittest.main()
