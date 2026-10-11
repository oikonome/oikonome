"""A folder import holds the household's sync lock one file at a time.

Held for a whole folder, the lock kept the hourly sync, a restore and an
account erasure waiting for as long as two hundred files took. Between
files it is free, so they take turns with the import instead."""

import unittest
from unittest import mock

from oikonome.db import staging, tenancy
from oikonome.jobs import bulk_import_job

from .bulk_job_util import csv_for, submit, tenant_of
from .util import _ensure_db, make_db, write_config


class SyncLockPerFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_another_holder_gets_the_lock_between_files(self):
        conn = make_db()
        try:
            write_config(conn)
            tid = tenant_of(conn)
            token = staging.put(conn, "bulk", [
                (f"bank-{t}.csv", csv_for(f"SHOP {t}")) for t in "ABC"])
            with mock.patch.object(bulk_import_job, "nudge"):
                sub = submit(conn, token, [
                    {"index": i, "action": "import", "account_id": "chk"}
                    for i in range(3)])
            # probed as each file's turn begins: between files
            real = bulk_import_job._run_file
            free: list[bool] = []

            def probe(*a, **kw):
                other = tenancy.tenant_connect(tid)
                try:
                    got = other.execute(
                        "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                        (f"oikonome:sync:{tid}",)).fetchone()["ok"]
                    if got:
                        other.execute(
                            "SELECT pg_advisory_unlock(hashtext(%s))",
                            (f"oikonome:sync:{tid}",))
                    free.append(got)
                finally:
                    other.close()
                return real(*a, **kw)

            with mock.patch.object(bulk_import_job, "_run_file",
                                   side_effect=probe):
                bulk_import_job.run_job(tid, sub["job_id"])
            self.assertEqual(free, [True, True, True])
            self.assertEqual(
                bulk_import_job.status(conn, sub["job_id"])["state"], "done")
        finally:
            conn.close()

    def test_a_busy_lock_is_waited_for_not_refused(self):
        """A sync holding the lock makes the next file wait its turn; the
        old refusal ("a sync is already running") is gone with the
        request that gave it."""
        conn = make_db()
        try:
            write_config(conn)
            tid = tenant_of(conn)
            token = staging.put(conn, "bulk", [("a.csv", csv_for("SHOP A"))])
            with mock.patch.object(bulk_import_job, "nudge"):
                sub = submit(conn, token, [
                    {"index": 0, "action": "import", "account_id": "chk"}])
            other = tenancy.tenant_connect(tid)
            try:
                self.assertTrue(other.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                    (f"oikonome:sync:{tid}",)).fetchone()["ok"])
                waits = []

                def sleep(s):
                    waits.append(s)
                    if len(waits) == 2:
                        other.execute(
                            "SELECT pg_advisory_unlock(hashtext(%s))",
                            (f"oikonome:sync:{tid}",))

                with mock.patch.object(bulk_import_job, "_sleep",
                                       side_effect=sleep):
                    bulk_import_job.run_job(tid, sub["job_id"])
            finally:
                other.close()
            self.assertEqual(len(waits), 2)
            st = bulk_import_job.status(conn, sub["job_id"])
            self.assertEqual(st["state"], "done", st)
            self.assertTrue(st["files"][0]["ok"], st)
        finally:
            conn.close()

    def test_a_lock_that_stays_busy_skips_the_file(self):
        conn = make_db()
        try:
            write_config(conn)
            tid = tenant_of(conn)
            token = staging.put(conn, "bulk", [("a.csv", csv_for("SHOP A"))])
            with mock.patch.object(bulk_import_job, "nudge"):
                sub = submit(conn, token, [
                    {"index": 0, "action": "import", "account_id": "chk"}])
            other = tenancy.tenant_connect(tid)
            try:
                other.execute("SELECT pg_advisory_lock(hashtext(%s))",
                              (f"oikonome:sync:{tid}",))
                with mock.patch.object(bulk_import_job, "_sleep"), \
                        mock.patch.object(bulk_import_job,
                                          "LOCK_GIVE_UP_SECONDS", 0):
                    bulk_import_job.run_job(tid, sub["job_id"])
                other.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                              (f"oikonome:sync:{tid}",))
            finally:
                other.close()
            st = bulk_import_job.status(conn, sub["job_id"])
            self.assertEqual(st["files"][0]["state"], "skipped", st)
            self.assertIn("busy", st["files"][0]["error"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
