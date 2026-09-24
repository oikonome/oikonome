"""Outbound integrations: read-scoped script tokens, the summary and
metrics doors, webhooks and their outbox, and the local MCP server.

What breaks if these fail: a push token can read the ledger, or a read
token can import into it; the metrics text and the JSON summary say
different numbers; a webhook secret shows up in a list; a delivery is
sent from inside the request; a failed receiver is retried forever or
never; an alert that merely persists fires the event again; a member can
point the household's ledger at a URL; the MCP server answers a tool
call with the wrong shape.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import pathlib
import socket
import threading
import time
import unittest
import uuid
import zipfile
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import activity, alerts
from oikonome.notify import webhooks

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"
# a literal address, so no DNS lookup runs in the address policy, and a
# port nothing listens on, so a test that ever escaped the mock transport
# would fail fast rather than reach a real server
HOOK_URL = "http://127.0.0.1:9/hook"


def _app():
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    return appmod.app


class _Receiver:
    """A fake webhook receiver behind httpx.MockTransport: records every
    request and answers with whatever status it was told to."""

    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list[httpx.Request] = []

    def transport(self):
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.status == 0:
                raise httpx.ConnectError("refused")
            return httpx.Response(self.status, text="thanks"
                                  if self.status < 300 else "nope")
        return httpx.MockTransport(handle)


class _Trickle:
    """A real receiver on a loopback port that answers a status line and
    then one header byte at a time, slower than nothing but fast enough
    that no per-read timeout ever fires. Stops after `life` seconds so a
    sender with no total deadline is not left hanging forever."""

    def __init__(self, interval: float = 0.2, life: float = 8.0):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.interval, self.life = interval, life
        self.got = b""
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        end = time.monotonic() + self.life
        self.sock.settimeout(self.life)
        try:
            c, _ = self.sock.accept()
        except OSError:
            return
        try:
            self.got = c.recv(65536)
            c.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            while time.monotonic() < end:
                c.sendall(b"a")
                time.sleep(self.interval)
        except OSError:
            pass
        finally:
            c.close()
            self.sock.close()


class _CountingBody(httpx.SyncByteStream):
    """A response body far larger than anything worth reading, which
    counts how much of it the sender actually pulled."""

    def __init__(self, total: int, chunk: int = 4096):
        self.total, self.chunk, self.read = total, chunk, 0

    def __iter__(self):
        while self.read < self.total:
            self.read += self.chunk
            yield b"x" * self.chunk


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        cls.owner = TestClient(cls.app)
        cls.owner.post("/api/signup", data={
            "email": f"integ-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            add_txn(conn, TODAY, 42.5, "CORNER MARKET",
                    primary="FOOD_AND_DRINK")
            add_txn(conn, TODAY - dt.timedelta(days=3), 18.0, "HARDWARE HUT")
        finally:
            conn.close()

    def setUp(self):
        webhooks._transport_override = None
        # every test starts with no hooks: the tenant is shared across the
        # class, and a count of deliveries must be this test's own
        conn = self._conn()
        try:
            conn.execute("DELETE FROM webhook_deliveries")
            conn.execute("DELETE FROM webhooks")
        finally:
            conn.close()

    def tearDown(self):
        webhooks._transport_override = None

    def _conn(self):
        return tenancy.tenant_connect(self.tid)

    def _mint(self, scope: str, name: str = "t"):
        r = self.owner.post("/api/tokens", json={"name": name, "scope": scope,
                                                 "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _bare(self, token: str) -> TestClient:
        c = TestClient(self.app)
        c.headers["Authorization"] = f"Bearer {token}"
        return c

    def _hook(self, events=("test.ping",), **extra) -> dict:
        r = self.owner.post("/api/webhooks", json={
            "url": HOOK_URL, "events": list(events), "name": "ha",
            "password": PW, **extra})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TokenScopeTests(Base):
    """push reads nothing; read writes nothing; each has its own doors."""

    def test_a_read_token_reads_the_summary_and_the_metrics(self):
        tok = self._mint("read", "sensor")
        self.assertEqual(tok["scope"], "read")
        c = self._bare(tok["token"])
        r = c.get("/api/integrations/summary")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("verdict", r.json())
        m = c.get("/metrics")
        self.assertEqual(m.status_code, 200, m.text)
        self.assertTrue(m.headers["content-type"].startswith("text/plain"))
        self.assertIn("oikonome_left_today_dollars", m.text)

    def test_a_read_token_cannot_import_or_read_the_pages(self):
        c = self._bare(self._mint("read")["token"])
        r = c.post("/api/import", data={"account_id": "chk"},
                   files={"file": ("x.csv", b"Date,Description,Amount\n",
                                   "text/csv")})
        self.assertEqual(r.status_code, 403, r.text)
        for path in ("/api/transactions", "/api/settings", "/api/tokens",
                     "/api/me", "/api/webhooks"):
            self.assertEqual(c.get(path).status_code, 403, path)
        # the integrations prefix is GET-only for a token (405 when the
        # router refuses the verb before auth runs, 403 when auth does)
        self.assertIn(c.post("/api/integrations/summary").status_code,
                      (403, 405))

    def test_a_push_token_cannot_read_the_integrations_doors(self):
        c = self._bare(self._mint("push")["token"])
        for path in ("/api/integrations/summary", "/metrics",
                     "/api/integrations/transactions"):
            r = c.get(path)
            self.assertEqual(r.status_code, 403, f"{path} -> {r.text}")
            self.assertIn("push-scoped", r.text)

    def test_the_default_scope_is_push_and_lists_say_which(self):
        r = self.owner.post("/api/tokens", json={"name": "old-client",
                                                 "password": PW})
        self.assertEqual(r.json()["scope"], "push")
        listed = self.owner.get("/api/tokens").json()["tokens"]
        self.assertTrue(all("scope" in t for t in listed))
        bad = self.owner.post("/api/tokens", json={"name": "x",
                                                   "scope": "admin",
                                                   "password": PW})
        self.assertEqual(bad.status_code, 400)

    def test_metrics_refuses_in_json_never_a_login_redirect(self):
        # a scraper that is handed a 303 to /login scrapes a login page;
        # the text door answers like the API, with the status and a reason
        bare = TestClient(self.app)
        r = bare.get("/metrics", follow_redirects=False)
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("detail", r.json())
        r = bare.get("/metrics", headers={"Authorization": "Bearer oik_nope"},
                     follow_redirects=False)
        self.assertEqual(r.status_code, 401, r.text)

    def test_a_revoked_read_token_stops(self):
        tok = self._mint("read")
        self.owner.post("/api/tokens/revoke", json={"id": tok["id"],
                                                    "password": PW})
        self.assertEqual(self._bare(tok["token"])
                         .get("/api/integrations/summary").status_code, 401)


class SummaryTests(Base):
    """One computation, two renderings."""

    def test_summary_shape(self):
        s = self.owner.get("/api/integrations/summary").json()
        for k in ("verdict", "today", "month", "net_worth", "accounts",
                  "bills_due", "alerts", "connections", "transactions"):
            self.assertIn(k, s)
        self.assertGreaterEqual(s["transactions"]["total"], 2)
        names = {a["name"] for a in s["accounts"]}
        self.assertIn("Test Checking", names)
        chk = next(a for a in s["accounts"] if a["id"] == "chk")
        self.assertEqual(chk["balance"], 5000.0)
        self.assertIsInstance(s["today"]["left"], float)
        self.assertEqual(s["month"]["budget"], 2000.0)

    def test_metrics_agree_with_the_summary(self):
        s = self.owner.get("/api/integrations/summary").json()
        m = self.owner.get("/metrics").text
        self.assertIn(f"oikonome_left_today_dollars {float(s['today']['left'])!r}", m)
        self.assertIn('oikonome_account_balance_dollars{account="Test Checking",'
                      'institution="Test Bank",type="depository",id="chk",'
                      'counted="1"} 5000.0',
                      m)
        self.assertIn("# TYPE oikonome_net_worth_dollars gauge", m)
        self.assertIn(f"oikonome_transactions_total {float(s['transactions']['total'])!r}", m)
        self.assertIn('oikonome_verdict{verdict="ON BUDGET"}', m)

    def test_a_viewer_may_read_the_summary(self):
        # the doors disclose nothing the pages do not; a viewer is a reader
        r = self.owner.post("/api/invites", json={"label": "v", "role": "viewer",
                                                  "password": PW})
        token = r.json()["url"].rsplit("token=", 1)[1]
        viewer = TestClient(self.app)
        viewer.post("/api/invite/claim", json={
            "token": token, "email": f"v-{uuid.uuid4().hex[:6]}@example.dev",
            "password": "family-member-pass"})
        self.assertEqual(viewer.get("/api/integrations/summary").status_code, 200)
        self.assertEqual(viewer.get("/api/webhooks").status_code, 403)

    def test_the_query_doors(self):
        t = self.owner.get("/api/integrations/transactions",
                           params={"q": "corner"}).json()
        self.assertEqual(t["total"], 1)
        self.assertEqual(t["transactions"][0]["payee"], "CORNER MARKET")
        self.assertEqual(t["transactions"][0]["amount"], 42.5)
        bad = self.owner.get("/api/integrations/transactions",
                             params={"since": "yesterday"})
        self.assertEqual(bad.status_code, 400)
        # the spending window is anchored on the real calendar (the last
        # N months), unlike the fixture's fixed TODAY — so a row dated now
        conn = self._conn()
        try:
            add_txn(conn, dt.date.today(), 9.0, "TODAYS BAGEL",
                    primary="FOOD_AND_DRINK", txn_id="t-bagel")
        finally:
            conn.close()
        sp = self.owner.get("/api/integrations/spending",
                            params={"months": 1}).json()
        self.assertTrue(any(c["category"] == "FOOD_AND_DRINK"
                            for c in sp["categories"]), sp)
        b = self.owner.get("/api/integrations/bills").json()
        self.assertIn("due", b)
        self.assertIn("bills", b)
        nw = self.owner.get("/api/integrations/networth").json()
        self.assertEqual(nw["total"], 4750.0)          # 5000 − 250 card
        a = self.owner.get("/api/integrations/accounts").json()["accounts"]
        self.assertEqual({x["id"] for x in a}, {"chk", "card"})
        ev = self.owner.get("/api/integrations/events").json()["events"]
        self.assertIn("transactions.new", {e["name"] for e in ev})


class SpendingDoorTests(Base):
    """The spending door is the app's own spending rollup: every figure it
    returns is one the Spending page shows, and every category key it
    returns opens the rows behind it through the transactions door.

    What breaks if these fail: an assistant or a dashboard reports twice
    the spend of a card reached through two aggregators, counts an account
    the household hid, puts the whole of a hand-split charge under one
    category, or names a category the transactions door cannot filter on
    — so the drill-down behind a number comes back empty."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from oikonome.engine import links, splits
        day = dt.date.today()
        conn = tenancy.tenant_connect(cls.tid)
        try:
            # the same card through a second aggregator, linked as one
            conn.execute("INSERT INTO items (id, aggregator, institution_name,"
                         " access_token) VALUES ('it2','test','Other Feed','t2')")
            conn.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                         "balance_current) VALUES ('card2','it2','Test Card',"
                         "'credit','credit card',250)")
            # and an account the household hid
            conn.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                         "balance_current,user_removed_at) VALUES ('gone',"
                         "'it1','Old Card','credit','credit card',0,now())")
            links.create(conn, ["card", "card2"])
            add_txn(conn, day, 50.0, "BOOK NOOK", primary="ENTERTAINMENT",
                    txn_id="sp-book")
            add_txn(conn, day, 50.0, "BOOK NOOK", primary="ENTERTAINMENT",
                    account="card2", txn_id="sp-book-2")
            add_txn(conn, day, 70.0, "BOOK NOOK", primary="ENTERTAINMENT",
                    account="gone", txn_id="sp-book-hidden")
            # one warehouse run, split by hand
            add_txn(conn, day, 300.0, "WAREHOUSE CLUB",
                    primary="FOOD_AND_DRINK", txn_id="sp-club")
            splits.set_split(conn, "sp-club", [
                {"category": "FOOD_AND_DRINK", "amount": 200.0},
                {"category": "HOME_IMPROVEMENT", "amount": 100.0}])
            add_txn(conn, day, 12.0, "MYSTERY CHARGE", primary=None,
                    txn_id="sp-uncat")
        finally:
            conn.close()

    def _spending(self) -> dict:
        r = self.owner.get("/api/integrations/spending", params={"months": 1})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _cat(self, sp: dict, key: str) -> float:
        return next((c["amount"] for c in sp["categories"]
                     if c["category"] == key), 0.0)

    def test_a_linked_card_counts_once_and_a_hidden_one_not_at_all(self):
        self.assertEqual(self._cat(self._spending(), "ENTERTAINMENT"), 50.0)

    def test_a_split_charge_lands_in_each_part_category(self):
        sp = self._spending()
        self.assertEqual(self._cat(sp, "FOOD_AND_DRINK"), 200.0)
        self.assertEqual(self._cat(sp, "HOME_IMPROVEMENT"), 100.0)
        # the month's parts still add up to the charge, and the charge is
        # one transaction in each category it touches, not one per part
        mo = sp["months"][-1]
        self.assertEqual(mo["categories"]["HOME_IMPROVEMENT"]["count"], 1)

    def test_every_category_key_opens_its_rows_in_the_transactions_door(self):
        sp = self._spending()
        mo = sp["months"][-1]
        for key, v in mo["categories"].items():
            t = self.owner.get("/api/integrations/transactions", params={
                "category": key, "since": sp["from"], "until": sp["to"],
                "limit": 200}).json()
            self.assertEqual(t["total"], v["count"], (key, t))
        self.assertIn("?", mo["categories"])
        self.assertNotIn("uncategorized", mo["categories"])

    def test_the_summary_counts_the_rows_the_transactions_door_lists(self):
        # the sensor's "rows in the ledger" and "uncategorized" are the
        # Transactions page's own counts: a linked card's second copy and
        # a hidden account's rows are in neither
        s = self.owner.get("/api/integrations/summary").json()["transactions"]
        every = self.owner.get("/api/integrations/transactions").json()
        none = self.owner.get("/api/integrations/transactions",
                              params={"category": "?"}).json()
        self.assertEqual(s["total"], every["total"])
        self.assertEqual(s["uncategorized"], none["total"])

    def test_a_split_row_with_no_category_of_its_own_is_not_uncategorized(self):
        """A split charge is categorized by its parts, and the "?" filter
        leaves it out; the summary's uncategorized count (and the gauge
        built from it) must leave it out too, or a dashboard reports rows
        its drill-down can never list."""
        from oikonome.engine import splits
        conn = self._conn()
        try:
            add_txn(conn, dt.date(2020, 1, 15), 80.0, "FARM STAND",
                    primary=None, txn_id="sp-bare")
            splits.set_split(conn, "sp-bare", [
                {"category": "FOOD_AND_DRINK", "amount": 50.0},
                {"category": "HOME_IMPROVEMENT", "amount": 30.0}])
        finally:
            conn.close()
        try:
            s = self.owner.get("/api/integrations/summary").json()
            none = self.owner.get("/api/integrations/transactions",
                                  params={"category": "?"}).json()
            self.assertNotIn("sp-bare",
                             [t["id"] for t in none["transactions"]])
            self.assertEqual(s["transactions"]["uncategorized"],
                             none["total"])
            self.assertIn("oikonome_transactions_uncategorized "
                          f"{float(none['total'])!r}",
                          self.owner.get("/metrics").text)
        finally:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM transaction_splits "
                             "WHERE txn_id = 'sp-bare'")
                conn.execute("DELETE FROM transactions WHERE id = 'sp-bare'")
            finally:
                conn.close()

    def test_a_linked_copy_is_listed_but_marked_not_counted(self):
        # both halves of a linked card stay listed (the Accounts page shows
        # both), but a dashboard that sums balances must be able to tell
        # which one the money math reads
        for path in ("/api/integrations/accounts", "/api/integrations/summary"):
            accts = {a["id"]: a for a in self.owner.get(path).json()["accounts"]}
            self.assertTrue(accts["card"]["counted"], path)
            self.assertFalse(accts["card2"]["counted"], path)
            self.assertTrue(accts["chk"]["counted"], path)
            self.assertNotIn("gone", accts, path)

    def test_the_balance_gauge_says_which_copy_is_counted(self):
        # sum(oikonome_account_balance_dollars{counted="1"}) is the
        # scraper's version of the money math: a linked card once
        m = self.owner.get("/metrics").text
        self.assertIn('id="card",counted="1"} 250.0', m)
        self.assertIn('id="card2",counted="0"} 250.0', m)


