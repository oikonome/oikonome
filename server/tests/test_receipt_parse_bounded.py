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


class BusyFailedReceiptsAreRetriedBySweep(unittest.TestCase):
    """A receipt that gave up waiting for the decode gate failed only because
    the server was busy, so the sweep takes it up again — for a bounded
    number of days, so one that can never get a turn does not queue for
    ever. Any other failure stays for a person to retry."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)
        self.txn = add_txn(self.conn, TODAY, 10.0, "STORE")

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def _model(calls):
        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps({"merchant": "X", "total": 10.0,
                                       "items": []})}}]})
        return httpx.MockTransport(handler)

    def _failed(self, error, age_days=0):
        rid = receipts.add(self.conn, self.txn, _png(), "image/png")
        self.conn.execute(
            "UPDATE receipts SET status='failed', error=%s,"
            " created_at = now() - make_interval(days => %s)"
            " WHERE id=%s::uuid", (error, age_days, rid))
        return rid

    def _status(self, rid):
        return self.conn.execute(
            "SELECT status FROM receipts WHERE id=%s::uuid",
            (rid,)).fetchone()["status"]

    def test_the_sweep_parses_a_receipt_that_failed_busy(self):
        rid = self._failed(receipts.BUSY_ERROR)
        calls = []
        out = receipts.parse_pending(self.conn, transport=self._model(calls))
        self.assertEqual(out["parsed"], 1)
        self.assertEqual(self._status(rid), "parsed")

    def test_other_failures_are_left_for_a_person(self):
        rid = self._failed(receipts.PROSE_ERROR)
        calls = []
        out = receipts.parse_pending(self.conn, transport=self._model(calls))
        self.assertEqual(out["parsed"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(self._status(rid), "failed")

    def test_retries_stop_after_the_window(self):
        rid = self._failed(receipts.BUSY_ERROR,
                           age_days=receipts.BUSY_RETRY_DAYS + 1)
        calls = []
        receipts.parse_pending(self.conn, transport=self._model(calls))
        self.assertEqual(calls, [])
        self.assertEqual(self._status(rid), "failed")

    def test_a_busy_gate_stops_the_sweep_instead_of_waiting_on_every_row(self):
        for _ in range(3):
            self._failed(receipts.BUSY_ERROR)
        held = 0
        while receipts._DECODE_GATE.acquire(blocking=False):
            held += 1
        saved = receipts._DECODE_WAIT_S
        receipts._DECODE_WAIT_S = 0.05
        waits = []
        real_acquire = receipts._DECODE_GATE.acquire

        def counting_acquire(*a, **k):
            waits.append(1)
            return real_acquire(*a, **k)
        try:
            receipts._DECODE_GATE.acquire = counting_acquire
            out = receipts.parse_pending(self.conn,
                                         transport=self._model([]))
        finally:
            receipts._DECODE_GATE.acquire = real_acquire
            receipts._DECODE_WAIT_S = saved
            for _ in range(held):
                receipts._DECODE_GATE.release()
        self.assertEqual(len(waits), 1)
        self.assertEqual(out["failed"], 1)


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
