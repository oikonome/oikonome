"""LLM calls in one process share a bounded number of slots.

Import, a bank pull, a tax-document read and a receipt read each hold a
pooled database connection while they wait for the model. Unbounded, enough
of them at once leave the pool empty and every other request on the instance
stalls behind the model's latency. Every chat call takes a slot first. The
wait for one is SHORT (OIKONOME_LLM_SLOT_WAIT_S, never longer than the reply
deadline): a caller queued for a slot still holds its own connection, so a
wait as long as the reply deadline would empty the pool the cap protects.
Past it the call fails fast with LLMBusy, an LLMError, and the caller lets
its connection go. The slot is given back on every exit path.

The default never runs fewer slots than the receipt reader runs reads at
once: a read past the slots would otherwise fail where the reader had
promised it a turn. The assistant has one slot of its own on top, so a
sweep of reads filling the shared slots never makes a question fail as busy,
and one question in flight never takes a read's slot.

The slots are a threading primitive: they bound the calls of ONE process. The
web process and the worker each have their own, sized from their own pool.
"""

import os
import threading
import time
import unittest
from unittest import mock

import httpx

from oikonome.engine import llm_categorize as llm

_BE = {"url": "http://127.0.0.1:1", "model": "m", "extra_body": None,
       "source": "env"}
_MSG = [{"role": "user", "content": "hi"}]
_OK = {"choices": [{"message": {"content": "{}", "tool_calls": []}}]}


def _no_reserve():
    """The assistant's own slot, already taken."""
    s = threading.BoundedSemaphore(1)
    s.acquire()
    return s


def _cap(n: int):
    """Exactly `n` slots for every caller: the assistant's own slot is taken
    away, so these tests see the shared bound alone."""
    return mock.patch.multiple(llm, LLM_SLOTS=n,
                               _LLM_SLOTS=threading.BoundedSemaphore(n),
                               _ASSISTANT_SLOT=_no_reserve())


def _free_slots() -> int:
    """How many slots can be taken right now (and gives them back)."""
    got = 0
    while llm._LLM_SLOTS.acquire(blocking=False):
        got += 1
    for _ in range(got):
        llm._LLM_SLOTS.release()
    return got


class _Held:
    """`n` chat calls parked inside the backend until release()."""

    def __init__(self, n: int, fn=None):
        self.gate = threading.Event()
        self.inside = threading.Semaphore(0)
        self.threads = []
        fn = fn or (lambda **kw: llm.chat_tools(_MSG, [], 10, **kw))

        def handler(req):
            self.inside.release()
            self.gate.wait(10)
            return httpx.Response(200, json=_OK)
        self.transport = httpx.MockTransport(handler)
        for _ in range(n):
            th = threading.Thread(target=fn, kwargs=dict(
                backend=_BE, transport=self.transport, timeout=10.0),
                daemon=True)
            th.start()
            self.threads.append(th)
        for _ in range(n):
            assert self.inside.acquire(timeout=5), "held call never started"

    def release(self):
        self.gate.set()
        for th in self.threads:
            th.join(5)


