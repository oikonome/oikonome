"""Every receipt parse decodes an image (or rasterizes a PDF) on its own
thread. Without bounds, a burst of parse-now clicks across stored receipts
stacks full-resolution Pillow buffers in one process — the same OOM the
optimize path already guards against. Two bounds: a process-wide decode
gate the parse thread queues on (giving up, retryably, after a wait) and a
rate limit on the parse route."""

import json
import unittest

import httpx

from oikonome.engine import receipts

from .util import TODAY, add_txn, make_db, write_config


def _png(w=1200, h=1600):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


def _enable_llm(conn):
    from oikonome.engine import budget
    cfg = budget.load_config(conn)
    cfg["llm_url"] = "http://llm.test"
    cfg["llm_model"] = "vision-model"
    budget.save_config(conn, cfg)


class ParseDecodeGateTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)
        self.txn = add_txn(self.conn, TODAY, 10.0, "STORE")
        self.rid = receipts.add(self.conn, self.txn, _png(), "image/png")

    def tearDown(self):
        self.conn.close()

    def test_saturated_gate_fails_the_parse_retryably_without_calling_the_model(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps({"merchant": "X", "items": []})}}]})
        held = 0
        while receipts._DECODE_GATE.acquire(blocking=False):
            held += 1
        saved = receipts._DECODE_WAIT_S
        receipts._DECODE_WAIT_S = 0.05
        try:
            out = receipts.parse_one(self.conn, self.rid,
                                     transport=httpx.MockTransport(handler))
        finally:
            receipts._DECODE_WAIT_S = saved
            for _ in range(held):
                receipts._DECODE_GATE.release()
        self.assertEqual(out["status"], "failed")
        self.assertEqual(calls, [])                  # nothing decoded or sent
        row = receipts.for_txn(self.conn, self.txn)[0]
        self.assertEqual(row["status"], "failed")     # the sweep can retry it
        self.assertIn("busy", row["error"])

    def test_gate_is_released_after_a_parse(self):
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps({"merchant": "X", "items": []})}}]})
        for _ in range(6):                             # more than the gate size
            out = receipts.parse_one(self.conn, self.rid,
                                     transport=httpx.MockTransport(handler))
            self.assertEqual(out["status"], "parsed")


class ParseRouteIsRateLimitedTests(unittest.TestCase):
    def test_parse_route_declares_a_rate_limit(self):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod

        from .util import all_routes
        match = [r for r in all_routes(appmod.app)
                 if r.path.endswith("/receipts/{rid}/parse")
                 and "POST" in r.methods]
        self.assertTrue(match, "route missing: /receipts/{rid}/parse")
        dep = getattr(match[0], "dependant", None)
        names = [getattr(d.call, "__qualname__", "")
                 for d in (dep.dependencies if dep else [])]
        self.assertTrue(any("limit" in n for n in names),
                        f"/receipts/{{rid}}/parse has no rate limit: {names}")
