"""Receipt reads in the web process share a few read turns. Handed out first
come, first served, one household uploading a batch takes every turn and
every other household's read waits behind all of it. A freed turn goes to
the waiting household holding the fewest turns (ties by arrival), and no
turn is ever left idle: a household alone may use them all.

The nightly pass reads households one after another, so what one household
can take from the rest there is time; its receipt reads stop claiming new
receipts once a fixed time has gone, and the next night carries on."""
import json
import random
import threading
import time
import unittest
import uuid
from unittest import mock

import httpx

from oikonome.engine import receipts
from oikonome.web import api

from .util import TODAY, add_txn, make_db, write_config

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def _wait_for(pred, timeout=10.0):
    """Polls a condition the threads under test reach on their own; the
    outcome never depends on how long this waits, only whether it gets
    there."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.001)
    return pred()


class _Waiter(threading.Thread):
    """One read queueing for a turn on its own thread."""

    def __init__(self, turns, tenant, timeout=30.0):
        super().__init__(daemon=True)
        self.turns, self.tenant, self.timeout = turns, tenant, timeout
        self.got = None
        self.gave_back = False
        self.done = threading.Event()

    def run(self):
        try:
            self.got = self.turns.acquire(self.tenant, self.timeout)
        finally:
            self.done.set()


def _queued(turns, n):
    return lambda: len(turns.snapshot()["waiting"]) == n


class FairTurnTests(unittest.TestCase):
    def _start(self, turns, tenant, timeout=30.0):
        """Start one read and return once it either holds a turn or sits
        in the queue, so arrival order is the order of these calls."""
        before = turns.snapshot()
        w = _Waiter(turns, tenant, timeout)
        w.start()
        self.assertTrue(_wait_for(
            lambda: w.done.is_set()
            or len(turns.snapshot()["waiting"])
            == len(before["waiting"]) + 1))
        self.addCleanup(self._settle, turns, w)
        return w

    def _settle(self, turns, w):
        # leave no thread blocked behind a failed assertion
        while not w.done.wait(0.01):
            for t in list(turns.snapshot()["held"]):
                turns.release(t)
                break

    def test_a_household_alone_may_use_every_read_turn(self):
        turns = receipts._FairTurns(3)
        ws = [self._start(turns, "A") for _ in range(3)]
        self.assertTrue(all(w.done.is_set() and w.got for w in ws))
        snap = turns.snapshot()
        self.assertEqual(snap["free"], 0)
        self.assertEqual(snap["held"], {"A": 3})

    def test_a_household_with_a_batch_waiting_cannot_delay_anothers_read(self):
        # three turns; A queues ten reads, B one: B's read starts before
        # A's fourth
        turns = receipts._FairTurns(3)
        a = [self._start(turns, "A") for _ in range(10)]
        b = self._start(turns, "B")
        self.assertEqual(sum(w.done.is_set() for w in a), 3)
        self.assertFalse(b.done.is_set())
        turns.release("A")
        self.assertTrue(b.done.wait(5))
        self.assertTrue(b.got)
        self.assertEqual(sum(w.done.is_set() for w in a), 3)
        self.assertEqual(turns.snapshot()["held"], {"A": 2, "B": 1})

    def test_a_freed_turn_goes_to_the_waiting_household_holding_fewest(self):
        turns = receipts._FairTurns(3)
        self._start(turns, "A")
        self._start(turns, "A")
        self._start(turns, "B")
        a3 = self._start(turns, "A")          # arrived first, holds 2
        b2 = self._start(turns, "B")          # arrived later, holds 1
        c1 = self._start(turns, "C")          # arrived last, holds 0
        turns.release("A")
        self.assertTrue(c1.done.wait(5))
        self.assertFalse(a3.done.is_set() or b2.done.is_set())
        # A 1, B 1, C 1: the tie goes to whoever arrived first
        turns.release("C")
        self.assertTrue(_wait_for(lambda: a3.done.is_set()
                                  or b2.done.is_set()))
        self.assertTrue(a3.done.is_set())
        self.assertFalse(b2.done.is_set())

    def test_a_household_holding_every_turn_gets_none_back_while_another_waits(self):
        turns = receipts._FairTurns(3)
        a = [self._start(turns, "A") for _ in range(6)]
        b = [self._start(turns, "B") for _ in range(2)]
        self.assertEqual(turns.snapshot()["held"], {"A": 3})
        turns.release("A")
        self.assertTrue(b[0].done.wait(5))
        for _ in range(3):
            done = sum(w.done.is_set() for w in a + b)
            turns.release("A")
            self.assertTrue(_wait_for(
                lambda: sum(w.done.is_set() for w in a + b) == done + 1))
            # while B still has a read waiting, A never gets its third back
            if not b[1].done.is_set():
                self.assertLessEqual(turns.snapshot()["held"]["A"], 2)
        self.assertEqual(turns.snapshot()["free"], 0)

    def test_level_households_take_turns_in_arrival_order(self):
        turns = receipts._FairTurns(1)
        self._start(turns, "A")
        order = []
        ws = [self._start(turns, t) for t in ("B", "C", "A", "B", "C")]
        for _ in range(len(ws)):
            holder = next(iter(turns.snapshot()["held"]))
            turns.release(holder)
            self.assertTrue(_wait_for(
                lambda: sum(w.done.is_set() for w in ws) == len(order) + 1))
            order.append(next(iter(turns.snapshot()["held"])))
        self.assertEqual(order, ["B", "C", "A", "B", "C"])

    def test_a_waiter_that_times_out_leaves_the_queue_and_the_next_is_served(self):
        turns = receipts._FairTurns(1)
        self._start(turns, "A")
        quick = self._start(turns, "B", timeout=0.05)
        slow = self._start(turns, "C")
        self.assertTrue(quick.done.wait(5))
        self.assertFalse(quick.got)
        self.assertEqual(turns.snapshot()["waiting"], ["C"])
        turns.release("A")
        self.assertTrue(slow.done.wait(5))
        self.assertTrue(slow.got)

    def test_two_turns_freed_at_once_reach_two_waiting_households(self):
        # both turns come free before either waiter runs; whichever wakes
        # first, the one served must not leave the other asleep beside a
        # free turn. Wake order is the scheduler's, so this is repeated.
        for _ in range(200):
            turns = receipts._FairTurns(2)
            self.assertTrue(turns.acquire("C", 0))
            self.assertTrue(turns.acquire("C", 0))
            a = _Waiter(turns, "A", timeout=5)
            a.start()
            self.assertTrue(_wait_for(_queued(turns, 1)))
            b = _Waiter(turns, "B", timeout=5)
            b.start()
            self.assertTrue(_wait_for(_queued(turns, 2)))
            with turns._cond:             # re-entrant: both land at once
                turns.release("C")
                turns.release("C")
            self.assertTrue(a.done.wait(5) and b.done.wait(5))
            self.assertTrue(a.got and b.got,
                            f"stranded beside a free turn: {turns.snapshot()}")

    def test_a_release_by_a_household_holding_nothing_is_refused(self):
        turns = receipts._FairTurns(2)
        self.assertTrue(turns.acquire("A", 0))
        with self.assertRaises(ValueError):
            turns.release("B")
        turns.release("A")
        with self.assertRaises(ValueError):
            turns.release("A")
        self.assertEqual(turns.snapshot(),
                         {"free": 2, "held": {}, "waiting": []})


def _drain(turns, waiters):
    """Release turns until every waiter has had one and given it back."""
    while True:
        held = turns.snapshot()["held"]
        if not held and all(w.done.is_set() for w in waiters):
            return
        for t, n in held.items():
            for _ in range(n):
                turns.release(t)
        _wait_for(lambda: all(w.done.is_set() for w in waiters)
                  or turns.snapshot()["held"], 1)


class FairTurnPropertyTests(unittest.TestCase):
    """Random schedules of acquire, release and timed-out acquire, checked
    after every step once the threads have settled."""

    def _settled(self, turns, live):
        snap = turns.snapshot()
        queued = [w for w in live if not w.done.is_set()]
        return (len(snap["waiting"]) == len(queued)
                and not (snap["free"] > 0 and snap["waiting"]))

    def test_random_schedules_keep_every_turn_and_serve_the_fewest_first(self):
        rng = random.Random(48173)
        for schedule in range(500):
            slots = rng.randint(1, 5)
            homes = [f"h{i}" for i in range(rng.randint(1, 4))]
            turns = receipts._FairTurns(slots)
            live = []                 # every blocking waiter, arrival order
            holders = []              # waiters holding a turn now
            try:
                for _step in range(rng.randint(5, 30)):
                    queued = [w for w in live if not w.done.is_set()]
                    move = rng.random()
                    if holders and move < 0.4:
                        w = holders.pop(rng.randrange(len(holders)))
                        w.gave_back = True
                        held = dict(turns.snapshot()["held"])
                        held[w.tenant] -= 1
                        # the rule, worked out from the queue as it stands
                        expect = min(
                            queued, key=lambda q: (held.get(q.tenant, 0),
                                                   live.index(q)),
                            default=None)
                        turns.release(w.tenant)
                        if expect is not None:
                            self.assertTrue(expect.done.wait(5),
                                            f"schedule {schedule}: lost wake")
                            self.assertTrue(expect.got)
                    elif move < 0.55:
                        w = _Waiter(turns, rng.choice(homes), timeout=0.002)
                        w.start()
                        self.assertTrue(w.done.wait(5))
                        if w.got:
                            # only when nothing waited and a turn was free
                            self.assertFalse(queued)
                            holders.append(w)
                    else:
                        w = _Waiter(turns, rng.choice(homes))
                        live.append(w)
                        w.start()
                    self.assertTrue(_wait_for(
                        lambda: self._settled(turns, live)),
                        f"schedule {schedule}: a waiter is stuck while "
                        f"a turn is free: {turns.snapshot()}")
                    for w in live:
                        if (w.done.is_set() and w.got and not w.gave_back
                                and w not in holders):
                            holders.append(w)
                    snap = turns.snapshot()
                    self.assertEqual(
                        sum(snap["held"].values()) + snap["free"], slots,
                        f"schedule {schedule}")
                    by_home = {}
                    for w in holders:
                        by_home[w.tenant] = by_home.get(w.tenant, 0) + 1
                    self.assertEqual(by_home, snap["held"],
                                     f"schedule {schedule}")
            finally:
                _drain(turns, live)

    def test_two_households_waiting_throughout_never_drift_apart(self):
        rng = random.Random(7)
        for schedule in range(100):
            slots = rng.randint(2, 5)
            turns = receipts._FairTurns(slots)
            ws = [_Waiter(turns, h) for h in ("A", "B")
                  for _ in range(slots * 3)]
            rng.shuffle(ws)
            try:
                for i, w in enumerate(ws):
                    w.start()
                    self.assertTrue(_wait_for(
                        lambda: len(turns.snapshot()["waiting"])
                        + sum(x.done.is_set() for x in ws) == i + 1))
                held = turns.snapshot()["held"]
                gap = abs(held.get("A", 0) - held.get("B", 0))
                while {"A", "B"} <= set(turns.snapshot()["waiting"]):
                    done = sum(w.done.is_set() for w in ws)
                    turns.release(rng.choice(sorted(turns.snapshot()["held"])))
                    self.assertTrue(_wait_for(
                        lambda: sum(w.done.is_set() for w in ws) == done + 1))
                    held = turns.snapshot()["held"]
                    now = abs(held.get("A", 0) - held.get("B", 0))
                    # a gap from before both queued only closes; once
                    # level within one, it stays there
                    self.assertLessEqual(now, max(1, gap),
                                         f"schedule {schedule}")
                    gap = now
            finally:
                _drain(turns, ws)


class ReadTurnDoorTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        self.addCleanup(self.conn.close)

    def test_a_read_that_raises_still_gives_its_turn_back(self):
        n = receipts.PARSE_SLOTS * 2
        with mock.patch.object(receipts, "parse_one",
                               side_effect=RuntimeError("boom")) as one:
            for _ in range(n):
                api._parse_receipt_async(self.tid, str(uuid.uuid4()))
            self.assertTrue(_wait_for(lambda: one.call_count == n))
        self.assertTrue(_wait_for(
            lambda: receipts._PARSE_TURNS.snapshot()
            == {"free": receipts.PARSE_SLOTS, "held": {}, "waiting": []}))

    def test_the_turn_api_gives_back_what_each_household_took(self):
        self.assertTrue(receipts.wait_for_parse_turn(self.tid))
        try:
            self.assertEqual(receipts._PARSE_TURNS.snapshot()["held"],
                             {self.tid: 1})
        finally:
            receipts.end_parse_turn(self.tid)
        with self.assertRaises(ValueError):
            receipts.end_parse_turn(self.tid)


class _HeldReads:
    """A parse_one whose every read blocks until the test lets that one
    finish, recording the order reads start in."""

    def __init__(self):
        self.lock = threading.Lock()
        self.started = []
        self.go = {}
        self.all = False

    def __call__(self, conn, receipt_id, claimed=False, **kw):
        ev = threading.Event()
        with self.lock:
            if self.all:
                ev.set()
            self.go[receipt_id] = ev
            self.started.append(receipt_id)
        ev.wait(30)
        return {"status": "parsed"}

    def finish_all(self):
        with self.lock:
            self.all = True
            for ev in self.go.values():
                ev.set()


class TwoHouseholdsThroughTheUploadDoorTests(unittest.TestCase):
    def setUp(self):
        self.conns = [make_db(), make_db()]
        for c in self.conns:
            self.addCleanup(c.close)
        self.a, self.b = (str(c.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
            for c in self.conns)

    def test_one_households_batch_does_not_delay_anothers_single_read(self):
        turns = receipts._FairTurns(3)
        reads = _HeldReads()
        for name, value in (("_PARSE_TURNS", turns), ("parse_one", reads)):
            patcher = mock.patch.object(receipts, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # cleanups run last-first: every read finishes and gives its turn
        # back while the patched queue is still the one in use
        self.addCleanup(lambda: _wait_for(
            lambda: turns.snapshot()["held"] == {}
            and not turns.snapshot()["waiting"]))
        self.addCleanup(reads.finish_all)
        a_reads = [f"a-{i}" for i in range(10)]
        for i, rid in enumerate(a_reads):
            api._parse_receipt_async(self.a, rid)
            # each read is running or queued before the next arrives
            self.assertTrue(_wait_for(
                lambda: len(reads.started)
                + len(turns.snapshot()["waiting"]) == i + 1))
        self.assertTrue(_wait_for(lambda: len(reads.started) == 3))
        api._parse_receipt_async(self.b, "b-0")
        self.assertTrue(_wait_for(
            lambda: len(turns.snapshot()["waiting"]) == 8))
        reads.go[reads.started[0]].set()      # A's first read answers
        self.assertTrue(_wait_for(lambda: len(reads.started) == 4))
        self.assertEqual(reads.started[3], "b-0")
        self.assertEqual(turns.snapshot()["held"],
                         {self.a: 2, self.b: 1})
        reads.finish_all()
        self.assertTrue(_wait_for(lambda: len(reads.started) == 11))
        self.assertTrue(_wait_for(
            lambda: turns.snapshot()
            == {"free": 3, "held": {}, "waiting": []}))


class NightlyReadTimeTests(unittest.TestCase):
    def setUp(self):
        from oikonome.engine import budget
        self.conn = make_db()
        write_config(self.conn)
        cfg = budget.load_config(self.conn)
        cfg["llm_url"] = "http://llm.test"
        cfg["llm_model"] = "vision-model"
        budget.save_config(self.conn, cfg)
        self.addCleanup(self.conn.close)
        txn = add_txn(self.conn, TODAY, 10.0, "STORE")
        self.rids = []
        for day in range(3):
            rid = receipts.add(self.conn, txn, _png(), "image/png")
            self.conn.execute(
                "UPDATE receipts SET created_at = now() - make_interval("
                "days => %s) WHERE id=%s::uuid", (10 - day, rid))
            self.rids.append(rid)

    def _status(self):
        return [self.conn.execute(
            "SELECT status FROM receipts WHERE id=%s::uuid",
            (r,)).fetchone()["status"] for r in self.rids]

    def test_one_households_slow_reads_stop_after_its_nightly_time(self):
        now = [1000.0]

        def slow_model(request):
            # each read takes just past the household's 300 s nightly time
            now[0] += 301
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps({"merchant": "X", "total": 10.0,
                                       "items": []})}}]})
        transport = httpx.MockTransport(slow_model)
        with mock.patch.object(receipts, "_clock", lambda: now[0],
                               create=True):
            out = receipts.parse_pending(self.conn, transport=transport)
            # the first read always runs; the second is never claimed
            self.assertEqual(out["parsed"], 1)
            self.assertEqual(self._status(),
                             ["parsed", "uploaded", "uploaded"])
            # the next night carries on with the oldest still unread
            out = receipts.parse_pending(self.conn, transport=transport)
            self.assertEqual(out["parsed"], 1)
            self.assertEqual(self._status(),
                             ["parsed", "parsed", "uploaded"])

    def test_quick_reads_all_fit_in_one_night(self):
        now = [1000.0]

        def model(request):
            now[0] += 1
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps({"merchant": "X", "total": 10.0,
                                       "items": []})}}]})
        with mock.patch.object(receipts, "_clock", lambda: now[0],
                               create=True):
            out = receipts.parse_pending(
                self.conn, transport=httpx.MockTransport(model))
        self.assertEqual(out["parsed"], 3)


def _png(w=600, h=800):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
