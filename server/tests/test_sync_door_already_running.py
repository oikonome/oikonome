"""Pressing Sync while a sync is running must say so.

A held lock reports not-started, never an empty success. `sync_tenant`
answers the sentinel {"_status": "already-running"} when the per-tenant
single-flight lock is held; read as ok=True with an empty result set it is
indistinguishable from "you have no connections", and the SPA would tell the
user to go connect a bank while that bank's data was being pulled.
"""

import unittest
from unittest import mock

from oikonome.web import api


class SyncDoorTests(unittest.TestCase):

    def test_a_held_lock_is_reported_as_not_started(self):
        with mock.patch("oikonome.jobs.worker.sync_tenant",
                        return_value={"_status": "already-running"}), \
             mock.patch("oikonome.web.demoguard.deny"):
            r = api.jobs_sync_api(user={"tenant_id": "t-1"})
        self.assertIs(r["started"], False)
        self.assertIn("already running", r["reason"])
        self.assertEqual(r["results"], {},
                         "the sentinel must not leak out as a per-item result")

    def test_a_real_sync_still_reports_its_items(self):
        with mock.patch("oikonome.jobs.worker.sync_tenant",
                        return_value={"item-a": "ok", "item-b": "ok"}), \
             mock.patch("oikonome.web.demoguard.deny"):
            r = api.jobs_sync_api(user={"tenant_id": "t-1"})
        self.assertIs(r["started"], True)
        self.assertIs(r["ok"], True)
        self.assertEqual(len(r["results"]), 2)

    def test_an_item_error_still_fails_the_call(self):
        with mock.patch("oikonome.jobs.worker.sync_tenant",
                        return_value={"item-a": "error: nope"}), \
             mock.patch("oikonome.web.demoguard.deny"):
            r = api.jobs_sync_api(user={"tenant_id": "t-1"})
        self.assertIs(r["ok"], False)
        self.assertIs(r["started"], True)


class BackgroundSyncDoorHonesty(unittest.TestCase):
    """The background doors (/jobs/sync/start and the wizard backfill
    nudge) run sync_tenant in a thread. When another door already holds
    the per-tenant advisory lock, sync_tenant returns the already-running
    sentinel having pulled nothing — the thread must NOT stamp the
    job_progress row 'done' (a sync that never happened reported as a
    success); the run that holds the lock owns the row and settles it."""

    @classmethod
    def setUpClass(cls):
        import os
        import uuid

        from fastapi.testclient import TestClient

        from .util import _ensure_db
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"syncdoor-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def test_start_with_lock_held_elsewhere_is_not_reported_done(self):
        import time

        from oikonome.db import tenancy
        from oikonome.engine.compat import as_dict
        key = f"oikonome:sync:{self.tid}"
        holder = tenancy.tenant_connect(self.tid)
        try:
            got = holder.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (key,)).fetchone()["ok"]
            self.assertTrue(got, "test could not take the sync lock")
            r = self.client.post("/api/jobs/sync/start")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["started"])
            conn = tenancy.tenant_connect(self.tid)
            try:
                state, progress = None, {}
                deadline = time.time() + 15
                while time.time() < deadline:
                    row = conn.execute(
                        "SELECT state, progress FROM job_progress "
                        "WHERE id='sync'").fetchone()
                    if row is not None:
                        state = row["state"]
                        progress = as_dict(row["progress"]) or {}
                        if (progress.get("already_running")
                                or state in ("done", "error")):
                            break
                    time.sleep(0.05)
                self.assertNotEqual(
                    state, "done",
                    "a sync that never ran must not be reported as done")
                self.assertTrue(
                    progress.get("already_running"),
                    f"the thread must record the truthful state; "
                    f"got state={state!r} progress={progress!r}")
                self.assertNotIn(
                    "results", progress,
                    "no per-item results exist for a run that never pulled")
            finally:
                conn.close()
        finally:
            holder.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
            holder.close()


