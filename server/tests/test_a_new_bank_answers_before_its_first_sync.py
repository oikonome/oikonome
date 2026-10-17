"""A finished bank link is answered as soon as the Item is saved; its first
pull runs on the worker.

A first pull of a multi-year ledger runs for minutes. Run inside the
client's status poll, it held a pooled database connection and the
household's sync lock long after the edge proxy had stopped waiting for
the answer, and a few households finishing Link at once took every
connection the web process had. So the poll exchanges the token, saves the
Item, answers "linked, first sync pending" and queues the pull; the queued
job pulls transactions and then holdings and liabilities, and one that
finds another sync holding the lock is tried again rather than dropped
(the holder covers the transactions; only this job pulls the products).

The other doors respect that: the welcome wizard's backfill nudge reads a
new Item whose first pull has not finished as "syncing" and queues that
job again under its one id instead of pulling in the web process; a Plaid
webhook pull that reaches the Item first does its first pull (nothing
announced as new charges, holdings included); and only a few first pulls
run at once, so a burst of new households does not take every job slot
and pooled connection the worker has."""

import asyncio
import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.jobs import worker
from oikonome.sync import plaid
from oikonome.web import pages

from .test_plaid_link_pages import _patch_client
from .util import _ensure_db, seed_accounts, write_config

SLOW_SYNC_S = 3.0


class _FakeRedis:
    """The few calls the worker makes on arq's Redis pool, kept in a dict."""

    def __init__(self):
        self.store: dict = {}

    async def incr(self, name):
        self.store[name] = self.store.get(name, 0) + 1
        return self.store[name]

    async def expire(self, name, seconds):
        return name in self.store

    async def delete(self, *names):
        for n in names:
            self.store.pop(n, None)
        return len(names)


class FirstSyncLeavesThePollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"fs-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = str(cls.client.get("/api/me").json()["tenant_id"])
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn, plaid_client_id="cid", plaid_secret="sec",
                         plaid_env="sandbox")
        finally:
            conn.close()

    def setUp(self):
        pages._PLAID_LINK.clear()
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM plaid_link_sessions WHERE tenant=%s",
                          (self.tid,))
        finally:
            admin.close()

    def _link(self, token: str, item_id: str, queued: list) -> dict:
        state = {"link_token": token, "item_id": item_id}
        with _patch_client(state), mock.patch.object(
                worker, "queue_first_sync",
                lambda t, i: queued.append((str(t), i))):
            r = self.client.post("/accounts/plaid/link", data={},
                                 follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            state["link_done"] = True
            return self.client.get(
                f"/accounts/plaid/link/{token}/status").json()

    def test_the_poll_answers_without_waiting_for_a_slow_first_sync(self):
        calls = []

        def slow(*a, **k):
            calls.append("sync")
            time.sleep(SLOW_SYNC_S)
            return {"added": 0}
        queued = []
        with mock.patch.object(plaid, "sync", slow), \
                mock.patch.object(plaid, "sync_products",
                                  lambda *a, **k: calls.append("products")):
            t0 = time.monotonic()
            s = self._link("lt-fs-slow", "pl-fs-slow", queued)
            took = time.monotonic() - t0
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add")
        self.assertEqual(s["first_sync"], "pending")
        self.assertLess(took, SLOW_SYNC_S)
        self.assertEqual(calls, [])                 # nothing pulled inline
        self.assertEqual(queued, [(self.tid, "pl-fs-slow")])
        # the household's sync lock was let go with the answer
        conn = tenancy.tenant_connect(self.tid)
        try:
            got = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (f"oikonome:sync:{self.tid}",)).fetchone()["ok"]
            self.assertTrue(got)
            tenancy.release_lock(conn, f"oikonome:sync:{self.tid}")
        finally:
            conn.close()

    def test_the_queued_job_gives_the_new_item_its_first_sync(self):
        queued = []
        s = self._link("lt-fs-job", "pl-fs-job", queued)
        self.assertEqual(s["first_sync"], "pending")
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertIsNone(conn.execute(
                "SELECT tx_cursor FROM items WHERE id='pl-fs-job'"
            ).fetchone()["tx_cursor"])
        finally:
            conn.close()
        products = []
        real_products = plaid.sync_products
        with _patch_client({}), mock.patch.object(
                plaid, "sync_products",
                lambda c, i, **k: (products.append(i),
                                   real_products(c, i, **k))[1]):
            for t, i in queued:
                asyncio.run(worker.sync_item({"job_try": 1}, t, i, True))
        self.assertEqual(products, ["pl-fs-job"])
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertEqual(conn.execute(
                "SELECT tx_cursor FROM items WHERE id='pl-fs-job'"
            ).fetchone()["tx_cursor"], "c-end")
        finally:
            conn.close()

    def test_a_first_sync_behind_a_running_sync_is_tried_again(self):
        queued = []
        self._link("lt-fs-busy", "pl-fs-busy", queued)
        from arq.worker import Retry
        holder = tenancy.tenant_connect(self.tid)
        key = f"oikonome:sync:{self.tid}"
        try:
            self.assertTrue(holder.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (key,)).fetchone()["ok"])
            with _patch_client({}), \
                    mock.patch.object(worker, "_enqueue_tenant_sync"):
                with self.assertRaises(Retry):
                    asyncio.run(worker.sync_item({"job_try": 2}, self.tid,
                                                 "pl-fs-busy", True))
                # a webhook's pull behind the same holder is covered by
                # the holder, and is not retried
                r = asyncio.run(worker.sync_item({"job_try": 1}, self.tid,
                                                 "pl-fs-busy"))
                self.assertEqual(r, {"skipped": "already-running"})
        finally:
            tenancy.release_lock(holder, key)
            holder.close()


