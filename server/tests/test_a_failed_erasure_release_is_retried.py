"""A Plaid Item that fails to release while its household is erased is
retried until it is gone.

An erasure (or a data reset) removes every Item at Plaid and then deletes
the household's rows — and the rows are where the access tokens live. A
single timeout at that moment used to leave the Item billing at Plaid with
no token anywhere on the instance, so nothing could ever remove it: the
per-household reaper only sees the rows of households that still exist.

The invariants:

* a release that fails is queued with its token encrypted, together with
  the keys the Item was made under;
* the nightly retry removes it (or accepts Plaid saying it is already
  gone), stamps the slot ledger and forgets the token;
* a retry that fails again is kept and counted, and given up after
  RELEASE_RETRY_DAYS — a token that no longer decrypts (sealed under a
  master key the instance has since lost) included, said at error level
  every night, never retried for good;
* an Item some household still holds is never released by the queue — a
  purge abandoned after its release keeps that connection.
"""

import datetime as dt
import json
import os
import unittest
import uuid
from unittest import mock

import httpx
from cryptography.fernet import Fernet

from oikonome.db import crypto, tenancy
from oikonome.jobs import reaper
from oikonome.sync import base as sync_base
from oikonome.sync import plaid

from .util import make_db, write_config


def _transport(state: dict):
    def handler(request):
        if request.url.path == "/item/remove":
            tok = json.loads(request.content.decode())["access_token"]
            state.setdefault("removed", []).append(tok)
            how = state.get("remove")
            if how == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if how == "gone":
                return httpx.Response(400, text=json.dumps(
                    {"error_type": "ITEM_ERROR",
                     "error_code": "ITEM_NOT_FOUND",
                     "error_message": "gone"}))
            return httpx.Response(200, text=json.dumps({"request_id": "r"}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


def _queued(item_id):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(
            "SELECT * FROM plaid_release_queue WHERE item_id=%s",
            (item_id,)).fetchone()
    finally:
        admin.close()


def _ledger(item_id, when=None):
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "INSERT INTO plaid_item_ledger (item_id, tenant) VALUES (%s, "
            "'t') ON CONFLICT DO NOTHING", (item_id,))
        return admin.execute(
            "SELECT removed_at, removed_reason FROM plaid_item_ledger "
            "WHERE item_id=%s", (item_id,)).fetchone()
    finally:
        admin.close()


class ReleaseQueueTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        write_config(self.conn, plaid_client_id="byo-cid",
                     plaid_secret="byo-sec")
        self.item = f"pl-q-{uuid.uuid4().hex[:10]}"
        self.token = f"access-sandbox-{uuid.uuid4().hex[:6]}"
        sync_base.upsert_item(self.conn, self.item, "plaid", "Q Bank",
                              self.token)
        _ledger(self.item)
        self.state = {}

    def tearDown(self):
        self.conn.close()
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM plaid_release_queue WHERE item_id=%s",
                          (self.item,))
        finally:
            admin.close()

    def _release_failing(self):
        self.state["remove"] = "timeout"

        def for_tenant(conn, transport=None):
            return plaid.Client("byo-cid", "byo-sec",
                                plaid.ENV_URLS["sandbox"],
                                transport=_transport(self.state))
        with mock.patch.object(plaid.Client, "for_tenant",
                               staticmethod(for_tenant)):
            n = plaid.release_tenant_items(self.tid)
        self.assertEqual(n.failed_ids, (self.item,))

    def _erase_rows(self):
        self.conn.execute("DELETE FROM items WHERE id=%s", (self.item,))

    def _retry(self, now=None):
        return reaper.retry_queued_releases(
            now=now, transport=_transport(self.state))

    def test_a_failed_release_is_queued_with_its_keys(self):
        self._release_failing()
        q = _queued(self.item)
        self.assertIsNotNone(q)
        self.assertEqual(q["client_id"], "byo-cid")
        self.assertEqual(q["base_url"], plaid.ENV_URLS["sandbox"])
        self.assertEqual(q["last_error"], plaid.TRANSPORT_CODE)
        self.assertEqual(crypto.decrypt_cp(q["access_token"]), self.token)
        self.assertEqual(crypto.decrypt_cp(q["secret"]), "byo-sec")
        if crypto.master_key():
            self.assertNotEqual(q["access_token"], self.token)

    def test_the_retry_releases_the_item_once_its_household_is_gone(self):
        self._release_failing()
        self._erase_rows()
        self.state["remove"] = None
        out = self._retry()
        self.assertIn(self.item, out["released"])
        self.assertIsNone(_queued(self.item))
        self.assertEqual(self.state["removed"][-1], self.token)
        self.assertIsNotNone(_ledger(self.item)["removed_at"])

    def test_an_item_already_gone_at_plaid_leaves_the_queue(self):
        self._release_failing()
        self._erase_rows()
        self.state["remove"] = "gone"
        self.assertIn(self.item, self._retry()["released"])
        self.assertIsNone(_queued(self.item))

    def test_a_retry_that_fails_again_is_kept_then_given_up(self):
        self._release_failing()
        self._erase_rows()
        out = self._retry()
        self.assertIn(self.item, out["kept"])
        q = _queued(self.item)
        self.assertEqual(q["attempts"], 1)
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            days=reaper.RELEASE_RETRY_DAYS + 1)
        with self.assertLogs("oikonome.jobs", level="ERROR"):
            out = self._retry(now=later)
        self.assertIn(self.item, out["given_up"])
        self.assertIsNone(_queued(self.item))

    def test_an_item_a_household_still_holds_is_never_released_by_it(self):
        self._release_failing()
        # the purge was abandoned: the household kept its rows
        self.state["remove"] = None
        self.state["removed"] = []
        out = self._retry()
        self.assertIn(self.item, out["dropped"])
        self.assertEqual(self.state["removed"], [])
        self.assertIsNone(_queued(self.item))
        self.assertEqual(sync_base.get_access_token(self.conn, self.item),
                         self.token)

    def test_a_token_no_longer_decryptable_is_given_up_not_kept_forever(self):
        prev = os.environ.get("OIKONOME_MASTER_KEY")
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        try:
            self._release_failing()
            self.assertTrue(_queued(self.item)["access_token"].startswith(
                crypto.CP_PREFIX))
            self._erase_rows()
            # the key it was sealed under is gone
            os.environ["OIKONOME_MASTER_KEY"] = \
                Fernet.generate_key().decode()
            self.state["remove"] = None
            self.state["removed"] = []
            with self.assertLogs("oikonome.jobs", level="ERROR"):
                out = self._retry()
            self.assertIn(self.item, out["kept"])
            self.assertEqual(self.state["removed"], [])
            q = _queued(self.item)
            self.assertEqual(q["attempts"], 1)
            self.assertEqual(q["last_error"], "UNDECRYPTABLE")
            later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                days=reaper.RELEASE_RETRY_DAYS + 1)
            with self.assertLogs("oikonome.jobs", level="ERROR"):
                out = self._retry(now=later)
            self.assertIn(self.item, out["given_up"])
            self.assertIsNone(_queued(self.item))
        finally:
            if prev is None:
                os.environ.pop("OIKONOME_MASTER_KEY", None)
            else:
                os.environ["OIKONOME_MASTER_KEY"] = prev


if __name__ == "__main__":
    unittest.main()
