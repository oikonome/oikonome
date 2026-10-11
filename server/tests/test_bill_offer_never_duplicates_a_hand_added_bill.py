"""A bill the person adds by hand while detection runs is never offered
back to them, and an offer made before they added it cannot create a
second bill (or overwrite theirs) when it is approved.

Detection reads coverage again right before it proposes, after its slow
maintenance pass, so a save that commits during that pass counts as
cover. A save that lands after that read is caught at approval: the
offer is withdrawn as superseded instead of applied.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import bills as rd

from .util import TODAY, add_bill, add_txn, make_db, write_config


class HandAddedBillCoversTheOffer(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for i in range(5):
            m = TODAY.month - 5 + i
            add_txn(self.conn, dt.date(TODAY.year, m, 8), 15.49,
                    "STREAMFLIX.COM")

    def tearDown(self):
        self.conn.close()

    def _adds(self):
        return self.conn.execute(
            "SELECT * FROM bill_proposals WHERE status='pending' "
            "AND kind='add'").fetchall()

    def test_a_bill_saved_during_maintenance_is_not_offered(self):
        real = rd._maintain_bills

        def save_meanwhile(conn, *a, **kw):
            add_bill(conn, "Streamflix", 15.49, source="manual")
            return real(conn, *a, **kw)

        with mock.patch.object(rd, "_maintain_bills", save_meanwhile):
            rd.run(self.conn, TODAY)
        self.assertEqual(self._adds(), [])

    def test_approving_a_stale_offer_does_not_duplicate_the_bill(self):
        rd.run(self.conn, TODAY)
        adds = self._adds()
        self.assertEqual(len(adds), 1)
        add_bill(self.conn, "Streamflix", 16.00, source="manual")
        r = rd.apply_proposal(self.conn, adds[0]["id"], "approve")
        self.assertIn("already tracked", r.get("error", ""))
        active = self.conn.execute(
            "SELECT payee, amount FROM bills WHERE active = 1").fetchall()
        self.assertEqual([(b["payee"], float(b["amount"])) for b in active],
                         [("Streamflix", -16.00)])
        st = self.conn.execute(
            "SELECT status FROM bill_proposals WHERE id = %s",
            (adds[0]["id"],)).fetchone()["status"]
        self.assertEqual(st, "superseded")


if __name__ == "__main__":
    unittest.main()
