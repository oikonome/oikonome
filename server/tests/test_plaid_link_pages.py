"""Plaid hosted-link web plumbing: start add/re-auth sessions, the polling
status route that completes the exchange or clears the error status, the
tenant pinning on link sessions, and the per-item manual sync route."""

import json
import unittest
import uuid
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import base as sync_base
from oikonome.sync import plaid
from oikonome.web import pages

from .util import _ensure_db, seed_accounts, write_config

BALANCES = {"accounts": [
    {"account_id": "p-chk", "name": "Plaid Checking", "type": "depository",
     "subtype": "checking",
     "balances": {"current": 1000.0, "iso_currency_code": "USD"}}]}
TXN_PAGE = {"added": [], "modified": [], "removed": [],
            "has_more": False, "next_cursor": "c-end"}


def _transport(state: dict):
    """One mock Plaid backend; `state` mutates to drive the flow:
    state['link_done'] flips /link/token/get to report completion."""
    def handler(request):
        path = request.url.path
        body = json.loads(request.content.decode() or "{}")
        if path == "/link/token/create":
            state["create_body"] = body
            return httpx.Response(200, text=json.dumps(
                {"link_token": state.get("link_token", "lt-1"),
                 "hosted_link_url": "https://hosted.plaid.com/x"}))
        if path == "/link/token/get" and state.get("session_get_fails"):
            return httpx.Response(400, text=json.dumps(
                {"error_type": "INVALID_INPUT",
                 "error_code": "INVALID_LINK_TOKEN",
                 "error_message": "detail-from-plaid-session",
                 "request_id": "req-session-77"}))
        if path == "/link/token/get":
            if not state.get("link_done"):
                return httpx.Response(200, text=json.dumps(
                    {"link_sessions": []}))
            if state.get("mode") == "update":
                return httpx.Response(200, text=json.dumps(
                    {"link_sessions": [{"finished_at": "2026-07-15T00:00:00Z"}]}))
            return httpx.Response(200, text=json.dumps(
                {"link_sessions": [{"results": {"item_add_results": [
                    {"public_token": "pub-1"}]}}]}))
        if path == "/item/public_token/exchange" and state.get("exchange_fails"):
            return httpx.Response(400, text=json.dumps(
                {"error_type": "INVALID_INPUT",
                 "error_code": "INVALID_PUBLIC_TOKEN",
                 "error_message": "detail-from-plaid-exchange",
                 "request_id": "req-exchange-88"}))
        if path == "/item/public_token/exchange":
            state["exchanges"] = state.get("exchanges", 0) + 1
            if state["exchanges"] > 1 and state.get("single_use"):
                # Plaid's answer to a public token used a second time
                return httpx.Response(400, text=json.dumps(
                    {"error_type": "INVALID_INPUT",
                     "error_code": "INVALID_PUBLIC_TOKEN",
                     "error_message": "public token already exchanged"}))
            return httpx.Response(200, text=json.dumps(
                {"item_id": state.get("item_id", "pl-new-1"),
                 "access_token": "access-prod-new"}))
        if path == "/item/get" and state.get("item_get_fails") == "plaid":
            return httpx.Response(400, text=json.dumps(
                {"error_type": "API_ERROR",
                 "error_code": "INTERNAL_SERVER_ERROR",
                 "error_message": "detail-from-plaid-xyz",
                 "request_id": "req-secret-123"}))
        if path == "/item/get" and state.get("item_get_fails") == "network":
            raise httpx.ConnectError("connect to 10.9.8.7:443 refused",
                                     request=request)
        if path == "/item/get":
            return httpx.Response(200, text=json.dumps(
                {"item": {"institution_id": "ins_1",
                          "error": state.get("item_error")}}))
        if path == "/institutions/get_by_id":
            return httpx.Response(200, text=json.dumps(
                {"institution": {"name": "Chase"}}))
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps(BALANCES))
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(TXN_PAGE))
        if path == "/liabilities/get":
            return httpx.Response(200, text=json.dumps(
                {"liabilities": {"credit": [], "mortgage": [], "student": []}}))
        if path == "/investments/holdings/get":
            return httpx.Response(200, text=json.dumps(
                {"accounts": [], "securities": [], "holdings": []}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


def _patch_client(state):
    """Route code calls plaid.Client.for_tenant(conn) — pin it to the mock
    transport (config creds are set, but tests must never do real HTTP)."""
    def for_tenant(conn, transport=None):
        return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                            transport=_transport(state))
    return mock.patch.object(plaid.Client, "for_tenant", staticmethod(for_tenant))


class PlaidLinkPagesTests(unittest.TestCase):
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
            "email": f"pl-{uuid.uuid4().hex[:8]}@example.dev",
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

    def test_add_flow_end_to_end(self):
        from oikonome.jobs import worker
        state = {"link_token": "lt-add"}
        queued = []
        with _patch_client(state), mock.patch.object(
                worker, "queue_first_sync",
                lambda t, i: queued.append((str(t), i))):
            r = self.client.post("/accounts/plaid/link", data={},
                                 follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertEqual(r.headers["location"],
                             "/accounts/plaid/link/lt-add")
            # add mode negotiated transactions + liabilities
            self.assertEqual(state["create_body"]["products"],
                             ["transactions"])
            # waiting page renders with the hosted URL
            r = self.client.get("/accounts/plaid/link/lt-add")
            self.assertEqual(r.status_code, 200)
            self.assertIn("https://hosted.plaid.com/x", r.text)
            self.assertIn("Add a bank connection", r.text)
            # not finished yet
            s = self.client.get("/accounts/plaid/link/lt-add/status").json()
            self.assertEqual(s, {"done": False})
            # user finishes on the hosted page → poll completes the exchange
            state["link_done"] = True
            s = self.client.get("/accounts/plaid/link/lt-add/status").json()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add")
        self.assertEqual(s["institution"], "Chase")   # resolved via /item/get
        # the first pull is the worker's: the answer says so, and the job
        # for the new Item was queued
        self.assertEqual(s["first_sync"], "pending")
        self.assertEqual(queued, [(str(self.tid), "pl-new-1")])
        with _patch_client(state):
            worker._sync_item_body(str(self.tid), "pl-new-1", True)
        conn = tenancy.tenant_connect(self.tid)
        try:
            it = conn.execute("SELECT institution_name, status, tx_cursor "
                              "FROM items WHERE id='pl-new-1'").fetchone()
            self.assertEqual(it["institution_name"], "Chase")
            self.assertEqual(it["status"], "ok")
            self.assertEqual(it["tx_cursor"], "c-end")   # first sync ran
            # polling again replays the cached completion (no re-exchange)
            with _patch_client({"link_done": True}):
                s = self.client.get(
                    "/accounts/plaid/link/lt-add/status").json()
            self.assertEqual(s["kind"], "add")
        finally:
            conn.close()

    def test_add_mode_exit_resolves_instead_of_polling_forever(self):
        """A user who bails out of Hosted Link gets finished_at with NO
        item_add_results. Falling through to {"done": False} forever would
        leave the SPA's add button on "Connecting…" until its 15-minute
        deadline. Instead: one grace re-poll
        (results can lag the stamp), then done with kind "exited"."""
        import time
        state = {"link_token": "lt-exit", "link_done": True,
                 "mode": "update"}   # finished_at, no results — an exit
        with _patch_client(state):
            self.client.post("/accounts/plaid/link", data={},
                             follow_redirects=False)
            # first sight of the empty finished session starts the grace
            s = self.client.get("/accounts/plaid/link/lt-exit/status").json()
            self.assertEqual(s, {"done": False})
            # inside the grace window it still waits for late results
            s = self.client.get("/accounts/plaid/link/lt-exit/status").json()
            self.assertEqual(s, {"done": False})
            # …and once the grace passes, the session resolves as exited
            pages._PLAID_LINK["lt-exit"]["finished_empty_at"] = \
                time.time() - 30
            s = self.client.get("/accounts/plaid/link/lt-exit/status").json()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "exited")
        # the resolution is cached like every other terminal state
        with _patch_client({"link_done": True, "mode": "update"}):
            s = self.client.get("/accounts/plaid/link/lt-exit/status").json()
        self.assertEqual(s["kind"], "exited")

    def test_manage_accounts_opens_the_selection_checklist(self):
        """manage_accounts=1 on an update session must ask Link for the
        shared-accounts checklist — the only lever that stops Plaid
        billing a deselected account. Add mode must never send it."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, "it-sel", "plaid", "Demo",
                                  "access-prod-abc")
        finally:
            conn.close()
        state = {"link_token": "lt-sel"}
        with _patch_client(state):
            r = self.client.post("/accounts/plaid/link",
                                 data={"item_id": "it-sel",
                                       "manage_accounts": "1"},
                                 headers={"accept": "application/json"})
            self.assertEqual(r.status_code, 200)
        self.assertEqual(state["create_body"].get("update"),
                         {"account_selection_enabled": True})
        state = {"link_token": "lt-add2"}
        with _patch_client(state):
            self.client.post("/accounts/plaid/link", data={
                "manage_accounts": "1"}, follow_redirects=False)
        self.assertNotIn("update", state["create_body"])

    def _flag_new_accounts(self, item_id):
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, item_id, "plaid", "Demo",
                                  "access-prod-abc")
            conn.execute("UPDATE items SET webhook_status='new_accounts', "
                         "webhook_status_at=now() WHERE id=%s", (item_id,))
        finally:
            conn.close()

    def _webhook_status(self, item_id):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute("SELECT webhook_status FROM items WHERE id=%s",
                                (item_id,)).fetchone()["webhook_status"]
        finally:
            conn.close()

    def test_new_accounts_to_share_opens_the_checklist_by_itself(self):
        """An item Plaid flagged with new accounts can only share them
        through the shared-accounts checklist, so the server asks Link for
        it whatever the client sends — an installed app that sends no
        manage_accounts must still get a picker that can do what its
        'share' button says. Its completion then clears the flag."""
        self._flag_new_accounts("it-new")
        state = {"link_token": "lt-new", "mode": "update"}
        with _patch_client(state):
            r = self.client.post("/accounts/plaid/link",
                                 data={"item_id": "it-new"},
                                 headers={"accept": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(state["create_body"].get("update"),
                             {"account_selection_enabled": True})
            state["link_done"] = True
            s = self.client.get("/accounts/plaid/link/lt-new/status").json()
        self.assertEqual(s["kind"], "update")
        self.assertIsNone(self._webhook_status("it-new"))

    def test_a_session_without_the_checklist_leaves_new_accounts_flagged(self):
        """A completion that could not have shared anything (a session
        opened before the flag arrived, so without the checklist) must not
        clear the new-accounts prompt: Plaid does not send it again, and
        the new accounts would never be offered."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, "it-late", "plaid", "Demo",
                                  "access-prod-abc")
        finally:
            conn.close()
        state = {"link_token": "lt-late", "mode": "update"}
        with _patch_client(state):
            self.client.post("/accounts/plaid/link",
                             data={"item_id": "it-late"},
                             headers={"accept": "application/json"})
            self.assertNotIn("update", state["create_body"])
            self._flag_new_accounts("it-late")      # arrives mid-session
            state["link_done"] = True
            s = self.client.get("/accounts/plaid/link/lt-late/status").json()
        self.assertEqual(s["kind"], "update")
        self.assertEqual(self._webhook_status("it-late"), "new_accounts")

    def test_deselected_accounts_are_hidden_when_selection_completes(self):
        """A deselected account stops being returned by Plaid — hide its
        local row on completion or it lingers looking connected with a
        forever-stale balance. Legacy history imports (NULL balance) ride
        under the same item and must NOT be touched: hiding them would
        pull years of closed-account history out of net worth."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, "it-sel2", "plaid", "Demo",
                                  "access-prod-abc")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES "
                "('p-chk','it-sel2','Live kept','depository','checking',100),"
                "('p-sav','it-sel2','Live deselected','depository',"
                "'savings',200) ON CONFLICT (tenant_id, id) DO UPDATE "
                "SET item_id=EXCLUDED.item_id, "
                "balance_current=EXCLUDED.balance_current, "
                "user_removed_at=NULL")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES ('legacy-hist','it-sel2',"
                "'Old import','investment','closed',NULL) "
                "ON CONFLICT (tenant_id, id) DO NOTHING")
        finally:
            conn.close()
        # mock /accounts/get returns ONLY p-chk (BALANCES) — the user
        # kept it and deselected p-sav
        state = {"link_token": "lt-sel2", "link_done": True,
                 "mode": "update"}
        with _patch_client(state):
            self.client.post("/accounts/plaid/link",
                             data={"item_id": "it-sel2",
                                   "manage_accounts": "1"},
                             headers={"accept": "application/json"})
            s = self.client.get(
                "/accounts/plaid/link/lt-sel2/status").json()
        self.assertEqual(s["kind"], "update")
        conn = tenancy.tenant_connect(self.tid)
        try:
            rows = {r["id"]: r["user_removed_at"] for r in conn.execute(
                "SELECT id, user_removed_at FROM accounts "
                "WHERE item_id='it-sel2'").fetchall()}
        finally:
            conn.close()
        self.assertIsNone(rows["p-chk"], "a kept account must stay visible")
        self.assertIsNotNone(rows["p-sav"],
                             "the deselected account must be hidden")
        self.assertIsNone(rows["legacy-hist"],
                          "legacy imports must never be auto-hidden")

    def test_json_start_returns_hosted_url_for_spa(self):
        """SPA Accept: application/json — open Plaid only, no waiting-tab
        redirect (welcome stays put and polls /status itself)."""
        state = {"link_token": "lt-json"}
        with _patch_client(state):
            r = self.client.post(
                "/accounts/plaid/link", data={},
                headers={"Accept": "application/json"})
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["link_token"], "lt-json")
            self.assertEqual(body["hosted_link_url"],
                             "https://hosted.plaid.com/x")
            self.assertEqual(body["kind"], "add")
            # session is live for status polls
            s = self.client.get(
                "/accounts/plaid/link/lt-json/status").json()
            self.assertEqual(s, {"done": False})

    def test_reauth_flow_clears_error_status(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, "pl-fix", "plaid", "Old Bank",
                                  "access-prod-old")
            conn.execute("UPDATE items SET "
                         "status='error:ITEM_LOGIN_REQUIRED' "
                         "WHERE id='pl-fix'")
        finally:
            conn.close()
        state = {"link_token": "lt-fix", "mode": "update"}
        with _patch_client(state):
            r = self.client.post("/accounts/plaid/link",
                                 data={"item_id": "pl-fix"},
                                 follow_redirects=False)
            self.assertEqual(r.headers["location"],
                             "/accounts/plaid/link/lt-fix")
            # update mode passes the access token, not products
            self.assertIn("access_token", state["create_body"])
            self.assertNotIn("products", state["create_body"])
            r = self.client.get("/accounts/plaid/link/lt-fix")
            self.assertIn("Re-authenticate Old Bank", r.text)
            state["link_done"] = True
            s = self.client.get("/accounts/plaid/link/lt-fix/status").json()
        self.assertEqual(s["kind"], "update")
        conn = tenancy.tenant_connect(self.tid)
        try:
            st = conn.execute("SELECT status FROM items WHERE id='pl-fix'"
                              ).fetchone()["status"]
            self.assertEqual(st, "ok")
        finally:
            conn.close()

    def test_reauth_abandoned_session_stays_broken(self):
        """finished_at without success (user abandoned Link) must NOT flip
        the item healthy — verified against the item's live error state."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, "pl-bad", "plaid", "Bad Bank",
                                  "access-prod-bad")
            conn.execute("UPDATE items SET "
                         "status='error:ITEM_LOGIN_REQUIRED' "
                         "WHERE id='pl-bad'")
        finally:
            conn.close()
        state = {"link_token": "lt-bad", "mode": "update", "link_done": True,
                 "item_error": {"error_code": "ITEM_LOGIN_REQUIRED"}}
        with _patch_client(state):
            self.client.post("/accounts/plaid/link",
                             data={"item_id": "pl-bad"},
                             follow_redirects=False)
            s = self.client.get("/accounts/plaid/link/lt-bad/status").json()
        self.assertEqual(s["kind"], "update_failed")
        self.assertEqual(s["error"], "ITEM_LOGIN_REQUIRED")
        conn = tenancy.tenant_connect(self.tid)
        try:
            st = conn.execute("SELECT status FROM items WHERE id='pl-bad'"
                              ).fetchone()["status"]
            self.assertEqual(st, "error:ITEM_LOGIN_REQUIRED")
        finally:
            conn.close()

    def test_a_failed_item_check_reports_a_code_not_its_message(self):
        """When the after-Link item check itself fails, the page is told
        Plaid's error code, or the failure's kind — never the message,
        which carries request ids, hosts and response text."""
        for i, (how, want) in enumerate((("plaid", "INTERNAL_SERVER_ERROR"),
                                         ("network", "HTTP_TRANSPORT"))):
            item, token = f"pl-chk{i}", f"lt-chk{i}"
            conn = tenancy.tenant_connect(self.tid)
            try:
                sync_base.upsert_item(conn, item, "plaid", "Check Bank",
                                      "access-prod-chk")
                conn.execute("UPDATE items SET "
                             "status='error:ITEM_LOGIN_REQUIRED' "
                             "WHERE id=%s", (item,))
            finally:
                conn.close()
            state = {"link_token": token, "mode": "update",
                     "link_done": True, "item_get_fails": how}
            with _patch_client(state), self.assertLogs("oikonome.plaid"):
                self.client.post("/accounts/plaid/link",
                                 data={"item_id": item},
                                 follow_redirects=False)
                r = self.client.get(f"/accounts/plaid/link/{token}/status")
            self.assertEqual(r.json()["kind"], "update_failed", r.text)
            self.assertEqual(r.json()["error"], want)
            for leak in ("detail-from-plaid", "req-secret", "10.9.8.7"):
                self.assertNotIn(leak, r.text)

    def _narrow_bank(self, item: str, inst: str = "ins_1", days: int = 31):
        from oikonome.sync import base as sync_base
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, item, "plaid", "Narrow Bank",
                                  f"access-prod-{item}",
                                  raw={"days_requested": days})
            conn.execute("UPDATE items SET institution_id=%s WHERE id=%s",
                         (inst, item))
        finally:
            conn.close()

    def _ledger_reason(self, item: str):
        admin = tenancy.admin_connect()
        try:
            led = admin.execute("SELECT removed_reason FROM plaid_item_ledger "
                                "WHERE item_id=%s", (item,)).fetchone()
        finally:
            admin.close()
        return led["removed_reason"] if led else None

    def test_a_relink_for_more_history_replaces_the_same_bank_only_once_the_new_link_is_in(self):
        """A bank linked with a short window can only widen it through a
        fresh Item. Starting a link with relink_item_id binds the old Item
        to the session but leaves it live and syncing; the old Item is
        released — under the relink reason the institution cap ignores —
        only when the new Item of the SAME bank is saved, and the household's
        slot count never rises for the swap. The connections list says
        which window each bank was linked with."""
        from oikonome.sync import base as sync_base
        from oikonome.sync import plaid as plaid_mod
        item = "pl-relink-old"
        self._narrow_bank(item)
        conn = tenancy.tenant_connect(self.tid)
        try:
            held_before = sync_base.institutions_held(conn)
            rows = {c["id"]: c for c in
                    self.client.get("/api/connections").json()["connections"]}
            self.assertEqual(rows[item]["history_months"], 1)
        finally:
            conn.close()
        state = {"link_token": "lt-relink", "item_id": "pl-relink-new"}
        removed = []
        with _patch_client(state), mock.patch.object(
                plaid_mod.Client, "remove_item",
                side_effect=lambda tok: removed.append(tok) or {"removed": True}):
            r = self.client.post("/accounts/plaid/link",
                                 data={"relink_item_id": item},
                                 headers={"accept": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["kind"], "add")
            conn = tenancy.tenant_connect(self.tid)
            try:
                # the start released nothing: the old bank is live and bound
                it = conn.execute("SELECT status, raw->>'relink_session' AS b "
                                  "FROM items WHERE id=%s", (item,)).fetchone()
                self.assertNotEqual(it["status"], "archived")
                self.assertEqual(it["b"], "lt-relink")
                self.assertEqual(removed, [])
            finally:
                conn.close()
            # the person finishes at the same bank (ins_1): the exchange
            # saves the new Item, then releases the old one
            state["link_done"] = True
            s = self.client.get("/accounts/plaid/link/lt-relink/status").json()
        self.assertTrue(s["done"], s)
        self.assertEqual(s["kind"], "add", s)
        conn = tenancy.tenant_connect(self.tid)
        try:
            old = conn.execute("SELECT status, raw->>'relink_session' AS b "
                               "FROM items WHERE id=%s", (item,)).fetchone()
            self.assertEqual(old["status"], "archived")
            self.assertIsNone(old["b"])
            self.assertEqual(removed, [f"access-prod-{item}"])
            new = conn.execute("SELECT status FROM items WHERE id=%s",
                               ("pl-relink-new",)).fetchone()
            self.assertIsNotNone(new)
            self.assertNotEqual(new["status"], "archived")
            self.assertIn(self._ledger_reason(item),
                          (None, sync_base.RELINK_LEDGER_REASON))
            self.assertEqual(sync_base.released_count(conn), 0)
            self.assertLessEqual(sync_base.institutions_held(conn), held_before)
        finally:
            conn.close()
        # an archived Item is refused the door
        r = self.client.post("/accounts/plaid/link",
                             data={"relink_item_id": item},
                             headers={"accept": "application/json"})
        self.assertEqual(r.status_code, 400)

    def test_a_relink_that_links_another_bank_keeps_the_old_one_and_takes_its_own_place(self):
        """The relink door is not an uncounted disconnect: a sign-in at a
        DIFFERENT bank from that door is an ordinary add — the old Item
        stays live, nothing is released, and the new one is counted."""
        from oikonome.sync import base as sync_base
        from oikonome.sync import plaid as plaid_mod
        item = "pl-relink-keep"
        self._narrow_bank(item, inst="ins_other")
        state = {"link_token": "lt-relink2", "item_id": "pl-relink-other"}
        removed = []
        with _patch_client(state), mock.patch.object(
                plaid_mod.Client, "remove_item",
                side_effect=lambda tok: removed.append(tok) or {"removed": True}):
            r = self.client.post("/accounts/plaid/link",
                                 data={"relink_item_id": item},
                                 headers={"accept": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
            state["link_done"] = True      # the fake bank is ins_1 — another bank
            s = self.client.get("/accounts/plaid/link/lt-relink2/status").json()
        self.assertTrue(s["done"], s)
        conn = tenancy.tenant_connect(self.tid)
        try:
            old = conn.execute("SELECT status, raw->>'relink_session' AS b "
                               "FROM items WHERE id=%s", (item,)).fetchone()
            self.assertNotEqual(old["status"], "archived")
            self.assertIsNone(old["b"])          # the binding is spent
            self.assertEqual(removed, [])
            self.assertIsNone(self._ledger_reason(item))
            self.assertEqual(sync_base.released_count(conn), 0)
        finally:
            conn.close()

    def test_a_relink_is_refused_unless_the_window_is_wider(self):
        """Relink is offered for more history only: a bank already on the
        household's window (or on everything) is refused before any
        session is minted, and a start that is refused leaves it live."""
        item = "pl-relink-wide"
        self._narrow_bank(item, days=730)      # everything already
        state = {"link_token": "lt-relink3"}
        with _patch_client(state):
            r = self.client.post("/accounts/plaid/link",
                                 data={"relink_item_id": item},
                                 headers={"accept": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertNotIn("create_body", state)   # no link token minted
        # a one-month bank with the household on one month: refused too
        self.client.post("/api/settings", json={"history_months": 1})
        item2 = "pl-relink-same-window"
        self._narrow_bank(item2, days=31)
        with _patch_client(state):
            r = self.client.post("/accounts/plaid/link",
                                 data={"relink_item_id": item2},
                                 headers={"accept": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)
        self.client.post("/api/settings", json={"history_months": None})
        conn = tenancy.tenant_connect(self.tid)
        try:
            for i in (item, item2):
                st = conn.execute("SELECT status FROM items WHERE id=%s",
                                  (i,)).fetchone()["status"]
                self.assertNotEqual(st, "archived")
        finally:
            conn.close()

    def test_one_relink_at_a_time_and_a_failed_release_still_counts_as_a_relink(self):
        """A second relink start on an Item whose session is still open is
        refused (409) rather than overwriting the binding; and when Plaid
        does not confirm the release at the settle, the old Item is
        archived with its token kept for the retry and the ledger already
        says relink, so the swap never holds a slot."""
        from oikonome.sync import base as sync_base
        from oikonome.sync import plaid as plaid_mod
        item = "pl-relink-twice"
        self._narrow_bank(item)
        state = {"link_token": "lt-relink5", "item_id": "pl-relink-twice-new"}
        with _patch_client(state), mock.patch.object(
                plaid_mod.Client, "remove_item",
                side_effect=plaid_mod.PlaidError(
                    {"error_code": "INTERNAL_SERVER_ERROR",
                     "error_type": "API_ERROR", "error_message": "down"}, 500)):
            r = self.client.post("/accounts/plaid/link",
                                 data={"relink_item_id": item},
                                 headers={"accept": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
            r2 = self.client.post("/accounts/plaid/link",
                                  data={"relink_item_id": item},
                                  headers={"accept": "application/json"})
            self.assertEqual(r2.status_code, 409, r2.text)
            state["link_done"] = True
            s = self.client.get("/accounts/plaid/link/lt-relink5/status").json()
        self.assertTrue(s["done"], s)
        self.assertEqual(s["kind"], "add", s)
        conn = tenancy.tenant_connect(self.tid)
        try:
            old = conn.execute(
                "SELECT status, archived_reason, access_token, "
                "raw->>'relink_session' AS b FROM items WHERE id=%s",
                (item,)).fetchone()
            self.assertEqual(old["status"], "archived")
            self.assertEqual(old["archived_reason"], "disconnect-release-failed")
            self.assertTrue(old["access_token"])     # kept for the retry
            self.assertIsNone(old["b"])
            self.assertIn(self._ledger_reason(item),
                          (None, sync_base.RELINK_LEDGER_REASON))
            self.assertEqual(sync_base.released_count(conn), 0)
        finally:
            conn.close()

    def test_a_relink_exchange_that_cannot_name_the_bank_keeps_the_old_one(self):
        """Releasing a live login needs positive evidence: when Plaid does
        not say which bank the new Item is, the old Item keeps its token
        and its status, and the new one is an ordinary add."""
        from oikonome.sync import plaid as plaid_mod
        item = "pl-relink-unknown"
        self._narrow_bank(item)
        state = {"link_token": "lt-relink6", "item_id": "pl-relink-unknown-new",
                 "item_get_fails": "plaid"}
        removed = []
        with _patch_client(state), mock.patch.object(
                plaid_mod.Client, "remove_item",
                side_effect=lambda tok: removed.append(tok) or {"removed": True}), \
                self.assertLogs("oikonome", level="INFO"):
            r = self.client.post("/accounts/plaid/link",
                                 data={"relink_item_id": item},
                                 headers={"accept": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
            state["link_done"] = True
            s = self.client.get("/accounts/plaid/link/lt-relink6/status").json()
        self.assertTrue(s["done"], s)
        conn = tenancy.tenant_connect(self.tid)
        try:
            old = conn.execute("SELECT status, access_token FROM items WHERE id=%s",
                               (item,)).fetchone()
            self.assertNotEqual(old["status"], "archived")
            self.assertTrue(old["access_token"])
            self.assertEqual(removed, [])
        finally:
            conn.close()

    def test_a_relink_whose_start_fails_leaves_the_bank_live(self):
        """A Plaid refusal of the link token (an outage, a bad completion
        redirect) after the binding check releases nothing."""
        from oikonome.sync import plaid as plaid_mod
        item = "pl-relink-outage"
        self._narrow_bank(item)
        state = {"link_token": "lt-relink4"}
        with _patch_client(state), mock.patch.object(
                plaid_mod.Client, "create_hosted_link",
                side_effect=plaid_mod.PlaidError(
                    {"error_code": "INVALID_FIELD", "error_type": "INVALID_REQUEST",
                     "error_message": "redirect uri not allowed"}, 400)):
            r = self.client.post("/accounts/plaid/link",
                                 data={"relink_item_id": item},
                                 headers={"accept": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)
        conn = tenancy.tenant_connect(self.tid)
        try:
            it = conn.execute("SELECT status, access_token, raw->>'relink_session' AS b "
                              "FROM items WHERE id=%s", (item,)).fetchone()
            self.assertNotEqual(it["status"], "archived")
            self.assertTrue(it["access_token"])
            self.assertIsNone(it["b"])
        finally:
            conn.close()

    def test_the_completion_redirect_is_opt_in_and_https_only(self):
        """The bank tab is sent back to this install only when the operator
        asked for it: `1` builds /plaid/done under the base URL, a full
        https URL is passed as given, anything else (unset, http) leaves
        Plaid's own closing page."""
        import os
        from oikonome.web import pages
        keys = ("OIKONOME_PLAID_COMPLETION_REDIRECT", "OIKONOME_BASE_URL")
        cases = [({}, None),
                 ({"OIKONOME_PLAID_COMPLETION_REDIRECT": "1",
                   "OIKONOME_BASE_URL": "https://money.example.test/"},
                  "https://money.example.test/app/plaid/done"),
                 ({"OIKONOME_PLAID_COMPLETION_REDIRECT": "1"}, None),
                 ({"OIKONOME_PLAID_COMPLETION_REDIRECT":
                   "https://x.example.test/done"}, "https://x.example.test/done"),
                 ({"OIKONOME_PLAID_COMPLETION_REDIRECT":
                   "http://x.example.test/done"}, None)]
        for env, want in cases:
            with mock.patch.dict("os.environ", env, clear=False):
                for k in keys:
                    if k not in env:
                        os.environ.pop(k, None)
                self.assertEqual(pages.plaid_completion_redirect(), want, env)
        state = {"link_token": "lt-redir"}
        with _patch_client(state), mock.patch.dict(
                "os.environ", {"OIKONOME_PLAID_COMPLETION_REDIRECT":
                               "https://x.example.test/done"}):
            self.client.post("/accounts/plaid/link", data={},
                             follow_redirects=False)
        self.assertEqual(state["create_body"]["hosted_link"]
                         ["completion_redirect_uri"],
                         "https://x.example.test/done")

    def test_a_plaid_refusal_of_the_poll_answers_typed_fields(self):
        """A Plaid error while the poll reads the session answers Plaid's
        own code, message and request id as fields — never the
        exception's text, which is where a transport detail or a trace
        would ride."""
        state = {"link_token": "lt-typed", "session_get_fails": True}
        with _patch_client(state):
            self.client.post("/accounts/plaid/link", data={},
                             follow_redirects=False)
            s = self.client.get("/accounts/plaid/link/lt-typed/status").json()
        self.assertFalse(s["done"])
        self.assertEqual(s["error_code"], "INVALID_LINK_TOKEN")
        self.assertEqual(s["request_id"], "req-session-77")
        self.assertEqual(s["error"],
                         "INVALID_LINK_TOKEN: detail-from-plaid-session")
        self.assertNotIn("[request_id", s["error"])

    def test_a_failed_exchange_answers_typed_fields(self):
        """The exchange failing finishes the session as a failed add with
        the same typed fields."""
        state = {"link_token": "lt-exfail", "link_done": True,
                 "exchange_fails": True}
        with _patch_client(state):
            self.client.post("/accounts/plaid/link", data={},
                             follow_redirects=False)
            s = self.client.get("/accounts/plaid/link/lt-exfail/status").json()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add_failed")
        self.assertEqual(s["error_code"], "INVALID_PUBLIC_TOKEN")
        self.assertEqual(s["request_id"], "req-exchange-88")
        self.assertEqual(s["error"],
                         "INVALID_PUBLIC_TOKEN: detail-from-plaid-exchange")

    def test_a_poll_queued_behind_the_finishing_one_reports_its_success(self):
        """Two polls of one finished session: the second passed the
        unlocked done-check before the first completed and took the sync
        lock after it. It must answer the finished link, never exchange
        the single-use public token again and turn the success into a
        failure."""
        import contextlib
        from oikonome.sync import base as base_mod
        token = "lt-race"
        state = {"link_token": token, "link_done": True,
                 "item_id": f"pl-race-{uuid.uuid4().hex[:6]}",
                 "single_use": True}
        user = {"tenant_id": self.tid, "role": "owner"}
        real_lock = base_mod.tenant_sync_lock
        first_ran = []

        @contextlib.contextmanager
        def lock_after_the_first_poll(conn, tid):
            if not first_ran:
                # this is the second poll, waiting on the lock while the
                # first one runs to completion
                first_ran.append(None)
                first_ran[0] = pages.plaid_link_status(token, user)
            with real_lock(conn, tid) as held:
                yield held

        with _patch_client(state):
            self.client.post("/accounts/plaid/link", data={},
                             follow_redirects=False)
            with mock.patch.object(base_mod, "tenant_sync_lock",
                                   lock_after_the_first_poll):
                second = pages.plaid_link_status(token, user)
        self.assertEqual(first_ran[0]["kind"], "add")
        self.assertEqual(second["kind"], "add", second)
        self.assertTrue(second["done"])
        self.assertEqual(state["exchanges"], 1)

    def test_parallel_add_sessions_cannot_pass_the_institution_cap(self):
        """Every open add session can still finish and create a billed
        Item, so with one slot left a second add session is refused until
        the first finishes or ages out — opening two and completing both
        would land the household over the cap."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            held = sync_base.institutions_held(conn)
        finally:
            conn.close()
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: held + 1), \
                _patch_client({"link_token": "lt-cap-1"}):
            r = self.client.post("/accounts/plaid/link", data={},
                                 headers={"Accept": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: held + 1), \
                _patch_client({"link_token": "lt-cap-2"}):
            r = self.client.post("/accounts/plaid/link", data={},
                                 headers={"Accept": "application/json"})
            self.assertEqual(r.status_code, 400, r.text)
            self.assertNotIn("lt-cap-2", pages._PLAID_LINK)
            # a finished session no longer holds the slot
            pages._PLAID_LINK["lt-cap-1"]["done"] = True
            r = self.client.post("/accounts/plaid/link", data={},
                                 headers={"Accept": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
            # nor does one abandoned past the hold window
            pages._PLAID_LINK["lt-cap-2"]["at"] -= pages._PLAID_ADD_HOLD + 1
            with _patch_client({"link_token": "lt-cap-3"}):
                r = self.client.post("/accounts/plaid/link", data={},
                                     headers={"Accept": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("lt-cap-3", pages._PLAID_LINK)

    def _cap_one_slot(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            held = sync_base.institutions_held(conn)
        finally:
            conn.close()
        return mock.patch.object(sync_base, "institution_cap",
                                 lambda conn=None: held + 1)

    def _start_json(self, token):
        with _patch_client({"link_token": token}):
            return self.client.post("/accounts/plaid/link", data={},
                                    headers={"Accept": "application/json"})

    def test_an_abandoned_add_window_gives_its_slot_to_a_new_start(self):
        """A closed bank window stops being polled; the next add at the
        cap must not wait out the hour behind it, and the abandoned
        session must not be able to finish beside its replacement."""
        with self._cap_one_slot():
            self.assertEqual(self._start_json("lt-gone").status_code, 200)
            pages._PLAID_LINK["lt-gone"]["at"] -= pages._PLAID_ADD_LIVE + 1
            r = self._start_json("lt-next")
            self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("lt-next", pages._PLAID_LINK)
        self.assertNotIn("lt-gone", pages._PLAID_LINK)
        r = self.client.get("/accounts/plaid/link/lt-gone/status")
        self.assertEqual(r.status_code, 404)

    def test_an_unpolled_add_survives_a_start_that_has_room(self):
        """A phone backgrounded through a long bank sign-in stops polling
        and comes back; another start with slots to spare must not take
        its session away. Only at the cap does the oldest unpolled one
        give up its place."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            held = sync_base.institutions_held(conn)
        finally:
            conn.close()
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: held + 2):
            self.assertEqual(self._start_json("lt-away").status_code, 200)
            pages._PLAID_LINK["lt-away"]["at"] -= pages._PLAID_ADD_LIVE + 1
            r = self._start_json("lt-room")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("lt-away", pages._PLAID_LINK)
            self.assertIn("lt-room", pages._PLAID_LINK)
            # now at the cap: the new start fits only without the
            # unpolled session, and the polled one is untouched
            r = self._start_json("lt-full")
            self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("lt-away", pages._PLAID_LINK)
        self.assertIn("lt-room", pages._PLAID_LINK)
        self.assertIn("lt-full", pages._PLAID_LINK)

    def test_two_windows_still_being_polled_cannot_pass_the_cap(self):
        with self._cap_one_slot():
            self.assertEqual(self._start_json("lt-live").status_code, 200)
            # older than the liveness window, but its client keeps polling
            pages._PLAID_LINK["lt-live"]["at"] -= pages._PLAID_ADD_LIVE + 1
            with _patch_client({"link_token": "lt-live"}):
                r = self.client.get("/accounts/plaid/link/lt-live/status")
            self.assertEqual(r.status_code, 200, r.text)
            r = self._start_json("lt-second")
            self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("lt-live", pages._PLAID_LINK)
        self.assertNotIn("lt-second", pages._PLAID_LINK)

    def test_link_session_pinned_to_tenant(self):
        pages._PLAID_LINK["lt-foreign"] = {
            "tenant_id": str(uuid.uuid4()),      # someone else's session
            "item_id": None, "kind": "add", "institution": None,
            "url": "https://hosted.plaid.com/x", "done": True}
        r = self.client.get("/accounts/plaid/link/lt-foreign",
                            follow_redirects=False)
        self.assertEqual(r.status_code, 303)     # bounced to /accounts
        r = self.client.get("/accounts/plaid/link/lt-foreign/status")
        self.assertEqual(r.status_code, 404)

    def test_unknown_reauth_item_404(self):
        with _patch_client({}):
            r = self.client.post("/accounts/plaid/link",
                                 data={"item_id": "nope"},
                                 follow_redirects=False)
        self.assertEqual(r.status_code, 404)

    def test_manual_item_sync_route(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, "pl-sync", "plaid", "Sync Bank",
                                  "access-prod-s")
            # pulled before: a bank's first pull is the worker's job
            conn.execute("INSERT INTO sync_log (item_id, added) "
                         "VALUES ('pl-sync', 0)")
        finally:
            conn.close()
        with _patch_client({}):
            r = self.client.post("/accounts/sync",
                                 data={"item_id": "pl-sync"},
                                 follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        # A pull that adds nothing is the ORDINARY outcome — a sync reads
        # the aggregator's copy and the bank refreshes that copy a few
        # times a day — so the message says "nothing new" rather than
        # "0 new transactions", which reads as a failure.
        self.assertIn("Sync%20Bank%3A%20nothing%20new", r.headers["location"])
        conn = tenancy.tenant_connect(self.tid)
        try:
            it = conn.execute("SELECT status, tx_cursor FROM items "
                              "WHERE id='pl-sync'").fetchone()
            self.assertEqual(it["status"], "ok")
            self.assertEqual(it["tx_cursor"], "c-end")
        finally:
            conn.close()
        # unknown item → friendly message, no 500
        r = self.client.post("/accounts/sync", data={"item_id": "ghost"},
                             follow_redirects=False)
        self.assertIn("unknown", r.headers["location"])

    def test_json_sync_door_reports_a_failure_as_an_error(self):
        """The clients' sync door must answer a failed or refused pull with
        an error status carrying the reason, so the app shows it as an
        error — a success notice that happens to say "failed" reads as done."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, "pl-json", "plaid", "Json Bank",
                                  "access-prod-j")
            # pulled before: a bank's first pull is the worker's job
            conn.execute("INSERT INTO sync_log (item_id, added) "
                         "VALUES ('pl-json', 0)")
        finally:
            conn.close()
        with _patch_client({}):
            r = self.client.post("/api/accounts/sync",
                                 json={"item_id": "pl-json"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertIn("Json Bank", r.json()["message"])
        with mock.patch.object(pages.plaid, "sync",
                               side_effect=RuntimeError("bank is down")):
            r = self.client.post("/api/accounts/sync",
                                 json={"item_id": "pl-json"})
        # not a 5xx: an edge proxy swaps an origin 502 for its own page
        # and the detail below would never reach the person
        self.assertEqual(r.status_code, 424)
        self.assertIn("bank is down", r.json()["detail"])
        r = self.client.post("/api/accounts/sync", json={"item_id": "ghost"})
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/accounts/sync", json={})
        self.assertEqual(r.status_code, 400)

    def test_accounts_page_shows_connections(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, "pl-show", "plaid", "Shown Bank",
                                  "access-prod-x")
            conn.execute("UPDATE items SET "
                         "status='error:ITEM_LOGIN_REQUIRED' "
                         "WHERE id='pl-show'")
        finally:
            conn.close()
        # Jinja page retired → SPA; the connection health comes from the API
        r = self.client.get("/accounts", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/app/accounts")
        conns = self.client.get("/api/connections").json()["connections"]
        shown = next(c for c in conns if c["institution_name"] == "Shown Bank")
        self.assertIn("ITEM_LOGIN_REQUIRED", shown["status"])


class LinkOutcomesAreHonest(unittest.TestCase):
    """All three Link call sites must branch on kind=update_failed —
    'Reconnected.' over a still-broken connection teaches the user to
    stop retrying."""

    def _src(self, rel):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / rel).read_text(encoding="utf-8")

    # The branch lives once, in ConnectHub's plaidLinkOutcome; every call
    # site must route its result through it.
    def test_the_shared_outcome_branches_on_update_failed(self):
        s = self._src("components/ConnectHub.tsx")
        fn = s[s.index("export function plaidLinkOutcome("):]
        self.assertIn('case "update_failed"', fn[:fn.index("\n}\n")])

    def test_manage_accounts_sites(self):
        s = self._src("components/ManageAccounts.tsx")
        self.assertEqual(s.count('plaidLinkOutcome(s, '), 2,
                         "both the fix door and the shared-accounts door")

    def test_accounts_editor_site(self):
        s = self._src("pages/Accounts.tsx")
        self.assertIn('plaidLinkOutcome(s, "manage")', s)
        self.assertIn('plaidLinkOutcome(s, itemId ? "fix" : "add")', s)


if __name__ == "__main__":
    unittest.main()