class OtherDoorsLeaveTheFirstSyncToTheWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FirstSyncLeavesThePollTests.setUpClass.__func__(cls)

    def _item(self, iid: str) -> None:
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("DELETE FROM items WHERE aggregator='plaid'")
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, "
                "access_token) VALUES (%s, 'plaid', 'New Bank', 'tok')",
                (iid,))
        finally:
            conn.close()

    def test_the_wizard_nudge_queues_the_first_sync_and_pulls_nothing(self):
        self._item("pl-nudge-new")
        r = self.client.get("/api/onboarding/backfill").json()
        self.assertTrue(r["syncing"],
                        "a new bank whose first pull is queued reads as "
                        "idle, so the nudge would start a second pull")
        self.assertTrue(r["items"][0]["active"])
        queued = []
        with mock.patch.object(worker, "sync_tenant") as st, \
                mock.patch.object(worker, "_sync_item_body") as body, \
                mock.patch.object(
                    worker, "_enqueue_in_background",
                    lambda what, job, *a, job_id=None, **kw:
                        queued.append((job, a, job_id))):
            r = self.client.post("/api/onboarding/backfill/nudge")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["syncing"])
        st.assert_not_called()
        body.assert_not_called()
        self.assertEqual(queued, [("sync_item",
                                   (self.tid, "pl-nudge-new", True),
                                   worker.first_sync_job_id("pl-nudge-new"))])

    def test_the_link_poll_queues_the_first_sync_under_the_same_id(self):
        seen = []
        with mock.patch.object(
                worker, "_enqueue_in_background",
                lambda what, job, *a, job_id=None, **kw:
                    seen.append(job_id)):
            worker.queue_first_sync(self.tid, "pl-x")
        self.assertEqual(seen, [worker.first_sync_job_id("pl-x")])

    def test_a_webhook_pull_that_reaches_a_new_item_first_is_its_first(self):
        self._item("pl-hook-first")
        announced, products = [], []
        with _patch_client({}), \
                mock.patch.object(worker, "_recent_txn_ids",
                                  lambda c: {"seen"}), \
                mock.patch.object(
                    worker, "_webhooks_after_sync",
                    lambda c, res, before, tid="": announced.append(before)), \
                mock.patch.object(plaid, "sync_products",
                                  lambda c, i, **k: products.append(i)):
            asyncio.run(worker.sync_item({"job_try": 1}, self.tid,
                                         "pl-hook-first"))
            # the next webhook pull of the same Item is an ordinary one
            asyncio.run(worker.sync_item({"job_try": 1}, self.tid,
                                         "pl-hook-first"))
        self.assertEqual(announced, [None, {"seen"}],
                         "the new bank's history went out as new charges")
        self.assertEqual(products, ["pl-hook-first"])

    def test_only_a_few_first_syncs_run_at_once(self):
        from arq.worker import Retry
        slots = worker._FIRST_SYNC_SLOTS
        taken = 0
        while slots.acquire(blocking=False):
            taken += 1
        self.assertEqual(taken, worker.FIRST_SYNC_CONCURRENCY)
        try:
            with mock.patch.object(worker, "_sync_item_body") as body:
                with self.assertRaises(Retry):
                    asyncio.run(worker.sync_item({"job_try": 1}, self.tid,
                                                 "pl-wait", True))
                body.assert_not_called()
        finally:
            for _ in range(taken):
                slots.release()
        # a finished first pull gives its turn back
        with mock.patch.object(worker, "_sync_item_body",
                               return_value={"added": 0}):
            for _ in range(worker.FIRST_SYNC_CONCURRENCY + 1):
                asyncio.run(worker.sync_item({"job_try": 1}, self.tid,
                                             "pl-turn", True))
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_a_webhook_pull_that_is_a_first_pull_waits_for_a_turn(self):
        from arq.worker import Retry
        self._item("pl-hook-wait")
        slots = worker._FIRST_SYNC_SLOTS
        taken = 0
        while slots.acquire(blocking=False):
            taken += 1
        try:
            with mock.patch.object(worker, "_sync_item_body") as body:
                with self.assertRaises(Retry):
                    asyncio.run(worker.sync_item({"job_try": 1}, self.tid,
                                                 "pl-hook-wait"))
                body.assert_not_called()
                # an Item a pull already reached is an ordinary pull, and
                # never waits behind the first pulls
                conn = tenancy.tenant_connect(self.tid)
                try:
                    conn.execute("UPDATE items SET linked_at = now() - "
                                 "interval '2 days' WHERE id='pl-hook-wait'")
                finally:
                    conn.close()
                asyncio.run(worker.sync_item({"job_try": 1}, self.tid,
                                             "pl-hook-wait"))
                body.assert_called_once_with(self.tid, "pl-hook-wait")
        finally:
            for _ in range(taken):
                slots.release()

    def test_a_deep_backlog_of_first_pulls_all_get_their_turn(self):
        """25 new Items behind 3 turns: every one is pulled, none runs out
        of tries while it waits, and turns go oldest job first."""
        import datetime as dt
        import threading
        import time as _time

        from arq.worker import Retry
        from oikonome.jobs.worker import WorkerSettings
        max_tries = next(f for f in WorkerSettings.functions
                         if getattr(f, "name", None) == "sync_item").max_tries
        turns = worker._FirstSyncTurns(3)
        running, peak, started = [0], [0], []
        lock = threading.Lock()

        def body(tid, iid, first=False):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
                started.append(iid)
            _time.sleep(0.03)
            with lock:
                running[0] -= 1
            return {"added": 0}

        base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)

        async def job(n):
            ctx = {"job_id": f"first-sync:pl-{n:02d}", "job_try": 0,
                   "enqueue_time": base + dt.timedelta(seconds=n)}
            while True:
                ctx["job_try"] += 1
                if ctx["job_try"] > max_tries:
                    return "dropped"
                try:
                    await worker.sync_item(ctx, self.tid, f"pl-{n:02d}",
                                           True)
                    return "done"
                except Retry:
                    await asyncio.sleep(0.002)

        async def burst():
            return await asyncio.gather(*(job(n) for n in range(25)))
        with mock.patch.object(worker, "_FIRST_SYNC_SLOTS", turns), \
                mock.patch.object(worker, "_sync_item_body", body):
            results = asyncio.run(burst())
        self.assertEqual(results, ["done"] * 25)
        self.assertLessEqual(peak[0], 3)
        # oldest first: free turns go to the oldest jobs asking, so a job
        # starts at most a turn-count away from its place in the queue
        self.assertLess(max(abs(started.index(f"pl-{n:02d}") - n)
                            for n in range(25)), 3, started)
        # far more waits than the job's real attempts would allow
        self.assertGreater(max_tries, worker.SYNC_ITEM_MAX_TRIES)

    def test_waiting_for_a_turn_spends_none_of_the_real_attempts(self):
        from arq.worker import Retry
        turns = worker._FirstSyncTurns(1)
        self.assertTrue(turns.acquire())
        waited = worker.SYNC_ITEM_MAX_TRIES + 5
        for _ in range(waited):
            self.assertFalse(turns.take("job-a", 1.0))
        turns.release()
        # the try after all that waiting is the job's first real attempt:
        # behind a busy household lock it is tried again, not dropped
        ctx = {"job_id": "job-a", "job_try": waited + 1}
        with mock.patch.object(worker, "_FIRST_SYNC_SLOTS", turns), \
                mock.patch.object(worker, "_sync_item_body",
                                  return_value={"skipped":
                                                "already-running"}):
            with self.assertRaises(Retry):
                asyncio.run(worker.sync_item(ctx, self.tid, "pl-a", True))

    def test_a_webhook_whose_item_another_job_pulled_leaves_the_queue(self):
        """A webhook pull that waited for a turn because its Item had never
        been pulled, and comes back to find another job pulled it, must not
        keep its place: an old enqueue time stays ahead of every younger
        first pull and would hold one of the few turns while nothing runs."""
        import datetime as dt

        from arq.worker import Retry
        self._item("pl-hook-ghost")
        turns = worker._FirstSyncTurns(1)
        self.assertTrue(turns.acquire())
        ctx = {"job_id": "hook-ghost", "job_try": 1,
               "enqueue_time": dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(minutes=5)}
        with mock.patch.object(worker, "_FIRST_SYNC_SLOTS", turns), \
                mock.patch.object(worker, "_sync_item_body",
                                  return_value={"added": 0}) as body:
            with self.assertRaises(Retry):
                asyncio.run(worker.sync_item(ctx, self.tid, "pl-hook-ghost"))
            # the Item's own first-sync job takes the turn and pulls it
            turns.release()
            conn = tenancy.tenant_connect(self.tid)
            try:
                conn.execute("UPDATE items SET linked_at = now() - "
                             "interval '2 days' WHERE id='pl-hook-ghost'")
            finally:
                conn.close()
            ctx["job_try"] = 2
            asyncio.run(worker.sync_item(ctx, self.tid, "pl-hook-ghost"))
            body.assert_called_once_with(self.tid, "pl-hook-ghost")
        self.assertTrue(turns.take("younger-first-sync", time.time()),
                        "a webhook that left the first-sync road still "
                        "holds a place ahead of younger first pulls")
        turns.release()

    def test_a_first_pull_that_fails_gives_up_its_place(self):
        """Every way out of a first pull but a wait releases the job's
        place: a pull that raises is not left counted in the queue."""
        from arq.worker import Retry
        turns = worker._FirstSyncTurns(1)
        self.assertTrue(turns.acquire())
        ctx = {"job_id": "first-sync:pl-boom", "job_try": 1}
        with mock.patch.object(worker, "_FIRST_SYNC_SLOTS", turns), \
                mock.patch.object(worker, "_sync_item_body",
                                  side_effect=RuntimeError("plaid down")):
            with self.assertRaises(Retry):
                asyncio.run(worker.sync_item(ctx, self.tid, "pl-boom", True))
            turns.release()
            ctx["job_try"] = 2
            with self.assertRaises(RuntimeError):
                asyncio.run(worker.sync_item(ctx, self.tid, "pl-boom", True))
        self.assertEqual(turns.waits("first-sync:pl-boom"), 0)
        self.assertTrue(turns.acquire())
        turns.release()

    def test_real_attempts_behind_a_busy_lock_survive_a_worker_restart(self):
        """arq's try count lives in Redis and outlives the worker; the
        waits for a turn that the process remembered do not. A first pull
        that waited a hundred times before a restart and then meets a busy
        household lock has made one real attempt, not a hundred."""
        from arq.worker import Retry
        redis = _FakeRedis()
        turns = worker._FirstSyncTurns(1)      # a restarted process
        ctx = {"job_id": "first-sync:pl-restart", "job_try": 101,
               "redis": redis}
        with mock.patch.object(worker, "_FIRST_SYNC_SLOTS", turns), \
                mock.patch.object(worker, "_sync_item_body",
                                  return_value={"skipped":
                                                "already-running"}):
            with self.assertRaises(Retry):
                asyncio.run(worker.sync_item(ctx, self.tid, "pl-restart",
                                             True))
            # and the real attempts still run out: the job is not retried
            # behind the lock for ever
            for n in range(worker.SYNC_ITEM_MAX_TRIES - 2):
                ctx["job_try"] += 1
                with self.assertRaises(Retry):
                    asyncio.run(worker.sync_item(ctx, self.tid, "pl-restart",
                                                 True))
            ctx["job_try"] += 1
            r = asyncio.run(worker.sync_item(ctx, self.tid, "pl-restart",
                                             True))
        self.assertEqual(r, {"skipped": "already-running"})
        self.assertEqual(redis.store, {}, "the count outlived its job")

    def test_the_wait_for_a_turn_is_measured_from_the_job_not_the_process(
            self):
        """The six-hour wait for a turn starts when the job was queued. A
        restart must not hand a job that has already waited it out a fresh
        budget, or arq drops it at max_tries without the give-up log."""
        import datetime as dt
        turns = worker._FirstSyncTurns(1)      # a restarted process
        self.assertTrue(turns.acquire())
        ctx = {"job_id": "first-sync:pl-stale", "job_try": 401,
               "enqueue_time": dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(seconds=worker.FIRST_SYNC_MAX_WAIT_S + 60)}
        with mock.patch.object(worker, "_FIRST_SYNC_SLOTS", turns), \
                mock.patch.object(worker, "_sync_item_body") as body:
            r = asyncio.run(worker.sync_item(ctx, self.tid, "pl-stale", True))
        body.assert_not_called()
        self.assertEqual(r, {"skipped": "no-turn"})
        self.assertEqual(turns.waits("first-sync:pl-stale"), 0)
        turns.release()

    def test_arq_never_drops_a_first_pull_before_its_own_budgets_run_out(
            self):
        """Every wait for a turn inside the time budget plus every real
        attempt behind a busy lock fits inside arq's max_tries, so the
        job's own give-up log is what ends it."""
        from oikonome.jobs.worker import WorkerSettings
        fn = next(f for f in WorkerSettings.functions
                  if getattr(f, "name", None) == "sync_item")
        waits = worker.FIRST_SYNC_MAX_WAIT_S // worker.FIRST_SYNC_SLOT_WAIT_S
        self.assertGreater(fn.max_tries,
                           waits + 1 + worker.SYNC_ITEM_MAX_TRIES)

    def test_the_sweep_hands_a_new_bank_to_its_first_sync_job(self):
        """The hourly sweep (and the sync buttons that run it) must not
        make a new bank's first pull itself: that pull is minutes of
        history outside the few first-sync turns, without the holdings,
        and its rows would go out to the household's webhooks as new
        charges. It queues the Item's one first-sync job instead."""
        self._item("pl-sweep-new")
        pulled, products, queued, emitted = [], [], [], []
        from oikonome.notify import webhooks as _wh
        with mock.patch.object(plaid, "sync",
                               lambda c, i, **k: (pulled.append(i),
                                                  {"added": 5})[1]), \
                mock.patch.object(plaid, "sync_products",
                                  lambda c, i, **k: products.append(i)), \
                mock.patch.object(worker, "queue_first_sync",
                                  lambda t, i: queued.append((str(t), i))), \
                mock.patch.object(_wh, "emit",
                                  lambda c, ev, body: emitted.append(
                                      (ev, body))):
            r = worker.sync_tenant(self.tid)
        self.assertEqual(pulled, [], "the sweep pulled a new bank itself")
        self.assertEqual(products, [])
        self.assertEqual(queued, [(self.tid, "pl-sweep-new")])
        self.assertEqual(r["pl-sweep-new"], "queued:first-sync")
        for ev, body in emitted:
            if ev == "sync.completed":
                self.assertNotIn("pl-sweep-new",
                                 [c["id"] for c in body["connections"]],
                                 "a queued first sync reads as a failed "
                                 "connection to the household's webhooks")
        # an Item a pull has already reached is the sweep's to pull
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("UPDATE items SET linked_at = now() - "
                         "interval '2 days' WHERE id='pl-sweep-new'")
        finally:
            conn.close()
        queued.clear()
        with mock.patch.object(plaid, "sync",
                               lambda c, i, **k: (pulled.append(i),
                                                  {"added": 0})[1]), \
                mock.patch.object(plaid, "sync_products",
                                  lambda c, i, **k: {"liabilities": 0,
                                                     "holdings": 0}), \
                mock.patch.object(worker, "queue_first_sync",
                                  lambda t, i: queued.append((str(t), i))):
            r = worker.sync_tenant(self.tid)
        self.assertEqual(pulled, ["pl-sweep-new"])
        self.assertEqual(queued, [])
        self.assertEqual(r["pl-sweep-new"], "ok:0")

    def test_a_sweep_that_only_queued_first_syncs_synced_no_items(self):
        """The sync heartbeat names what the pass synced. A new bank the
        sweep handed to its first-sync job was not synced by it, so a pass
        that only queued is not stamped as having synced that bank."""
        self._item("pl-heartbeat-new")
        beats = []
        with mock.patch.object(worker, "queue_first_sync",
                               lambda t, i: None), \
                mock.patch.object(worker.alerts, "heartbeat",
                                  lambda c, job, note="", **k:
                                      beats.append((job, note))):
            r = worker.sync_tenant(self.tid)
        self.assertEqual(r["pl-heartbeat-new"], "queued:first-sync")
        self.assertEqual([b for b in beats if b[0] == "sync"], [],
                         "a pass that only queued a first sync was stamped "
                         "as having synced it")

    def test_a_first_sync_has_tries_to_outlast_a_busy_lock(self):
        from oikonome.jobs.worker import WorkerSettings
        fn = next(f for f in WorkerSettings.functions
                  if getattr(f, "name", None) == "sync_item")
        self.assertGreaterEqual(fn.max_tries, 20)


