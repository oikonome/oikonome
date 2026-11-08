"""A new bank link records the history window its link token asked for.

Plaid fixes an Item's backfill when the link token is created. A history
choice saved while Link is open must not change what the Item is recorded
with: recorded narrower than its backfill, the Item would be offered a
"relink for more history" it already has, and every such relink is a new
Item the operator pays for — one that could be repeated without end by
narrowing the choice each time.

* every door that finishes a session (the poll, the worker) records the
  window kept with the session, whatever the setting says by then;
* an Item so recorded is not offered the relink again;
* a session with no window on record records the widest window, never
  the setting read at the exchange, which may be narrower than the
  backfill.
"""

import time
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.jobs import plaid_links
from oikonome.sync import plaid
from oikonome.web import pages

from .test_a_finished_bank_link_completes_without_a_client import _patch
from .util import _ensure_db, seed_accounts, write_config


class LinkWindowKeptWithTheSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"plw-{uuid.uuid4().hex[:8]}@example.dev",
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
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM plaid_link_sessions WHERE tenant=%s",
                          (str(self.tid),))
        finally:
            admin.close()
        self.lt = f"lt-{uuid.uuid4().hex[:10]}"
        self.state = {"link_token": self.lt, "link_done": True,
                      "item_id": f"pl-{uuid.uuid4().hex[:10]}"}

    def tearDown(self):
        self._choose(None)

    def _choose(self, months):
        r = self.client.post("/api/settings",
                             json={"history_months": months})
        self.assertEqual(r.status_code, 200, r.text)

    def _window(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT raw->'days_requested' AS d FROM items WHERE id=%s",
                (self.state["item_id"],)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "the link saved no Item")
        return row["d"]

    def test_the_worker_records_the_window_the_token_asked_for(self):
        # the token asked for twelve months; the choice narrowed to one
        # while Link was open
        self.assertTrue(plaid_links.record(self.tid, self.lt, "add", None,
                                           False, None, days_requested=372))
        self._choose(1)
        with _patch(self.state):
            self.assertEqual(plaid_links.finish_session(self.tid, self.lt),
                             "done")
        self.assertEqual(self._window(), 372)
        # widened back to twelve months, the Item already has them: no
        # relink is offered for it
        self._choose(12)
        with _patch(self.state):
            r = self.client.post(
                "/accounts/plaid/link",
                data={"relink_item_id": self.state["item_id"]},
                headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("already has", r.json()["detail"])

    def test_the_poll_records_the_window_on_record(self):
        # the web process's own entry carries no window: the record does
        self.assertTrue(plaid_links.record(self.tid, self.lt, "add", None,
                                           False, None, days_requested=186))
        pages._PLAID_LINK[self.lt] = {
            "tenant_id": str(self.tid), "item_id": None, "kind": "add",
            "manage_accounts": False, "institution": None,
            "at": time.time(), "url": None, "done": False,
            "relink_of": None}
        self._choose(1)
        with _patch(self.state):
            s = self.client.get(f"/accounts/plaid/link/{self.lt}/status")
        self.assertEqual(s.status_code, 200, s.text)
        self.assertEqual(s.json()["kind"], "add", s.text)
        self.assertEqual(self._window(), 186)

    def test_a_session_with_no_window_on_record_records_the_widest(self):
        self.assertTrue(plaid_links.record(self.tid, self.lt, "add", None,
                                           False, None))
        self._choose(1)
        with _patch(self.state):
            self.assertEqual(plaid_links.finish_session(self.tid, self.lt),
                             "done")
        self.assertEqual(self._window(), plaid.DAYS_REQUESTED)


if __name__ == "__main__":
    unittest.main()
