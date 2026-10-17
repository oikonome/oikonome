"""Two approvals of different bill offers cannot both create a bill on the
same charges.

An approval checks that no active bill already claims the charges its bill
would match, then creates the bill. Two approvals running at once — one
offer renamed onto the other's charges, the other approved as it stands —
would each check before the other's bill was committed, both pass, and the
plan would count the charge twice. The approvals take turns on the
household's claim lock, so the second sees the first one's bill and its
offer is withdrawn.
"""

import datetime as dt
import os
import threading
import unittest

from psycopg.types.json import Jsonb

from oikonome.db import tenancy
from oikonome.engine import bills
from oikonome.web import api

from .util import TEST_DB, make_db, write_config


def _offer(conn, pid, payee):
    conn.execute(
        """INSERT INTO bill_proposals (id, kind, bill_type, payee, amount,
               frequency, "interval", next_due, evidence, status, created_at)
           VALUES (%s, 'add', 'occurrence', %s, 15.49, 'MONTHLY', 1, %s, %s,
                   'pending', now())""",
        (pid, payee, dt.date.today() + dt.timedelta(days=10), Jsonb({})))


class TwoApprovalsOneChargeTests(unittest.TestCase):
    def setUp(self):
        self.a = make_db()
        write_config(self.a)
        tid = self.a.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.b = tenancy.tenant_connect(tid, os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))
        _offer(self.a, "prop:x", "Spinwheel Audio")
        _offer(self.a, "prop:y", "NORTHWIND STREAM")

    def tearDown(self):
        self.a.close()
        self.b.close()

    def test_the_second_approval_sees_the_first_ones_bill(self):
        out: dict = {}
        # approval A holds the claim lock: offer X is approved and renamed
        # onto Northwind's charges, not yet committed
        with self.a.transaction():
            api._hold_bill_claims(self.a)
            r = bills.apply_proposal(self.a, "prop:x", "approve")
            api._adjust_approved_bill(self.a, r["approved"],
                                      {"name": "Northwind"})
            # approval B of offer Y, as it stands, on another connection
            t = threading.Thread(target=lambda: out.update(
                r=api._decide_proposal(self.b, "prop:y", "approve", {})))
            t.start()
            t.join(1.0)
            waited = t.is_alive()
        t.join(10)
        self.assertTrue(waited, "the second approval did not wait its turn")
        self.assertIn("already tracked", out["r"].get("error", ""))
        live = [r["payee"] for r in self.a.execute(
            "SELECT payee FROM bills WHERE active = 1")]
        self.assertEqual(live, ["Northwind"])
        self.assertEqual(self.a.execute(
            "SELECT status FROM bill_proposals WHERE id='prop:y'"
        ).fetchone()["status"], "superseded")


if __name__ == "__main__":
    unittest.main()
