"""Job task bodies (redis-free): tenant-scoped sync with heartbeats,
nightly detection, email build, per-item failure isolation."""

import datetime as dt
import json
import unittest
from unittest import mock

import httpx

from oikonome.jobs import worker
from oikonome.sync import base as sync_base

from .util import TODAY, add_bill, add_txn, make_db, write_config
from .test_simplefin import PAYLOAD


def _transport(fail_ids=()):
    def handler(request):
        return httpx.Response(200, text=json.dumps(PAYLOAD))
    return httpx.MockTransport(handler)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def tearDown(self):
        self.conn.close()

    def test_sync_tenant_pulls_and_heartbeats(self):
        sync_base.upsert_item(self.conn, "sfin-1", "simplefin", "Demo",
                              "https://u:p@bridge.test/simplefin")
        real_sync = worker.simplefin.sync
        with mock.patch.object(
                worker.simplefin, "sync",
                side_effect=lambda conn, iid, tok, since=None: real_sync(
                    conn, iid, tok, since=since, transport=_transport())):
            out = worker.sync_tenant(self.tid)
        self.assertTrue(out["sfin-1"].startswith("ok:"))
        hb = self.conn.execute(
            "SELECT job FROM job_runs WHERE job='sync'").fetchone()
        self.assertIsNotNone(hb)

    def test_sync_tenant_runs_inline_categorize(self):
        # sync_tenant categorizes what it just pulled, so onboarding
        # + hourly sweeps don't wait for the nightly run.
        from oikonome.engine import llm_categorize
        sync_base.upsert_item(self.conn, "sfin-1", "simplefin", "Demo",
                              "https://u:p@bridge.test/simplefin")
        real_sync = worker.simplefin.sync
        with mock.patch.object(
                worker.simplefin, "sync",
                side_effect=lambda conn, iid, tok, since=None: real_sync(
                    conn, iid, tok, since=since, transport=_transport())), \
             mock.patch.object(
                llm_categorize, "categorize_new",
                return_value={"merchants_classified": 2, "rows_updated": 3}
             ) as spy:
            out = worker.sync_tenant(self.tid)
        spy.assert_called_once()
        self.assertEqual(out["categorize"], "+2m/3rows")

    def test_sync_tenant_survives_categorize_failure(self):
        # a categorization blow-up must never fail the sync itself
        from oikonome.engine import llm_categorize
        sync_base.upsert_item(self.conn, "sfin-1", "simplefin", "Demo",
                              "https://u:p@bridge.test/simplefin")
        real_sync = worker.simplefin.sync
        with mock.patch.object(
                worker.simplefin, "sync",
                side_effect=lambda conn, iid, tok, since=None: real_sync(
                    conn, iid, tok, since=since, transport=_transport())), \
             mock.patch.object(llm_categorize, "categorize_new",
                               side_effect=RuntimeError("model down")):
            out = worker.sync_tenant(self.tid)
        self.assertTrue(out["sfin-1"].startswith("ok:"))   # sync still ok
        self.assertNotIn("categorize", out)

    def test_sync_tenant_isolates_item_failures(self):
        sync_base.upsert_item(self.conn, "sfin-bad", "simplefin", "Demo",
                              "https://u:p@bridge.test/simplefin")
        def boom(conn, iid, tok, since=None):
            raise RuntimeError("bridge down")
        with mock.patch.object(worker.simplefin, "sync", side_effect=boom):
            out = worker.sync_tenant(self.tid)
        self.assertEqual(out["sfin-bad"], "error:RuntimeError")
        # NO heartbeat on a failing sweep — the freshness watchdog must fire
        hb = self.conn.execute(
            "SELECT 1 FROM job_runs WHERE job='sync'").fetchone()
        self.assertIsNone(hb)

    def test_nightly_tenant_runs_detection(self):
        # anchor to the REAL today — detection runs against date.today(), and
        # its freshness gate (last charge within ~1.5 cycles) drops a series
        # whose most recent hit is stale, so a fixed-TODAY fixture rots as the
        # calendar advances. 30-day spacing → monthly cadence, last hit 30d ago.
        anchor = dt.date.today()
        for k in range(5, 0, -1):
            add_txn(self.conn, anchor - dt.timedelta(days=30 * k), 11.99,
                    "SPOTIFY")
        stats = worker.nightly_tenant(self.tid)
        self.assertGreaterEqual(stats["proposed_add"], 1)
        hb = self.conn.execute(
            "SELECT note FROM job_runs WHERE job='nightly-detect'").fetchone()
        self.assertIn("proposals", hb["note"])

    def test_email_tenant_builds_subject(self):
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_txn(self.conn, dt.date.today(), 42.5, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        subject = worker.email_tenant(self.tid, send_it=False)
        self.assertTrue(subject.startswith(("🔴", "🟡", "🟢")))


if __name__ == "__main__":
    unittest.main()
