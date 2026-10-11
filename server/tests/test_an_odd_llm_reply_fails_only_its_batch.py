"""A 200 from the model backend that is not a chat completion is a backend
failure for that batch, not a crash of the run.

An auth proxy's login page, an app's catch-all route at the base URL, an
`{"error": ...}` body or a content filter's empty `choices` all answer
200. Read blindly, each raised past the per-batch handlers and took the
rest of the nightly run with it, so the failure was never recorded and
the steps after it never ran.
"""

import os
import unittest
from unittest.mock import patch

import httpx

from oikonome.engine import llm_categorize

from .test_llm_categorize import ENV, add_raw_txn
from .util import make_db

REPLIES = (
    ("html", lambda: httpx.Response(
        200, text="<html>sign in</html>",
        headers={"content-type": "text/html"})),
    ("error", lambda: httpx.Response(200, json={"error": "rate limited"})),
    ("no choices", lambda: httpx.Response(200, json={"choices": []})),
    ("message not a dict", lambda: httpx.Response(
        200, json={"choices": [{"message": "hello"}]})),
    ("a list", lambda: httpx.Response(200, json=[1, 2])),
)


def _transport(reply):
    return httpx.MockTransport(lambda request: reply())


class OddReplyTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        add_raw_txn(self.conn, "t1", "2025-07-01", 42, "Mystery Shop", raw={})

    def tearDown(self):
        self.conn.close()

    def test_each_shape_is_an_llm_error(self):
        for name, reply in REPLIES:
            with self.subTest(reply=name):
                with patch.dict(os.environ, ENV):
                    with self.assertRaises(llm_categorize.LLMError):
                        llm_categorize._chat(
                            [{"role": "user", "content": "x"}], 10,
                            transport=_transport(reply))
                    with self.assertRaises(llm_categorize.LLMError):
                        llm_categorize.chat_tools(
                            [{"role": "user", "content": "x"}], [], 10,
                            transport=_transport(reply))

    def test_the_run_finishes_and_records_the_failure(self):
        for name, reply in REPLIES:
            with self.subTest(reply=name):
                with patch.dict(os.environ, ENV):
                    stats = llm_categorize.run(self.conn,
                                               transport=_transport(reply))
                self.assertEqual(stats["merchants_classified"], 0)
                self.assertTrue(stats.get("backend_error"))
                last = llm_categorize.last_run(self.conn)
                self.assertEqual(last["state"], "error")
                self.assertNotIn("sign in", last["error"])


if __name__ == "__main__":
    unittest.main()
