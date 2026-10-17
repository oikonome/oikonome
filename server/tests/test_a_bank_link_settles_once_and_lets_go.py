"""A bank link's outcome is settled the moment the Item is ours, and a
link nobody is waiting on stops holding the household's place.

* A finisher that dies during the first sync has already recorded the
  Item: the next door answers "linked" without spending the token again.
* A spent token whose outcome was never recorded is answered by the Item
  its Plaid session created, not reported as a failed link.
* From the exchange until the Item's row commits, the token is held in
  the release queue: a process killed in between leaves it there for the
  nightly retry, which releases it only once no link could still be
  saving it. A link that settles (saved, or handed back) leaves no hold.
* A link refused after the exchange whose hand-back to Plaid fails is
  queued for the nightly retry rather than left billing.
* A household that may no longer keep a new Item still has a finished
  link exchanged and handed straight back.
* A client resuming a session it was not watching does not keep it under
  the institution cap; a session the person set aside stops counting and
  is not offered again, yet is still finished if Plaid reports a bank.
* Sessions on record that a restarted web process has not seen yet count
  under the cap.
* A session on record never moves to another household: a start naming a
  link token already recorded for someone else records nothing.
* Purging a household takes its link sessions with it — after a session
  the person finished at Plaid is exchanged and its Item handed back, so
  no Item outlives the household's record of it.
* A full instance reset does not promise a retry it is about to delete.
"""

import json
import unittest
import uuid
from unittest import mock

import httpx
from fastapi.testclient import TestClient

import datetime as dt

from oikonome.db import crypto, tenancy
from oikonome.jobs import plaid_links, reaper
from oikonome.sync import base as sync_base
from oikonome.sync import plaid
from oikonome.web import pages

from .test_a_finished_bank_link_completes_without_a_client import (
    _backend, _row)
from .util import _ensure_db, make_db, seed_accounts, write_config


class _Killed(BaseException):
    """The process going away mid-call: nothing catches it."""


def _patch(state, lsid=None, remove_fails=False):
    inner = _backend(state).handler

    def handler(request):
        path = request.url.path
        if path == "/item/remove":
            state["removes"] = state.get("removes", 0) + 1
            if remove_fails:
                raise httpx.ReadTimeout("no answer", request=request)
            return httpx.Response(200, text="{}")
        r = inner(request)
        if lsid and path == "/link/token/get" and state.get("link_done"):
            body = json.loads(r.content)
            for s in body["link_sessions"]:
                s["link_session_id"] = lsid
            return httpx.Response(200, text=json.dumps(body))
        return r

    def for_tenant(conn, transport=None):
        return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                            transport=httpx.MockTransport(handler))
    return mock.patch.object(plaid.Client, "for_tenant",
                             staticmethod(for_tenant))


class BankLinkSettlesOnceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"pls-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
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
                          (str(self.tid),))
        finally:
            admin.close()
        self.lt = f"lt-{uuid.uuid4().hex[:10]}"
        self.state = {"link_token": self.lt,
                      "item_id": f"pl-{uuid.uuid4().hex[:10]}"}

    def _start(self, token=None):
        state = dict(self.state, link_token=token or self.lt)
        with _patch(state):
            return self.client.post("/accounts/plaid/link", data={},
                                    headers={"Accept": "application/json"})

    def _has_item(self, item_id=None) -> bool:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute("SELECT 1 FROM items WHERE id=%s",
                                (item_id or self.state["item_id"],)
                                ).fetchone() is not None
        finally:
            conn.close()

    def _held(self) -> int:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return sync_base.institutions_held(conn)
        finally:
            conn.close()

    def test_a_recorded_session_never_moves_to_another_household(self):
        self.assertTrue(plaid_links.record(self.tid, self.lt, "add", None,
                                           False, "Mine"))
        other = str(uuid.uuid4())
        self.assertFalse(plaid_links.record(other, self.lt, "add", None,
                                            False, "Theirs"))
        self.assertIsNone(plaid_links.load(self.lt, other))
        self.assertEqual(plaid_links.load(self.lt, self.tid)["institution"],
                         "Mine")

    # -- settling -------------------------------------------------------

    @staticmethod
    def _first_sync_inline():
        """The finisher queues the new Item's first pull once the session
        is settled; run here, the queued job's pull dies in this thread."""
        from oikonome.jobs import worker
        return mock.patch.object(
            worker, "queue_first_sync",
            lambda t, i: worker._sync_item_body(str(t), i, True))

    def test_a_finisher_dying_in_the_first_sync_leaves_the_link_linked(self):
        self.assertEqual(self._start().status_code, 200)
        self.state["link_done"] = True

        def die(*a, **k):
            raise _Killed()
        with _patch(self.state), mock.patch.object(plaid, "sync", die), \
                self._first_sync_inline():
            with self.assertRaises(_Killed):
                plaid_links.finish_session(self.tid, self.lt)
        self.assertTrue(self._has_item())
        settled = list(_row(self.lt)["exchanged"].values())
        self.assertEqual(settled[0]["item_id"], self.state["item_id"])
        # the next door answers from the record: linked, token not spent
        # a second time
        with _patch(self.state):
            self.assertEqual(plaid_links.finish_session(self.tid, self.lt),
                             "done")
        self.assertEqual(_row(self.lt)["reply"]["kind"], "add")
        self.assertEqual(self.state["exchanges"], 1)

    def test_a_spent_token_with_no_record_is_answered_by_its_item(self):
        lsid = f"ls-{uuid.uuid4().hex[:8]}"
        self.assertEqual(self._start().status_code, 200)
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, self.state["item_id"], "plaid",
                                  "Sample Credit Union", "access-x")
            conn.execute("UPDATE items SET link_session_id=%s WHERE id=%s",
                         (lsid, self.state["item_id"]))
        finally:
            conn.close()
        # the token was spent by a finisher that died before recording it
        self.state["exchanges"] = 1
        self.state["link_done"] = True
        with _patch(self.state, lsid=lsid):
            self.assertEqual(plaid_links.finish_session(self.tid, self.lt),
                             "done")
        reply = _row(self.lt)["reply"]
        self.assertEqual(reply["kind"], "add", reply)

    def test_the_session_id_is_on_the_item_row_from_its_commit(self):
        lsid = f"ls-{uuid.uuid4().hex[:8]}"
        self.assertEqual(self._start().status_code, 200)
        self.state["link_done"] = True
        seen = []

        def die(conn, item_id, transport=None):
            seen.append(conn.execute(
                "SELECT link_session_id FROM items WHERE id=%s",
                (item_id,)).fetchone()["link_session_id"])
            raise _Killed()
        with _patch(self.state, lsid=lsid), \
                mock.patch.object(plaid, "sync", die), \
                self._first_sync_inline():
            with self.assertRaises(_Killed):
                plaid_links.finish_session(self.tid, self.lt)
        self.assertEqual(seen, [lsid])

    # -- the token between the exchange and the row ----------------------

    def _queued(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT * FROM plaid_release_queue WHERE item_id=%s",
                (self.state["item_id"],)).fetchone()
        finally:
            admin.close()

    def _unqueue(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM plaid_release_queue WHERE item_id=%s",
                          (self.state["item_id"],))
        finally:
            admin.close()

    def test_a_link_killed_before_its_row_saves_leaves_the_token_queued(self):
        self.addCleanup(self._unqueue)

        def die(*a, **k):
            raise _Killed()
        conn = tenancy.tenant_connect(self.tid)
        try:
            with _patch(self.state), \
                    mock.patch.object(plaid, "_institution_ids", die):
                with self.assertRaises(_Killed):
                    plaid.link_item(conn, "pub-k")
        finally:
            conn.close()
        self.assertFalse(self._has_item())
        q = self._queued()
        self.assertIsNotNone(q)
        self.assertEqual(q["reason"], plaid.LINK_HOLD_REASON)
        self.assertEqual(str(q["tenant"]), str(self.tid))
        self.assertEqual(crypto.decrypt_cp(q["access_token"]),
                         "access-sandbox-y")

        removed = []

        def plaid_side(request):
            if request.url.path == "/item/remove":
                removed.append(json.loads(request.content)["access_token"])
                return httpx.Response(200, text="{}")
            return httpx.Response(404, text="{}")
        transport = httpx.MockTransport(plaid_side)
        # a hold younger than any link could run is left alone: the link
        # that queued it may be about to save the Item
        out = reaper.retry_queued_releases(transport=transport)
        self.assertNotIn(self.state["item_id"], out["released"])
        self.assertEqual(removed, [])
        self.assertIsNotNone(self._queued())
        # once it has outlived every link, the Item is released
        later = dt.datetime.now(dt.timezone.utc) + reaper.LINK_HOLD_GRACE \
            + dt.timedelta(minutes=1)
        out = reaper.retry_queued_releases(now=later, transport=transport)
        self.assertIn(self.state["item_id"], out["released"])
        self.assertEqual(removed, ["access-sandbox-y"])
        self.assertIsNone(self._queued())

    def test_a_saved_link_leaves_no_hold(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            with _patch(self.state):
                plaid.link_item(conn, "pub-s")
        finally:
            conn.close()
        self.assertTrue(self._has_item())
        self.assertIsNone(self._queued())

    def test_a_refused_link_handed_back_leaves_no_hold(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            def capped(c):
                raise ValueError("at the cap")
            with _patch(self.state), \
                    mock.patch.object(plaid, "check_institution_cap",
                                      capped):
                with self.assertRaises(ValueError):
                    plaid.link_item(conn, "pub-r")
        finally:
            conn.close()
        self.assertEqual(self.state["removes"], 1)
        self.assertIsNone(self._queued())

    def test_a_hold_for_an_item_the_household_saved_is_dropped_unused(self):
        """A crash between the row's commit and dropping the hold leaves a
        hold naming a held Item; the retry must never release that."""
        self.addCleanup(self._unqueue)
        conn = tenancy.tenant_connect(self.tid)
        try:
            with _patch(self.state), \
                    mock.patch.object(plaid, "_drop_link_hold",
                                      lambda item_id: None):
                plaid.link_item(conn, "pub-c")
        finally:
            conn.close()
        self.assertIsNotNone(self._queued())
        removed = []

        def plaid_side(request):
            removed.append(request.url.path)
            return httpx.Response(200, text="{}")
        later = dt.datetime.now(dt.timezone.utc) + reaper.LINK_HOLD_GRACE \
            + dt.timedelta(minutes=1)
        out = reaper.retry_queued_releases(
            now=later, transport=httpx.MockTransport(plaid_side))
        self.assertIn(self.state["item_id"], out["dropped"])
        self.assertEqual(removed, [])
        self.assertTrue(self._has_item())
        self.assertIsNone(self._queued())

    # -- handing back ---------------------------------------------------

    def test_a_refused_link_whose_hand_back_fails_is_queued(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            def capped(c):
                raise ValueError("at the cap")
            with _patch(self.state, remove_fails=True), \
                    mock.patch.object(plaid, "check_institution_cap",
                                      capped):
                with self.assertRaises(ValueError):
                    plaid.link_item(conn, "pub-q")
        finally:
            conn.close()
        admin = tenancy.admin_connect()
        try:
            q = admin.execute(
                "SELECT tenant, reason FROM plaid_release_queue "
                "WHERE item_id=%s", (self.state["item_id"],)).fetchone()
            admin.execute("DELETE FROM plaid_release_queue WHERE item_id=%s",
                          (self.state["item_id"],))
        finally:
            admin.close()
        self.assertIsNotNone(q)
        self.assertEqual(str(q["tenant"]), str(self.tid))
        self.assertEqual(q["reason"], "link-refused")

    def test_a_frozen_household_has_a_finished_link_handed_back(self):
        self.assertEqual(self._start().status_code, 200)
        self.state["link_done"] = True
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET status='suspended' WHERE id=%s",
                          (self.tid,))
        finally:
            admin.close()
        try:
            with _patch(self.state):
                word = plaid_links.finish_session(self.tid, self.lt)
        finally:
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE tenants SET status='active' "
                              "WHERE id=%s", (self.tid,))
            finally:
                admin.close()
        self.assertEqual(word, "blocked")
        self.assertEqual(self.state["exchanges"], 1)
        self.assertEqual(self.state["removes"], 1)
        self.assertFalse(self._has_item())
        self.assertEqual(_row(self.lt)["reply"]["kind"], "add_failed")

    # -- letting go -----------------------------------------------------

    def test_a_resume_poll_does_not_hold_the_last_slot(self):
        cap = self._held() + 1
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: cap):
            self.assertEqual(self._start().status_code, 200)
            # the window that opened it closed long ago
            pages._PLAID_LINK[self.lt]["at"] -= pages._PLAID_ADD_LIVE + 5
            with _patch(self.state):
                r = self.client.get(
                    f"/accounts/plaid/link/{self.lt}/status?resume=1")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse(r.json()["done"])
            self.assertEqual(self._start(f"lt-{uuid.uuid4().hex[:8]}"
                                         ).status_code, 200)

    def test_a_watching_poll_still_holds_the_last_slot(self):
        cap = self._held() + 1
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: cap):
            self.assertEqual(self._start().status_code, 200)
            pages._PLAID_LINK[self.lt]["at"] -= pages._PLAID_ADD_LIVE + 5
            with _patch(self.state):
                self.client.get(f"/accounts/plaid/link/{self.lt}/status")
            self.assertEqual(self._start(f"lt-{uuid.uuid4().hex[:8]}"
                                         ).status_code, 400)

    def test_a_set_aside_session_frees_its_slot_but_still_finishes(self):
        cap = self._held() + 1
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: cap):
            self.assertEqual(self._start().status_code, 200)
            r = self.client.delete(f"/accounts/plaid/link/{self.lt}")
            self.assertEqual(r.status_code, 200, r.text)
            listed = [s["link_token"] for s in self.client.get(
                "/accounts/plaid/links/open").json()["sessions"]]
            self.assertNotIn(self.lt, listed)
            # a set-aside session is neither counted nor offered after a
            # restart either
            pages._PLAID_LINK.clear()
            other = f"lt-{uuid.uuid4().hex[:8]}"
            self.assertEqual(self._start(other).status_code, 200)
        # the person did finish at Plaid after all: still exchanged
        self.state["link_done"] = True
        self.state["any_token"] = True
        with _patch(self.state):
            self.assertEqual(plaid_links.finish_session(self.tid, self.lt),
                             "done")
        self.assertTrue(self._has_item())

    def test_another_household_cannot_set_a_session_aside(self):
        self.assertEqual(self._start().status_code, 200)
        pages._PLAID_LINK.clear()
        self.assertFalse(plaid_links.set_aside(self.lt, str(uuid.uuid4())))
        self.assertIsNone(_row(self.lt)["reply"])

    def test_a_session_opened_before_a_restart_counts_under_the_cap(self):
        cap = self._held() + 1
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: cap):
            self.assertEqual(self._start().status_code, 200)
            pages._PLAID_LINK.clear()          # the web process restarted
            self.assertEqual(self._start(f"lt-{uuid.uuid4().hex[:8]}"
                                         ).status_code, 400)