class CapTests(unittest.TestCase):
    def test_the_call_past_the_cap_waits_then_fails_like_a_timeout(self):
        with _cap(2):
            held = _Held(2)
            reached = []
            try:
                t0 = time.monotonic()
                with self.assertLogs(llm.log, "WARNING") as logs, \
                        self.assertRaises(llm.LLMError):
                    llm.chat_tools(_MSG, [], 10, backend=_BE, timeout=0.4,
                                   transport=httpx.MockTransport(
                        lambda req: reached.append(1) or
                        httpx.Response(200, json=_OK)))
                waited = time.monotonic() - t0
            finally:
                held.release()
            self.assertGreaterEqual(waited, 0.35, "it did not wait")
            self.assertLess(waited, 3.0, "it waited past its deadline")
            self.assertEqual(reached, [], "the backend was asked anyway")
            self.assertTrue(any("slot" in m for m in logs.output))
            self.assertEqual(_free_slots(), 2)
            # once a slot frees, the next call goes through
            got = llm.chat_tools(_MSG, [], 10, backend=_BE, timeout=5.0,
                                 transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=_OK)))
            self.assertEqual(got["content"], "{}")

    def test_the_categorizer_path_is_capped_too(self):
        with _cap(1):
            held = _Held(1)
            try:
                with mock.patch.object(llm, "REQUEST_TIMEOUT", 0.3), \
                        self.assertRaises(llm.LLMError):
                    llm._chat(_MSG, 10, backend=_BE,
                              transport=httpx.MockTransport(
                        lambda req: httpx.Response(200, json=_OK)))
            finally:
                held.release()
            self.assertEqual(_free_slots(), 1)

    def test_a_waiter_takes_the_slot_a_finished_call_frees(self):
        with _cap(1):
            held = _Held(1)
            threading.Timer(0.2, held.release).start()
            got = llm.chat_tools(_MSG, [], 10, backend=_BE, timeout=5.0,
                                 transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=_OK)))
            self.assertEqual(got["content"], "{}")
            self.assertEqual(_free_slots(), 1)

    def test_the_wait_comes_out_of_the_reply_deadline(self):
        def drip():
            yield b'{"choices": ['
            for _ in range(200):
                time.sleep(0.05)
                yield b" "
        with _cap(1):
            held = _Held(1)
            threading.Timer(0.5, held.release).start()
            t0 = time.monotonic()
            with self.assertRaises(llm.LLMError):
                llm.chat_tools(_MSG, [], 10, backend=_BE, timeout=1.0,
                               transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, content=drip())))
            took = time.monotonic() - t0
        self.assertLess(took, 1.4, "wait + reply outlived the one deadline")

    def test_slots_come_back_after_every_kind_of_failure(self):
        def refuse(req):
            raise httpx.ConnectError("down", request=req)
        failures = [
            refuse,
            lambda req: httpx.Response(500, text="boom"),
            lambda req: httpx.Response(200, text="<html>login</html>"),
            lambda req: httpx.Response(200, json={"choices": []}),
            lambda req: (_ for _ in ()).throw(RuntimeError("bug")),
        ]
        with _cap(2):
            for handler in failures * 2:
                with self.assertRaises(Exception):
                    llm.chat_tools(_MSG, [], 10, backend=_BE, timeout=2.0,
                                   transport=httpx.MockTransport(handler))
                with self.assertRaises(Exception):
                    llm._chat(_MSG, 10, backend=_BE,
                              transport=httpx.MockTransport(handler))
            self.assertEqual(_free_slots(), 2)

    def test_a_waiter_fails_fast_past_the_short_wait(self):
        with _cap(1), mock.patch.object(llm, "SLOT_WAIT_S", 0.3):
            held = _Held(1)
            try:
                t0 = time.monotonic()
                with self.assertRaises(llm.LLMBusy):
                    llm.chat_tools(_MSG, [], 10, backend=_BE, timeout=60.0,
                                   transport=httpx.MockTransport(
                        lambda req: httpx.Response(200, json=_OK)))
                waited = time.monotonic() - t0
                t0 = time.monotonic()
                with mock.patch.object(llm, "REQUEST_TIMEOUT", 60.0), \
                        self.assertRaises(llm.LLMBusy):
                    llm._chat(_MSG, 10, backend=_BE,
                              transport=httpx.MockTransport(
                        lambda req: httpx.Response(200, json=_OK)))
                waited_cat = time.monotonic() - t0
            finally:
                held.release()
            self.assertGreaterEqual(waited, 0.25, "it did not wait at all")
            self.assertLess(waited, 1.3, "it waited out the reply deadline")
            self.assertLess(waited_cat, 1.3,
                            "the categorizer waited out the reply deadline")
            self.assertEqual(_free_slots(), 1)

    def test_the_short_wait_never_outlasts_the_reply_deadline(self):
        with _cap(1), mock.patch.object(llm, "SLOT_WAIT_S", 30.0):
            held = _Held(1)
            try:
                t0 = time.monotonic()
                with self.assertRaises(llm.LLMBusy):
                    llm.chat_tools(_MSG, [], 10, backend=_BE, timeout=0.3,
                                   transport=httpx.MockTransport(
                        lambda req: httpx.Response(200, json=_OK)))
                waited = time.monotonic() - t0
            finally:
                held.release()
            self.assertLess(waited, 1.3)
            self.assertEqual(_free_slots(), 1)

    def test_a_refused_endpoint_takes_no_slot(self):
        with _cap(1):
            with self.assertRaises(llm.LLMError):
                llm._chat(_MSG, 10, backend=dict(_BE, url=""))
            self.assertEqual(_free_slots(), 1)


