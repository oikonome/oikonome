"""Item-slot accounting. Plaid consumes a slot per Item CREATED and never
refunds it, so the burn count is an append-only ledger (plaid_item_ledger)
that survives archiving AND tenant deletion — never a live-rows count.
Production Items cost money whether or not any UI renders a cap, so the
row must outlive item archival and tenant deletion."""

import json
import os
import unittest
import uuid

import httpx
import psycopg

from oikonome.db import tenancy
from oikonome.sync import plaid

from .util import _admin_dsn, TEST_DB, make_db, write_config

TXN_PAGE = {"added": [], "modified": [], "removed": [],
            "has_more": False, "next_cursor": "c-end"}


def _transport(item_id: str):
    def handler(request):
        path = request.url.path
        if path == "/item/public_token/exchange":
            return httpx.Response(200, text=json.dumps(
                {"item_id": item_id, "access_token": "access-prod-abc"}))
        if path == "/item/get":
            return httpx.Response(200, text=json.dumps(
                {"item": {"institution_id": "ins_1"}}))
        if path == "/institutions/get_by_id":
            return httpx.Response(200, text=json.dumps(
                {"institution": {"name": "Demo Bank"}}))
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps({"accounts": []}))
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(TXN_PAGE))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)



def _burn() -> dict:
    """Ledger burn + live/errored counts, read straight from the tables.

    The cap arithmetic belongs to whatever UI renders an allowance; the
    ledger itself matters regardless — production Items cost money and
    the row must survive item and tenant deletion — so its invariants are
    covered here.
    """
    admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
    try:
        created = admin.execute(
            "SELECT count(*) AS n FROM plaid_item_ledger "
            "WHERE environment = 'production' "
            "AND credential_source = 'platform'").fetchone()["n"]
        row = admin.execute(
            """SELECT count(*) FILTER (WHERE COALESCE(status,'ok')
                          != 'archived') AS live,
                      count(*) FILTER (WHERE COALESCE(status,'ok')
                          != 'archived' AND (status LIKE 'error:%%'
                              OR webhook_status LIKE 'error:%%')) AS errored
               FROM items WHERE aggregator = 'plaid'""").fetchone()
        return {"created": created, "live": row["live"],
                "errored": row["errored"]}
    finally:
        admin.close()

class SlotLedgerTests(unittest.TestCase):
    ENV_KEYS = ("OIKONOME_PLAID_CLIENT_ID", "OIKONOME_PLAID_SECRET",
                "OIKONOME_PLAID_ENV")

    def setUp(self):
        self.conn = make_db()
        self._env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        self.conn.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _link_platform_prod(self) -> str:
        """Link an Item under PLATFORM env credentials in production —
        the shape that burns a platform slot."""
        os.environ.update(OIKONOME_PLAID_CLIENT_ID="cid",
                          OIKONOME_PLAID_SECRET="sec",
                          OIKONOME_PLAID_ENV="production")
        item_id = f"pl-slot-{uuid.uuid4().hex[:10]}"
        plaid.link_item(self.conn, "public-x", transport=_transport(item_id))
        return item_id

    def _ledger(self, item_id: str):
        return self.conn.execute(
            "SELECT * FROM plaid_item_ledger WHERE item_id=%s",
            (item_id,)).fetchone()

    def test_platform_link_appends_production_platform_row(self):
        item_id = self._link_platform_prod()
        row = self._ledger(item_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["environment"], "production")
        self.assertEqual(row["credential_source"], "platform")

    def test_byo_link_recorded_as_byo(self):
        """A tenant's own keys burn THEIR Plaid slots, not the platform's —
        the platform burn count must not include them."""
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        item_id = f"pl-slot-{uuid.uuid4().hex[:10]}"
        plaid.link_item(self.conn, "public-x", transport=_transport(item_id))
        row = self._ledger(item_id)
        self.assertEqual(row["credential_source"], "byo")
        self.assertEqual(row["environment"], "sandbox")

    def test_burn_survives_archive_and_tenant_deletion(self):
        item_id = self._link_platform_prod()
        base = _burn()
        # disconnect archives the item row: live drops, burn does NOT
        self.conn.execute("UPDATE items SET status='archived' WHERE id=%s",
                          (item_id,))
        after = _burn()
        self.assertEqual(after["created"], base["created"])
        self.assertEqual(after["live"], base["live"] - 1)
        # tenant deletion cascades the items row away entirely —
        # the ledger row (and so the burn count) must survive
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            tenancy.delete_tenant_rows(admin, tid)
        finally:
            admin.close()
        gone = _burn()
        self.assertEqual(gone["created"], base["created"])
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            self.assertIsNotNone(admin.execute(
                "SELECT 1 FROM plaid_item_ledger WHERE item_id=%s",
                (item_id,)).fetchone())
            self.assertIsNone(admin.execute(
                "SELECT 1 FROM items WHERE id=%s", (item_id,)).fetchone())
        finally:
            admin.close()

    def test_burn_and_live_counts_track_the_ledger(self):
        """The counts any cap arithmetic would rest on have to be right."""
        base = _burn()
        item_id = self._link_platform_prod()
        after = _burn()
        self.assertEqual(after["created"], base["created"] + 1)
        self.assertEqual(after["live"], base["live"] + 1)
        # an errored / webhook-flagged item shows in the errored count
        self.conn.execute(
            "UPDATE items SET webhook_status='error:ITEM_LOGIN_REQUIRED' "
            "WHERE id=%s", (item_id,))
        self.assertEqual(_burn()["errored"], after["errored"] + 1)

    def test_sandbox_items_do_not_burn(self):
        os.environ.update(OIKONOME_PLAID_CLIENT_ID="cid",
                          OIKONOME_PLAID_SECRET="sec",
                          OIKONOME_PLAID_ENV="sandbox")
        base = _burn()
        item_id = f"pl-slot-{uuid.uuid4().hex[:10]}"
        plaid.link_item(self.conn, "public-x", transport=_transport(item_id))
        self.assertEqual(_burn()["created"],
                         base["created"])

    def test_ledger_is_append_only_for_app_role(self):
        """The monotonic guarantee is enforced at the GRANT level: the app
        role can insert and read but never update or delete."""
        item_id = self._link_platform_prod()
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.conn.execute(
                "DELETE FROM plaid_item_ledger WHERE item_id=%s", (item_id,))
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.conn.execute(
                "UPDATE plaid_item_ledger SET environment='sandbox' "
                "WHERE item_id=%s", (item_id,))


if __name__ == "__main__":
    unittest.main()
