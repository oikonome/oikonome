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
        if path == "/item/public_token/exchange":
            return httpx.Response(200, text=json.dumps(
                {"item_id": "pl-new-1", "access_token": "access-prod-new"}))
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

    def test_add_flow_end_to_end(self):
        state = {"link_token": "lt-add"}
        with _patch_client(state):
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

    def test_manage_accounts_sites(self):
        s = self._src("components/ManageAccounts.tsx")
        self.assertEqual(s.count('update_failed'), 2,
                         "both the fix door and the shared-accounts door")

    def test_accounts_editor_site(self):
        s = self._src("pages/Accounts.tsx")
        self.assertIn('update_failed', s)


if __name__ == "__main__":
    unittest.main()
