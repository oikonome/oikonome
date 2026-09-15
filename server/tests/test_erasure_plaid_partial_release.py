"""A Plaid Item that fails to release at erasure leaves a retry handle.

Erasure releases Plaid Items best-effort, then wipes the rows — and the
rows are where the access tokens live. The release used to report only a
success count, so a Plaid outage at delete-time read as "released 0", the
wipe went ahead, and the Items kept billing with nothing in the database
to find them by. Now a shortfall (fewer released than held, or the call
blowing up) counts as a failed service: the pending-erasure marker is
written and names the Item ids that did not release, which is exactly what
an operator removes from the Plaid dashboard.
"""

import json
import unittest
import uuid
from unittest import mock

import httpx

from oikonome.db import tenancy
from oikonome.sync import base as sync_base
from oikonome.sync import plaid

from .util import make_db, write_config


def _flaky_transport(fail_tokens: set):
    def handler(request):
        if request.url.path == "/item/remove":
            tok = json.loads(request.content.decode())["access_token"]
            if tok in fail_tokens:
                return httpx.Response(500, text=json.dumps(
                    {"error_code": "INTERNAL_SERVER_ERROR"}))
            return httpx.Response(200, text=json.dumps({"request_id": "r"}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class PlaidPartialReleaseTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec")
        self.good = f"pl-ok-{uuid.uuid4().hex[:8]}"
        self.bad = f"pl-bad-{uuid.uuid4().hex[:8]}"
        sync_base.upsert_item(self.conn, self.good, "plaid", "A", "tok-good")
        sync_base.upsert_item(self.conn, self.bad, "plaid", "B", "tok-bad")

    def tearDown(self):
        self.conn.close()

    def _patch_client(self, fail_tokens):
        def for_tenant(conn, transport=None):
            return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                                transport=_flaky_transport(fail_tokens))
        return mock.patch.object(plaid.Client, "for_tenant",
                                 staticmethod(for_tenant))

    def _pending(self):
        admin = tenancy.admin_connect()
        try:
            rows = admin.execute(
                "SELECT detail FROM admin_audit WHERE "
                "action='external_release_pending' AND target=%s",
                (self.tid,)).fetchall()
        finally:
            admin.close()
        return [r["detail"] if isinstance(r["detail"], dict)
                else json.loads(r["detail"]) for r in rows]

    def test_release_count_names_the_items_that_did_not_release(self):
        with self._patch_client({"tok-bad"}):
            n = plaid.release_tenant_items(self.tid)
        self.assertEqual(n, 1)                 # still counts as an int
        self.assertEqual(n.expected, 2)
        self.assertEqual(n.failed_ids, (self.bad,))



    def test_a_db_failure_during_release_propagates_instead_of_reading_as_zero(self):
        """A tenant connection that cannot even be opened must not come
        back as ReleaseCount(0) with expected=0 — that is indistinguishable
        from 'the tenant held no Items', so the erasure wiped the rows (and
        the only access tokens) with no failed-service marker while the
        Items kept billing."""
        with mock.patch.object(tenancy, "tenant_connect",
                               side_effect=RuntimeError("pool down")):
            with self.assertRaises(RuntimeError):
                plaid.release_tenant_items(self.tid)




if __name__ == "__main__":
    unittest.main()