class HouseholdPurgeTests(unittest.TestCase):
    def test_purging_a_household_takes_its_link_sessions(self):
        conn = make_db()
        try:
            tid = conn.execute("SELECT current_setting('app.tenant_id') "
                               "AS t").fetchone()["t"]
        finally:
            conn.close()
        lt = f"lt-{uuid.uuid4().hex[:10]}"
        plaid_links.record(tid, lt, "add", None, False, None)
        admin = tenancy.admin_connect()
        try:
            tenancy.delete_tenant_rows(admin, tid)
        finally:
            admin.close()
        self.assertIsNone(_row(lt))


class ErasureHandsBackOpenLinksTests(unittest.TestCase):
    def setUp(self):
        conn = make_db()
        try:
            self.tid = conn.execute("SELECT current_setting('app.tenant_id') "
                                    "AS t").fetchone()["t"]
            write_config(conn, plaid_client_id="cid", plaid_secret="sec",
                         plaid_env="sandbox")
        finally:
            conn.close()
        self.lt = f"lt-{uuid.uuid4().hex[:10]}"
        self.state = {"link_token": self.lt,
                      "item_id": f"pl-{uuid.uuid4().hex[:10]}"}
        plaid_links.record(self.tid, self.lt, "add", None, False, None)

    def _erase(self):
        from oikonome import erasure
        with _patch(self.state):
            erasure.release_external(self.tid)
        admin = tenancy.admin_connect()
        try:
            tenancy.delete_tenant_rows(admin, self.tid)
        finally:
            admin.close()

    def test_a_link_finished_just_before_erasure_is_handed_back(self):
        self.state["link_done"] = True
        self._erase()
        self.assertEqual(self.state.get("exchanges"), 1)
        self.assertEqual(self.state.get("removes"), 1)
        self.assertIsNone(_row(self.lt))

    def test_a_hand_back_that_fails_is_queued_for_the_retry(self):
        self.state["link_done"] = True
        self.addCleanup(self._unqueue)
        from oikonome import erasure
        with _patch(self.state, remove_fails=True):
            erasure.release_external(self.tid)
        admin = tenancy.admin_connect()
        try:
            q = admin.execute(
                "SELECT reason FROM plaid_release_queue WHERE item_id=%s",
                (self.state["item_id"],)).fetchone()
            tenancy.delete_tenant_rows(admin, self.tid)
        finally:
            admin.close()
        self.assertIsNotNone(q)
        self.assertEqual(q["reason"], "link-refused")

    def test_a_link_still_in_the_banks_window_spends_nothing(self):
        self._erase()
        self.assertIsNone(self.state.get("exchanges"))
        self.assertIsNone(_row(self.lt))

    def _unqueue(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM plaid_release_queue WHERE item_id=%s",
                          (self.state["item_id"],))
        finally:
            admin.close()


class FullResetWordingTests(unittest.TestCase):
    def test_a_full_reset_says_to_remove_stuck_links_now(self):
        from oikonome.db import reset
        full = " ".join(reset.unreleased({"plaid_failed": ["it-1"]},
                                         queue_survives=False))
        self.assertIn("it-1", full)
        self.assertIn("now", full)
        self.assertNotIn("nightly", full)
        one = " ".join(reset.unreleased({"plaid_failed": ["it-1"]}))
        self.assertIn("nightly", one)


class SweepTimeoutTests(unittest.TestCase):
    def test_link_finishing_jobs_have_room_for_a_first_sync(self):
        from oikonome.jobs import worker
        named = {getattr(f, "name", None): f
                 for f in worker.WorkerSettings.functions}
        for n in ("plaid_link_sweep", "plaid_link_finish"):
            self.assertIn(n, named)
            self.assertGreaterEqual(named[n].timeout_s,
                                    worker.PLAID_LINK_TIMEOUT)
        cron = [c for c in worker.WorkerSettings.cron_jobs
                if c.name.endswith("plaid_link_sweep")]
        self.assertEqual(len(cron), 1)
        self.assertGreaterEqual(cron[0].timeout_s, worker.PLAID_LINK_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
