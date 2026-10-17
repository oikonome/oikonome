"""A bank link that repeats a connection the household already has is
handed back, not kept.

A client that did not see its first link finish (or two taps on the same
button) can take the person through Plaid twice for the same login. Each
pass creates an Item, and keeping both syncs every account twice. The
server is the backstop whatever the client does:

* a fresh Item whose every masked account (same type and mask) is
  already held at that institution by a live Item is handed back to
  Plaid and the link answers as a failed add saying why. Plaid created
  and bills that Item, so the ledger records it — as a repeat, which
  holds no place under the institution cap (the login it repeats holds
  one);
* a household whose repeats keep coming (a client stuck relinking) is
  not exchanged again once it has had three refused inside a day: the
  exchange is what creates the billed Item, so the limit has to stop it;
* that holds while the first Item's accounts are still on their way (its
  first sync not landed yet) — they are asked of Plaid — and when that
  first Item commits while the second link is already under way;
* a second login at the same bank with different accounts is kept, and
  so is one that shares a joint account but brings an account of its own
  — two people in one household may each link their own;
* a connection that was archived, or whose consent was withdrawn, does
  not count as held (only a fresh link repairs it); one that only needs
  signing in again does (Reconnect repairs it);
* Plaid is never called while the household's link lock is held.
"""

import contextlib

import json
import unittest
import uuid
from unittest import mock

import httpx

from oikonome.db import tenancy
from oikonome.jobs import plaid_links
from oikonome.sync import base as sync_base
from oikonome.sync import plaid

from .util import make_db, write_config

INST = "ins_repeat"


def _acct(mask, kind="depository", sub="checking"):
    return {"account_id": f"a-{uuid.uuid4().hex[:8]}", "name": "Checking",
            "type": kind, "subtype": sub, "mask": mask,
            "balances": {"current": 10.0}}


class SecondLinkTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        self.new_item = f"pl-new-{uuid.uuid4().hex[:8]}"
        # access token -> the accounts Plaid reports for it
        self.accounts = {"access-new": [_acct("1111")]}
        self.removed = []
        self.exchanged = []
        self.minted = []

    def tearDown(self):
        self.conn.close()
        admin = tenancy.admin_connect()
        try:
            for iid in {self.new_item, *self.minted}:
                admin.execute("DELETE FROM plaid_release_queue "
                              "WHERE item_id=%s", (iid,))
                admin.execute("DELETE FROM plaid_item_ledger "
                              "WHERE item_id=%s", (iid,))
        finally:
            admin.close()

    def _handler(self, request):
        path = request.url.path
        body = json.loads(request.content.decode() or "{}")
        if path == "/item/public_token/exchange":
            self.exchanged.append(self.new_item)
            return httpx.Response(200, text=json.dumps(
                {"item_id": self.new_item, "access_token": "access-new"}))
        if path == "/item/get":
            return httpx.Response(200, text=json.dumps(
                {"item": {"institution_id": INST, "error": None}}))
        if path == "/institutions/get_by_id":
            return httpx.Response(200, text=json.dumps(
                {"institution": {"name": "Repeat Bank"}}))
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps(
                {"accounts": self.accounts.get(body["access_token"], [])}))
        if path == "/item/remove":
            self.removed.append(body["access_token"])
            return httpx.Response(200, text="{}")
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(
                {"added": [], "modified": [], "removed": [],
                 "has_more": False, "next_cursor": "c-1"}))
        return httpx.Response(400, text=json.dumps(
            {"error_type": "INVALID_REQUEST",
             "error_code": "PRODUCTS_NOT_SUPPORTED", "error_message": "n/a"}))

    def _patch(self):
        def for_tenant(conn, transport=None):
            return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                                transport=httpx.MockTransport(self._handler))
        return mock.patch.object(plaid.Client, "for_tenant",
                                 staticmethod(for_tenant))

    def _held_item(self, masks=("1111",), status=None, with_rows=True,
                   webhook_status=None):
        iid = f"pl-old-{uuid.uuid4().hex[:8]}"
        token = f"access-old-{iid}"
        sync_base.upsert_item(self.conn, iid, "plaid", "Repeat Bank", token)
        self.conn.execute("UPDATE items SET institution_id=%s, status=%s, "
                          "webhook_status=%s WHERE id=%s",
                          (INST, status, webhook_status, iid))
        self.accounts[token] = [_acct(m) for m in masks]
        if with_rows:
            for m in masks:
                self.conn.execute(
                    "INSERT INTO accounts (id, item_id, name, type, "
                    "subtype, mask) VALUES (%s,%s,'Checking','depository',"
                    "'checking',%s)", (f"{iid}-{m}", iid, m))
        return iid

    def _link(self):
        with self._patch():
            return plaid.link_item(self.conn, "pub-new")

    def _has(self, iid) -> bool:
        return self.conn.execute("SELECT 1 FROM items WHERE id=%s",
                                 (iid,)).fetchone() is not None

    def _ledgered(self) -> bool:
        return self._ledger_row() is not None

    def _ledger_row(self, iid=None):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT removed_reason, removed_at, credential_source "
                "FROM plaid_item_ledger WHERE item_id=%s",
                (iid or self.new_item,)).fetchone()
        finally:
            admin.close()

    def _hold_left(self) -> bool:
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT 1 FROM plaid_release_queue WHERE item_id=%s",
                (self.new_item,)).fetchone() is not None
        finally:
            admin.close()

    def test_a_repeat_of_a_held_login_is_handed_back(self):
        self._held_item()
        with self.assertRaises(ValueError) as cm:
            self._link()
        self.assertEqual(str(cm.exception), plaid.ALREADY_LINKED)
        self.assertFalse(self._has(self.new_item))
        self.assertEqual(self.removed, ["access-new"])
        row = self._ledger_row()
        self.assertEqual(row["removed_reason"], plaid.REPEAT_LEDGER_REASON)
        self.assertIsNotNone(row["removed_at"])
        self.assertFalse(self._hold_left())

    def _platform_keys(self):
        # the instance's own keys, so the ledger row is one the
        # institution cap would count
        return mock.patch.dict("os.environ",
                               {"OIKONOME_PLAID_CLIENT_ID": "cid"})

    def test_a_repeat_burns_a_ledger_row_but_no_place_under_the_cap(self):
        self._held_item()
        held_before = sync_base.institutions_held(self.conn)
        with self._platform_keys(), self.assertRaises(ValueError):
            self._link()
        row = self._ledger_row()
        self.assertEqual(row["credential_source"], "platform")
        self.assertEqual(row["removed_reason"], plaid.REPEAT_LEDGER_REASON)
        self.assertEqual(sync_base.released_count(self.conn), 0)
        self.assertEqual(sync_base.institutions_held(self.conn), held_before)

    def test_a_repeat_whose_hand_back_fails_stays_a_repeat_when_released(self):
        self._held_item()
        with self._platform_keys(), mock.patch.object(
                plaid.Client, "remove_item",
                side_effect=plaid.PlaidError(
                    {"error_code": "INTERNAL_SERVER_ERROR"}, 500)):
            with self.assertRaises(ValueError):
                self._link()
        self.assertTrue(self._hold_left())
        plaid.stamp_ledger_removed(self.new_item, "link-refused")
        self.assertEqual(self._ledger_row()["removed_reason"],
                         plaid.REPEAT_LEDGER_REASON)
        self.assertEqual(sync_base.released_count(self.conn), 0)

    def test_a_fourth_repeat_in_a_day_is_not_exchanged(self):
        self._held_item()
        with self._platform_keys():
            for _ in range(plaid.REPEAT_LIMIT):
                self.new_item = f"pl-new-{uuid.uuid4().hex[:8]}"
                self.minted.append(self.new_item)
                with self.assertRaises(ValueError):
                    self._link()
            self.assertEqual(len(self.exchanged), plaid.REPEAT_LIMIT)
            self.new_item = f"pl-new-{uuid.uuid4().hex[:8]}"
            with self.assertRaises(ValueError) as cm:
                self._link()
        self.assertEqual(str(cm.exception), plaid.TOO_MANY_REPEATS)
        self.assertEqual(len(self.exchanged), plaid.REPEAT_LIMIT)
        self.assertFalse(self._ledgered())
        self.assertEqual(sync_base.released_count(self.conn), 0)

    def test_a_different_bank_after_the_repeat_limit_is_not_called_a_repeat(self):
        """Past the limit no token is exchanged, so nothing knows whether
        the next bank is a repeat: it is told the limit, never that the
        bank is already connected."""
        self._held_item()
        with self._platform_keys():
            for _ in range(plaid.REPEAT_LIMIT):
                self.new_item = f"pl-new-{uuid.uuid4().hex[:8]}"
                self.minted.append(self.new_item)
                with self.assertRaises(ValueError):
                    self._link()
            self.new_item = f"pl-new-{uuid.uuid4().hex[:8]}"
            self.accounts["access-new"] = [_acct("9999")]
            with self.assertRaises(ValueError) as cm:
                self._link()
        self.assertEqual(str(cm.exception), plaid.TOO_MANY_REPEATS)
        self.assertNotEqual(str(cm.exception), plaid.ALREADY_LINKED)
        self.assertEqual(len(self.exchanged), plaid.REPEAT_LIMIT)

    def test_a_repeat_before_the_first_sync_landed_is_handed_back(self):
        self._held_item(with_rows=False)
        with self.assertRaises(ValueError):
            self._link()
        self.assertFalse(self._has(self.new_item))
        self.assertEqual(self.removed, ["access-new"])

    def test_another_login_at_the_same_bank_is_kept(self):
        self._held_item(masks=("2222",))
        self.assertEqual(self._link(), self.new_item)
        self.assertTrue(self._has(self.new_item))
        self.assertEqual(self.removed, [])

    def test_an_archived_connection_does_not_block_linking_again(self):
        self._held_item(status="archived")
        self.assertEqual(self._link(), self.new_item)
        self.assertTrue(self._has(self.new_item))

    def test_a_login_sharing_a_joint_account_but_bringing_its_own_is_kept(self):
        self._held_item(masks=("1111",))
        self.accounts["access-new"] = [_acct("1111"), _acct("3333")]
        self.assertEqual(self._link(), self.new_item)
        self.assertTrue(self._has(self.new_item))
        self.assertEqual(self.removed, [])
        self.assertTrue(self._ledgered())

    def test_a_repeat_of_a_login_holding_several_accounts_is_handed_back(self):
        self._held_item(masks=("1111", "3333"))
        self.accounts["access-new"] = [_acct("3333"), _acct("1111")]
        with self.assertRaises(ValueError) as cm:
            self._link()
        self.assertEqual(str(cm.exception), plaid.ALREADY_LINKED)
        self.assertEqual(self._ledger_row()["removed_reason"],
                         plaid.REPEAT_LEDGER_REASON)

    def test_a_connection_whose_consent_was_withdrawn_does_not_block(self):
        for status, hook in (("revoked", None),
                             ("error:USER_PERMISSION_REVOKED", None),
                             ("error:ACCESS_NOT_GRANTED", None),
                             (None, "revoked")):
            with self.subTest(status=status, webhook=hook):
                self.new_item = f"pl-new-{uuid.uuid4().hex[:8]}"
                self.conn.execute("DELETE FROM items")
                self._held_item(status=status, webhook_status=hook)
                self.assertEqual(self._link(), self.new_item)
                self.assertTrue(self._has(self.new_item))

    def test_a_connection_needing_sign_in_still_counts_as_held(self):
        self._held_item(status="error:ITEM_LOGIN_REQUIRED")
        with self.assertRaises(ValueError) as cm:
            self._link()
        self.assertEqual(str(cm.exception), plaid.ALREADY_LINKED)
        self.assertFalse(self._has(self.new_item))

    def test_a_first_link_landing_while_the_second_is_under_way_is_seen(self):
        real = plaid._repeat_inputs

        def first_tap_commits(*a, **k):
            ctx = real(*a, **k)
            # the first tap's row lands after the second's inputs were
            # gathered, before the second takes the lock
            self._held_item(with_rows=False)
            return ctx
        with mock.patch.object(plaid, "_repeat_inputs", first_tap_commits):
            with self.assertRaises(ValueError) as cm:
                self._link()
        self.assertEqual(str(cm.exception), plaid.ALREADY_LINKED)
        self.assertFalse(self._has(self.new_item))

    def test_plaid_is_not_called_under_the_link_lock(self):
        self._held_item(with_rows=False)
        self.locked = False
        calls = []
        handler = self._handler

        def recording(request):
            calls.append((request.url.path, self.locked))
            return handler(request)
        self._handler = recording
        real_lock = plaid.link_lock
        real_tx = self.conn.transaction
        depth = [0]
        taken = []

        def lock(conn):
            real_lock(conn)
            self.locked = True
            taken.append(depth[0])

        @contextlib.contextmanager
        def tx(*a, **k):
            depth[0] += 1
            try:
                with real_tx(*a, **k) as t:
                    yield t
            finally:
                depth[0] -= 1
                if depth[0] == 0:
                    self.locked = False
        with mock.patch.object(plaid, "link_lock", lock), \
                mock.patch.object(self.conn, "transaction", tx):
            with self.assertRaises(ValueError):
                self._link()
        self.assertEqual(
            [p for p, _ in calls if p == "/accounts/get"],
            ["/accounts/get", "/accounts/get"])
        self.assertTrue(taken and all(taken))
        self.assertEqual([p for p, locked in calls if locked], [])

    def test_the_session_answers_a_failed_add_saying_why(self):
        self._held_item()
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        sess = {"tenant_id": tid}
        with self._patch():
            reply = plaid_links.complete_add(
                self.conn, sess, f"lt-{uuid.uuid4().hex[:8]}",
                [{"results": {"item_add_results": [
                    {"public_token": "pub-new"}]}}])
        self.assertEqual(reply["kind"], "add_failed")
        self.assertEqual(reply["error"], plaid.ALREADY_LINKED)


if __name__ == "__main__":
    unittest.main()
