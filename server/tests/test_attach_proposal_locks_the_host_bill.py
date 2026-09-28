"""Approving a fee-attach proposal locks the host bill for its
read-modify-write of the companions list.

Two attach proposals for the same bill are two proposal rows, so the
proposal lock never contends between them: each reads the host's
companions, appends its own fee and writes the whole list back, and
without the host lock the second write replaces the first's and one fee
is silently lost.
"""

import json
import unittest

import psycopg

from oikonome.db import tenancy
from oikonome.engine import bills
from oikonome.engine.compat import jsonb

from .util import add_bill, make_db, write_config


class AttachLockTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        add_bill(self.conn, "Water Utility", 60.0, frequency="MONTHLY")
        self.conn.execute(
            """INSERT INTO bill_proposals (id, kind, payee, amount, evidence,
                                           status, created_at)
               VALUES ('prop:fee1', 'attach', 'convenience fee', -1.5, %s,
                       'pending', now())""",
            (jsonb({"bill_payee": "Water Utility",
                    "tokens": "convenience fee", "window_days": 3}),))

    def tearDown(self):
        self.conn.close()

    def test_the_host_row_is_locked_while_the_attach_applies(self):
        other = tenancy.tenant_connect(self.tid)
        try:
            other.execute("SET lock_timeout = '300ms'")
            # the outer transaction keeps apply_proposal's lock held (its
            # own transaction() nests as a savepoint) so a second writer
            # can be observed waiting on the host row
            with self.conn.transaction():
                out = bills.apply_proposal(self.conn, "prop:fee1", "approve")
                self.assertNotIn("error", out, out)
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    other.execute(
                        "SELECT id FROM bills WHERE payee='Water Utility' "
                        "FOR UPDATE").fetchone()
            # committed: the companion landed exactly once
            raw = self.conn.execute(
                "SELECT raw FROM bills WHERE payee='Water Utility'"
            ).fetchone()["raw"]
            raw = raw if isinstance(raw, dict) else json.loads(raw)
            self.assertEqual(len(raw.get("companions") or []), 1)
        finally:
            # session-level, and `other` is a POOLED connection: the wrapper's
            # close() resets only the app.* scope, so without this the 300ms
            # timeout rides the connection back into the pool and trips the
            # next test that deliberately blocks on a lock.
            other.execute("RESET lock_timeout")
            other.close()


if __name__ == "__main__":
    unittest.main()