class MetricsLabelTests(unittest.TestCase):
    """A label value is one line of the exposition, whatever the name holds.

    What breaks if this fails: an account or category name carrying a
    carriage return or another control character splits its sample across
    lines for any consumer that reads universal newlines, and the scrape
    fails to parse."""

    def test_a_control_character_in_a_name_stays_on_one_line(self):
        from oikonome.web import integrations
        lab = integrations._lbl('Joint\r\x00Card\x1b"x"\\\x7f\n')
        self.assertEqual(lab, 'Joint  Card \\"x\\"\\\\  ')
        self.assertEqual(len(lab.splitlines()), 1)


class WebhookDoorTests(Base):
    """Create shows the secret once; list never does; owner only."""

    def test_create_lists_and_hides_the_secret(self):
        h = self._hook(("test.ping", "alert.raised"))
        self.assertTrue(h["secret"].startswith(webhooks.SECRET_PREFIX))
        self.assertEqual(h["events"], ["alert.raised", "test.ping"])
        listed = self.owner.get("/api/webhooks").json()
        me = next(w for w in listed["webhooks"] if w["id"] == h["id"])
        self.assertNotIn("secret", me)
        self.assertTrue(me["enabled"])
        self.assertIn("events", listed)
        from oikonome.db import crypto
        conn = self._conn()
        try:
            stored = conn.execute("SELECT secret FROM webhooks WHERE id=%s",
                                  (h["id"],)).fetchone()["secret"]
        finally:
            conn.close()
        if crypto.master_key():
            self.assertNotEqual(stored, h["secret"])
            self.assertTrue(stored.startswith(crypto.PREFIX))

    def test_bad_input_is_a_400(self):
        r = self.owner.post("/api/webhooks", json={
            "url": HOOK_URL, "events": ["nope"], "password": PW})
        self.assertEqual(r.status_code, 400)
        self.assertIn("unknown event", r.text)
        r = self.owner.post("/api/webhooks", json={
            "url": "ftp://x/y", "events": ["test.ping"], "password": PW})
        self.assertEqual(r.status_code, 400)
        r = self.owner.post("/api/webhooks", json={
            "url": HOOK_URL, "events": [], "password": PW})
        self.assertEqual(r.status_code, 400)

    def test_edit_toggle_rotate_delete(self):
        h = self._hook()
        r = self.owner.post(f"/api/webhooks/{h['id']}",
                            json={"enabled": False, "events": ["*"],
                                  "name": "renamed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["enabled"])
        self.assertEqual(r.json()["events"], ["*"])
        self.assertEqual(r.json()["name"], "renamed")
        # a new URL steps up; the toggle did not need to
        r = self.owner.post(f"/api/webhooks/{h['id']}",
                            json={"url": HOOK_URL + "2", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["url"].endswith("/hook2"))
        rot = self.owner.post(f"/api/webhooks/{h['id']}/rotate",
                              json={"password": PW})
        self.assertEqual(rot.status_code, 200, rot.text)
        self.assertNotEqual(rot.json()["secret"], h["secret"])
        self.assertEqual(self.owner.post(
            f"/api/webhooks/{h['id']}/delete").json(), {"ok": True})
        self.assertEqual(self.owner.post(
            f"/api/webhooks/{h['id']}/delete").status_code, 404)
        self.assertEqual(self.owner.post(
            "/api/webhooks/not-a-uuid/delete").status_code, 404)

    def test_members_cannot_manage_webhooks(self):
        r = self.owner.post("/api/invites", json={"label": "m", "role": "member",
                                                  "password": PW})
        token = r.json()["url"].rsplit("token=", 1)[1]
        member = TestClient(self.app)
        member.post("/api/invite/claim", json={
            "token": token, "email": f"m-{uuid.uuid4().hex[:6]}@example.dev",
            "password": "family-member-pass"})
        self.assertEqual(member.get("/api/webhooks").status_code, 403)
        self.assertEqual(member.post("/api/webhooks", json={
            "url": HOOK_URL, "events": ["test.ping"],
            "password": "family-member-pass"}).status_code, 403)

    def test_the_test_button_delivers_now_and_signs(self):
        h = self._hook()
        rcv = _Receiver(200)
        webhooks._transport_override = rcv.transport()
        r = self.owner.post(f"/api/webhooks/{h['id']}/test")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"], r.text)
        self.assertEqual(r.json()["status"], 200)
        self.assertEqual(len(rcv.requests), 1)
        req = rcv.requests[0]
        self.assertEqual(req.headers["x-oikonome-event"], "test.ping")
        self.assertEqual(req.headers["content-type"], "application/json")
        body = json.loads(req.content)
        self.assertEqual(body["event"], "test.ping")
        self.assertEqual(body["attempt"], 1)
        self.assertIn("Hello from Oikonome", body["data"]["message"])
        self.assertEqual(req.headers["x-oikonome-delivery"], body["id"])
        # the receiver's check, with the secret the create door showed
        self.assertTrue(webhooks.verify(h["secret"],
                                        req.headers["x-oikonome-signature"],
                                        req.content))
        self.assertFalse(webhooks.verify("wrong",
                                         req.headers["x-oikonome-signature"],
                                         req.content))
        # ...and it is in the delivery log
        d = self.owner.get(f"/api/webhooks/{h['id']}/deliveries").json()
        self.assertEqual(d["deliveries"][0]["state"], "delivered")
        self.assertEqual(d["deliveries"][0]["response_status"], 200)
        listed = self.owner.get("/api/webhooks").json()["webhooks"]
        me = next(w for w in listed if w["id"] == h["id"])
        self.assertEqual(me["last_status"], 200)
        self.assertEqual(me["failures"], 0)

    def test_a_failed_test_says_why(self):
        h = self._hook()
        rcv = _Receiver(500)
        webhooks._transport_override = rcv.transport()
        r = self.owner.post(f"/api/webhooks/{h['id']}/test").json()
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], 500)
        self.assertIn("HTTP 500", r["error"])


    def test_the_test_ping_is_never_also_sent_by_the_drain(self):
        """The test button attempts its own row inside the request. While
        that attempt is in flight the row must not look due to the minute
        drain, or the receiver gets the same ping twice."""
        h = self._hook()
        seen: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["x-oikonome-delivery"])
            if len(seen) == 1:
                # the minute drain fires while the button's POST is open
                conn = self._conn()
                try:
                    webhooks.deliver_pending(
                        conn, now=dt.datetime.now(dt.timezone.utc)
                        + dt.timedelta(seconds=1))
                finally:
                    conn.close()
            return httpx.Response(200)
        webhooks._transport_override = httpx.MockTransport(handle)
        r = self.owner.post(f"/api/webhooks/{h['id']}/test").json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(seen, [r["delivery_id"]])

    def test_the_test_button_holds_no_connection_while_it_waits(self):
        """The test ping waits on a stranger's server for up to the send
        deadline. The tenant pool is a handful of connections, so the
        request must give its connection back for that wait — a few slow
        receivers tested at once would otherwise starve every other
        request of the household's database."""
        h = self._hook()
        pool = tenancy._pool(tenancy.APP_DSN)
        held: list[int] = []

        def held_now() -> int:
            st = pool.get_stats()
            return st.get("pool_size", 0) - st.get("pool_available", 0)

        def send(*a, **kw):
            held.append(held_now())
            return 200, None
        before = held_now()
        with mock.patch.object(webhooks, "_send", send):
            r = self.owner.post(f"/api/webhooks/{h['id']}/test").json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(held, [before])
        conn = self._conn()
        try:
            state = conn.execute(
                "SELECT state FROM webhook_deliveries WHERE id = %s",
                (r["delivery_id"],)).fetchone()["state"]
        finally:
            conn.close()
        self.assertEqual(state, "delivered")


