"""Starting the app must not stall ledger reads behind one slow report.

migrate runs schema.sql on every start, and its `ALTER TABLE ... ADD COLUMN
IF NOT EXISTS` lines ask for ACCESS EXCLUSIVE on the ledger before finding
the column already there. Postgres queues every later read behind that
request, so a start that waited for a long-running read would freeze all
reads for as long as that read ran. Invariant: while migrate waits, a new
read still completes promptly, and migrate itself still finishes once the
long read ends.
"""

import threading
import time
import unittest
from unittest import mock

import psycopg

from oikonome.db import migrate

from .util import TEST_DB, _admin_dsn, _ensure_db

DB = f"{TEST_DB}_migrate_locks"


class MigrateLockWaitTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {DB}")
            c.execute(f"CREATE DATABASE {DB}")
        migrate.run(_admin_dsn(DB))

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")

    def test_a_read_arriving_mid_start_is_not_held_behind_it(self):
        long_read = psycopg.connect(_admin_dsn(DB))
        self.addCleanup(long_read.close)
        long_read.execute("SELECT count(*) FROM transactions")  # txn open

        outcome = {}

        def start():
            try:
                migrate.run(_admin_dsn(DB))
                outcome["ok"] = True
            except Exception as e:                     # noqa: BLE001
                outcome["error"] = e

        with mock.patch.object(migrate, "SCHEMA_LOCK_TIMEOUT_MS", 150,
                               create=True), \
                mock.patch.object(migrate, "SCHEMA_LOCK_BACKOFF_S", 0.3,
                                  create=True), \
                mock.patch.object(migrate, "SCHEMA_LOCK_ATTEMPTS", 20,
                                  create=True):
            t = threading.Thread(target=start, daemon=True)
            t.start()
            time.sleep(0.8)                  # migrate is waiting on the lock
            with psycopg.connect(_admin_dsn(DB), autocommit=True) as reader:
                reader.execute("SET statement_timeout = '3s'")
                began = time.monotonic()
                reader.execute("SELECT count(*) FROM transactions")
                self.assertLess(time.monotonic() - began, 3)
            long_read.rollback()             # the long read ends
            t.join(30)
        self.assertFalse(t.is_alive(), "migrate never finished")
        self.assertEqual(outcome, {"ok": True})


if __name__ == "__main__":
    unittest.main()
