"""A bank link the person finished at Plaid arrives whether or not a client
is still polling for it.

The exchange of a public token used to run only inside the client's status
poll, against a session held in the web process's memory. A closed tab, a
phone that dropped the app mid-OAuth, a client that gave up, or a web
restart all left the Item created and billed at Plaid with nothing here to
exchange its token. The session is on record now, and must be finishable
by every door — exactly once:

* a poll after the web process lost its memory picks the recorded session
  up instead of answering 404;
* the worker (behind the LINK webhook and the sweep) exchanges the token
  with nobody polling, and a later poll gets that answer with no second
  exchange;
* the LINK webhook resolves the link token to its household and queues
  the worker;
* a client that comes back can list its household's open sessions;
* a session given up under the institution cap stays given up.
"""

import json
import time
import unittest
import uuid
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.jobs import plaid_links
from oikonome.sync import plaid
from oikonome.web import pages

from .util import _ensure_db, seed_accounts, write_config


def _backend(state: dict):
    def handler(request):
        path = request.url.path
        if path == "/link/token/create":
            return httpx.Response(200, text=json.dumps(
                {"link_token": state["link_token"],
                 "hosted_link_url": "https://hosted.plaid.com/x"}))
        if path == "/link/token/get":
            asked = json.loads(request.content.decode())["link_token"]
            if asked != state["link_token"] and not state.get("any_token"):
                # a session some other test left open: still waiting
                return httpx.Response(200, text=json.dumps(
                    {"link_sessions": []}))
            if state.get("exited"):
                return httpx.Response(200, text=json.dumps(
                    {"link_sessions": [
                        {"finished_at": "2026-01-01T00:00:00Z"}]}))
            if not state.get("link_done"):
                return httpx.Response(200, text=json.dumps(
                    {"link_sessions": []}))
            return httpx.Response(200, text=json.dumps(
                {"link_sessions": [{"results": {"item_add_results": [
                    {"public_token": "pub-y"}]}}]}))
        if path == "/item/public_token/exchange":
            state["exchanges"] = state.get("exchanges", 0) + 1
            if state["exchanges"] > 1:
                return httpx.Response(400, text=json.dumps(
                    {"error_type": "INVALID_INPUT",
                     "error_code": "INVALID_PUBLIC_TOKEN",
                     "error_message": "public token already exchanged"}))
            return httpx.Response(200, text=json.dumps(
                {"item_id": state["item_id"],
                 "access_token": "access-sandbox-y"}))
        if path == "/item/get":
            return httpx.Response(200, text=json.dumps(
                {"item": {"institution_id": "ins_8", "error": None}}))
        if path == "/institutions/get_by_id":
            return httpx.Response(200, text=json.dumps(
                {"institution": {"name": "Sample Credit Union"}}))
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps({"accounts": []}))
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(
                {"added": [], "modified": [], "removed": [],
                 "has_more": False, "next_cursor": "c-1"}))
        if path in ("/liabilities/get", "/investments/holdings/get"):
            return httpx.Response(400, text=json.dumps(
                {"error_type": "INVALID_REQUEST",
                 "error_code": "PRODUCTS_NOT_SUPPORTED",
                 "error_message": "n/a"}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


def _patch(state):
    def for_tenant(conn, transport=None):
        return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                            transport=_backend(state))
    return mock.patch.object(plaid.Client, "for_tenant",
                             staticmethod(for_tenant))


def _age(link_token: str, seconds: int) -> None:
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE plaid_link_sessions SET created_at = now() - "
            "make_interval(secs => %s) WHERE link_token=%s",
            (seconds, link_token))
    finally:
        admin.close()


def _row(link_token: str):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(
            "SELECT done_at, reply, exchanged FROM plaid_link_sessions "
            "WHERE link_token=%s", (link_token,)).fetchone()
    finally:
        admin.close()


class FinishedLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"plf-{uuid.uuid4().hex[:8]}@example.dev",
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
        # a fresh process: nothing open on record either, or a session an
        # earlier test left would count under the cap as one opened
        # before a restart
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM plaid_link_sessions WHERE tenant=%s",
                          (str(self.tid),))
        finally:
            admin.close()
        self.lt = f"lt-{uuid.uuid4().hex[:10]}"
        self.state = {"link_token": self.lt,
                      "item_id": f"pl-{uuid.uuid4().hex[:10]}"}

    def _start(self):
        with _patch(self.state):
            r = self.client.post("/accounts/plaid/link", data={},
                                 headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 200, r.text)

    def _poll(self):
        with _patch(self.state):
            return self.client.get(f"/accounts/plaid/link/{self.lt}/status")

    def _has_item(self) -> bool:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute("SELECT 1 FROM items WHERE id=%s",
                                (self.state["item_id"],)
                                ).fetchone() is not None
        finally:
            conn.close()

    def test_a_poll_after_a_restart_finishes_the_recorded_session(self):
        self._start()
        pages._PLAID_LINK.clear()          # the web process restarted
        self.state["link_done"] = True
        r = self._poll()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["kind"], "add")
        self.assertTrue(self._has_item())

    def test_the_worker_finishes_a_link_nobody_is_polling(self):
        self._start()
        self.state["link_done"] = True
        with _patch(self.state):
            word = plaid_links.finish_session(self.tid, self.lt)
        self.assertEqual(word, "done")
        self.assertTrue(self._has_item())
        row = _row(self.lt)
        self.assertIsNotNone(row["done_at"])
        self.assertEqual(row["reply"]["kind"], "add")
        # the public token itself is never on record, only its hash
        self.assertNotIn("pub-y", json.dumps(row["exchanged"]))
        # the client coming back gets the worker's answer, and the spent
        # token is not exchanged a second time
        s = self._poll().json()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add")
        self.assertEqual(self.state["exchanges"], 1)

    def test_a_finisher_that_wins_the_race_answers_the_poll_at_once(self):
        """The LINK webhook usually reaches the worker before the client's
        next poll. Its finish saves the Item and finishes the session
        without pulling, then queues the first pull — so the poll gets
        "linked, first sync pending" instead of "busy" for as long as a
        multi-year pull takes, and the household's lock is free again."""
        from oikonome.jobs import worker
        self._start()
        self.state["link_done"] = True
        pulled, queued, polled = [], [], []

        def pull(*a, **k):
            pulled.append("sync")
            return {"added": 0}

        real_queue = worker.queue_first_sync

        def queue_then_poll(t, i):
            queued.append((str(t), i))
            # the poll in flight while the finisher hands the pull over
            polled.append(self._poll().json())
        with _patch(self.state), \
                mock.patch.object(plaid, "sync", pull), \
                mock.patch.object(plaid, "sync_products", pull), \
                mock.patch.object(worker, "queue_first_sync",
                                  queue_then_poll):
            self.assertEqual(plaid_links.finish_session(self.tid, self.lt),
                             "done")
        self.assertIs(worker.queue_first_sync, real_queue)
        self.assertEqual(pulled, [], "the finisher pulled under the lock")
        self.assertEqual(queued, [(str(self.tid), self.state["item_id"])])
        self.assertTrue(polled[0]["done"], polled[0])
        self.assertEqual(polled[0]["kind"], "add")
        self.assertEqual(polled[0].get("first_sync"), "pending")
        self.assertEqual(self.state["exchanges"], 1)

    def test_the_sweep_finishes_open_sessions_and_closes_old_ones(self):
        self._start()
        self.state["link_done"] = True
        _age(self.lt, plaid_links.SWEEP_AFTER_S + 5)
        with _patch(self.state):
            out = plaid_links.sweep()
        self.assertEqual(out.get(self.lt), "done")
        self.assertTrue(self._has_item())
        # an abandoned one past the open window is closed as exited
        other = f"lt-{uuid.uuid4().hex[:10]}"
        plaid_links.record(self.tid, other, "add", None, False, None)
        _age(other, plaid_links.OPEN_FOR_S + 5)
        with _patch(self.state):
            plaid_links.sweep()
        self.assertEqual(_row(other)["reply"]["kind"], "exited")
        _age(other, plaid_links.KEEP_S + 5)
        with _patch(self.state):
            plaid_links.sweep()
        self.assertIsNone(_row(other))

    def test_an_exit_with_nobody_polling_is_closed_by_the_worker(self):
        self._start()
        self.state["exited"] = True
        with _patch(self.state):
            self.assertEqual(plaid_links.finish_session(self.tid, self.lt),
                             "done")
        self.assertEqual(_row(self.lt)["reply"]["kind"], "exited")

    def test_a_returning_client_lists_its_open_sessions(self):
        self._start()
        r = self.client.get("/accounts/plaid/links/open")
        self.assertEqual(r.status_code, 200, r.text)
        tokens = [s["link_token"] for s in r.json()["sessions"]]
        self.assertIn(self.lt, tokens)
        self.state["link_done"] = True
        self._poll()
        tokens = [s["link_token"] for s in
                  self.client.get("/accounts/plaid/links/open"
                                  ).json()["sessions"]]
        self.assertNotIn(self.lt, tokens)

    def test_another_household_cannot_reach_a_recorded_session(self):
        self._start()
        pages._PLAID_LINK.clear()
        self.assertIsNone(plaid_links.load(self.lt, str(uuid.uuid4())))
        self.assertEqual(plaid_links.finish_session(str(uuid.uuid4()),
                                                    self.lt), "gone")

    def test_a_session_given_up_under_the_cap_stays_given_up(self):
        from oikonome.sync import base as sync_base
        conn = tenancy.tenant_connect(self.tid)
        try:
            held = sync_base.institutions_held(conn)
        finally:
            conn.close()
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: held + 1):
            self._start()
            pages._PLAID_LINK[self.lt]["at"] -= pages._PLAID_ADD_LIVE + 1
            gone = self.lt
            self.lt = self.state["link_token"] = \
                f"lt-{uuid.uuid4().hex[:10]}"
            self._start()
        self.assertIsNone(_row(gone))
        self.state["link_done"] = True
        self.state["any_token"] = True
        with _patch(self.state):
            self.assertEqual(plaid_links.finish_session(self.tid, gone),
                             "gone")
            r = self.client.get(f"/accounts/plaid/link/{gone}/status")
        self.assertEqual(r.status_code, 404)


