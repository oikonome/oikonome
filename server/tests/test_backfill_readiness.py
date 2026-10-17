"""Historical-backfill readiness: Plaid delivers its two-year window
asynchronously after a link, so the setup wizard's bills-&-income step
waits until every Plaid item reports HISTORICAL_UPDATE_COMPLETE before it
runs detection. sync() records the status; /api/onboarding/backfill is the
gate the wizard polls. An errored item flags as stalled but never blocks
the step; items that predate the column count as ready once a day old."""

import json
import unittest
import uuid

import httpx
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import plaid

from .util import add_txn, _ensure_db, make_db, seed_accounts, write_config

ACCOUNTS = {"accounts": [
    {"account_id": "p-chk", "name": "Plaid Checking", "type": "depository",
     "subtype": "checking",
     "balances": {"current": 1000.0, "iso_currency_code": "USD"}}]}


def _transport(update_status: str):
    def handler(request):
        path = request.url.path
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps(ACCOUNTS))
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(
                {"added": [], "modified": [], "removed": [],
                 "has_more": False, "next_cursor": "c-end",
                 "transactions_update_status": update_status}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class SyncRecordsUpdateStatusTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES ('it-p','plaid','P Bank','tok-p')")

    def tearDown(self):
        self.conn.close()

    def test_sync_records_the_latest_update_status(self):
        plaid.sync(self.conn, "it-p",
                   transport=_transport("HISTORICAL_UPDATE_COMPLETE"))
        row = self.conn.execute(
            "SELECT tx_update_status FROM items WHERE id='it-p'").fetchone()
        self.assertEqual(row["tx_update_status"],
                         "HISTORICAL_UPDATE_COMPLETE")

    def test_a_missing_status_never_clears_a_recorded_one(self):
        """COALESCE, not overwrite: an older mocked page (or a Plaid
        response without the field) must not un-finish a finished item."""
        plaid.sync(self.conn, "it-p",
                   transport=_transport("HISTORICAL_UPDATE_COMPLETE"))

        def handler(request):
            if request.url.path == "/accounts/get":
                return httpx.Response(200, text=json.dumps(ACCOUNTS))
            return httpx.Response(200, text=json.dumps(
                {"added": [], "modified": [], "removed": [],
                 "has_more": False, "next_cursor": "c-2"}))
        plaid.sync(self.conn, "it-p",
                   transport=httpx.MockTransport(handler))
        row = self.conn.execute(
            "SELECT tx_update_status FROM items WHERE id='it-p'").fetchone()
        self.assertEqual(row["tx_update_status"],
                         "HISTORICAL_UPDATE_COMPLETE")


class BackfillEndpointTests(unittest.TestCase):
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
            "email": f"bf-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def setUp(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("DELETE FROM items WHERE aggregator='plaid'")
            # park the sync job as running so the endpoint's nudge never
            # spawns a real background sweep inside a test
            conn.execute(
                """INSERT INTO job_progress (id, state, progress)
                   VALUES ('sync', 'running', '{}'::jsonb)
                   ON CONFLICT (tenant_id, id) DO UPDATE SET
                       state='running', updated_at=now()""")
        finally:
            conn.close()

    def _item(self, iid, **cols):
        conn = tenancy.tenant_connect(self.tid)
        try:
            keys = {"aggregator": "plaid", "institution_name": "P Bank",
                    "access_token": "tok", **cols}
            names = ", ".join(["id"] + list(keys))
            ph = ", ".join(["%s"] * (1 + len(keys)))
            conn.execute(
                f"INSERT INTO items ({names}) VALUES ({ph})",
                [iid] + list(keys.values()))
        finally:
            conn.close()

    def test_no_plaid_items_is_ready(self):
        r = self.client.get("/api/onboarding/backfill").json()
        self.assertTrue(r["all_ready"])
        self.assertEqual(r["items"], [])

    def test_fresh_item_blocks_until_history_lands(self):
        self._item("it-new")
        r = self.client.get("/api/onboarding/backfill").json()
        self.assertFalse(r["all_ready"])
        self.assertFalse(r["items"][0]["ready"])
        self.assertTrue(r["syncing"])

    def test_reports_how_far_back_the_history_reaches(self):
        # the one honest progress signal a backfill has: Plaid never says
        # how many rows are coming, but the oldest date landed so far
        # tells the person the window is filling
        self._item("it-part")
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("UPDATE accounts SET item_id='it-part' "
                         "WHERE id='card'")
            add_txn(conn, "2025-02-14", 12.0, "OLDEST", account="card")
            add_txn(conn, "2026-01-05", 9.0, "NEWER", account="card")
        finally:
            conn.close()
        r = self.client.get("/api/onboarding/backfill").json()
        it = r["items"][0]
        self.assertEqual(it["transactions"], 2)
        self.assertEqual(it["earliest"], "2025-02-14")
        # the bar: how much of the two-year window the oldest row covers,
        # never full while the bank has not said the window is complete
        # (a bar parked at 100% mid-import reads as hung), and never
        # zero once a row has landed
        self.assertGreater(it["progress"], 0.3)
        self.assertLessEqual(it["progress"], 0.95)
        self.assertFalse(it["active"])
        self._item("it-empty")
        r = self.client.get("/api/onboarding/backfill").json()
        empty = next(i for i in r["items"] if i["id"] == "it-empty")
        self.assertIsNone(empty["earliest"])
        self.assertEqual(empty["progress"], 0.0)

    def test_an_item_fills_against_the_window_its_link_asked_for(self):
        """The window cannot be changed on an Item once it is made: one
        linked for two years keeps backfilling two years after the
        household picks three months. Measured against the household's new
        choice, three months of landed history would read as a full bar
        while most of that Item's history is still to come."""
        import datetime as dt
        from oikonome.engine import budget
        self._item("it-two-years", raw=json.dumps({"days_requested": 730}))
        self._item("it-before-record")          # predates the record: 730
        self._item("it-short", raw=json.dumps({"days_requested": 93}))
        start = (dt.date.today() - dt.timedelta(days=92)).isoformat()
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["history_months"] = 3
            # an account of each Item's own (the shared fixture accounts
            # would go with the Items when the next test clears them)
            for n, iid in enumerate(("it-two-years", "it-before-record",
                                     "it-short")):
                acct = f"acct-{iid}"
                conn.execute(
                    "INSERT INTO accounts (id, item_id, name, type, "
                    "subtype, balance_current) VALUES "
                    "(%s, %s, 'Window Checking', 'depository', 'checking', 0)",
                    (acct, iid))
                add_txn(conn, start, 10.0 + n, f"FIRST {n}", account=acct)
        finally:
            conn.close()
        try:
            r = self.client.get("/api/onboarding/backfill").json()
        finally:
            conn = tenancy.tenant_connect(self.tid)
            try:
                with budget.config_txn(conn) as cfg:
                    cfg.pop("history_months", None)
            finally:
                conn.close()
        by = {i["id"]: i for i in r["items"]}
        self.assertFalse(r["all_ready"])
        for iid in ("it-two-years", "it-before-record"):
            self.assertFalse(by[iid]["ready"], iid)
            self.assertLess(by[iid]["progress"], 0.2, iid)
        # an Item linked under the short window is as far along as it gets
        # before the bank says complete
        self.assertEqual(by["it-short"]["progress"], 0.95)

    def test_progress_is_full_only_when_the_bank_says_complete(self):
        self._item("it-done", tx_update_status="HISTORICAL_UPDATE_COMPLETE")
        r = self.client.get("/api/onboarding/backfill").json()
        it = next(i for i in r["items"] if i["id"] == "it-done")
        self.assertEqual(it["progress"], 1.0)
        self.assertFalse(it["active"])

    def test_historical_complete_is_ready(self):
        self._item("it-done",
                   tx_update_status="HISTORICAL_UPDATE_COMPLETE")
        r = self.client.get("/api/onboarding/backfill").json()
        self.assertTrue(r["all_ready"])
        self.assertTrue(r["items"][0]["ready"])

    def test_errored_item_is_stalled_but_never_blocks(self):
        self._item("it-broken", status="error:ITEM_LOGIN_REQUIRED")
        r = self.client.get("/api/onboarding/backfill").json()
        self.assertTrue(r["all_ready"],
                        "a broken connection must not strand the wizard")
        self.assertTrue(r["items"][0]["stalled"])
        self.assertFalse(r["items"][0]["ready"])

    def test_pre_column_item_counts_as_ready_once_old(self):
        self._item("it-old")
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("UPDATE items SET linked_at=now()-interval '2 days'"
                         " WHERE id='it-old'")
        finally:
            conn.close()
        r = self.client.get("/api/onboarding/backfill").json()
        self.assertTrue(r["all_ready"])


class BackfillPollIsReadOnlyTests(unittest.TestCase):
    """The status GET is polled every few seconds by two clients and is
    reachable by a family viewer and by a cross-site top-level GET (a
    SameSite=Lax cookie rides along). It must therefore never start a
    sync — pulling transactions, moving cursors and spending aggregator
    quota is a POST, owner-only, and refused on the demo."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.appobj = app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"bfro-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                """INSERT INTO items (id, aggregator, institution_name,
                                      access_token)
                   VALUES ('it-wait', 'plaid', 'P Bank', 'tok')""")
            # its first pull finished a while ago; the rest of the history
            # is still on its way
            conn.execute(
                "INSERT INTO sync_log (item_id, added, ran_at) "
                "VALUES ('it-wait', 0, now() - interval '5 minutes')")
        finally:
            conn.close()

    def setUp(self):
        # no sync running, so a nudge WOULD claim the job row
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("DELETE FROM job_progress WHERE id='sync'")
        finally:
            conn.close()

    def _sync_state(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT state FROM job_progress WHERE id='sync'").fetchone()
            return row["state"] if row else None
        finally:
            conn.close()

    def test_status_get_never_starts_a_sync(self):
        from unittest import mock
        with mock.patch("oikonome.jobs.worker.sync_tenant") as st:
            r = self.client.get("/api/onboarding/backfill")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertFalse(body["items"][0]["ready"])
            self.assertFalse(body["syncing"])
            self.assertIsNone(self._sync_state())
            st.assert_not_called()

    def test_the_nudge_queues_a_pull_on_the_worker_and_pulls_nothing_here(self):
        from unittest import mock

        from oikonome.jobs import worker
        queued = []
        with mock.patch("oikonome.jobs.worker.sync_tenant") as st, \
                mock.patch("oikonome.jobs.worker._sync_item_body") as body, \
                mock.patch.object(worker, "BACKFILL_NUDGE_WINDOW_S",
                                  10 ** 9), \
                mock.patch.object(
                    worker, "_enqueue_in_background",
                    lambda what, job, *a, job_id=None:
                        queued.append((job, a, job_id))):
            r = self.client.post("/api/onboarding/backfill/nudge")
            self.assertEqual(r.status_code, 200, r.text)
            body_json = r.json()
            self.assertEqual(set(body_json), {"items", "all_ready",
                                              "syncing"})
            self.assertTrue(body_json["syncing"])
            # a second nudge within the minute names the same job
            self.client.post("/api/onboarding/backfill/nudge")
        st.assert_not_called()
        body.assert_not_called()
        self.assertEqual([q[0] for q in queued], ["sync_item", "sync_item"])
        self.assertEqual(queued[0][1], (str(self.tid), "it-wait", False))
        self.assertEqual(queued[0][2], queued[1][2])
        # the web process claimed nothing it would have to settle
        self.assertIsNone(self._sync_state())

    def test_nudge_is_owner_only(self):
        inv = self.client.post("/api/invites",
                               json={"label": "fam",
                                     "password": "correct-horse-battery"})
        self.assertEqual(inv.status_code, 200, inv.text)
        token = inv.json()["url"].rsplit("token=", 1)[1]
        viewer = TestClient(self.appobj)
        r = viewer.post("/api/invite/claim", json={
            "token": token,
            "email": f"fam-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(viewer.get("/api/me").json()["role"], "viewer")
        r = viewer.post("/api/onboarding/backfill/nudge")
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self._sync_state())
        # the viewer may still read the status
        self.assertEqual(
            viewer.get("/api/onboarding/backfill").status_code, 200)


class WizardGateWiringTests(unittest.TestCase):
    """The SPA side: step 2's detection body must mount behind the gate."""

    def _src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2]
                / "webapp" / "src" / name).read_text(encoding="utf-8")

    def test_step_two_waits_for_the_backfill(self):
        w = self._src("pages/Welcome.tsx")
        # the auto pane carries the scan; it must not mount until the gate opens
        self.assertIn("<BackfillGate><AutoBillsPane /></BackfillGate>", w,
                      "detection must not mount until the gate opens")
        self.assertIn("onboardingBackfill", w)

    def test_gate_fails_open(self):
        w = self._src("pages/Welcome.tsx")
        self.assertIn("q.isError", w,
                      "an endpoint error must never strand the wizard")

    def test_footer_waits_for_the_backfill_too(self):
        # gating the body alone is not enough: Continue / Skip in the
        # footer would let a second tab (or an impatient click) advance
        # on the 30-day sliver and seed income, bills and the budget from
        # incomplete history. The bills step's footer must derive both
        # from the same readiness the gate reads (all_ready, failing open
        # on error) — the mechanism, not a label.
        import pathlib
        import re
        w = self._src("pages/Welcome.tsx")
        m = re.search(r"const backfillReady = (.*?);", w, re.S)
        self.assertIsNotNone(m, "the wizard must compute backfillReady")
        self.assertIn("isError", m.group(1))
        self.assertIn("all_ready", m.group(1))
        bills = re.search(r"label: \"Confirm bills & income\".*?body:", w,
                          re.S)
        self.assertIsNotNone(bills)
        self.assertRegex(bills.group(0),
                         r"primary: backfillReady \? \{[^}]*\} : null")
        self.assertIn("skippable: backfillReady", bills.group(0))
        # the same rule on mobile
        mw = (pathlib.Path(__file__).resolve().parents[2] / "mobile" / "src" / "app"
              / "welcome.tsx").read_text(encoding="utf-8")
        self.assertIn("skippable: backfillReady", mw)

    def test_auto_scan_waits_for_the_first_bills_load(self):
        # the scan must wait for q.isSuccess: fired on mount it would race
        # the page's first GET /api/bills, its invalidate would fold into
        # that in-flight request (react-query never cancels a first fetch),
        # and the wizard would report found bills over an empty list.
        import re
        b = self._src("pages/Bills.tsx")
        m = re.search(r"if \(autoDetect && (.*?)\) \{\s*autoRan\.current = true", b,
                      re.S)
        self.assertIsNotNone(m, "the auto-scan guard is gone")
        self.assertIn("q.isSuccess", m.group(1))
        # the wizard's own auto pane runs the same scan under the same guard
        wb = self._src("components/WizardBills.tsx")
        self.assertRegex(wb, r"if \(q\.isSuccess && !ran\.current\)")

    def test_gate_keeps_nudging_while_it_waits(self):
        # one nudge on mount is not enough: the link-time pull is still
        # too recent then, so the server declines, and without a webhook
        # (Sandbox, self-host) nothing pulls again — the bar stays at its
        # first fill. Both clients repeat while waiting.
        import pathlib
        w = self._src("pages/Welcome.tsx")
        self.assertIn("setInterval", w.split("function BackfillGate")[1]
                      .split("if (q.isError")[0])
        mw = (pathlib.Path(__file__).resolve().parents[2] / "mobile" / "src"
              / "app" / "welcome.tsx").read_text(encoding="utf-8")
        self.assertIn("backfillWaiting", mw)
        self.assertIn("setInterval", mw)

    def test_gate_has_no_scan_anyway_escape(self):
        # everything downstream (income, bills, the
        # pre-filled budget) derives from the full history, so the step
        # waits for it — a bypass produced wrong budgets, not faster ones
        w = self._src("pages/Welcome.tsx")
        self.assertNotIn("setBypass", w)
        self.assertNotIn("scan now with what", w)
