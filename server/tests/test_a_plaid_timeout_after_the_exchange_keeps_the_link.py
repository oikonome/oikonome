"""A network failure while a new bank link completes must not lose the link.

A public token is single-use, and exchanging it is what creates the Item
(and starts Plaid billing it). So once the exchange has succeeded, nothing
that follows may report the link as failed: a re-poll would exchange the
spent token again, get INVALID_PUBLIC_TOKEN, and tell the person their bank
did not link while it sits connected — they link it again and pay for two.
And a failure between the exchange and the insert must not leave a live
Item with no row and nobody holding its token.

The invariants, each with the failure it rules out:

* Client.post turns every httpx transport error into a PlaidError (code
  HTTP_TRANSPORT), so each `except PlaidError` in the link and sync paths
  sees it; its message names the failure's kind, never hosts.
* A timeout in the first sync, or in the institution lookup before the
  insert, still reports kind "add", exchanges once, and keeps the Item;
  a first sync that did not land says first_sync "pending".
* A timeout ON the exchange leaves the session open to try again rather
  than settling it as failed.
* The item row failing to save hands the Item back to Plaid and reports
  a failure, never a 500.
"""

import json
import unittest
import uuid
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import plaid
from oikonome.web import pages

from .util import _ensure_db, seed_accounts, write_config


def _backend(state: dict):
    """A mock Plaid. `state['fail']` maps an endpoint path to the httpx
    error class it raises (once, when `state['fail_once']` names it)."""
    def handler(request):
        path = request.url.path
        state.setdefault("calls", []).append(path)
        fail = (state.get("fail") or {}).get(path)
        if fail is not None:
            if path in state.get("fail_once", ()):
                state["fail"].pop(path)
            raise fail("simulated failure to 203.0.113.7", request=request)
        if path == "/link/token/create":
            return httpx.Response(200, text=json.dumps(
                {"link_token": state["link_token"],
                 "hosted_link_url": "https://hosted.plaid.com/x"}))
        if path == "/link/token/get":
            if not state.get("link_done"):
                return httpx.Response(200, text=json.dumps(
                    {"link_sessions": []}))
            return httpx.Response(200, text=json.dumps(
                {"link_sessions": [{"link_session_id": "ls-1",
                                    "results": {"item_add_results": [
                                        {"public_token": "pub-x"}]}}]}))
        if path == "/item/public_token/exchange":
            state["exchanges"] = state.get("exchanges", 0) + 1
            if state["exchanges"] > 1 and state.get("spent"):
                return httpx.Response(400, text=json.dumps(
                    {"error_type": "INVALID_INPUT",
                     "error_code": "INVALID_PUBLIC_TOKEN",
                     "error_message": "public token already exchanged"}))
            return httpx.Response(200, text=json.dumps(
                {"item_id": state["item_id"],
                 "access_token": "access-sandbox-x"}))
        if path == "/item/get":
            return httpx.Response(200, text=json.dumps(
                {"item": {"institution_id": "ins_9", "error": None}}))
        if path == "/institutions/get_by_id":
            return httpx.Response(200, text=json.dumps(
                {"institution": {"name": "Example Bank"}}))
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps({"accounts": [
                {"account_id": f"{state['item_id']}-chk",
                 "name": "Checking", "type": "depository",
                 "subtype": "checking",
                 "balances": {"current": 10.0,
                              "iso_currency_code": "USD"}}]}))
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(
                {"added": [], "modified": [], "removed": [],
                 "has_more": False, "next_cursor": "c-1"}))
        if path == "/item/remove":
            state["removes"] = state.get("removes", 0) + 1
            return httpx.Response(200, text=json.dumps({"request_id": "r"}))
        if path == "/liabilities/get":
            return httpx.Response(200, text=json.dumps(
                {"liabilities": {"credit": [], "mortgage": [],
                                 "student": []}}))
        if path == "/investments/holdings/get":
            return httpx.Response(200, text=json.dumps(
                {"accounts": [], "securities": [], "holdings": []}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


def _patch(state):
    def for_tenant(conn, transport=None):
        return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                            transport=_backend(state))
    return mock.patch.object(plaid.Client, "for_tenant",
                             staticmethod(for_tenant))


class TransportErrorsArePlaidErrorsTests(unittest.TestCase):
    def _client(self, exc):
        def handler(request):
            raise exc("connect to 203.0.113.7:443 failed", request=request)
        return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                            transport=httpx.MockTransport(handler))

    def test_a_read_timeout_is_a_plaid_error_whose_outcome_is_unknown(self):
        with self.assertRaises(plaid.PlaidError) as cm:
            self._client(httpx.ReadTimeout).post("/item/get", {})
        e = cm.exception
        self.assertIsInstance(e, plaid.PlaidTransportError)
        self.assertEqual(e.code, plaid.TRANSPORT_CODE)
        self.assertTrue(e.sent)
        self.assertNotIn("203.0.113.7", str(e))
        self.assertIn("ReadTimeout", str(e))

    def test_a_refused_connection_never_reached_plaid(self):
        with self.assertRaises(plaid.PlaidTransportError) as cm:
            self._client(httpx.ConnectError).post("/item/get", {})
        self.assertFalse(cm.exception.sent)

    def test_no_answer_reads_as_retrying_not_as_a_broken_connection(self):
        self.assertEqual(
            plaid.status_kind(f"error:{plaid.TRANSPORT_CODE}"), "retrying")


class LinkSurvivesLostAnswersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"plt-{uuid.uuid4().hex[:8]}@example.dev",
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
        self.state = {"link_token": f"lt-{uuid.uuid4().hex[:10]}",
                      "item_id": f"pl-{uuid.uuid4().hex[:10]}",
                      "spent": True}

    def _start_and_finish(self):
        lt = self.state["link_token"]
        with _patch(self.state):
            r = self.client.post("/accounts/plaid/link", data={},
                                 headers={"Accept": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
            self.state["link_done"] = True
            return self.client.get(f"/accounts/plaid/link/{lt}/status")

    def _poll(self):
        with _patch(self.state):
            return self.client.get(
                f"/accounts/plaid/link/{self.state['link_token']}/status")

    def _item(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT institution_name, status FROM items WHERE id=%s",
                (self.state["item_id"],)).fetchone()
        finally:
            conn.close()

    def test_a_first_sync_timeout_still_reports_the_bank_linked(self):
        self.state["fail"] = {"/transactions/sync": httpx.ReadTimeout}
        r = self._start_and_finish()
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add")
        self.assertEqual(s["first_sync"], "pending")
        # the next poll answers the same, without a second exchange
        s2 = self._poll().json()
        self.assertEqual(s2["kind"], "add")
        self.assertEqual(self.state["exchanges"], 1)
        self.assertIsNotNone(self._item())
        self.assertEqual(self.state.get("removes", 0), 0)

    def test_a_lookup_timeout_before_the_insert_keeps_the_item(self):
        self.state["fail"] = {"/item/get": httpx.ConnectTimeout}
        s = self._start_and_finish().json()
        self.assertEqual(s["kind"], "add")
        it = self._item()
        self.assertIsNotNone(it, "the exchanged Item must have a row")
        self.assertEqual(it["institution_name"], "Plaid")
        self.assertEqual(self.state.get("removes", 0), 0)
        self.assertEqual(self.state["exchanges"], 1)

    def test_a_timeout_on_the_exchange_keeps_the_session_open(self):
        self.state["fail"] = {"/item/public_token/exchange":
                              httpx.ReadTimeout}
        self.state["fail_once"] = ("/item/public_token/exchange",)
        self.state["spent"] = False      # the lost request never landed
        s = self._start_and_finish().json()
        self.assertFalse(s["done"])
        s = self._poll().json()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add")
        self.assertIsNotNone(self._item())

    def test_an_item_row_that_will_not_save_is_handed_back(self):
        with mock.patch.object(plaid, "upsert_item",
                               side_effect=RuntimeError("db went away")), \
                self.assertLogs("oikonome", level="ERROR"):
            r = self._start_and_finish()
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add_failed")
        self.assertEqual(self.state.get("removes"), 1)
        self.assertIsNone(self._item())


if __name__ == "__main__":
    unittest.main()