class ManualSyncCategorizes(unittest.TestCase):
    """The ↻ button is a sync door like the hourly sweep and the webhook,
    and like them it runs the sync-time categorize pass after the pull;
    otherwise rows a person has just pulled sit under the aggregator's
    generic category — variable spend, no bill match — until the next
    hourly run."""

    @staticmethod
    def _an_item():
        """A tenant with one plaid connection to press ↻ on."""
        from .util import make_db
        conn = make_db()
        try:
            tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, status) "
                "VALUES ('it-cat', 'plaid', 'Test Bank', 'ok')")
        finally:
            conn.close()
        return tid

    def test_the_manual_sync_door_categorizes_what_it_pulled(self):
        from oikonome.web import pages
        tid = self._an_item()
        with mock.patch("oikonome.sync.plaid.sync",
                        return_value={"added": 2}) as pulled, \
             mock.patch("oikonome.sync.plaid.sync_products"), \
             mock.patch("oikonome.engine.llm_categorize.categorize_new",
                        return_value={}) as cat, \
             mock.patch("oikonome.engine.bills.apply_txn_categories") as txc, \
             mock.patch("oikonome.web.pages._spawn",
                        new=lambda fn: fn()), \
             mock.patch("oikonome.web.demoguard.deny"):
            r = pages.account_sync(user={"tenant_id": tid}, item_id="it-cat")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(pulled.call_count, 1)
        self.assertEqual(cat.call_count, 1,
                         "the pull must be followed by categorize_new")
        self.assertEqual(txc.call_count, 1)
        self.assertEqual(txc.call_args.kwargs.get("days"), 120)

    def test_the_categorize_pass_does_not_run_on_the_request_thread(self):
        """The pass can spend ~13 LLM calls at a ten-minute timeout each,
        so running it inside the POST makes a reverse proxy time out while the
        per-tenant sync lock is still held — and the person's retry is told
        a sync is already running. The request must answer as soon as the
        pull is done and leave the pass to a worker."""
        from oikonome.web import pages
        tid = self._an_item()
        deferred: list = []
        with mock.patch("oikonome.sync.plaid.sync",
                        return_value={"added": 2}), \
             mock.patch("oikonome.sync.plaid.sync_products"), \
             mock.patch("oikonome.engine.llm_categorize.categorize_new",
                        return_value={}) as cat, \
             mock.patch("oikonome.engine.bills.apply_txn_categories") as txc, \
             mock.patch("oikonome.web.pages._spawn",
                        new=deferred.append), \
             mock.patch("oikonome.web.demoguard.deny"):
            r = pages.account_sync(user={"tenant_id": tid}, item_id="it-cat")
            self.assertEqual(r.status_code, 303)
            self.assertEqual(len(deferred), 1,
                             "the pull must hand the pass to exactly one "
                             "background runner")
            self.assertEqual(cat.call_count, 0,
                             "categorize_new must not run on the request "
                             "thread")
            self.assertEqual(txc.call_count, 0)
            # and the deferred work is the real pass, not a stub
            deferred[0]()
            self.assertEqual(cat.call_count, 1)
            self.assertEqual(txc.call_count, 1)
            self.assertEqual(txc.call_args.kwargs.get("days"), 120)

    def test_a_second_pass_skips_while_one_is_running(self):
        """Two presses a few seconds apart must not run two full passes
        over one ledger: apply()'s ledger-wide UPDATE racing itself is
        wasted work and a deadlock candidate. Every step is pending-gated,
        so the loser skipping costs nothing."""
        from oikonome.db import tenancy
        from oikonome.web import pages
        tid = self._an_item()
        key = f"oikonome:sync-categorize:{tid}"
        holder = tenancy.tenant_connect(tid)
        try:
            self.assertTrue(
                holder.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                    (key,)).fetchone()["ok"],
                "test could not take the categorize lock")
            with mock.patch("oikonome.sync.plaid.sync",
                            return_value={"added": 2}), \
                 mock.patch("oikonome.sync.plaid.sync_products"), \
                 mock.patch("oikonome.engine.llm_categorize.categorize_new",
                            return_value={}) as cat, \
                 mock.patch("oikonome.engine.bills.apply_txn_categories"), \
                 mock.patch("oikonome.web.pages._spawn",
                            new=lambda fn: fn()), \
                 mock.patch("oikonome.web.demoguard.deny"):
                r = pages.account_sync(user={"tenant_id": tid},
                                       item_id="it-cat")
            self.assertEqual(r.status_code, 303,
                             "a skipped categorize must not fail the sync")
            self.assertEqual(cat.call_count, 0)
        finally:
            holder.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
            holder.close()


class ManualSyncTakesTheTenantLock(unittest.TestCase):
    def test_route_takes_and_releases_the_advisory_lock(self):
        """Structural: the door must use the SAME lock key family as
        worker.sync_tenant and answer 'already running' when it loses —
        and must release explicitly (pooled connection)."""
        import inspect

        from oikonome.web import pages
        src = inspect.getsource(pages.account_sync)
        self.assertIn("pg_try_advisory_lock", src)
        self.assertIn("oikonome:sync:", src)
        self.assertIn("already running", src.lower())
        # released through the one helper that drops the connection when
        # the unlock fails — a bare unlock-then-close pools a locked session
        self.assertIn("tenancy.release_lock", src)
