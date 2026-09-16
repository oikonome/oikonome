"""A bulk run that cannot take the tenant's sync lock leaves the batch
staged.

Popping the stage first and then refusing every file with "a sync is
running" loses the staged bytes, and the person re-uploads a batch the
server has just thrown away.
"""

import unittest

from oikonome.db import staging, tenancy
from oikonome.web import bulk_import

from .util import make_db, write_config


class BulkLockTests(unittest.TestCase):
    def test_a_held_lock_refuses_and_keeps_the_stage(self):
        conn = make_db()
        try:
            write_config(conn)
            tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
            token = staging.put(conn, "bulk", [("a.csv", b"date,name\n")],
                                {"plan": []})
            other = tenancy.tenant_connect(tid)
            try:
                got = other.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                    (f"oikonome:sync:{tid}",)).fetchone()["ok"]
                self.assertTrue(got)
                with self.assertRaises(ValueError) as cm:
                    bulk_import.run(conn, token,
                                    [{"index": 0, "action": "import"}])
                self.assertIn("sync", str(cm.exception))
                other.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                              (f"oikonome:sync:{tid}",))
            finally:
                other.close()
            self.assertIsNotNone(staging.peek(conn, "bulk", token),
                                 "the batch was consumed by a refusal")
        finally:
            conn.close()