class _FakeArqPool:
    """arq's pool as the enqueue door sees it: a job id is refused while
    its job is queued or running AND while its finished result is kept."""

    def __init__(self):
        from arq.constants import job_key_prefix, result_key_prefix
        self.job_key, self.result_key = job_key_prefix, result_key_prefix
        self.keys: dict = {}
        self.enqueued: list = []

    async def enqueue_job(self, fn, *args, _job_id=None):
        if (self.job_key + _job_id in self.keys
                or self.result_key + _job_id in self.keys):
            return None
        self.keys[self.job_key + _job_id] = (fn, args)
        self.enqueued.append((fn, args, _job_id))
        return object()

    async def exists(self, *names):
        return sum(n in self.keys for n in names)

    async def delete(self, *names):
        for n in names:
            self.keys.pop(n, None)
        return len(names)

    async def close(self):
        pass

    def finish(self, job_id):
        """The worker ends a try: job key gone, result kept for an hour."""
        self.keys.pop(self.job_key + job_id, None)
        self.keys[self.result_key + job_id] = b"result"


class _InlineThread:
    def __init__(self, target, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class AFirstSyncThatGaveUpIsQueuedAgainTests(unittest.TestCase):
    """A first-sync try that ends without a pull (no turn inside its wait,
    every try behind a busy lock, a failure that records nothing) leaves
    the bank never pulled, so the hourly sweep and the sync-now button ask
    for it again. arq keeps the finished try's result under the same id
    for an hour; that ask must queue a new try, not be silently refused."""

    def _queue(self, pool, item_id):
        async def _create_pool(*a, **kw):
            return pool
        with mock.patch("arq.create_pool", _create_pool), \
                mock.patch.object(worker.threading, "Thread", _InlineThread):
            worker.queue_first_sync("t-1", item_id)

    def test_an_ask_after_a_try_gave_up_queues_a_new_try(self):
        pool = _FakeArqPool()
        jid = worker.first_sync_job_id("pl-gave-up")
        self._queue(pool, "pl-gave-up")
        pool.finish(jid)
        self._queue(pool, "pl-gave-up")
        self.assertEqual(
            [e[2] for e in pool.enqueued], [jid, jid],
            "the re-ask within the hour met the kept result and was refused")
        self.assertIn(pool.job_key + jid, pool.keys)

    def test_an_ask_while_the_try_is_queued_adds_nothing(self):
        pool = _FakeArqPool()
        self._queue(pool, "pl-waiting")
        self._queue(pool, "pl-waiting")
        self.assertEqual(len(pool.enqueued), 1)

    def test_a_backfill_nudge_still_stands_for_its_window(self):
        pool = _FakeArqPool()

        async def _create_pool(*a, **kw):
            return pool
        with mock.patch("arq.create_pool", _create_pool), \
                mock.patch.object(worker.threading, "Thread", _InlineThread):
            worker.queue_backfill_sync("t-1", "pl-nudge", now=1000.0)
            pool.finish(pool.enqueued[0][2])
            worker.queue_backfill_sync("t-1", "pl-nudge", now=1001.0)
        self.assertEqual(len(pool.enqueued), 1,
                         "a finished nudge pull no longer stands for the "
                         "rest of its window")


if __name__ == "__main__":
    unittest.main()