class OutboxTests(Base):
    """emit queues; the drain sends; failures climb the ladder."""

    def _pending(self, hook_id):
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT * FROM webhook_deliveries WHERE webhook_id=%s "
                "ORDER BY created_at", (hook_id,)).fetchall()
        finally:
            conn.close()

    def test_emit_queues_only_for_subscribers_and_never_sends(self):
        h = self._hook(("alert.raised",))
        other = self._hook(("test.ping",))
        rcv = _Receiver(200)
        webhooks._transport_override = rcv.transport()
        conn = self._conn()
        try:
            self.assertTrue(webhooks.wanted(conn, "alert.raised"))
            self.assertFalse(webhooks.wanted(conn, "networth.snapshot"))
            n = webhooks.emit(conn, "alert.raised", {"kind": "x"})
            self.assertEqual(n, 1)
            self.assertEqual(webhooks.emit(conn, "not.an.event", {}), 0)
        finally:
            conn.close()
        self.assertEqual(len(rcv.requests), 0)          # queued, not sent
        self.assertEqual(len(self._pending(h["id"])), 1)
        self.assertEqual(len(self._pending(other["id"])), 0)
        self.assertEqual(self._pending(h["id"])[0]["state"], "pending")

    def test_the_drain_delivers_and_settles(self):
        h = self._hook(("networth.snapshot",))
        rcv = _Receiver(204)
        webhooks._transport_override = rcv.transport()
        conn = self._conn()
        try:
            webhooks.emit(conn, "networth.snapshot", {"total": 1.5,
                                                      "date": TODAY})
            out = webhooks.deliver_pending(conn)
        finally:
            conn.close()
        self.assertEqual(out["sent"], 1)
        row = self._pending(h["id"])[0]
        self.assertEqual(row["state"], "delivered")
        self.assertEqual(row["attempts"], 1)
        body = json.loads(rcv.requests[0].content)
        self.assertEqual(body["data"], {"total": 1.5, "date": TODAY.isoformat()})
        self.assertEqual(body["event"], "networth.snapshot")
        # nothing left to send
        conn = self._conn()
        try:
            self.assertEqual(webhooks.deliver_pending(conn)["sent"], 0)
        finally:
            conn.close()

    def test_a_failure_climbs_the_ladder_then_gives_up(self):
        h = self._hook(("test.ping",))
        rcv = _Receiver(503)
        webhooks._transport_override = rcv.transport()
        conn = self._conn()
        try:
            webhooks.emit(conn, "test.ping", {"n": 1})
            # the row is stamped by the database clock; "now" for the
            # drain must be at or after it
            now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
            out = webhooks.deliver_pending(conn, now=now)
            self.assertEqual(out["failed"], 1)
            row = self._pending(h["id"])[0]
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["attempts"], 1)
            self.assertEqual(row["response_status"], 503)
            self.assertEqual((row["next_attempt_at"] - now).total_seconds(),
                             webhooks.BACKOFF[0])
            # not due yet: the drain leaves it
            self.assertEqual(webhooks.deliver_pending(conn, now=now)["failed"], 0)
            # walk the whole ladder
            t = now
            for i in range(1, webhooks.MAX_ATTEMPTS):
                t = t + dt.timedelta(seconds=webhooks.BACKOFF[min(
                    i - 1, len(webhooks.BACKOFF) - 1)])
                webhooks.deliver_pending(conn, now=t)
            row = self._pending(h["id"])[0]
            self.assertEqual(row["state"], "failed")
            self.assertEqual(row["attempts"], webhooks.MAX_ATTEMPTS)
            hook = conn.execute("SELECT failures, enabled, last_error FROM "
                                "webhooks WHERE id=%s", (h["id"],)).fetchone()
            self.assertEqual(hook["failures"], webhooks.MAX_ATTEMPTS)
            self.assertTrue(hook["enabled"])
            self.assertIn("HTTP 503", hook["last_error"])
        finally:
            conn.close()
        self.assertEqual(len(rcv.requests), webhooks.MAX_ATTEMPTS)
        self.assertEqual(json.loads(rcv.requests[-1].content)["attempt"],
                         webhooks.MAX_ATTEMPTS)

    def test_a_dead_receiver_switches_the_hook_off(self):
        h = self._hook(("test.ping",))
        rcv = _Receiver(0)                       # connection refused
        webhooks._transport_override = rcv.transport()
        conn = self._conn()
        try:
            for _ in range(webhooks.DISABLE_AFTER):
                webhooks.emit(conn, "test.ping", {})
                webhooks.deliver_pending(
                    conn, now=dt.datetime.now(dt.timezone.utc)
                    + dt.timedelta(seconds=1))
            hook = conn.execute("SELECT failures, enabled, disabled_reason "
                                "FROM webhooks WHERE id=%s",
                                (h["id"],)).fetchone()
            self.assertFalse(hook["enabled"])
            self.assertIn("switched off", hook["disabled_reason"])
            # everything still queued is failed with it, and emit skips it
            states = {r["state"] for r in self._pending(h["id"])}
            self.assertEqual(states, {"failed"})
            self.assertEqual(webhooks.emit(conn, "test.ping", {}), 0)
            # turning it back on clears the count
            r = self.owner.post(f"/api/webhooks/{h['id']}",
                                json={"enabled": True}).json()
            self.assertTrue(r["enabled"])
            self.assertEqual(r["failures"], 0)
            self.assertIsNone(r["disabled_reason"])
        finally:
            conn.close()

    def test_a_disabled_hook_gets_nothing(self):
        h = self._hook(("test.ping",))
        self.owner.post(f"/api/webhooks/{h['id']}", json={"enabled": False})
        conn = self._conn()
        try:
            self.assertEqual(webhooks.emit(conn, "test.ping", {}), 0)
        finally:
            conn.close()

    def test_prune_keeps_the_pending_and_the_recent(self):
        h = self._hook(("test.ping",))
        conn = self._conn()
        try:
            webhooks.emit(conn, "test.ping", {})
            conn.execute(
                "INSERT INTO webhook_deliveries (webhook_id, event, payload, "
                "state, created_at) VALUES (%s, 'test.ping', '{}', "
                "'delivered', now() - interval '9 days')", (h["id"],))
            self.assertEqual(webhooks.prune(conn), 1)
            self.assertEqual(len(self._pending(h["id"])), 1)
        finally:
            conn.close()


    def _due(self):
        return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)

    def test_a_trickling_receiver_is_cut_off_at_the_total_deadline(self):
        """One attempt has a wall-clock budget. A receiver that feeds the
        response a byte at a time never trips a per-read timeout, and
        without a total deadline it holds the worker thread (or, for the
        test button, a request thread and a pooled connection) for as long
        as it likes."""
        srv = _Trickle(interval=0.2, life=8.0)
        out: dict = {}

        def attempt():
            with mock.patch.object(webhooks, "DEADLINE", 1.0, create=True):
                out["r"] = webhooks._send(
                    f"http://127.0.0.1:{srv.port}/hook", "s", "test.ping",
                    str(uuid.uuid4()), b"{}")
        t0 = time.monotonic()
        th = threading.Thread(target=attempt, daemon=True)
        th.start()
        th.join(5.0)
        self.assertFalse(th.is_alive(), "the attempt outlived its deadline")
        self.assertLess(time.monotonic() - t0, 4.0)
        status, error = out["r"]
        self.assertIsNotNone(error)
        self.assertIn("Timeout", error)

    def test_an_egress_proxy_in_the_environment_carries_the_delivery(self):
        """A self-hosted instance whose only way out is an egress proxy
        (HTTP(S)_PROXY in the container's environment) must send webhooks
        through it, as every other outbound client does — a sender that
        connects directly fails every attempt until the hook disables
        itself. The attempt's deadline must hold through the proxy too."""
        proxy = _Trickle(interval=0.2, life=8.0)
        env = {"HTTP_PROXY": f"http://127.0.0.1:{proxy.port}",
               "HTTPS_PROXY": f"http://127.0.0.1:{proxy.port}",
               "NO_PROXY": "", "no_proxy": ""}
        out: dict = {}

        def attempt():
            with mock.patch.dict("os.environ", env), \
                    mock.patch.object(webhooks, "DEADLINE", 1.0):
                # port 1 has no listener: only the proxy can answer
                out["r"] = webhooks._send(
                    "http://127.0.0.1:1/hook", "s", "test.ping",
                    str(uuid.uuid4()), b"{}")
        t0 = time.monotonic()
        th = threading.Thread(target=attempt, daemon=True)
        th.start()
        th.join(5.0)
        self.assertFalse(th.is_alive(), "the attempt outlived its deadline")
        self.assertLess(time.monotonic() - t0, 4.0)
        # an absolute-URI request line is what a client sends a proxy
        self.assertTrue(proxy.got.startswith(b"POST http://127.0.0.1:1/hook"),
                        proxy.got[:80])
        status, error = out["r"]
        self.assertIn("Timeout", error or "")

    def test_a_huge_error_body_is_not_read_past_the_cap(self):
        """Only the first ERROR_MAX characters of a refusal are kept, so
        only a few KB may be read — a multi-GB answer must not be pulled
        into memory."""
        body = _CountingBody(50 * 1024 * 1024)
        webhooks._transport_override = httpx.MockTransport(
            lambda req: httpx.Response(500, stream=body))
        status, error = webhooks._send(HOOK_URL, "s", "test.ping",
                                       str(uuid.uuid4()), b"{}")
        self.assertLess(body.read, 64 * 1024)
        self.assertEqual(status, 500)
        self.assertTrue(error.startswith("HTTP 500: xxx"))

    def test_overlapping_passes_send_each_delivery_once(self):
        """A pass that is still sending when the next minute's pass starts
        must have claimed its rows: the second pass sends none of them."""
        h = self._hook(("test.ping",))
        conn = self._conn()
        try:
            for i in range(3):
                webhooks.emit(conn, "test.ping", {"n": i})
        finally:
            conn.close()
        seen: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["x-oikonome-delivery"])
            if len(seen) == 1:
                other = self._conn()
                try:
                    webhooks.deliver_pending(other, now=self._due())
                finally:
                    other.close()
            return httpx.Response(200)
        webhooks._transport_override = httpx.MockTransport(handle)
        conn = self._conn()
        try:
            webhooks.deliver_pending(conn, now=self._due())
        finally:
            conn.close()
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 3, seen)
        self.assertEqual({r["state"] for r in self._pending(h["id"])},
                         {"delivered"})

    def test_a_late_failure_cannot_undo_a_delivery(self):
        """Settling is conditional on the row still being the attempt that
        was read: a stale failure arriving after another pass delivered the
        row must neither reopen it nor count against the hook."""
        h = self._hook(("test.ping",))
        webhooks._transport_override = _Receiver(200).transport()
        conn = self._conn()
        try:
            webhooks.emit(conn, "test.ping", {})
            stale = conn.execute(
                "SELECT * FROM webhook_deliveries WHERE webhook_id=%s",
                (h["id"],)).fetchone()
            webhooks.deliver_pending(conn, now=self._due())
            webhooks._settle(conn, stale, False, None, "ReadTimeout",
                             dt.datetime.now(dt.timezone.utc))
            row = conn.execute(
                "SELECT state, attempts FROM webhook_deliveries "
                "WHERE id=%s", (stale["id"],)).fetchone()
            hook = conn.execute("SELECT failures FROM webhooks WHERE id=%s",
                                (h["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["state"], "delivered")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(hook["failures"], 0)

    def test_a_hook_switched_off_mid_batch_gets_nothing_more(self):
        """The owner switching a hook off while a pass is sending stops the
        pass for that hook at once; the rest stay queued for when it is
        switched back on."""
        h = self._hook(("test.ping",))
        conn = self._conn()
        try:
            for i in range(3):
                webhooks.emit(conn, "test.ping", {"n": i})
        finally:
            conn.close()
        seen: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["x-oikonome-delivery"])
            other = self._conn()
            try:
                other.execute("UPDATE webhooks SET enabled = FALSE "
                              "WHERE id=%s", (h["id"],))
            finally:
                other.close()
            return httpx.Response(200)
        webhooks._transport_override = httpx.MockTransport(handle)
        conn = self._conn()
        try:
            webhooks.deliver_pending(conn, now=self._due())
            left = conn.execute(
                "SELECT next_attempt_at FROM webhook_deliveries "
                "WHERE webhook_id=%s AND state='pending'",
                (h["id"],)).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(left), 2)
        # released, not leased: switching it back on resumes them now
        for r in left:
            self.assertLessEqual(r["next_attempt_at"], self._due())

    def test_a_hook_the_app_switches_off_mid_batch_gets_nothing_more(self):
        """The failure that crosses DISABLE_AFTER switches the hook off and
        fails its queue; the pass must not go on sending the rows it had
        already fetched."""
        h = self._hook(("test.ping",))
        rcv = _Receiver(0)
        webhooks._transport_override = rcv.transport()
        conn = self._conn()
        try:
            conn.execute("UPDATE webhooks SET failures=%s WHERE id=%s",
                         (webhooks.DISABLE_AFTER - 1, h["id"]))
            for i in range(3):
                webhooks.emit(conn, "test.ping", {"n": i})
            webhooks.deliver_pending(conn, now=self._due())
            hook = conn.execute("SELECT enabled, failures FROM webhooks "
                                "WHERE id=%s", (h["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(len(rcv.requests), 1)
        self.assertFalse(hook["enabled"])
        self.assertEqual(hook["failures"], webhooks.DISABLE_AFTER)
        self.assertEqual({r["state"] for r in self._pending(h["id"])},
                         {"failed"})

    def test_one_pass_spends_a_bounded_time_on_a_tenant(self):
        """A slow receiver with a full queue must not keep the pass on one
        household past its budget; what was not attempted stays due for
        the next pass, with no attempt counted."""
        h = self._hook(("test.ping",))
        conn = self._conn()
        try:
            for i in range(5):
                webhooks.emit(conn, "test.ping", {"n": i})
        finally:
            conn.close()

        def handle(request):
            time.sleep(0.3)
            return httpx.Response(200)
        webhooks._transport_override = httpx.MockTransport(handle)
        conn = self._conn()
        try:
            with mock.patch.object(webhooks, "TENANT_BUDGET", 0.5,
                                   create=True):
                out = webhooks.deliver_pending(conn, now=self._due())
        finally:
            conn.close()
        self.assertLess(out["sent"], 5)
        rows = self._pending(h["id"])
        left = [r for r in rows if r["state"] == "pending"]
        self.assertEqual(len(left), 5 - out["sent"])
        for r in left:
            self.assertEqual(r["attempts"], 0)
            self.assertLessEqual(r["next_attempt_at"], self._due())

    def test_the_minute_cron_skips_while_the_previous_pass_runs(self):
        """arq cancels the coroutine at its timeout but not the thread, so
        an overlapping pass would pile another stuck thread onto the
        executor every minute. A pass that finds the previous one still
        running does nothing."""
        from oikonome.jobs import worker
        with mock.patch.object(worker, "webhooks_drain_tenant") as one, \
                mock.patch.object(worker, "_webhook_tenants",
                                  return_value=[self.tid]):
            self.assertTrue(worker._WEBHOOK_PASS.acquire(blocking=False))
            try:
                out = worker.webhooks_drain()
            finally:
                worker._WEBHOOK_PASS.release()
            one.assert_not_called()
            self.assertEqual(out, {})
            worker.webhooks_drain()
            one.assert_called_once_with(self.tid)

    def test_webhooks_stay_out_of_both_exports(self):
        """A receiver URL is a credential in its own right (a Home
        Assistant webhook id, a chat incoming-webhook URL), and a delivery
        payload is a copy of the ledger: neither table may ride the
        household /export ZIP or the operator portability archive."""
        from oikonome import tenant_export
        from oikonome.sync import export
        secret_url = "http://127.0.0.1:9/api/webhook/" + uuid.uuid4().hex
        r = self.owner.post("/api/webhooks", json={
            "url": secret_url, "events": ["test.ping"], "name": "ha",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        conn = self._conn()
        try:
            webhooks.emit(conn, "test.ping", {"marker": secret_url})
            data = export.build_zip(conn)
        finally:
            conn.close()
        archive = tenant_export.build_archive(self.tid, operator="t",
                                              consented=True)
        for blob in (data, archive):
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                names = z.namelist()
                for n in names:
                    self.assertNotIn("webhook", n)
                    self.assertNotIn(secret_url.encode(), z.read(n), n)


class EmitterTests(Base):
    """The doors that observe events queue them — once, on the edge."""

    def test_activity_record_emits_the_sentence(self):
        h = self._hook(("activity.logged",))
        conn = self._conn()
        try:
            activity.record(conn, {"user_id": None, "email": "pat@example.dev",
                                   "role": "member"},
                            "note", "added", target="t1", label="T",
                            detail={"after": "call the bank"})
            row = conn.execute(
                "SELECT payload FROM webhook_deliveries WHERE webhook_id=%s",
                (h["id"],)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["payload"]["actor"], "pat@example.dev")
        self.assertIn("call the bank", row["payload"]["summary"])
        self.assertEqual(row["payload"]["kind"], "note")

    def test_alert_raised_fires_on_the_edge_only(self):
        h = self._hook(("alert.raised",))
        a = [{"kind": "anomaly", "severity": "warn",
              "message": f"Unusual charge {uuid.uuid4().hex[:6]}"}]
        conn = self._conn()
        try:
            alerts.log(conn, a, TODAY, partial=True)
            alerts.log(conn, a, TODAY, partial=True)       # still active
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM webhook_deliveries "
                "WHERE webhook_id=%s AND event='alert.raised'",
                (h["id"],)).fetchone()["n"]
            self.assertEqual(n, 1)
            # cleared, then back: a recurrence is news
            conn.execute("UPDATE alerts_log SET active=0 WHERE message=%s",
                         (a[0]["message"],))
            alerts.log(conn, a, TODAY, partial=True)
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM webhook_deliveries "
                "WHERE webhook_id=%s AND event='alert.raised'",
                (h["id"],)).fetchone()["n"]
            self.assertEqual(n, 2)
            payload = conn.execute(
                "SELECT payload FROM webhook_deliveries WHERE webhook_id=%s "
                "ORDER BY created_at LIMIT 1", (h["id"],)).fetchone()["payload"]
        finally:
            conn.close()
        self.assertEqual(payload["kind"], "anomaly")
        self.assertEqual(payload["message"], a[0]["message"])

    def test_sync_reports_new_rows(self):
        from oikonome.jobs import worker
        h = self._hook(("transactions.new", "sync.completed"))
        conn = self._conn()
        try:
            before = worker._recent_txn_ids(conn)
            self.assertIsInstance(before, set)
            # a sync lands rows dated now, not on the fixture's July
            new_id = add_txn(conn, dt.date.today(), 7.25, "NEW COFFEE")
            worker._webhooks_after_sync(conn, {"it1": "ok:1"}, before, self.tid)
            rows = conn.execute(
                "SELECT event, payload FROM webhook_deliveries "
                "WHERE webhook_id=%s ORDER BY event", (h["id"],)).fetchall()
        finally:
            conn.close()
        by = {r["event"]: r["payload"] for r in rows}
        self.assertEqual(by["sync.completed"]["new_transactions"], 1)
        self.assertEqual(by["sync.completed"]["connections"][0]["name"],
                         "Test Bank")
        self.assertTrue(by["sync.completed"]["ok"])
        self.assertEqual(by["transactions.new"]["count"], 1)
        t = by["transactions.new"]["transactions"][0]
        self.assertEqual(t["id"], new_id)
        self.assertEqual(t["payee"], "NEW COFFEE")
        self.assertEqual(t["account"], "Test Card")

    def test_nobody_listening_costs_nothing(self):
        from oikonome.jobs import worker
        conn = self._conn()
        try:
            # a fresh tenant with no hooks — the pre-sync read is skipped
            admin = tenancy.admin_connect()
            try:
                tid = tenancy.create_tenant(admin, "quiet")
            finally:
                admin.close()
            quiet = tenancy.tenant_connect(tid)
            try:
                self.assertIsNone(worker._recent_txn_ids(quiet))
            finally:
                quiet.close()
        finally:
            conn.close()

    def test_the_daily_event_carries_the_summary_shape(self):
        """The daily cadence is due when a webhook wants it, with no
        email/SMS/push on; and the event is the integrations summary."""
        from oikonome.engine import budget
        from oikonome.jobs import worker
        h = self._hook(("report.daily",))
        conn = self._conn()
        try:
            cfg = budget.load_config(conn)
            cfg["email_schedule"] = {"daily": {"on": False, "hour": 0},
                                     "weekly": {"on": False},
                                     "monthly": {"on": False}}
            budget.save_config(conn, cfg)
            # a household created today never gets a same-day send; move
            # the owner's creation back a day
            conn.execute("DELETE FROM job_runs WHERE job='daily-email'")
        finally:
            conn.close()
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET created_at = now() - interval "
                          "'2 days' WHERE tenant_id=%s", (self.tid,))
        finally:
            admin.close()
        conn = self._conn()
        try:
            due = worker.emails_due(conn, dt.datetime.now(dt.timezone.utc))
        finally:
            conn.close()
        self.assertIn("daily", due)
        worker.email_tenant(self.tid, send_it=True, cadence="daily")
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT payload FROM webhook_deliveries WHERE webhook_id=%s "
                "AND event='report.daily'", (h["id"],)).fetchone()
            hb = conn.execute("SELECT 1 FROM job_runs WHERE job='daily-email'"
                              ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIn("verdict", row["payload"])
        self.assertIn("left", row["payload"]["today"])
        self.assertIsNotNone(hb)          # the cadence stamped for the day


    def test_the_daily_event_is_queued_once_even_when_the_mail_fails(self):
        """The daily email retries every hourly sweep until it goes out;
        the daily webhook event has its own once-a-day guard, so a relay
        that is down all day does not post a fresh report.daily (each with
        a new delivery id the receiver cannot dedupe) every hour."""
        from oikonome.engine import budget
        from oikonome.jobs import worker
        h = self._hook(("report.daily",))
        conn = self._conn()
        try:
            cfg = budget.load_config(conn)
            cfg["email_schedule"] = {"daily": {"on": True, "hour": 0}}
            budget.save_config(conn, cfg)
            conn.execute("DELETE FROM job_runs WHERE job IN "
                         "('daily-email', 'daily-webhook')")
        finally:
            conn.close()
        refused = [{"email": "owner@example.dev", "ok": False,
                    "error": "ConnectionRefusedError"}]
        with mock.patch.object(worker, "_recipients_and_raw",
                               return_value=(["owner@example.dev"],
                                             ["owner@example.dev"])), \
                mock.patch("oikonome.web.report.send_each",
                           return_value=refused):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    worker.email_tenant(self.tid, send_it=True,
                                        cadence="daily")
        conn = self._conn()
        try:
            n = conn.execute(
                "SELECT count(*) AS n FROM webhook_deliveries "
                "WHERE webhook_id=%s AND event='report.daily'",
                (h["id"],)).fetchone()["n"]
            conn.execute("DELETE FROM job_runs WHERE job IN "
                         "('daily-email', 'daily-webhook')")
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_moving_the_household_east_does_not_repost_the_day(self):
        """The daily event's once-a-day stamp is read like the daily
        email's: a day has turned only when both the zone in effect now
        and the zone the stamp was written in say so. Moving the household
        to a zone where it is already tomorrow puts today's midnight after
        this evening's send, and a check on the new zone alone posts the
        same day's report again."""
        from oikonome.engine import budget
        from oikonome.jobs import worker
        now = dt.datetime.now(dt.timezone.utc)
        # a whole-hour zone where it is 22:xx now, and one two hours east
        # where it is already 00:xx tomorrow (Etc/GMT signs run backwards)
        west = ((22 - now.hour + 12) % 24) - 12
        east = west + 2
        zone_w = f"Etc/GMT{-west:+d}" if west else "Etc/GMT"
        zone_e = f"Etc/GMT{-east:+d}" if east else "Etc/GMT"
        h = self._hook(("report.daily",))
        conn = self._conn()
        try:
            cfg = budget.load_config(conn)
            old_zone = cfg.get("timezone")
            cfg["email_schedule"] = {"daily": {"on": True, "hour": 0}}
            cfg["timezone"] = zone_e
            budget.save_config(conn, cfg)
            conn.execute("DELETE FROM job_runs WHERE job IN "
                         "('daily-email', 'daily-webhook')")
            # queued an hour ago, this evening in the old zone
            conn.execute(
                "INSERT INTO job_runs (job, ran_at, note, zone) "
                "VALUES ('daily-webhook', %s, '', %s)",
                (now - dt.timedelta(hours=1), zone_w))
        finally:
            conn.close()
        refused = [{"email": "owner@example.dev", "ok": False,
                    "error": "ConnectionRefusedError"}]
        try:
            with mock.patch.object(worker, "_recipients_and_raw",
                                   return_value=(["owner@example.dev"],
                                                 ["owner@example.dev"])), \
                    mock.patch("oikonome.web.report.send_each",
                               return_value=refused), \
                    self.assertRaises(RuntimeError):
                worker.email_tenant(self.tid, send_it=True, cadence="daily")
        finally:
            conn = self._conn()
            try:
                n = conn.execute(
                    "SELECT count(*) AS n FROM webhook_deliveries "
                    "WHERE webhook_id=%s AND event='report.daily'",
                    (h["id"],)).fetchone()["n"]
                conn.execute("DELETE FROM job_runs WHERE job IN "
                             "('daily-email', 'daily-webhook')")
                cfg = budget.load_config(conn)
                cfg.pop("timezone", None)
                if old_zone is not None:
                    cfg["timezone"] = old_zone
                budget.save_config(conn, cfg)
            finally:
                conn.close()
        self.assertEqual(n, 0)


class SignatureTests(unittest.TestCase):
    def test_roundtrip_and_replay_window(self):
        body = b'{"a":1}'
        hdr = webhooks.signature_header("s3cret", 1_700_000_000, body)
        self.assertTrue(webhooks.verify("s3cret", hdr, body,
                                        now=1_700_000_100))
        self.assertFalse(webhooks.verify("s3cret", hdr, body,
                                         now=1_700_000_900))
        self.assertFalse(webhooks.verify("s3cret", hdr, b'{"a":2}',
                                         now=1_700_000_100))
        self.assertFalse(webhooks.verify("s3cret", "garbage", body))

    def test_event_normalization(self):
        self.assertEqual(webhooks.normalize_events(["*", "alert.raised"]), ["*"])
        self.assertEqual(webhooks.normalize_events(["test.ping", "alert.raised",
                                                    "alert.raised"]),
                         ["alert.raised", "test.ping"])
        with self.assertRaises(webhooks.WebhookError):
            webhooks.normalize_events("alert.raised")


class McpServerTests(unittest.TestCase):
    """The stdio server: protocol shape, tool dispatch, error handling."""

    @classmethod
    def setUpClass(cls):
        path = (pathlib.Path(__file__).resolve().parents[2]
                / "integrations" / "mcp" / "oikonome_mcp.py")
        spec = importlib.util.spec_from_file_location("oikonome_mcp", path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _server(self, fetch=None):
        calls = []

        def _fetch(path, params):
            calls.append((path, dict(params)))
            if fetch:
                return fetch(path, params)
            return {"path": path, "params": dict(params)}
        return self.mod.Server(_fetch), calls

    def test_initialize_and_list(self):
        srv, _ = self._server()
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "oikonome")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertIsNone(srv.handle({"jsonrpc": "2.0",
                                      "method": "notifications/initialized"}))
        r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in r["result"]["tools"]]
        self.assertEqual(names, ["summary", "accounts", "transactions",
                                 "spending", "bills", "alerts", "net_worth"])
        for t in r["result"]["tools"]:
            self.assertNotIn("path", t)
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_call_maps_to_the_read_door(self):
        srv, calls = self._server()
        r = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "transactions",
                                   "arguments": {"q": "coffee", "limit": 5}}})
        self.assertEqual(calls, [("/api/integrations/transactions",
                                  {"q": "coffee", "limit": 5})])
        self.assertEqual(r["result"]["structuredContent"]["params"]["q"],
                         "coffee")
        self.assertEqual(r["result"]["content"][0]["type"], "text")
        self.assertNotIn("isError", r["result"])

    def test_tool_errors_are_results_not_protocol_errors(self):
        srv, _ = self._server()
        r = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "nope"}})
        self.assertTrue(r["result"]["isError"])
        r = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                        "params": {"name": "summary",
                                   "arguments": {"bogus": 1}}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("bogus", r["result"]["content"][0]["text"])

        def boom(path, params):
            raise RuntimeError("HTTP 401 from the instance: revoked")
        srv, _ = self._server(boom)
        r = srv.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                        "params": {"name": "summary"}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("401", r["result"]["content"][0]["text"])
        r = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "nothing/here"})
        self.assertEqual(r["error"]["code"], -32601)

    def test_serve_reads_lines_and_writes_lines(self):
        srv, _ = self._server()
        inp = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
            + "not json\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/x"}) + "\n")
        out = io.StringIO()
        self.mod.serve(srv, inp, out)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(lines[0], {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(lines[1]["error"]["code"], -32700)
        self.assertEqual(len(lines), 2)
