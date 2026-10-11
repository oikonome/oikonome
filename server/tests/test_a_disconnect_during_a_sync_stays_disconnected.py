"""A connection the owner disconnected stays disconnected, whatever a pull
that was already running does next.

The disconnect door does not wait for an in-flight sync, so every status a
sync writes on its own item — the re-upsert, the closing 'ok', the error
stamp — must leave 'archived' alone. Otherwise the connection comes back
into every hourly sweep, and for a SimpleFIN bank the next good pull brings
the disconnected bank back with its history."""

import copy
import datetime as dt
import json
import unittest

import httpx

from oikonome.sync import base as sync_base
from oikonome.sync import coinbase, mx, simplefin

from . import test_coinbase as cb_fx
from . import test_mx as mx_fx
from .test_simplefin import PAYLOAD
from .util import make_db, write_config

BRIDGE = "sfin-demo"
URL = "https://u:p@bridge.test/simplefin"
CHILD = BRIDGE + ":demo-bank"


def _status(conn, item_id):
    return conn.execute("SELECT status FROM items WHERE id=%s",
                        (item_id,)).fetchone()["status"]


class SimplefinDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        simplefin.sync(self.conn, BRIDGE, URL, transport=self._ok())

    def tearDown(self):
        self.conn.close()

    def _ok(self, during=None):
        def handler(request):
            if during:
                during()
            return httpx.Response(200, text=json.dumps(PAYLOAD))
        return httpx.MockTransport(handler)

    def test_a_bridge_disconnected_mid_pull_stays_archived(self):
        fired = []

        def disconnect():
            if not fired:
                fired.append(1)
                sync_base.disconnect_item(self.conn, BRIDGE)
        simplefin.sync(self.conn, BRIDGE, URL,
                       transport=self._ok(during=disconnect))
        self.assertEqual(_status(self.conn, BRIDGE), "archived")
        self.assertEqual(_status(self.conn, CHILD), "archived")

    def test_a_failed_pull_leaves_a_disconnected_bank_disconnected(self):
        self.assertEqual(_status(self.conn, CHILD), "ok")
        sync_base.disconnect_item(self.conn, CHILD)
        with self.assertRaises(Exception):
            simplefin.sync(self.conn, BRIDGE, URL,
                           transport=httpx.MockTransport(
                               lambda r: httpx.Response(500, text="down")))
        self.assertEqual(_status(self.conn, CHILD), "archived")
        self.assertTrue(_status(self.conn, BRIDGE).startswith("error:"))
        # the next good pull must still skip the bank the owner let go
        simplefin.sync(self.conn, BRIDGE, URL, transport=self._ok())
        self.assertEqual(_status(self.conn, CHILD), "archived")
        self.assertEqual(_status(self.conn, BRIDGE), "ok")

    def test_relinking_a_disconnected_bridge_brings_it_back(self):
        """The guard is for syncs only: a person linking again means it."""
        sync_base.disconnect_item(self.conn, BRIDGE)
        sync_base.upsert_item(self.conn, BRIDGE, "simplefin",
                              "SimpleFIN bridge", URL)
        self.assertEqual(_status(self.conn, BRIDGE), "ok")


class CoinbaseDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.iid = coinbase.link(self.conn, "original", cb_fx.KEY_NAME,
                                 cb_fx._PEM)

    def tearDown(self):
        self.conn.close()

    def _transport(self, fail=False):
        inner = cb_fx._transport(fail_accounts=fail)
        fired = []

        def handler(request):
            if not fired:
                fired.append(1)
                sync_base.disconnect_item(self.conn, self.iid)
            return inner.handle_request(request)
        return httpx.MockTransport(handler)

    def test_a_key_disconnected_mid_pull_stays_archived(self):
        coinbase.sync(self.conn, self.iid, transport=self._transport())
        self.assertEqual(_status(self.conn, self.iid), "archived")

    def test_a_failed_pull_after_a_disconnect_stays_archived(self):
        with self.assertRaises(Exception):
            coinbase.sync(self.conn, self.iid,
                          transport=self._transport(fail=True))
        self.assertEqual(_status(self.conn, self.iid), "archived")


class MxDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        mx_fx._save_creds(self.conn)
        mx.sync(self.conn, transport=mx_fx._transport(mx_fx.PAYLOAD))
        sync_base.disconnect_item(self.conn, "mx-MBR-1")

    def tearDown(self):
        self.conn.close()

    def test_a_good_pull_keeps_a_disconnected_member_archived(self):
        mx.sync(self.conn, transport=mx_fx._transport(mx_fx.PAYLOAD))
        self.assertEqual(_status(self.conn, "mx-MBR-1"), "archived")

    def test_a_failed_pull_keeps_a_disconnected_member_archived(self):
        with self.assertRaises(Exception):
            mx.sync(self.conn, transport=httpx.MockTransport(
                lambda r: httpx.Response(500, json={})))
        self.assertEqual(_status(self.conn, "mx-MBR-1"), "archived")

    def _payload(self, aggregated_at=None):
        """The fixture feed plus one new purchase, with the member stamped
        as aggregated at `aggregated_at` (a datetime, or none at all)."""
        pl = copy.deepcopy(mx_fx.PAYLOAD)
        pl["GET /users/USR-1/transactions"]["transactions"].append(
            {"guid": "TRN-NEW", "account_guid": "ACT-1", "amount": 7.25,
             "type": "DEBIT", "transacted_at": "2026-07-12T12:00:00Z",
             "description": "BAKERY", "status": "POSTED"})
        if aggregated_at is not None:
            pl["GET /users/USR-1/members"]["members"][0]["aggregated_at"] = (
                aggregated_at.isoformat())
        return pl

    def _has(self, txn_id):
        return self.conn.execute("SELECT 1 FROM transactions WHERE id=%s",
                                 (txn_id,)).fetchone() is not None

    def _balance(self):
        return self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id='mx-ACT-1'"
        ).fetchone()["balance_current"]

    def test_a_disconnected_member_gets_no_new_data(self):
        before = self._balance()
        pl = self._payload()
        pl["GET /users/USR-1/accounts"]["accounts"][0]["balance"] = 1.0
        mx.sync(self.conn, transport=mx_fx._transport(pl))
        self.assertFalse(self._has("mx:TRN-NEW"))
        self.assertEqual(self._balance(), before)

    def _recording(self, payload):
        """The fixture transport, noting the from_date of every
        transactions request."""
        asked = []
        inner = mx_fx._transport(payload)

        def handler(request):
            if request.url.path.endswith("/transactions"):
                asked.append(request.url.params.get("from_date"))
            return inner.handle_request(request)
        return httpx.MockTransport(handler), asked

    def _archived_days_ago(self, days):
        self.conn.execute(
            "UPDATE items SET archived_at = now() - make_interval(days => %s) "
            "WHERE id='mx-MBR-1'", (days,))

    def _resync_from(self):
        return self.conn.execute(
            "SELECT resync_from FROM items WHERE id='mx-MBR-1'"
        ).fetchone()["resync_from"]

    def test_a_refresh_at_mx_never_brings_a_disconnected_member_back(self):
        """MX keeps refreshing a member left alive on its side, at any hour
        and whatever else the owner is doing in the widget; only the owner
        reconnecting it brings it back."""
        mx.connect_widget_url(self.conn,
                              transport=mx_fx._transport(mx_fx.PAYLOAD))
        pl = self._payload()
        pl["GET /users/USR-1/members"]["members"][0]["aggregated_at"] = (
            dt.datetime.now(dt.timezone.utc).isoformat())
        mx.sync(self.conn, transport=mx_fx._transport(pl))
        self.assertEqual(_status(self.conn, "mx-MBR-1"), "archived")
        self.assertFalse(self._has("mx:TRN-NEW"))

    def test_reconnecting_brings_the_member_back_and_marks_the_gap(self):
        self._archived_days_ago(90)
        r = mx.reconnect(self.conn, "mx-MBR-1")
        want = dt.date.today() - dt.timedelta(days=97)
        self.assertEqual(r["resync_from"], want.isoformat())
        row = self.conn.execute(
            "SELECT status, archived_at FROM items WHERE id='mx-MBR-1'"
        ).fetchone()
        self.assertEqual((row["status"], row["archived_at"]), ("ok", None))
        self.assertEqual(self._resync_from(), want)

    def test_a_second_reconnect_before_a_pull_keeps_the_earlier_gap(self):
        """Reconnect, disconnect and reconnect again with no pull between:
        the first gap is still unfetched, so the start stays at the
        earlier date rather than moving up to the second disconnect."""
        self._archived_days_ago(90)
        mx.reconnect(self.conn, "mx-MBR-1")
        sync_base.disconnect_item(self.conn, "mx-MBR-1")
        self._archived_days_ago(10)
        r = mx.reconnect(self.conn, "mx-MBR-1")
        want = dt.date.today() - dt.timedelta(days=97)
        self.assertEqual(self._resync_from(), want)
        self.assertEqual(r["resync_from"], want.isoformat())

    def test_the_next_pull_reaches_back_over_the_gap_then_forgets_it(self):
        """The pull after a reconnect fetches from a week before the
        disconnect, not the usual 30 days, so nothing MX served while the
        bank was disconnected is left out; the pull after that is back to
        the usual window."""
        self._archived_days_ago(90)
        mx.reconnect(self.conn, "mx-MBR-1")
        transport, asked = self._recording(self._payload())
        mx.sync(self.conn, transport=transport)
        want = dt.date.today() - dt.timedelta(days=97)
        self.assertEqual(asked, [want.isoformat()])
        self.assertTrue(self._has("mx:TRN-NEW"))
        self.assertIsNone(self._resync_from())
        transport, asked = self._recording(self._payload())
        mx.sync(self.conn, transport=transport)
        self.assertEqual(
            asked, [(dt.date.today() - dt.timedelta(days=30)).isoformat()])

    def test_a_failed_pull_keeps_the_gap_for_the_next_one(self):
        self._archived_days_ago(90)
        mx.reconnect(self.conn, "mx-MBR-1")
        with self.assertRaises(Exception):
            mx.sync(self.conn, transport=httpx.MockTransport(
                lambda r: httpx.Response(500, json={})))
        self.assertIsNotNone(self._resync_from())

    def test_a_member_still_disconnected_neither_deepens_nor_gets_data(self):
        self.conn.execute("UPDATE items SET resync_from = current_date - 400 "
                          "WHERE id='mx-MBR-1'")
        transport, asked = self._recording(self._payload())
        mx.sync(self.conn, transport=transport)
        self.assertEqual(
            asked, [(dt.date.today() - dt.timedelta(days=30)).isoformat()])
        self.assertFalse(self._has("mx:TRN-NEW"))
        self.assertEqual(_status(self.conn, "mx-MBR-1"), "archived")

    def test_a_member_a_new_link_took_over_is_not_reconnected(self):
        """A member a newer link adopted keeps its accounts under that
        link; bringing the old member back would pull each of them in a
        second time, so the reconnect is refused with the reason."""
        self.conn.execute("UPDATE items SET archived_reason="
                          "'reconnected — accounts adopted by a new link' "
                          "WHERE id='mx-MBR-1'")
        with self.assertRaises(ValueError) as cm:
            mx.reconnect(self.conn, "mx-MBR-1")
        self.assertIn("newer connection", str(cm.exception))
        self.assertEqual(_status(self.conn, "mx-MBR-1"), "archived")

    def test_reconnecting_a_live_member_is_refused(self):
        mx.reconnect(self.conn, "mx-MBR-1")
        with self.assertRaises(ValueError):
            mx.reconnect(self.conn, "mx-MBR-1")


