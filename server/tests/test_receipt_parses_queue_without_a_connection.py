"""A background receipt read keeps its connection for the whole vision call,
up to ten minutes. The connection comes from the web process's tenant pool,
so reads must queue BEFORE taking one: however many receipts are waiting on a
slow or hung model, every other request on the instance still gets a
connection."""
import os
import threading
import time
import unittest
import uuid
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import receipts
from oikonome.web import api

from .util import _ensure_db, add_txn, make_db, write_config

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def _wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class _Reads:
    """A parse_one that blocks like a model that has not answered."""

    def __init__(self):
        self.go = threading.Event()
        self.lock = threading.Lock()
        self.started = 0
        self.finished = 0

    def __call__(self, conn, receipt_id, claimed=False, **kw):
        with self.lock:
            self.started += 1
        self.go.wait(30)
        with self.lock:
            self.finished += 1
        return {"status": "parsed"}


class ParseQueueTests(unittest.TestCase):
    def setUp(self):
        _ensure_db()
        self.conn = make_db()
        write_config(self.conn)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        self.addCleanup(self.conn.close)

    def test_a_backlog_of_reads_leaves_the_pool_to_everyone_else(self):
        reads = _Reads()
        self.addCleanup(reads.go.set)
        total = int(os.environ.get("OIKONOME_POOL_MAX") or "10") + 2
        with mock.patch.object(receipts, "parse_one", reads):
            for _ in range(total):
                api._parse_receipt_async(self.tid, str(uuid.uuid4()))
            self.assertTrue(_wait_for(lambda: reads.started >= 1))
            time.sleep(0.5)
            # any request from any household still gets a connection
            other = tenancy.tenant_connect(self.tid)
            other.close()
            self.assertLessEqual(reads.started, receipts.PARSE_SLOTS)
            # and the queue drains once the model answers
            reads.go.set()
            self.assertTrue(_wait_for(lambda: reads.finished == total),
                            f"{reads.finished} of {total} finished")

    def test_a_read_that_raises_gives_its_turn_back(self):
        with mock.patch.object(receipts, "parse_one",
                               side_effect=RuntimeError("boom")) as one:
            for _ in range(receipts.PARSE_SLOTS * 2):
                api._parse_receipt_async(self.tid, str(uuid.uuid4()))
            self.assertTrue(_wait_for(
                lambda: one.call_count == receipts.PARSE_SLOTS * 2))
        self.assertTrue(_wait_for(self._all_slots_free))

    def _all_slots_free(self):
        got = 0
        while receipts._PARSE_TURNS.acquire(blocking=False):
            got += 1
        for _ in range(got):
            receipts._PARSE_TURNS.release()
        return got == receipts.PARSE_SLOTS

    def _claimed_receipt(self):
        txn = add_txn(self.conn, "2026-07-01", 10.0, "STORE")
        rid = receipts.add(self.conn, txn, PNG, "image/png")
        self.assertTrue(receipts.claim(self.conn, rid))
        return rid

    def _hold_every_slot(self):
        held = 0
        while receipts._PARSE_TURNS.acquire(blocking=False):
            held += 1

        def _release():
            for _ in range(held):
                receipts._PARSE_TURNS.release()
        self.addCleanup(_release)

    def _status(self, rid):
        return self.conn.execute(
            "SELECT status, error FROM receipts WHERE id=%s::uuid",
            (rid,)).fetchone()

    def test_a_read_that_never_gets_a_turn_is_told_and_retried(self):
        rid = self._claimed_receipt()
        self._hold_every_slot()
        with mock.patch.object(receipts, "_PARSE_SLOT_WAIT_S", 0.05), \
                mock.patch.object(receipts, "parse_one") as one:
            api._parse_receipt_async(self.tid, rid)
            self.assertTrue(_wait_for(
                lambda: self._status(rid)["status"] == "failed"))
        one.assert_not_called()
        self.assertEqual(self._status(rid)["error"], receipts.BUSY_ERROR)
        # the nightly sweep takes exactly this failure up again
        self.assertTrue(receipts.claim(self.conn, rid))

    def test_a_queue_timeout_leaves_a_hand_entered_receipt_alone(self):
        rid = self._claimed_receipt()
        self.conn.execute(
            "UPDATE receipts SET status='parsed', "
            "parsed='{\"source\": \"manual\"}'::jsonb WHERE id=%s::uuid",
            (rid,))
        self.assertEqual(receipts.fail_busy(self.conn, rid), 0)
        self.assertEqual(self._status(rid)["status"], "parsed")

    def test_the_queue_gives_up_before_a_stale_claim_can_be_retaken(self):
        minutes = int(receipts.STALE_PARSE.split()[0])
        self.assertLess(receipts._PARSE_SLOT_WAIT_S, minutes * 60)


if __name__ == "__main__":
    unittest.main()
