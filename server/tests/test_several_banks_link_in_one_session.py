"""A person can connect several banks in one Plaid Link session.

The link token for a new link carries Plaid's multi-institution flag and
the household's Plaid user (one per household and client_id, made once).
Each bank Plaid reports is exchanged exactly once whichever door looks —
the poll, the LINK webhook, the sweep — and the session finishes only when
Plaid says the person left Link. A bank the household may not keep (over
the institution cap, a repeat of a login in the same session) is handed
back while the others stay, and the answer names it. A re-auth and a
relink for more history stay one bank; with the deployment switch off the
request is the one every earlier link sent.
"""

import json
import logging
import os
import threading
import time
import unittest
import uuid
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.jobs import plaid_links
from oikonome.sync import base as sync_base
from oikonome.sync import plaid, plaid_user
from oikonome.web import pages

from .util import _ensure_db, seed_accounts, write_config

FINISHED = "2026-03-04T05:06:07Z"
# the test household's keys as its Plaid user is kept under (sandbox)
CID = plaid_user.key("cid", plaid.ENV_URLS["sandbox"])


def _bank(name: str, tag: str | None = None, inst: str | None = None,
          masks=None) -> dict:
    tag = tag or uuid.uuid4().hex[:8]
    return {"public_token": f"pub-{tag}", "item_id": f"pl-{tag}",
            "inst_id": inst or f"ins_{tag}", "name": name,
            "masks": masks or [str(1000 + int(tag[:3], 16) % 9000)]}


def _backend(state: dict):
    """Plaid as the multi-institution flow answers it. `state["banks"]`
    are the banks the person will add, `state["added"]` how many Link has
    reported so far, `state["finished"]` whether the person left Link."""
    by_token = {}

    def handler(request):
        path = request.url.path
        body = json.loads(request.content.decode() or "{}")
        state.setdefault("clients", {}).setdefault(path, []).append(
            body.get("client_id"))
        for b in state.get("banks", []):
            by_token[f"access-{b['item_id']}"] = b
        if path == "/user/create":
            state["user_creates"] = state.get("user_creates", 0) + 1
            if state.get("user_create_delay"):
                time.sleep(state["user_create_delay"])
            state.setdefault("user_cuids", []).append(body["client_user_id"])
            if state.get("user_create_fails") or (
                    state.get("dup_plain")
                    and ":" not in body["client_user_id"]):
                return httpx.Response(400, text=json.dumps(
                    {"error_type": "INVALID_REQUEST",
                     "error_code": "INVALID_FIELD",
                     "error_message": "user already exists"}))
            return httpx.Response(201, text=json.dumps(
                {"user_id": f"usr-{state['user_creates']}-"
                            f"{body['client_id']}",
                 "request_id": "req-u"}))
        if path == "/link/token/create":
            state.setdefault("creates", []).append(request.content)
            if body.get("enable_multi_item_link") \
                    and state.get("refuse_multi"):
                return httpx.Response(400, text=json.dumps(
                    {"error_type": "INVALID_REQUEST",
                     "error_code": "INVALID_FIELD",
                     "error_message": "multi item link requires a user "
                                      "token or user id"}))
            answer = {"link_token": state["link_token"],
                      "hosted_link_url": "https://hosted.plaid.com/m"}
            if state.get("expiration"):
                answer["expiration"] = state["expiration"]
            return httpx.Response(200, text=json.dumps(answer))
        if path == "/link/token/get":
            if body["link_token"] != state["link_token"]:
                return httpx.Response(200, text=json.dumps(
                    {"link_sessions": []}))
            shown = state.get("banks", [])[:state.get("added", 0)]
            sess = {"link_session_id": "ls-1", "results": {
                "item_add_results": [
                    {"public_token": b["public_token"],
                     "accounts": [{"mask": m} for m in b["masks"]],
                     "institution": {"institution_id": b["inst_id"],
                                     "name": b["name"]}}
                    for b in shown]}}
            if state.get("finished"):
                sess["finished_at"] = FINISHED
            if state.get("events"):
                sess["events"] = state["events"]
            return httpx.Response(200, text=json.dumps(
                {"link_sessions": [sess]}))
        if path == "/item/public_token/exchange":
            tok = body["public_token"]
            ex = state.setdefault("exchanges", {})
            ex[tok] = ex.get(tok, 0) + 1
            if ex[tok] > 1:
                return httpx.Response(400, text=json.dumps(
                    {"error_type": "INVALID_INPUT",
                     "error_code": "INVALID_PUBLIC_TOKEN",
                     "error_message": "public token already exchanged"}))
            b = next(b for b in state["banks"] if b["public_token"] == tok)
            return httpx.Response(200, text=json.dumps(
                {"item_id": b["item_id"],
                 "access_token": f"access-{b['item_id']}"}))
        if path == "/item/get":
            b = by_token.get(body.get("access_token"))
            return httpx.Response(200, text=json.dumps(
                {"item": {"institution_id": b["inst_id"] if b else "ins_x",
                          "error": None}}))
        if path == "/institutions/get_by_id":
            b = next((b for b in state.get("banks", [])
                      if b["inst_id"] == body["institution_id"]), None)
            return httpx.Response(200, text=json.dumps(
                {"institution": {"name": b["name"] if b else "Bank",
                                 "products": ["transactions"]}}))
        if path == "/accounts/get":
            b = by_token.get(body.get("access_token"))
            accts = [{"account_id": f"{b['item_id']}-{m}", "mask": m,
                      "name": f"Checking {m}", "type": "depository",
                      "subtype": "checking",
                      "balances": {"current": 100.0,
                                   "iso_currency_code": "USD"}}
                     for m in (b["masks"] if b else [])]
            return httpx.Response(200, text=json.dumps({"accounts": accts}))
        if path == "/item/remove":
            state.setdefault("removed", []).append(
                body.get("access_token"))
            return httpx.Response(200, text=json.dumps({"removed": True}))
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


def _patch(state, cid="cid"):
    def for_tenant(conn, transport=None):
        return plaid.Client(cid, "sec", plaid.ENV_URLS["sandbox"],
                            transport=_backend(state))
    return mock.patch.object(plaid.Client, "for_tenant",
                             staticmethod(for_tenant))


def _set(link_token: str, assignment: str) -> None:
    admin = tenancy.admin_connect()
    try:
        admin.execute(f"UPDATE plaid_link_sessions SET {assignment} "
                      "WHERE link_token=%s", (link_token,))
    finally:
        admin.close()


def _ledger(item_id: str):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(
            "SELECT removed_reason FROM plaid_item_ledger WHERE item_id=%s",
            (item_id,)).fetchone()
    finally:
        admin.close()


def _row(link_token: str):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(
            "SELECT done_at, reply, exchanged, multi_item "
            "FROM plaid_link_sessions WHERE link_token=%s",
            (link_token,)).fetchone()
    finally:
        admin.close()


class SeveralBanksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"plm-{uuid.uuid4().hex[:8]}@example.dev",
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
        plaid_links.forget_cached_open()
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM plaid_link_sessions WHERE tenant=%s",
                          (str(self.tid),))
        finally:
            admin.close()
        self.lt = f"lt-{uuid.uuid4().hex[:10]}"
        self.state = {"link_token": self.lt}
        self.queued: list = []
        # a fresh day for the household: no pause, nothing handed back
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("UPDATE plaid_users SET multi_item_off_until=NULL, "
                         "refusals=0, cooldowns=0")
        finally:
            conn.close()
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM plaid_item_ledger WHERE tenant=%s",
                          (str(self.tid),))
        finally:
            admin.close()
        from oikonome.jobs import worker
        q = mock.patch.object(worker, "queue_first_sync",
                              lambda t, i: self.queued.append(i))
        q.start()
        self.addCleanup(q.stop)

    # -- helpers ------------------------------------------------------

    def _start(self, data=None):
        with _patch(self.state):
            r = self.client.post("/accounts/plaid/link", data=data or {},
                                 headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _poll(self, v=2):
        q = f"?v={v}" if v else ""
        with _patch(self.state):
            r = self.client.get(f"/accounts/plaid/link/{self.lt}/status{q}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _finish(self):
        with _patch(self.state):
            return plaid_links.finish_session(self.tid, self.lt)

    def _deliver(self, code, **extra):
        from oikonome.web import plaid_webhook

        async def ok(header, body):
            return True

        async def run_now(tenant_id, link_token, tokens=None):
            with _patch(self.state):
                plaid_links.finish_session(tenant_id, link_token,
                                           tokens=tokens)
            return True
        body = json.dumps({"webhook_type": "LINK", "webhook_code": code,
                           "link_token": self.lt, **extra}).encode()
        with mock.patch.object(plaid_webhook, "verify", ok), \
                mock.patch.object(plaid_webhook, "_enqueue_link_finish",
                                  run_now):
            r = self.client.post(
                "/api/plaid/webhook", content=body,
                headers={"plaid-verification": "x",
                         "content-type": "application/json"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _fresh_household(self) -> str:
        admin = tenancy.admin_connect()
        try:
            return tenancy.create_tenant(admin, "erased probe")
        finally:
            admin.close()

    def _create_body(self, n=-1) -> dict:
        return json.loads(self.state["creates"][n].decode())

    def _items(self, banks) -> set:
        conn = tenancy.tenant_connect(self.tid)
        try:
            rows = conn.execute(
                "SELECT id FROM items WHERE id = ANY(%s) "
                "AND COALESCE(status,'') <> 'archived' "
                "AND COALESCE(access_token,'') <> ''",
                ([b["item_id"] for b in banks],)).fetchall()
        finally:
            conn.close()
        return {r["id"] for r in rows}

    def _users(self, tid=None) -> list:
        conn = tenancy.tenant_connect(tid or self.tid)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT client_id, user_id FROM plaid_users "
                "ORDER BY client_id").fetchall()]
        finally:
            conn.close()

    # -- the link token -------------------------------------------------

    def test_a_new_link_asks_for_several_banks_with_one_plaid_user(self):
        self._start()
        body = self._create_body()
        self.assertIs(body["enable_multi_item_link"], True)
        users = [u for u in self._users() if u["client_id"] == CID]
        self.assertEqual(len(users), 1)
        self.assertEqual(body["user_id"], users[0]["user_id"])
        self.assertEqual(body["user"], {"client_user_id": str(self.tid)})
        self.assertIn("hosted_link", body)
        self.assertTrue(_row(self.lt)["multi_item"])
        made = self.state.get("user_creates", 0)
        # a second link reuses the user
        self.lt = self.state["link_token"] = f"lt-{uuid.uuid4().hex[:10]}"
        self._start()
        self.assertEqual(self.state.get("user_creates", 0), made)
        self.assertEqual(self._create_body()["user_id"], users[0]["user_id"])

    def test_one_plaid_user_per_client_id(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("DELETE FROM plaid_users")
            own = plaid.Client("cid-own", "s", plaid.ENV_URLS["sandbox"],
                               transport=_backend(self.state))
            platform = plaid.Client("cid-platform", "s",
                                    plaid.ENV_URLS["sandbox"],
                                    transport=_backend(self.state))
            a = plaid_user.ensure(conn, own)
            b = plaid_user.ensure(conn, platform)
            self.assertTrue(a and b)
            self.assertNotEqual(a, b)
            self.assertIn("cid-own", a[0])
            self.assertIn("cid-platform", b[0])
            self.assertEqual(a[1], str(self.tid))
            self.assertEqual(plaid_user.ensure(conn, own), a)
            self.assertEqual(plaid_user.ensure(conn, platform), b)
            self.assertEqual(self.state["user_creates"], 2)
        finally:
            conn.close()

    def test_two_link_starts_at_once_make_one_plaid_user(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("DELETE FROM plaid_users")
        finally:
            conn.close()
        self.state["user_create_delay"] = 0.4
        got, errs = [], []

        def start():
            c = tenancy.tenant_connect(self.tid)
            try:
                cl = plaid.Client("cid", "s", plaid.ENV_URLS["sandbox"],
                                  transport=_backend(self.state))
                got.append(plaid_user.ensure(c, cl))
            except Exception as e:                       # noqa: BLE001
                errs.append(e)
            finally:
                c.close()
        ts = [threading.Thread(target=start) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(self.state["user_creates"], 1)
        self.assertEqual(len(set(got)), 1)
        self.assertTrue(got[0])
        self.assertEqual(len(self._users()), 1)

    def test_a_plaid_refusal_of_the_flag_falls_back_to_one_bank(self):
        self.state["refuse_multi"] = True
        with self.assertLogs("oikonome.sync.plaid", logging.WARNING) as logs:
            self._start()
        self.assertEqual(len(self.state["creates"]), 2)
        self.assertIn("enable_multi_item_link", self._create_body(0))
        retry = self._create_body(1)
        self.assertNotIn("enable_multi_item_link", retry)
        self.assertNotIn("user_id", retry)
        self.assertFalse(_row(self.lt)["multi_item"])
        text = "\n".join(logs.output)
        self.assertIn("INVALID_FIELD", text)
        self.assertNotIn("usr-", text)        # the user id is never logged

    def _clear_users(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("DELETE FROM plaid_users")
        finally:
            conn.close()

    def _again(self):
        self.lt = self.state["link_token"] = f"lt-{uuid.uuid4().hex[:10]}"
        pages._PLAID_LINK.clear()
        self._start()

    def _flagged(self) -> list:
        return [json.loads(b.decode()) for b in self.state["creates"]
                if b"enable_multi_item_link" in b]

    def test_a_user_plaid_keeps_refusing_pauses_and_is_kept(self):
        """Plaid refusing every flagged token: two refusals pause several
        banks per sign-in (persisted, 24 h), no new user is minted, and
        after the pause the SAME user is tried once more."""
        self._start()                       # a user is on record
        uid = [u for u in self._users() if u["client_id"] == CID][0][
            "user_id"]
        made = self.state.get("user_creates", 0)
        self.state["refuse_multi"] = True
        self.state["creates"] = []
        for _ in range(10):
            self._again()
        self.assertEqual(self.state.get("user_creates", 0), made)
        self.assertEqual(len(self._flagged()), 2)
        self.assertNotIn("enable_multi_item_link", self._create_body())
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertTrue(plaid_user.paused(conn, CID))
            # the pause runs out
            conn.execute("UPDATE plaid_users SET multi_item_off_until = "
                         "now() - interval '1 minute'")
        finally:
            conn.close()
        self.state["refuse_multi"] = False
        self._again()
        self.assertEqual(self._create_body()["user_id"], uid)
        self.assertEqual(self.state.get("user_creates", 0), made)

    def test_a_second_pause_is_twice_as_long(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("DELETE FROM plaid_users")
            plaid_user.pause(conn, CID, "first")
            conn.execute("UPDATE plaid_users SET multi_item_off_until=NULL")
            plaid_user.pause(conn, CID, "second")
            left = conn.execute(
                "SELECT extract(epoch FROM multi_item_off_until - now()) "
                "AS s FROM plaid_users").fetchone()["s"]
        finally:
            conn.close()
        self.assertAlmostEqual(float(left), 2 * plaid_user.COOLDOWN_S,
                               delta=120)

    def test_a_refused_plain_user_is_made_under_a_suffix(self):
        """A /user/create Plaid refuses for the household's own id (one made
        earlier whose answer was lost) is asked once more under a suffixed
        client_user_id, which every link token then carries."""
        self._clear_users()
        self.state["dup_plain"] = True
        self._start()
        body = self._create_body()
        self.assertIs(body["enable_multi_item_link"], True)
        cuid = body["user"]["client_user_id"]
        self.assertTrue(cuid.startswith(f"{self.tid}:"), cuid)
        self.assertEqual(self.state["user_cuids"], [str(self.tid), cuid])

    def test_a_refused_user_create_pauses_without_asking_again(self):
        self._clear_users()
        self.state["user_create_fails"] = True
        self._start()
        asked = self.state["user_creates"]
        for _ in range(3):
            self._again()
        self.assertEqual(self.state["user_creates"], asked)
        self.assertNotIn("enable_multi_item_link", self._create_body())

    def _offer(self, **kw):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return pages.link_offer({"tenant_id": self.tid}, conn, **kw)
        finally:
            conn.close()

    def _cap(self, room):
        conn = tenancy.tenant_connect(self.tid)
        try:
            held = sync_base.institutions_held(conn)
        finally:
            conn.close()
        return mock.patch.object(sync_base, "institution_cap",
                                 lambda conn=None: held + room)

    def test_the_line_under_the_button_is_what_the_start_does(self):
        several = self._offer()
        self.assertTrue(several["multi"])
        self.assertIn("several banks in one go", several["line"])
        with self._cap(1):
            o = self._offer()
            self.assertEqual((o["multi"], o["line"]),
                             (False, "You can connect 1 more bank."))
        with self._cap(3):
            self.assertEqual(
                self._offer()["line"],
                "You can connect 3 more banks — several in one go: after "
                "the first, choose to add another before you finish.")
            # an open session takes one of the three
            self._start()
            self.assertIn("2 more banks", self._offer()["line"])
        pages._PLAID_LINK.clear()
        self.setUp()
        with mock.patch.dict(os.environ, {"OIKONOME_PLAID_MULTI_ITEM": "0"}):
            self.assertEqual(self._offer(), {"multi": False, "refuse": None,
                                             "line": None})
        conn = tenancy.tenant_connect(self.tid)
        try:
            plaid_user.pause(conn, CID, "a test pause")
        finally:
            conn.close()
        self.assertFalse(self._offer()["multi"])
        self.state["creates"] = []
        self._again()
        self.assertNotIn("enable_multi_item_link", self._create_body())
        self.setUp()
        me = self.client.get("/api/me").json()
        self.assertEqual(me["features"].get("bank_link_line"),
                         several["line"])

    def test_a_refused_start_is_what_the_line_says(self):
        from oikonome.notify import outbound_pause
        with mock.patch.object(outbound_pause, "links_held",
                               lambda cfg, which: True), \
                mock.patch.object(outbound_pause, "hold_message",
                                  lambda cfg, which: "links are on hold"):
            o = self._offer()
        self.assertEqual((o["multi"], o["refuse"], o["line"]),
                         (False, "links are on hold", "links are on hold"))

    # -- the day's ceiling and a session's overreach ---------------------

    def _ledger_rows(self, n, reason="link refused at the cap"):
        admin = tenancy.admin_connect()
        try:
            for _ in range(n):
                admin.execute(
                    "INSERT INTO plaid_item_ledger (item_id, environment, "
                    "credential_source, tenant, removed_reason, removed_at)"
                    " VALUES (%s,'sandbox','byo',%s,%s,now())",
                    (f"pl-hb-{uuid.uuid4().hex[:8]}", str(self.tid),
                     reason))
        finally:
            admin.close()

    def test_six_handed_back_in_a_day_refuse_new_links(self):
        self._ledger_rows(plaid.HANDED_BACK_PER_DAY - 1)
        self._start()                       # still allowed
        self._ledger_rows(1, "overshoot")
        self.lt = self.state["link_token"] = f"lt-{uuid.uuid4().hex[:10]}"
        with _patch(self.state):
            r = self.client.post("/accounts/plaid/link", data={},
                                 headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn(plaid.TOO_MANY_HANDED_BACK, r.json()["detail"])
        self.assertEqual(self._offer()["line"], plaid.TOO_MANY_HANDED_BACK)

    def test_a_session_far_past_the_cap_pauses_several_banks(self):
        banks = [_bank(f"Bank {c}") for c in "PQRST"]
        self.state["banks"] = banks
        with self._cap(2):
            self._start()
            self.state["added"], self.state["finished"] = 5, True
            self.assertTrue(self._poll()["done"])
        self.assertEqual(len(self.state["removed"]), 3)
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertTrue(plaid_user.paused(conn, CID))
            rows = conn.execute(
                "SELECT summary FROM activity_log WHERE kind='account' "
                "AND action='several_banks_paused'").fetchall()
        finally:
            conn.close()
        self.assertTrue(rows)
        # one bank per sign-in still works
        self.state["creates"] = []
        self._again()
        self.assertNotIn("enable_multi_item_link", self._create_body())

    def test_no_plaid_user_links_one_bank(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("DELETE FROM plaid_users")
        finally:
            conn.close()
        self.state["user_create_fails"] = True
        self._start()
        self.assertEqual(len(self.state["creates"]), 1)
        self.assertNotIn("enable_multi_item_link", self._create_body())
        self.assertEqual([u for u in self._users() if u["user_id"]], [])
        self.assertFalse(_row(self.lt)["multi_item"])

    def test_the_switch_off_sends_the_earlier_request_byte_for_byte(self):
        def body_of(multi, env):
            st = {"link_token": "lt-x"}
            conn = tenancy.tenant_connect(self.tid)
            try:
                cl = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                                  transport=_backend(st))
                with mock.patch.dict(os.environ,
                                     {"OIKONOME_PLAID_MULTI_ITEM": env}):
                    os.environ.pop("OIKONOME_PLAID_WEBHOOK_URL", None)
                    out = cl.create_hosted_link(
                        str(self.tid), client_name="Oikonome", conn=conn,
                        multi_item=multi)
            finally:
                conn.close()
            self.assertFalse(out["multi_item"])
            self.assertEqual(st.get("user_creates", 0), 0)
            return st["creates"][-1]
        off = body_of(True, "0")
        self.assertEqual(off, body_of(False, "1"))
        self.assertEqual(off, body_of(True, "off"))
        # the keys the base request carried, in its order
        sent = json.loads(off.decode())
        self.assertEqual(list(sent)[:9], [
            "client_id", "secret", "client_name", "language",
            "country_codes", "user", "hosted_link", "products",
            "transactions"])
        self.assertEqual(set(list(sent)[9:]) - {
            "required_if_supported_products"}, set())
        self.assertEqual(sent["user"], {"client_user_id": str(self.tid)})

    def test_a_reauth_never_asks_for_several_banks(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            cl = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                              transport=_backend(self.state))
            out = cl.create_hosted_link(str(self.tid), client_name="Oikonome",
                                        access_token="access-old",
                                        conn=conn, multi_item=True)
        finally:
            conn.close()
        body = self._create_body()
        self.assertNotIn("enable_multi_item_link", body)
        self.assertNotIn("user_id", body)
        self.assertEqual(body["access_token"], "access-old")
        self.assertNotIn("multi_item", out)

    def test_a_relink_for_more_history_stays_one_bank(self):
        iid = f"pl-relink-{uuid.uuid4().hex[:6]}"
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, iid, "plaid", "Harbor Bank",
                                  "access-relink", raw={"days_requested": 31})
        finally:
            conn.close()
        self._start({"relink_item_id": iid})
        body = self._create_body()
        self.assertNotIn("enable_multi_item_link", body)
        self.assertNotIn("user_id", body)
        self.assertFalse(_row(self.lt)["multi_item"])

    def test_no_room_for_two_more_asks_for_one_bank(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            held = sync_base.institutions_held(conn)
        finally:
            conn.close()
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: held + 1):
            self._start()
        self.assertNotIn("enable_multi_item_link", self._create_body())
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: held + 2):
            # the first session given up, so it holds no place
            self.setUp()
            self.state["creates"] = []
            self._start()
        self.assertIs(self._create_body()["enable_multi_item_link"], True)

    # -- finishing a session --------------------------------------------

    def test_two_banks_link_as_they_arrive_and_finish_once(self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        self.state["banks"] = [a, b]
        self._start()
        # the first bank arrives while the person is still in Link
        self.state["added"] = 1
        s = self._poll()
        self.assertFalse(s["done"])
        self.assertEqual(s["kind"], "busy")
        self.assertEqual([r["institution"] for r in s["results"]],
                         ["Harbor Bank"])
        self.assertEqual(self._items([a]), {a["item_id"]})
        self.assertIsNone(_row(self.lt)["done_at"])
        # the ITEM_ADD_RESULT webhook's finisher finds nothing new to do
        self.assertEqual(self._finish(), "busy")
        self.state["added"] = 2
        self.assertEqual(self._finish(), "busy")
        self.assertEqual(self._items([a, b]), {a["item_id"], b["item_id"]})
        self.assertIsNone(_row(self.lt)["done_at"])
        # the person presses finish: SESSION_FINISHED, then the poll
        self.state["finished"] = True
        self.assertEqual(self._finish(), "done")
        s = self._poll()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add")
        self.assertEqual(s["institution"],
                         "Harbor Bank, Northwind Credit Union")
        self.assertEqual(
            s["summary"], "Connected Harbor Bank and Northwind Credit Union"
            " — first sync running — accounts appear in a minute.")
        self.assertEqual([r["connected"] for r in s["results"]],
                         [True, True])
        # a retry of every door changes nothing
        self.assertEqual(self._finish(), "done")
        self.assertEqual(self._poll(), s)
        self.assertEqual(self.state["exchanges"],
                         {a["public_token"]: 1, b["public_token"]: 1})
        self.assertEqual(sorted(self.queued),
                         sorted([a["item_id"], b["item_id"]]))
        self.assertNotIn("pub-", json.dumps(_row(self.lt)["exchanged"]))

    def test_the_webhooks_finish_a_session_nobody_polls(self):
        from oikonome.web import plaid_webhook
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        self.state["banks"] = [a, b]
        self._start()
        pages._PLAID_LINK.clear()

        async def ok(header, body):
            return True

        async def run_now(tenant_id, link_token, tokens=None):
            with _patch(self.state):
                plaid_links.finish_session(tenant_id, link_token,
                                           tokens=tokens)
            return True

        def deliver(code, **extra):
            body = json.dumps({"webhook_type": "LINK", "webhook_code": code,
                               "link_token": self.lt, **extra}).encode()
            with mock.patch.object(plaid_webhook, "verify", ok), \
                    mock.patch.object(plaid_webhook, "_enqueue_link_finish",
                                      run_now):
                r = self.client.post(
                    "/api/plaid/webhook", content=body,
                    headers={"plaid-verification": "x",
                             "content-type": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
        self.state["added"] = 1
        deliver("ITEM_ADD_RESULT", public_token=a["public_token"])
        self.state["added"] = 2
        deliver("ITEM_ADD_RESULT", public_token=b["public_token"])
        self.assertIsNone(_row(self.lt)["done_at"])
        self.state["finished"] = True
        deliver("SESSION_FINISHED", status="success",
                public_tokens=[a["public_token"], b["public_token"]])
        row = _row(self.lt)
        self.assertIsNotNone(row["done_at"])
        self.assertEqual(row["reply"]["kind"], "add")
        self.assertEqual(self._items([a, b]), {a["item_id"], b["item_id"]})
        self.assertEqual(self.state["exchanges"],
                         {a["public_token"]: 1, b["public_token"]: 1})

    def test_one_of_three_over_the_cap_is_handed_back_and_named(self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        c = _bank("Juniper Savings")
        self.state["banks"] = [a, b, c]
        conn = tenancy.tenant_connect(self.tid)
        try:
            held = sync_base.institutions_held(conn)
        finally:
            conn.close()
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: held + 2):
            self._start()
            self.state["added"], self.state["finished"] = 3, True
            s = self._poll()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add")
        self.assertEqual(self._items([a, b, c]),
                         {a["item_id"], b["item_id"]})
        self.assertEqual(self.state["removed"], [f"access-{c['item_id']}"])
        self.assertEqual([r["connected"] for r in s["results"]],
                         [True, True, False])
        self.assertIn("Connected Harbor Bank and Northwind Credit Union",
                      s["summary"])
        self.assertIn("Juniper Savings was not connected: ", s["summary"])
        # what an older client reads: the banks that connected
        self.assertEqual(s["institution"],
                         "Harbor Bank, Northwind Credit Union")

    def test_the_same_bank_twice_in_one_session_keeps_one(self):
        a = _bank("Harbor Bank")
        again = _bank("Harbor Bank", inst=a["inst_id"], masks=a["masks"])
        self.state["banks"] = [a, again]
        self._start()
        self.state["added"], self.state["finished"] = 2, True
        s = self._poll()
        self.assertTrue(s["done"])
        self.assertEqual(self._items([a, again]), {a["item_id"]})
        self.assertEqual(self.state["removed"],
                         [f"access-{again['item_id']}"])
        self.assertEqual([r["connected"] for r in s["results"]],
                         [True, False])
        self.assertIn("Harbor Bank was not connected: ", s["summary"])

    def test_leaving_link_after_two_banks_keeps_both(self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        self.state["banks"] = [a, b]
        self._start()
        self.state["added"] = 2
        self.assertFalse(self._poll()["done"])
        # the person closes Link from inside it: Plaid stamps the session
        # finished ("exited") with both banks in its results
        self.state["finished"] = True
        s = self._poll()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add")
        self.assertEqual(self._items([a, b]), {a["item_id"], b["item_id"]})

    # -- the window closed without leaving Link ---------------------------

    def _quiet(self, seconds=None):
        """The session's last new bank, `seconds` ago (default: past the
        quiet time), in memory and on record."""
        seconds = plaid_links.QUIET_S + 5 if seconds is None else seconds
        for v in pages._PLAID_LINK.values():
            v["last_result_at"] = time.time() - seconds
        _set(self.lt, f"last_result_at = now() - make_interval(secs => "
                      f"{int(seconds)})")

    def test_a_quiet_session_answers_with_its_banks(self):
        """A person who closes the window without leaving Link: once the
        session has brought nothing new for QUIET_S the poll answers done
        with the banks so far, so no client spins on busy to a timeout."""
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        self._start()
        self.state["added"] = 1
        s = self._poll()
        self.assertFalse(s["done"])
        self.assertEqual(s["progress"], "Connected Harbor Bank — add another "
                                        "in the bank window, or you're done.")
        self._quiet(plaid_links.QUIET_S - 30)
        self.assertFalse(self._poll()["done"])
        self._quiet()
        s = self._poll()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add")
        self.assertEqual(s["institution"], "Harbor Bank")
        self.assertTrue(s.get("quiet"))

    def test_an_older_client_gets_the_quiet_answer_later(self):
        """A client that does not say it keeps its window open (no v=2)
        closes it on the answer, so the quiet clock is longer for it."""
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        self._start()
        self.state["added"] = 1
        self.assertFalse(self._poll(v=0)["done"])
        self._quiet()
        self.assertFalse(self._poll(v=0)["done"])
        self._quiet(plaid_links.OLD_CLIENT_QUIET_S + 5)
        s = self._poll(v=0)
        self.assertTrue(s["done"])
        self.assertTrue(s.get("quiet"))

    def test_a_link_event_keeps_the_session_open(self):
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        self._start()
        self.state["added"] = 1
        self._poll()
        self._quiet()
        # Link reports the person picking the next bank
        import datetime as dt
        self.state["events"] = [{"event_name": "SELECT_INSTITUTION",
                                 "timestamp": dt.datetime.now(
                                     dt.timezone.utc).isoformat()}]
        self.assertFalse(self._poll()["done"])

    # -- a finished session keeps taking banks ----------------------------

    def test_a_result_that_lags_the_finished_stamp_is_still_exchanged(self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        self.state["banks"] = [a, b]
        self._start()
        self.state["added"], self.state["finished"] = 1, True
        s = self._poll()
        self.assertTrue(s["done"])
        # then Plaid reports the second bank
        self.state["added"] = 2
        self._deliver("ITEM_ADD_RESULT", public_token=b["public_token"])
        self.assertEqual(self._items([a, b]), {a["item_id"], b["item_id"]})
        self.assertEqual(self.state["exchanges"],
                         {a["public_token"]: 1, b["public_token"]: 1})
        reply = _row(self.lt)["reply"]
        self.assertIn("Northwind Credit Union", reply["summary"])
        self.assertIn(b["item_id"], self.queued)
        # every later door changes nothing
        self._deliver("SESSION_FINISHED", status="success",
                      public_tokens=[a["public_token"], b["public_token"]])
        self.assertEqual(self._finish(), "done")
        self.assertEqual(self.state["exchanges"],
                         {a["public_token"]: 1, b["public_token"]: 1})

    def test_a_bank_added_after_the_quiet_answer_is_taken_by_the_sweep(self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        self.state["banks"] = [a, b]
        self._start()
        self.state["added"] = 1
        self._poll()
        self._quiet()
        self.assertTrue(self._poll()["done"])
        self.state["added"] = 2
        _set(self.lt, "looked_at = now() - interval '2 minutes'")
        with _patch(self.state):
            out = plaid_links.sweep()
        self.assertEqual(out.get(self.lt), "done")
        self.assertEqual(self._items([a, b]), {a["item_id"], b["item_id"]})
        self.assertEqual(self.state["exchanges"][b["public_token"]], 1)

    def test_a_late_bank_over_the_cap_is_handed_back(self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        self.state["banks"] = [a, b]
        conn = tenancy.tenant_connect(self.tid)
        try:
            held = sync_base.institutions_held(conn)
        finally:
            conn.close()
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: held + 2):
            self._start()
        with mock.patch.object(sync_base, "institution_cap",
                               lambda conn=None: held + 1):
            self.state["added"], self.state["finished"] = 1, True
            self.assertTrue(self._poll()["done"])
            self.state["added"] = 2
            self.assertEqual(self._finish(), "done")
        self.assertEqual(self._items([a, b]), {a["item_id"]})
        self.assertEqual(self.state["exchanges"][b["public_token"]], 1)
        self.assertEqual(self.state["removed"], [f"access-{b['item_id']}"])

    def test_a_token_only_the_webhook_names_is_exchanged(self):
        """SESSION_FINISHED's public_tokens are Plaid's word on what the
        session made: one missing from /link/token/get is settled too."""
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        self.state["banks"] = [a, b]
        self._start()
        self.state["added"], self.state["finished"] = 1, True
        self._deliver("SESSION_FINISHED", status="success",
                      public_tokens=[a["public_token"], b["public_token"]])
        self.assertEqual(self._items([a, b]), {a["item_id"], b["item_id"]})
        self.assertEqual(self.state["exchanges"],
                         {a["public_token"]: 1, b["public_token"]: 1})

    def test_a_result_past_the_window_is_logged_by_session_only(self):
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        self._start()
        self.state["added"], self.state["finished"] = 1, True
        self._poll()
        _set(self.lt, "results_until = now() - interval '1 minute'")
        self.assertIsNone(plaid_links.tenant_for_link(self.lt))
        with self.assertLogs("oikonome.web.plaid_webhook", logging.WARNING) as logs:
            self._deliver("ITEM_ADD_RESULT", public_token="pub-late-9",
                          link_session_id="ls-late-1")
        text = "\n".join(logs.output)
        self.assertIn("ls-late-1", text)
        self.assertNotIn("pub-late-9", text)

    def test_the_window_is_the_tokens_expiration_plus_six_hours(self):
        import datetime as dt
        at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=4)
        self.state["expiration"] = at.isoformat()
        self._start()
        admin = tenancy.admin_connect()
        try:
            left = admin.execute(
                "SELECT extract(epoch FROM results_until - now()) AS s "
                "FROM plaid_link_sessions WHERE link_token=%s",
                (self.lt,)).fetchone()["s"]
        finally:
            admin.close()
        self.assertAlmostEqual(float(left), 10 * 3600, delta=120)
        self.assertAlmostEqual(plaid_links._window_s(None),
                               plaid_links.RESULTS_WINDOW_DEFAULT_S)

    def test_an_erased_household_hands_back_a_late_bank(self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        self.state["banks"] = [a, b]
        tid = self._fresh_household()
        self.state["link_token"] = self.lt
        conn = tenancy.tenant_connect(tid)
        try:
            write_config(conn, plaid_client_id="cid", plaid_secret="sec",
                         plaid_env="sandbox")
        finally:
            conn.close()
        plaid_links.record(tid, self.lt, "add", None, False, None,
                           multi_item=True)
        admin = tenancy.admin_connect()
        try:
            with admin.transaction():
                tenancy.delete_tenant_rows(admin, tid)
        finally:
            admin.close()
        row = _row(self.lt)
        self.assertIsNotNone(row, "the session was not kept as a tombstone")
        self.state["added"] = 1
        with _patch(self.state):
            word = plaid_links.finish_session(tid, self.lt,
                                              tokens=[a["public_token"]])
        self.assertIn(word, ("blocked", "busy"))
        self.assertEqual(self.state["exchanges"][a["public_token"]], 1)
        self.assertEqual(self.state["removed"], [f"access-{a['item_id']}"])
        self.assertEqual(self.queued, [])

    # -- the cap and the repeat limit inside one session -------------------

    def test_an_overshoot_bank_holds_no_place_and_is_bounded(self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        c = _bank("Juniper Savings")
        self.state["banks"] = [a, b, c]
        conn = tenancy.tenant_connect(self.tid)
        try:
            held = sync_base.institutions_held(conn)
        finally:
            conn.close()
        with mock.patch.dict(os.environ,
                             {"OIKONOME_PLAID_CLIENT_ID": "cid",
                              "OIKONOME_PLAID_SECRET": "sec"}), \
                mock.patch.object(sync_base, "institution_cap",
                                  lambda conn=None: held + 2):
            self._start()
            self.state["added"], self.state["finished"] = 3, True
            self.assertTrue(self._poll()["done"])
            conn = tenancy.tenant_connect(self.tid)
            try:
                self.assertEqual(sync_base.institutions_held(conn), held + 2)
            finally:
                conn.close()
        self.assertEqual(_ledger(c["item_id"])["removed_reason"],
                         plaid.OVERSHOOT_LEDGER_REASON)
        # past the day's allowance an overshoot holds its place again
        with mock.patch.object(plaid, "OVERSHOOT_FREE_PER_DAY", 1):
            conn = tenancy.tenant_connect(self.tid)
            try:
                self.assertFalse(plaid._overshoot_free(conn, str(self.tid)))
            finally:
                conn.close()

    def test_a_new_bank_beside_repeats_past_the_limit_is_kept(self):
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        self._start()
        self.state["added"], self.state["finished"] = 1, True
        self._poll()
        admin = tenancy.admin_connect()
        try:
            for _ in range(plaid.REPEAT_LIMIT):
                admin.execute(
                    "INSERT INTO plaid_item_ledger (item_id, environment, "
                    "credential_source, tenant, removed_reason) VALUES "
                    "(%s,'sandbox','byo',%s,'repeat')",
                    (f"pl-rep-{uuid.uuid4().hex[:8]}", str(self.tid)))
        finally:
            admin.close()
        again = _bank("Harbor Bank", inst=a["inst_id"], masks=a["masks"])
        new = _bank("Juniper Savings")
        self.lt = self.state["link_token"] = f"lt-{uuid.uuid4().hex[:10]}"
        # the held bank stays known to the fake (its accounts are asked)
        self.state["banks"] = [again, new, a]
        pages._PLAID_LINK.clear()
        self._start()
        self.state["added"], self.state["finished"] = 2, True
        s = self._poll()
        self.assertTrue(s["done"])
        self.assertEqual(self._items([again, new]), {new["item_id"]})
        # the repeat was exchanged, so it could be handed back
        self.assertEqual(self.state["exchanges"][again["public_token"]], 1)
        self.assertIn(f"access-{again['item_id']}", self.state["removed"])
        self.assertIn("too many repeated bank links were made today, so "
                      "linking it again today will not help", s["summary"])

    # -- odds and ends --------------------------------------------------

    def test_a_lost_outcome_is_found_by_its_token_among_several(self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        self.state["banks"] = [a, b]
        self._start()
        self.state["added"] = 2
        self._poll()
        # the record of both exchanges is lost; the tokens are spent
        _set(self.lt, "exchanged = '{}'::jsonb")
        for v in pages._PLAID_LINK.values():
            v["exchanged"] = {}
        self.state["finished"] = True
        s = self._poll()
        self.assertTrue(s["done"])
        self.assertEqual([r["connected"] for r in s["results"]],
                         [True, True])
        self.assertEqual(sorted(r["institution"] for r in s["results"]),
                         ["Harbor Bank", "Northwind Credit Union"])

    def test_the_summary_groups_banks_and_speaks_plainly(self):
        out = plaid_links.summary([
            {"institution": "Harbor Bank", "connected": False,
             "error": "no room"},
            {"institution": "Harbor Bank", "connected": False,
             "error": "no room"},
            {"institution": "Juniper Savings", "connected": False,
             "error": "no room"}])
        self.assertEqual(out, "Harbor Bank and Juniper Savings were not "
                              "connected: no room.")
        a = _bank("Harbor Bank")
        self.state["banks"] = [a, dict(a, public_token="pub-spent",
                                        item_id=a["item_id"])]
        self.state["exchanges"] = {"pub-spent": 1}
        self._start()
        self.state["added"], self.state["finished"] = 2, True
        s = self._poll()
        self.assertNotIn("INVALID_PUBLIC_TOKEN", s["summary"])
        self.assertIn(plaid_links.NOT_COMPLETED, s["summary"])

    def test_a_several_bank_session_given_up_is_set_aside_not_forgotten(self):
        self._start()
        plaid_links.forget(self.lt, self.tid)
        row = _row(self.lt)
        self.assertIsNotNone(row)
        self.assertTrue(row["reply"]["cancelled"])

    def test_one_bank_in_a_several_bank_session_answers_as_before(self):
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        self._start()
        self.state["added"], self.state["finished"] = 1, True
        s = self._poll()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add")
        self.assertEqual(s["institution"], "Harbor Bank")
        self.assertNotIn("summary", s)
        self.assertNotIn("results", s)

    def test_a_one_bank_session_still_finishes_at_its_bank(self):
        """A session whose token allows one bank (an older build's, or a
        refused flag) finishes the moment its bank arrives, as before."""
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        self.state["refuse_multi"] = True
        self._start()
        self.state["added"] = 1
        s = self._poll()
        self.assertTrue(s["done"])
        self.assertEqual(s["kind"], "add")

    # -- webhook tokens, tombstones, notes, wording, keys -----------------

    def test_webhook_tokens_found_busy_are_queued_again(self):
        import asyncio
        from oikonome.jobs import worker
        from oikonome.sync.base import tenant_sync_lock
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        self._start()
        queued = []

        class Redis:
            async def enqueue_job(self, *args, **kw):
                queued.append((args, kw))
        hold = tenancy.tenant_connect(self.tid)
        try:
            with tenant_sync_lock(hold, self.tid) as held:
                self.assertTrue(held)
                with _patch(self.state):
                    word = asyncio.run(worker.plaid_link_finish(
                        {"redis": Redis()}, str(self.tid), self.lt,
                        [a["public_token"]]))
        finally:
            hold.close()
        self.assertEqual(word, "busy")
        self.assertEqual(len(queued), 1)
        args, kw = queued[0]
        self.assertEqual(args, ("plaid_link_finish", str(self.tid), self.lt,
                                [a["public_token"]], 1))
        self.assertEqual(kw, {"_defer_by": worker.LINK_FINISH_RETRY_S})
        # with the lock free the retry settles the token
        with _patch(self.state):
            asyncio.run(worker.plaid_link_finish(
                {"redis": Redis()}, *args[1:]))
        self.assertEqual(self.state["exchanges"][a["public_token"]], 1)
        # and the last retry gives up without queueing
        queued.clear()
        hold = tenancy.tenant_connect(self.tid)
        try:
            with tenant_sync_lock(hold, self.tid):
                with _patch(self.state):
                    asyncio.run(worker.plaid_link_finish(
                        {"redis": Redis()}, str(self.tid), self.lt,
                        ["pub-other"], worker.LINK_FINISH_RETRIES))
        finally:
            hold.close()
        self.assertEqual(queued, [])

    def test_the_token_carrying_job_keeps_no_result(self):
        from oikonome.jobs import worker
        fn = next(f for f in worker.WorkerSettings.functions
                  if getattr(f, "name", None) == "plaid_link_finish")
        self.assertEqual(fn.keep_result_s, 0)

    def test_an_erased_households_tombstone_keeps_only_the_hand_back(self):
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        tid = self._fresh_household()
        plaid_links.record(tid, self.lt, "add", None, False, "Harbor Bank",
                           days_requested=365, multi_item=True,
                           client_id="cid")
        _set(self.lt, "exchanged = jsonb_build_object('k1', "
                      "jsonb_build_object('item_id', 'pl-gone-1')), "
                      "reply = '{\"done\": true, \"institution\": "
                      "\"Harbor Bank\"}'::jsonb, done_at = now()")
        admin = tenancy.admin_connect()
        try:
            with admin.transaction():
                tenancy.delete_tenant_rows(admin, tid)
            row = admin.execute(
                "SELECT * FROM plaid_link_sessions WHERE link_token=%s",
                (self.lt,)).fetchone()
        finally:
            admin.close()
        self.assertEqual(row["reply"], {"refuse": plaid_links.ERASED})
        self.assertIsNone(row["institution"])
        self.assertIsNone(row["days_requested"])
        self.assertEqual(row["exchanged"], {"k1": {"erased": True}})
        self.assertEqual(row["client_id"], "cid")

    def test_a_late_bank_is_written_where_the_household_reads(self):
        a, b = _bank("Harbor Bank"), _bank("Juniper Savings")
        c = _bank("Northwind Credit Union")
        self.state["banks"] = [a, b, c]
        with self._cap(2):
            self._start()
            self.state["added"], self.state["finished"] = 1, True
            self.assertTrue(self._poll()["done"])
            self.state["added"] = 3
            self.assertEqual(self._finish(), "done")
        conn = tenancy.tenant_connect(self.tid)
        try:
            rows = [r["summary"] for r in conn.execute(
                "SELECT summary FROM activity_log WHERE kind='account' "
                "AND action IN ('late_connected', 'late_refused') "
                "ORDER BY at").fetchall()]
        finally:
            conn.close()
        self.assertIn("connected Juniper Savings, a bank added after the "
                      "sign-in had answered", rows)
        self.assertTrue(any(r.startswith("did not connect Northwind Credit "
                                         "Union: ") for r in rows), rows)

    def test_banks_refused_for_one_reason_are_worded_together(self):
        out = plaid_links.summary([
            {"institution": f"Old {n}", "connected": False,
             "error": plaid.ALREADY_LINKED} for n in range(3)])
        self.assertTrue(out.startswith(
            "Old 0, Old 1 and Old 2 were not connected: these banks are "
            "already connected"), out)
        one = plaid_links.summary([{"institution": "Old 0",
                                    "connected": False,
                                    "error": plaid.ALREADY_LINKED}])
        self.assertIn("this bank is already connected", one)

    def _platform(self, cid):
        def for_platform(transport=None):
            return plaid.Client(cid, "sec-p", plaid.ENV_URLS["sandbox"],
                                transport=_backend(self.state))
        return mock.patch.object(plaid.Client, "for_platform",
                                 staticmethod(for_platform))

    def test_a_late_bank_uses_the_keys_its_session_was_made_under(self):
        """The household's keys changed mid-window: the bank is exchanged
        under the keys that made the link token and, since those are no
        longer the household's, handed back under them — never orphaned."""
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        plaid_links.record(self.tid, self.lt, "add", None, False, None,
                           multi_item=True, client_id="cid-platform")
        self.state["added"], self.state["finished"] = 1, True
        with self._platform("cid-platform"):
            self.assertEqual(self._finish(), "done")
        self.assertEqual(self.state["exchanges"][a["public_token"]], 1)
        self.assertEqual(self.state["removed"], [f"access-{a['item_id']}"])
        self.assertEqual(self.state["clients"]["/item/public_token/exchange"],
                         ["cid-platform"])
        self.assertEqual(self.state["clients"]["/item/remove"],
                         ["cid-platform"])

    def test_a_session_whose_keys_are_gone_says_so_by_session(self):
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        plaid_links.record(self.tid, self.lt, "add", None, False, None,
                           multi_item=True, client_id="cid-gone")
        self.state["added"], self.state["finished"] = 1, True
        with self._platform("cid-platform"), \
                self.assertLogs("oikonome.plaid", logging.WARNING) as logs:
            self.assertEqual(self._finish(), "error")
        self.assertNotIn(self.lt, "\n".join(logs.output))
        self.assertIn("keys its link token was made under are gone",
                      "\n".join(logs.output))
        self.assertNotIn("exchanges", self.state)

    # -- a refused session stays refused through every rewrite ------------

    def _erased_session(self, banks, client_id=None) -> str:
        """A household with a several-bank session open, erased: the row
        left behind is the tombstone."""
        self.state["banks"] = banks
        tid = self._fresh_household()
        plaid_links.record(tid, self.lt, "add", None, False, None,
                           multi_item=True, client_id=client_id)
        admin = tenancy.admin_connect()
        try:
            with admin.transaction():
                tenancy.delete_tenant_rows(admin, tid)
        finally:
            admin.close()
        return tid

    def _finish_for(self, tid, tokens=None):
        with _patch(self.state):
            return plaid_links.finish_session(tid, self.lt, tokens=tokens)

    def _gone_quiet(self):
        _set(self.lt, "last_result_at = now() - make_interval(secs => "
                      f"{plaid_links.OLD_CLIENT_QUIET_S + 60})")

    def test_an_erased_household_hands_back_a_bank_after_its_quiet_answer(
            self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        tid = self._erased_session([a, b])
        self.state["added"] = 1
        self.assertEqual(self._finish_for(tid), "busy")
        self._gone_quiet()
        self.assertEqual(self._finish_for(tid), "blocked")
        row = _row(self.lt)
        self.assertIsNotNone(row["done_at"])
        self.assertEqual(row["reply"].get("refuse"), plaid_links.ERASED)
        # the bank window was still open: a second bank arrives
        self.state["added"] = 2
        self.assertEqual(self._finish_for(tid), "blocked")
        self.assertEqual(self.state["exchanges"][b["public_token"]], 1)
        self.assertEqual(self.state["removed"],
                         [f"access-{a['item_id']}", f"access-{b['item_id']}"])
        self.assertEqual(self.queued, [])

    def test_a_session_whose_tenant_is_gone_is_handed_back_unmarked(self):
        """A row left behind for a household that no longer exists, whatever
        its reply says, hands its banks back — never answers "gone" with
        the bank billing at Plaid."""
        a = _bank("Harbor Bank")
        tid = self._erased_session([a])
        _set(self.lt, "reply = NULL")
        self.state["added"] = 1
        self.assertIn(self._finish_for(tid), ("blocked", "busy"))
        self.assertEqual(self.state["removed"], [f"access-{a['item_id']}"])

    def test_an_erased_households_answer_names_no_bank(self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        c = _bank("Juniper Savings")
        tid = self._erased_session([a, b, c])
        self.state["added"], self.state["finished"] = 2, True
        self.assertEqual(self._finish_for(tid), "blocked")
        self.state["added"] = 3
        self.assertEqual(self._finish_for(tid), "blocked")
        self.assertEqual(len(self.state["removed"]), 3)
        row = _row(self.lt)
        text = json.dumps(row["reply"])
        for name in ("Harbor", "Northwind", "Juniper"):
            self.assertNotIn(name, text)
        self.assertEqual(row["reply"]["refuse"], plaid_links.ERASED)

    def test_a_reset_household_hands_back_every_later_bank(self):
        """A data reset marks the open session: the first bank is handed
        back, the client sets the window aside, the session answers on going
        quiet, and a bank added after all that is handed back too — never
        kept in the household just wiped."""
        from oikonome.db import reset
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        self.state["banks"] = [a, b]
        tid = self._fresh_household()
        conn = tenancy.tenant_connect(tid)
        try:
            write_config(conn, plaid_client_id="cid", plaid_secret="sec",
                         plaid_env="sandbox")
        finally:
            conn.close()
        plaid_links.record(tid, self.lt, "add", None, False, None,
                           multi_item=True, client_id="cid")
        with _patch(self.state):
            reset.reset_tenant_data(tid)
        self.state["added"] = 1
        self.assertEqual(self._finish_for(tid), "busy")
        self.assertTrue(plaid_links.set_aside(self.lt, tid))
        self.assertEqual(_row(self.lt)["reply"].get("refuse"),
                         reset.RESET_REFUSAL)
        self._gone_quiet()
        self.assertEqual(self._finish_for(tid), "blocked")
        self.assertEqual(_row(self.lt)["reply"].get("refuse"),
                         reset.RESET_REFUSAL)
        self.state["added"] = 2
        self.assertEqual(self._finish_for(tid), "blocked")
        self.assertEqual(self.state["removed"],
                         [f"access-{a['item_id']}", f"access-{b['item_id']}"])
        conn = tenancy.tenant_connect(tid)
        try:
            self.assertEqual(conn.execute(
                "SELECT id FROM items").fetchall(), [])
        finally:
            conn.close()

    def test_an_abandoned_refused_session_stays_refused(self):
        a = _bank("Harbor Bank")
        self.state["banks"] = [a]
        tid = self._fresh_household()
        plaid_links.record(tid, self.lt, "add", None, False, None,
                           multi_item=True, client_id="cid")
        admin = tenancy.admin_connect()
        try:
            plaid_links.refuse_open_adds(admin, tid, "a test refusal")
        finally:
            admin.close()
        _set(self.lt, "created_at = now() - interval '3 hours'")
        with _patch(self.state):
            plaid_links.sweep()
        row = _row(self.lt)
        self.assertIsNotNone(row["done_at"])
        self.assertEqual(row["reply"].get("refuse"), "a test refusal")
        self.state["added"] = 1
        self.assertEqual(self._finish_for(tid), "blocked")
        self.assertEqual(self.state["removed"], [f"access-{a['item_id']}"])

    def test_a_late_answer_from_a_stale_copy_keeps_the_refusal(self):
        """A web process holding a copy of the session from before it was
        marked rewrites the answer: the mark on record survives."""
        tid = self._fresh_household()
        plaid_links.record(tid, self.lt, "add", None, False, None,
                           multi_item=True, client_id="cid")
        stale = plaid_links.load(self.lt, tid)
        plaid_links.mark_done(stale, self.lt, {"done": True, "kind": "add"})
        admin = tenancy.admin_connect()
        try:
            plaid_links.refuse_open_adds(admin, tid, "a test refusal")
        finally:
            admin.close()
        plaid_links._rewrite_done(stale, self.lt,
                                  {"done": True, "kind": "add"})
        self.assertEqual(_row(self.lt)["reply"].get("refuse"),
                         "a test refusal")

    def test_a_tombstone_under_the_households_own_keys_stops_looking(self):
        """An erased household's session made under its own Plaid keys: the
        keys went with it, so no bank it brings can be exchanged here. The
        first look closes the row (logged by session) instead of every
        sweep retrying it for the rest of its window."""
        a = _bank("Harbor Bank")
        tid = self._erased_session([a], client_id="cid-own")
        self.state["added"] = 1
        with self.assertLogs("oikonome.plaid", logging.WARNING) as logs:
            self.assertEqual(self._finish_for(tid), "gone")
        self.assertNotIn(self.lt, "\n".join(logs.output))
        self.assertIsNone(plaid_links.tenant_for_link(self.lt))
        self.assertIsNotNone(_row(self.lt)["done_at"])
        with _patch(self.state):
            self.assertNotIn(self.lt, plaid_links.sweep())
        self.assertNotIn("exchanges", self.state)

    # -- the day's ceiling holds inside a session --------------------------

    def test_a_bank_past_the_days_ceiling_is_handed_back_as_it_arrives(self):
        a, b = _bank("Harbor Bank"), _bank("Northwind Credit Union")
        self.state["banks"] = [a, b]
        self._start()
        # the day's ceiling is reached while the window is open
        self._ledger_rows(plaid.HANDED_BACK_PER_DAY)
        self.state["added"] = 1
        s = self._poll()
        self.assertEqual(self._items([a]), set())
        self.assertEqual(self.state["removed"], [f"access-{a['item_id']}"])
        self.assertEqual(s["results"][0]["error"],
                         plaid.TOO_MANY_HANDED_BACK)
        self.state["added"], self.state["finished"] = 2, True
        self.assertTrue(self._poll()["done"])
        self.assertEqual(self._items([a, b]), set())
        self.assertEqual(self.state["removed"],
                         [f"access-{a['item_id']}", f"access-{b['item_id']}"])
        self.assertEqual(_row(self.lt)["reply"].get("refuse"),
                         plaid.TOO_MANY_HANDED_BACK)

    # -- the Plaid user belongs to one environment -------------------------

    def test_a_plaid_user_made_in_sandbox_is_not_sent_to_production(self):
        self._clear_users()
        conn = tenancy.tenant_connect(self.tid)
        try:
            sandbox = plaid.Client("cid", "s", plaid.ENV_URLS["sandbox"],
                                   transport=_backend(self.state))
            production = plaid.Client("cid", "s",
                                      plaid.ENV_URLS["production"],
                                      transport=_backend(self.state))
            a = plaid_user.ensure(conn, sandbox)
            plaid_user.pause(conn, plaid_user.key_of(sandbox), "a test")
            b = plaid_user.ensure(conn, production)
            self.assertTrue(a and b)
            self.assertNotEqual(a[0], b[0])
            self.assertEqual(self.state["user_creates"], 2)
            self.assertFalse(plaid_user.paused(
                conn, plaid_user.key_of(production)))
            self.assertEqual(plaid_user.ensure(conn, production), b)
        finally:
            conn.close()

    # -- the account endpoint ----------------------------------------------

    def test_the_account_answer_opens_no_owner_connection_each_time(self):
        calls = []
        real = plaid_links.recent_open

        def counted(*a, **kw):
            calls.append(a)
            return real(*a, **kw)
        plaid_links.forget_cached_open()
        with self._cap(3), \
                mock.patch.object(plaid_links, "recent_open", counted):
            for _ in range(3):
                me = self.client.get("/api/me")
                self.assertEqual(me.status_code, 200)
                self.assertIn("bank_link_line", me.json()["features"])
        self.assertEqual(len(calls), 1)

    def test_a_failing_link_offer_leaves_the_account_answer_whole(self):
        def boom(*a, **kw):
            raise RuntimeError("offer failed")
        with self._cap(3), mock.patch.object(pages, "link_offer", boom):
            me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200)
        feats = me.json()["features"]
        self.assertIn("institution_cap", feats)
        self.assertNotIn("bank_link_line", feats)


class PlaidUserStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _tenant(self) -> str:
        admin = tenancy.admin_connect()
        try:
            return tenancy.create_tenant(admin, "plaid user probe")
        finally:
            admin.close()

    def test_a_household_sees_only_its_own_plaid_user(self):
        mine, other = self._tenant(), self._tenant()
        conn = tenancy.tenant_connect(mine)
        try:
            conn.execute("INSERT INTO plaid_users (client_id, user_id) "
                         "VALUES ('cid', 'usr-mine')")
        finally:
            conn.close()
        conn = tenancy.tenant_connect(other)
        try:
            self.assertEqual(conn.execute(
                "SELECT * FROM plaid_users").fetchall(), [])
            with self.assertRaises(Exception):
                conn.execute("INSERT INTO plaid_users (tenant_id, "
                             "client_id, user_id) VALUES (%s,'cid','x')",
                             (mine,))
        finally:
            conn.close()

    def test_the_plaid_user_never_travels_in_an_export(self):
        from oikonome import tenant_export
        self.assertIn("plaid_users", pages.EXPORT_SKIP)
        self.assertIn("plaid_users", tenant_export.ARCHIVE_TABLE_SKIP)

    def test_the_plaid_user_is_erased_with_the_household(self):
        tid = self._tenant()
        conn = tenancy.tenant_connect(tid)
        try:
            conn.execute("INSERT INTO plaid_users (client_id, user_id) "
                         "VALUES ('cid', 'usr-gone')")
        finally:
            conn.close()
        admin = tenancy.admin_connect()
        try:
            with admin.transaction():
                tenancy.delete_tenant_rows(admin, tid)
            self.assertIsNone(admin.execute(
                "SELECT 1 FROM plaid_users WHERE tenant_id=%s",
                (tid,)).fetchone())
        finally:
            admin.close()

    def test_the_migration_runs_twice(self):
        import pathlib
        sql = (pathlib.Path(plaid.__file__).resolve().parent.parent / "db"
               / "migrations"
               / "236_a_household_links_several_banks_in_one_go.sql"
               ).read_text()
        admin = tenancy.admin_connect()
        try:
            for _ in range(2):
                with admin.transaction():
                    admin.execute(sql)
        finally:
            admin.close()

    def test_the_switch_reads_off_only_when_said(self):
        for raw, want in (("", True), ("1", True), ("on", True),
                          ("0", False), ("off", False), ("false", False),
                          ("no", False), (" OFF ", False)):
            with mock.patch.dict(os.environ,
                                 {"OIKONOME_PLAID_MULTI_ITEM": raw}):
                self.assertEqual(plaid_user.enabled(), want, raw)


if __name__ == "__main__":
    unittest.main()
