"""A receipt attached to a pending charge that has already posted lands
on the posted charge.

The phone can still show the hold after the sync has posted it under a new
id. Attached to the retired hold, the receipt would be reported as
attached and then appear on no screen: the settlement's carry has already
run, and a hold that posts for a different amount (a tip) has no
same-amount twin for a later sweep to find. The check and the insert are
also one step under the settlement's lock, so a receipt snapped while the
charge settles is either carried with it or sent to the posted row.
"""
import datetime as dt
import io
import unittest

from oikonome.engine import receipts
from oikonome.sync import base

from .util import TODAY, add_txn, make_db, write_config


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, "PNG")
    return buf.getvalue()


class SettledHoldReceiptTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 40.0, "BISTRO",
                pending=1, txn_id="hold-r")
        base.upsert_transactions(self.conn, [base.Transaction(
            id="posted-r", account_id="card", date=TODAY, amount=48.0,
            name="BISTRO", category_primary="FOOD_AND_DRINK",
            raw={"pending_transaction_id": "hold-r"})])
        self.conn.execute("UPDATE transactions SET removed = 1 "
                          "WHERE id = 'hold-r'")

    def tearDown(self):
        self.conn.close()

    def test_the_receipt_is_on_the_posted_charge(self):
        rid = receipts.add(self.conn, "hold-r", _png(), "image/png")
        self.assertEqual([r["id"] for r in receipts.for_txn(
            self.conn, "posted-r")], [rid])
        self.assertEqual(receipts.for_txn(self.conn, "hold-r"), [])

    def test_a_row_gone_with_no_posted_charge_is_refused(self):
        gone = add_txn(self.conn, TODAY, 12.0, "VOIDED", pending=1)
        self.conn.execute("UPDATE transactions SET removed = 1 "
                          "WHERE id = %s", (gone,))
        with self.assertRaises(ValueError):
            receipts.add(self.conn, gone, _png(), "image/png")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM receipts WHERE txn_id = %s",
            (gone,)).fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
