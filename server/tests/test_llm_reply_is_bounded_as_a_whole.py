"""An LLM reply is bounded as a whole exchange, not per read.

A tenant may point its backend at any public host. A per-read timeout never
fires on a server that sends one byte just inside it, so without a limit on
the whole exchange a call never returns, and whatever the caller holds for
the call stays held: a receipt read holds one of the instance's few read turns
and a pooled connection every other household needs.
"""

import threading
import time
import unittest

import httpx

from oikonome.engine import llm_categorize as llm

from .test_outbound_integrations import _Trickle

_BE = {"url": "http://127.0.0.1:1", "model": "m", "extra_body": None,
       "source": "env"}
_MSG = [{"role": "user", "content": "hi"}]


class WholeExchangeDeadlineTests(unittest.TestCase):
    def _call(self, seconds, **kw):
        """chat_tools with `seconds` as its timeout, on a thread so a call
        that never ends fails this test instead of hanging the suite."""
        out: dict = {}

        def run():
            try:
                llm.chat_tools(_MSG, [], 10, timeout=seconds, **kw)
            except Exception as e:                # noqa: BLE001
                out["e"] = e
        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(5.0)
        self.assertFalse(th.is_alive(), "the call outlived its deadline")
        return out.get("e")

    def test_a_reply_body_that_trickles_is_cut_off(self):
        def drip():
            yield b'{"choices": ['
            for _ in range(200):
                time.sleep(0.05)
                yield b" "
        e = self._call(0.3, backend=_BE, transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=drip())))
        self.assertIsInstance(e, llm.LLMError)

    def test_reply_headers_that_trickle_are_cut_off(self):
        srv = _Trickle(interval=0.2, life=12.0)
        e = self._call(1.0, backend=dict(
            _BE, url=f"http://127.0.0.1:{srv.port}"))
        self.assertIn("Timeout", type(e).__name__)

    def test_a_prompt_reply_is_still_read(self):
        msg = {"content": "ok", "tool_calls": []}
        got = llm.chat_tools(_MSG, [], 10, backend=_BE, timeout=5.0,
                             transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"choices": [{"message": msg}]})))
        self.assertEqual(got["content"], "ok")


if __name__ == "__main__":
    unittest.main()