class ReconnectDoorTests(unittest.TestCase):
    """POST /api/connections/{item_id}/reconnect."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        import uuid
        from fastapi.testclient import TestClient
        from oikonome.db import tenancy
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={
            "email": f"rc-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        self.conn = tenancy.tenant_connect(self.tid)
        mx_fx._save_creds(self.conn)
        mx.sync(self.conn, transport=mx_fx._transport(mx_fx.PAYLOAD))
        sync_base.disconnect_item(self.conn, "mx-MBR-1")

    def tearDown(self):
        self.conn.close()

    def test_the_door_brings_an_mx_member_back(self):
        r = self.client.post("/api/connections/mx-MBR-1/reconnect")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(_status(self.conn, "mx-MBR-1"), "ok")
        self.assertIsNotNone(self.conn.execute(
            "SELECT resync_from FROM items WHERE id='mx-MBR-1'"
        ).fetchone()["resync_from"])

    def test_another_aggregator_is_sent_to_its_own_path(self):
        sync_base.upsert_item(self.conn, "plaid-z", "plaid", "Bank", "tok")
        self.conn.execute("UPDATE items SET aggregator='plaid', "
                          "status='archived' WHERE id='plaid-z'")
        r = self.client.post("/api/connections/plaid-z/reconnect")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("Plaid", r.json()["detail"])

    def test_the_door_waits_for_a_running_sync(self):
        from oikonome.db import tenancy
        holder = tenancy.tenant_connect(self.tid)
        try:
            self.assertTrue(holder.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (f"oikonome:sync:{self.tid}",)).fetchone()["ok"])
            r = self.client.post("/api/connections/mx-MBR-1/reconnect")
            self.assertEqual(r.status_code, 409, r.text)
            self.assertEqual(_status(self.conn, "mx-MBR-1"), "archived")
        finally:
            holder.execute("SELECT pg_advisory_unlock_all()")
            holder.close()


if __name__ == "__main__":
    unittest.main()