class SizingTests(unittest.TestCase):
    _KEYS = ("OIKONOME_LLM_CONCURRENCY", "OIKONOME_POOL_MAX",
             "OIKONOME_LLM_SLOT_WAIT_S")

    def _env(self, env):
        clean = {k: v for k, v in os.environ.items() if k not in self._KEYS}
        return mock.patch.dict(os.environ, {**clean, **env}, clear=True)

    def _slots(self, reads=1, **env):
        from oikonome.engine import receipts
        with self._env(env), mock.patch.object(receipts, "PARSE_SLOTS", reads):
            return llm._slots_from_env()

    def test_default_is_a_quarter_of_the_pool(self):
        self.assertEqual(self._slots(), 2)                    # pool 10
        self.assertEqual(self._slots(OIKONOME_POOL_MAX="20"), 5)
        self.assertEqual(self._slots(OIKONOME_POOL_MAX="3"), 1)
        self.assertEqual(self._slots(OIKONOME_POOL_MAX=""), 2)

    def test_default_never_runs_fewer_slots_than_receipt_reads(self):
        from oikonome.engine import receipts
        with self._env({}):
            self.assertGreaterEqual(llm._slots_from_env(),
                                    receipts.PARSE_SLOTS)
        self.assertEqual(self._slots(reads=3), 3)             # pool 10
        self.assertEqual(self._slots(reads=3, OIKONOME_POOL_MAX="3"), 3)
        self.assertEqual(self._slots(reads=3, OIKONOME_POOL_MAX="40"), 10)

    def test_the_setting_wins_and_a_bad_value_falls_back(self):
        self.assertEqual(self._slots(OIKONOME_LLM_CONCURRENCY="6"), 6)
        self.assertEqual(self._slots(OIKONOME_LLM_CONCURRENCY=""), 2)
        self.assertEqual(self._slots(OIKONOME_LLM_CONCURRENCY="lots"), 2)
        self.assertEqual(self._slots(OIKONOME_LLM_CONCURRENCY="0"), 1)
        self.assertEqual(self._slots(OIKONOME_POOL_MAX="x"), 2)

    def test_the_slot_wait_is_short_and_bounded(self):
        def wait(**env):
            with self._env(env):
                return llm._slot_wait_from_env()
        self.assertEqual(wait(), 5.0)
        self.assertEqual(wait(OIKONOME_LLM_SLOT_WAIT_S=""), 5.0)
        self.assertEqual(wait(OIKONOME_LLM_SLOT_WAIT_S="soon"), 5.0)
        self.assertEqual(wait(OIKONOME_LLM_SLOT_WAIT_S="nan"), 5.0)
        self.assertEqual(wait(OIKONOME_LLM_SLOT_WAIT_S="2.5"), 2.5)
        self.assertEqual(wait(OIKONOME_LLM_SLOT_WAIT_S="-4"), 0.0)
        self.assertEqual(wait(OIKONOME_LLM_SLOT_WAIT_S="99999"), 60.0)


def _read(**kw):
    """A receipt read's call: the categorizer path, no deadline argument."""
    return llm._chat(_MSG, 10, backend=kw["backend"],
                     transport=kw["transport"])


class InteractiveReserveTests(unittest.TestCase):
    """The assistant shares the process's model calls with receipt reads,
    tax-document reads and the categorizer. Receipt reads alone can fill
    every shared slot (the default is sized to them), so the assistant has
    one slot of its own: a sweep of reads never makes a question fail as
    busy, and one question in flight never takes the slot a read was
    promised."""

    def _shared(self, n):
        return mock.patch.multiple(llm, LLM_SLOTS=n,
                                   _LLM_SLOTS=threading.BoundedSemaphore(n),
                                   _ASSISTANT_SLOT=threading.BoundedSemaphore(1),
                                   SLOT_WAIT_S=0.3)

    def test_an_assistant_call_gets_a_slot_while_reads_hold_them_all(self):
        with self._shared(3):
            reads = _Held(3, fn=_read)
            try:
                got = llm.chat_tools(_MSG, [], 10, backend=_BE, timeout=5.0,
                                     transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, json=_OK)))
            finally:
                reads.release()
            self.assertEqual(got["content"], "{}")
            self.assertEqual(_free_slots(), 3)

    def test_a_question_in_flight_leaves_every_read_its_slot(self):
        with self._shared(3):
            ask = _Held(1)
            try:
                reads = _Held(3, fn=_read)      # would raise past the cap
                reads.release()
            finally:
                ask.release()
            self.assertEqual(_free_slots(), 3)


if __name__ == "__main__":
    unittest.main()


class ReceiptBusyTests(unittest.TestCase):
    def test_a_receipt_read_with_no_slot_is_recorded_as_busy(self):
        """A read that waited past the slot wait is recorded with the busy
        sentence, which is the one the receipt sweep retries."""
        from oikonome.engine import receipts
        self.assertEqual(
            receipts._friendly_error(llm.LLMBusy("no slot"), True),
            receipts.BUSY_ERROR)
        self.assertNotEqual(
            receipts._friendly_error(RuntimeError("other"), True),
            receipts.BUSY_ERROR)

