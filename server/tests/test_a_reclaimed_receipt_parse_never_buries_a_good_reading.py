"""Two parses of one receipt can overlap: a live parse can outlast the
stale-claim window, and the sweep or the retry door then reclaims the row
while the first attempt is still running.

Invariant: whichever order the two attempts finish in, a good reading is
never replaced by 'failed', and an attempt that lost its claim writes no
failure over the attempt that holds it. A waiting receipt left 'failed'
with a total is never offered a charge."""

import json
import unittest

import httpx

from oikonome.engine import receipts

from .test_receipts import LLM_JSON, _enable_llm
from .util import TODAY, add_txn, make_db, write_config


def _good():
    return httpx.Response(200, json={"choices": [{"message": {
        "content": json.dumps(LLM_JSON)}}]})


def _down():
    return httpx.Response(500, json={"error": "down"})


class OverlappingParseTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)
        txn = add_txn(self.conn, TODAY, 63.00, "BIGBOX WHOLESALE")
        self.rid = receipts.add(self.conn, txn, b"\x89PNG...", "image/png")

    def tearDown(self):
        self.conn.close()

    def _row(self):
        return self.conn.execute(
            "SELECT status, parsed FROM receipts WHERE id=%s::uuid",
            (self.rid,)).fetchone()

    def _go_stale(self):
        self.conn.execute(
            "UPDATE receipts SET parsed_at=now() - interval '20 minutes' "
            "WHERE id=%s::uuid", (self.rid,))

    def _parse(self, answer, **kw):
        return receipts.parse_one(
            self.conn, self.rid,
            transport=httpx.MockTransport(lambda req: answer()), **kw)

    def test_a_late_failure_does_not_bury_the_other_attempts_reading(self):
        # the reclaiming attempt is waiting on the model while the first
        # attempt finishes with a good reading; then the reclaim fails
        def reclaimer_model(request):
            self._parse(_good, claimed=True)
            return _down()
        self.assertTrue(receipts.claim(self.conn, self.rid))
        self._go_stale()
        receipts.parse_one(self.conn, self.rid,
                           transport=httpx.MockTransport(reclaimer_model))
        row = self._row()
        self.assertEqual(row["status"], "parsed")
        self.assertEqual(row["parsed"]["total"], 63.0)

    def test_a_superseded_attempt_that_fails_leaves_the_reclaim_running(self):
        def first_model(request):
            self._go_stale()
            self.assertTrue(receipts.claim(self.conn, self.rid))
            return _down()
        self.assertTrue(receipts.claim(self.conn, self.rid))
        receipts.parse_one(self.conn, self.rid, claimed=True,
                           transport=httpx.MockTransport(first_model))
        self.assertEqual(self._row()["status"], "parsing")

    def test_the_stale_attempt_failing_after_the_reclaim_read_it(self):
        def first_model(request):
            self._go_stale()
            out = self._parse(_good)
            self.assertEqual(out["status"], "parsed")
            return _down()
        self.assertTrue(receipts.claim(self.conn, self.rid))
        receipts.parse_one(self.conn, self.rid, claimed=True,
                           transport=httpx.MockTransport(first_model))
        self.assertEqual(self._row()["status"], "parsed")

    def test_a_good_reading_lands_over_the_reclaims_failure(self):
        def first_model(request):
            self._go_stale()
            self.assertEqual(self._parse(_down)["status"], "failed")
            return _good()
        self.assertTrue(receipts.claim(self.conn, self.rid))
        out = receipts.parse_one(self.conn, self.rid, claimed=True,
                                 transport=httpx.MockTransport(first_model))
        self.assertEqual(out["status"], "parsed")
        self.assertEqual(self._row()["status"], "parsed")


if __name__ == "__main__":
    unittest.main()