class LinkWebhookTests(unittest.TestCase):
    def test_a_finished_link_webhook_queues_the_worker(self):
        from oikonome.web import plaid_webhook
        from .util import make_db
        conn = make_db()
        try:
            tid = conn.execute("SELECT current_setting('app.tenant_id') "
                               "AS t").fetchone()["t"]
        finally:
            conn.close()
        lt = f"lt-{uuid.uuid4().hex[:10]}"
        plaid_links.record(tid, lt, "add", None, False, None)
        calls = []

        async def fake_enqueue(tenant_id, link_token):
            calls.append((tenant_id, link_token))
            return True

        async def ok(header, body):
            return True
        from oikonome.web.app import app
        http = TestClient(app)
        for code, token, want in (("SESSION_FINISHED", lt,
                                   "link-finish-enqueued"),
                                  ("ITEM_ADD_RESULT", "lt-unknown",
                                   "unknown-link")):
            body = json.dumps({"webhook_type": "LINK", "webhook_code": code,
                               "link_token": token,
                               "public_token": "pub-z"}).encode()
            with mock.patch.object(plaid_webhook, "verify", ok), \
                    mock.patch.object(plaid_webhook, "_enqueue_link_finish",
                                      fake_enqueue):
                r = http.post("/api/plaid/webhook", content=body,
                              headers={"plaid-verification": "x",
                                       "content-type": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["disposition"], want)
        self.assertEqual(calls, [(str(tid), lt)])


class ExitGraceTests(unittest.TestCase):
    def test_an_exit_is_called_only_on_a_second_look(self):
        sess = {"tenant_id": str(uuid.uuid4()), "item_id": None,
                "kind": "add"}
        finished = [{"finished_at": "2026-01-01T00:00:00Z"}]
        now = time.time()
        self.assertIsNone(plaid_links.exited(sess, "lt-x", finished, now))
        self.assertIsNone(plaid_links.exited(sess, "lt-x", finished,
                                             now + 1))
        self.assertEqual(plaid_links.exited(
            sess, "lt-x", finished,
            now + plaid_links.EXIT_GRACE_S + 1)["kind"], "exited")


if __name__ == "__main__":
    unittest.main()
