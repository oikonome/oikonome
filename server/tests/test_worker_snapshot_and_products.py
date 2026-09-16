"""Two worker sweeps: the nightly net-worth snapshot (which must run from
an instance's first day, or the trend has no history to draw) and the
~daily Plaid liabilities/holdings cadence riding the hourly sync sweep."""

import asyncio
import datetime as dt
import unittest
from unittest import mock

from oikonome.jobs import worker

from .util import make_db, write_config


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def tearDown(self):
        self.conn.close()

    def test_snapshot_tenant_records_todays_networth(self):
        out = worker.snapshot_tenant(self.tid)
        self.assertTrue(out["snapshot"])
        row = self.conn.execute(
            "SELECT total FROM networth_snapshot WHERE date=%s",
            (dt.date.today(),)).fetchone()       # the job's clock, not pg's
        self.assertIsNotNone(row)
        # fixture: checking $5,000 − card $250
        self.assertEqual(row["total"], 4750.0)
        hb = self.conn.execute("SELECT 1 FROM job_runs "
                               "WHERE job='networth-snapshot'").fetchone()
        self.assertIsNotNone(hb)

    def test_nightly_all_sweeps_detection_then_snapshot(self):
        """The snapshot is its own sweep: one tenant's detection failure
        must never skip its snapshot (and vice versa)."""
        swept = []
        with mock.patch.object(
                worker, "_sweep",
                side_effect=lambda fn, label: swept.append((fn, label)) or {}):
            out = asyncio.run(worker.nightly_all({}))
        from oikonome.jobs import reaper
        self.assertEqual([s[0] for s in swept],
                         [worker.nightly_tenant, worker.snapshot_tenant,
                          worker.script_alert_tenant, reaper.reap_tenant])
        self.assertEqual(set(out), {"detect", "snapshot", "script_alerts", "purged",
                                    "plaid_reap", "frozen"})


class ProductsCadenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token, status) VALUES "
            "('pl-1','plaid','Demo Bank','enc-tok','ok')")

    def tearDown(self):
        self.conn.close()

    def test_products_due_gate(self):
        self.assertTrue(worker._products_due(self.conn))     # never ran
        from oikonome.engine import alerts
        alerts.heartbeat(self.conn, "plaid-products", "test")
        self.assertFalse(worker._products_due(self.conn))    # just ran
        stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=21)
        self.conn.execute("UPDATE job_runs SET ran_at=%s "
                          "WHERE job='plaid-products'", (stale,))
        self.assertTrue(worker._products_due(self.conn))     # >20h → due

    def test_sync_tenant_runs_products_daily_not_hourly(self):
        with mock.patch.object(worker.plaid, "sync",
                               return_value={"added": 0}), \
             mock.patch.object(
                 worker.plaid, "sync_products",
                 return_value={"liabilities": 1, "holdings": 2}) as prod:
            worker.sync_tenant(self.tid)                     # due: runs
            prod.assert_called_once()
            self.assertEqual(prod.call_args.args[1], "pl-1")
            hb = self.conn.execute(
                "SELECT note FROM job_runs WHERE job='plaid-products'"
            ).fetchone()
            self.assertIn("1 liabilities", hb["note"])
            worker.sync_tenant(self.tid)                     # not due: skipped
            prod.assert_called_once()

    def test_products_skip_broken_items_and_stay_due_on_failure(self):
        def fail(conn, item_id):
            raise RuntimeError("plaid down")
        with mock.patch.object(worker.plaid, "sync",
                               return_value={"added": 0}), \
             mock.patch.object(worker.plaid, "sync_products",
                               side_effect=fail):
            worker.sync_tenant(self.tid)
        # product pull failed → NO heartbeat → next hourly sweep retries
        hb = self.conn.execute("SELECT 1 FROM job_runs "
                               "WHERE job='plaid-products'").fetchone()
        self.assertIsNone(hb)

    def test_products_not_attempted_when_txn_sync_failed(self):
        def fail(conn, item_id):
            raise RuntimeError("boom")
        with mock.patch.object(worker.plaid, "sync", side_effect=fail), \
             mock.patch.object(worker.plaid, "sync_products") as prod:
            worker.sync_tenant(self.tid)
        prod.assert_not_called()
        # heartbeat still written: the tenant WAS swept, every healthy item
        # (none) succeeded — an all-broken tenant must not retry hourly
        hb = self.conn.execute("SELECT 1 FROM job_runs "
                               "WHERE job='plaid-products'").fetchone()
        self.assertIsNotNone(hb)


if __name__ == "__main__":
    unittest.main()
