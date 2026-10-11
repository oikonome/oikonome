"""Import staging in Postgres.

Three flows park an upload between requests (bulk plan, CSV mapping,
taxdoc preview). Staging lives in the RLS-bound import_staging table:
tenant binding is the database's job, so a token minted under tenant A
never commits rows under tenant B's connection; any worker can finish what
another started; eviction is an explicit TTL plus a per-tenant byte budget
(without one, an owner could park gigabytes of "pending" uploads on a
shared node).
"""

import unittest
import uuid
from unittest import mock

from oikonome.db import staging, tenancy
from oikonome.engine import taxdocs
from oikonome.web import bulk_import

from .bulk_job_util import submit_and_run
from .util import make_db, write_config


def _second_tenant():
    admin = tenancy.admin_connect()
    try:
        tid = tenancy.create_tenant(admin, f"stg-{uuid.uuid4().hex[:8]}")
    finally:
        admin.close()
    return tenancy.tenant_connect(tid)


class StagingStoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_put_pop_round_trip(self):
        token = staging.put(self.conn, "bulk",
                            files=[("a.csv", b"one"), ("b.ofx", b"two")],
                            meta={"x": 1})
        out = staging.pop(self.conn, "bulk", token)
        self.assertEqual(out["files"], [("a.csv", b"one"), ("b.ofx", b"two")])
        self.assertEqual(out["meta"], {"x": 1})
        # consumed — a second pop is gone
        self.assertIsNone(staging.pop(self.conn, "bulk", token))

    def test_kind_mismatch_is_a_miss(self):
        token = staging.put(self.conn, "csv", files=[("f.csv", b"x")])
        self.assertIsNone(staging.pop(self.conn, "taxdoc", token))
        self.assertIsNotNone(staging.pop(self.conn, "csv", token))

    def test_another_tenants_token_is_invisible(self):
        token = staging.put(self.conn, "bulk", files=[("f.csv", b"secret")])
        other = _second_tenant()
        try:
            self.assertIsNone(staging.pop(other, "bulk", token))
        finally:
            other.close()
        # and the rightful owner still holds it
        self.assertIsNotNone(staging.pop(self.conn, "bulk", token))

    def test_ttl_expires_stale_stages(self):
        token = staging.put(self.conn, "bulk", files=[("f.csv", b"x")])
        self.conn.execute(
            "UPDATE import_staging SET created_at = now() - interval "
            "'2 hours' WHERE token = %s", (token,))
        self.assertIsNone(staging.pop(self.conn, "bulk", token))

    def test_an_abandoned_stage_is_swept_without_its_household_returning(self):
        """The sweep inside put/pop/peek reaches only the household making
        the call, and a staged upload still holds the full account numbers
        the importers cut down. The instance-wide sweep reaches every
        household's expired stages and leaves fresh ones alone."""
        other = _second_tenant()
        try:
            stale_a = staging.put(self.conn, "bulk", files=[("a.csv", b"x")])
            stale_b = staging.put(other, "csv", files=[("b.csv", b"y")])
            fresh = staging.put(self.conn, "bulk", files=[("c.csv", b"z")])
            for c, tok in ((self.conn, stale_a), (other, stale_b)):
                c.execute("UPDATE import_staging SET created_at = now() - "
                          "interval '2 hours' WHERE token = %s", (tok,))
            self.assertGreaterEqual(staging.sweep_all(), 2)
            left = {r["token"] for r in self.conn.execute(
                "SELECT token FROM import_staging").fetchall()}
            self.assertEqual(left, {fresh})
            self.assertIsNone(other.execute(
                "SELECT 1 FROM import_staging").fetchone())
        finally:
            other.close()

    def test_the_worker_runs_the_instance_wide_sweep_every_hour(self):
        """Nothing else calls the instance-wide sweep, so without its cron
        entry an abandoned upload outlives its hour indefinitely."""
        import asyncio
        from oikonome.jobs import worker
        job = next((j for j in worker.WorkerSettings.cron_jobs
                    if j.coroutine is worker.staging_sweep), None)
        self.assertIsNotNone(job)
        self.assertIsNone(job.hour)
        self.assertIsInstance(job.minute, int)
        stale = staging.put(self.conn, "bulk", files=[("a.csv", b"x")])
        self.conn.execute("UPDATE import_staging SET created_at = now() - "
                          "interval '2 hours' WHERE token = %s", (stale,))
        self.assertGreaterEqual(asyncio.run(worker.staging_sweep({})), 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM import_staging WHERE token = %s",
            (stale,)).fetchone())

    def test_byte_budget_refuses_loudly(self):
        with mock.patch.object(staging, "TENANT_BUDGET", 1000):
            staging.put(self.conn, "bulk", files=[("a", b"x" * 800)])
            with self.assertRaises(ValueError) as e:
                staging.put(self.conn, "bulk", files=[("b", b"x" * 800)])
            self.assertIn("staging area is full", str(e.exception))

    def test_concurrent_puts_cannot_both_fit_under_the_budget(self):
        """The check-and-insert must serialize on a per-tenant advisory
        lock so the second caller sees the first's bytes and refuses;
        otherwise two parallel uploads read the same held-bytes SUM and both
        fit under a budget only one left room for."""
        import threading

        import os

        from oikonome.db import tenancy as _t
        from .util import TEST_DB
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        app_dsn = os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")
        rival = _t.tenant_connect(tid, app_dsn)
        self.addCleanup(rival.close)
        locked = threading.Event()
        release = threading.Event()

        def holder():
            # plays request A: takes the staging lock, inserts 800 bytes,
            # and holds the transaction open while B races the check
            with rival.transaction():
                rival.execute(
                    "SELECT pg_advisory_xact_lock(hashtext("
                    "'oikonome:staging:' "
                    "|| current_setting('app.tenant_id')))")
                rival.execute(
                    "INSERT INTO import_staging (token, idx, kind, data) "
                    "VALUES ('racer', 0, 'bulk', %s)", (b"x" * 800,))
                locked.set()
                release.wait(timeout=10)

        outcome = {}

        def racer():
            try:
                staging.put(self.conn, "bulk", files=[("b", b"x" * 800)])
                outcome["result"] = "accepted"
            except ValueError:
                outcome["result"] = "refused"

        with mock.patch.object(staging, "TENANT_BUDGET", 1000):
            t1 = threading.Thread(target=holder)
            t1.start()
            self.assertTrue(locked.wait(timeout=10))
            t2 = threading.Thread(target=racer)
            t2.start()
            t2.join(timeout=1.0)
            blocked = t2.is_alive()
            release.set()
            t1.join(timeout=10)
            t2.join(timeout=10)
        self.assertTrue(blocked, "put() did not wait for the concurrent "
                                 "upload's staging lock")
        self.assertEqual(outcome.get("result"), "refused")


class BulkStagingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_bulk_run_from_another_tenant_sees_expired(self):
        plan = bulk_import.analyze(
            self.conn, [("stmt.csv",
                         b"Date,Description,Amount\n2026-07-01,X,-5.00\n")])
        other = _second_tenant()
        try:
            with self.assertRaises(ValueError) as e:
                submit_and_run(other, plan["token"],
                               [{"index": 0, "action": "import",
                                 "account_id": "chk"}])
            self.assertIn("expired", str(e.exception))
        finally:
            other.close()
        # the rightful tenant's run still works
        out = submit_and_run(self.conn, plan["token"],
                             [{"index": 0, "action": "import",
                               "account_id": "chk"}])
        self.assertTrue(out[0]["ok"], out)

    def test_bulk_survives_a_worker_swap(self):
        # the follow-up request lands on a "different worker" — same DB,
        # different connection object; a per-process store would 404 here.
        plan = bulk_import.analyze(
            self.conn, [("stmt.csv",
                         b"Date,Description,Amount\n2026-07-01,Y,-7.00\n")])
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        worker2 = tenancy.tenant_connect(tid)
        try:
            out = submit_and_run(worker2, plan["token"],
                                 [{"index": 0, "action": "import",
                                   "account_id": "chk"}])
            self.assertTrue(out[0]["ok"], out)
        finally:
            worker2.close()


class TaxdocTenantBindingTests(unittest.TestCase):
    """A taxdoc stage commits only under the tenant that staged it."""

    SSA = b"""<?xml version="1.0"?>
<osss:OnlineSocialSecurityStatementData xmlns:osss="http://ssa.gov/oss">
  <osss:EarningsRecord>
    <osss:Earnings startYear="2020" endYear="2020">
      <osss:FicaEarnings>50000</osss:FicaEarnings>
      <osss:MedicareEarnings>50000</osss:MedicareEarnings>
    </osss:Earnings>
  </osss:EarningsRecord>
</osss:OnlineSocialSecurityStatementData>"""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _analyze(self):
        return taxdocs.analyze(self.conn, "earnings.xml", self.SSA,
                               "text/xml")

    def test_commit_from_another_tenant_is_refused(self):
        out = self._analyze()
        other = _second_tenant()
        try:
            with self.assertRaises(ValueError) as e:
                taxdocs.commit(other, out["token"], out["rows"])
            self.assertIn("expired", str(e.exception))
            n = other.execute("SELECT COUNT(*) AS n FROM income_annual"
                              ).fetchone()["n"]
            self.assertEqual(n, 0)               # nothing written cross-tenant
        finally:
            other.close()
        # owner's commit still lands
        res = taxdocs.commit(self.conn, out["token"], out["rows"])
        self.assertEqual(res["created"], 1)

    def test_commit_token_is_single_use(self):
        out = self._analyze()
        taxdocs.commit(self.conn, out["token"], out["rows"])
        with self.assertRaises(ValueError):
            taxdocs.commit(self.conn, out["token"], out["rows"])


if __name__ == "__main__":
    unittest.main()
