"""The single-row reimbursement doors and the receipt doors act on the row
a pending hold became once it settled.

A settlement moves a hold's awaiting-reimbursement flag, its pairs and its
receipts onto the posted row and retires the hold, under the pairing lock.
A list read before that still names the hold. Aimed at the hold's id:

- an unflag deleted nothing, so the flag stayed on the posted charge and
  kept counting toward what is owed back;
- an unlink found no pair, so the pair stayed;
- a flag answered "transaction not found";
- a receipt upload landed on the posted row but answered, and refetched,
  from the retired hold, so the screen showed nothing attached and invited
  a second, duplicate upload.

Each door resolves the id under the same lock and names the row it acted
on in its answer."""

import asyncio
import datetime as dt
import io
import unittest
from unittest import mock

from oikonome.web import api, data

from .util import add_txn, make_db, write_config

DAY = dt.date.today() - dt.timedelta(days=2)


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, "PNG")
    return buf.getvalue()


class DoorsFollowSettledHold(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.user = {"tenant_id": tid, "role": "owner", "id": None,
                     "email": "owner@example.dev"}
        add_txn(self.conn, DAY, 38.00, "CORNER BISTRO", account="chk",
                pending=1, txn_id="hold-1")
        rec = mock.patch.object(api._activity, "record")
        self.record = rec.start()
        self.addCleanup(rec.stop)

    def _settle(self):
        """What a settlement leaves behind: the posted row naming the hold,
        the hold's user state moved onto it, the hold retired."""
        add_txn(self.conn, DAY, 45.60, "CORNER BISTRO", account="chk",
                txn_id="post-1")
        self.conn.execute(
            "UPDATE transactions SET raw = jsonb_build_object("
            "'pending_transaction_id', 'hold-1'),"
            " pending_transaction_id='hold-1' WHERE id='post-1'")
        self.conn.execute("UPDATE reimburse_flags SET txn_id='post-1' "
                          "WHERE txn_id='hold-1'")
        self.conn.execute("UPDATE reimbursements SET expense_id='post-1' "
                          "WHERE expense_id='hold-1'")
        self.conn.execute("UPDATE transactions SET removed=1 "
                          "WHERE id='hold-1'")

    def _flagged(self, tid):
        return self.conn.execute(
            "SELECT 1 FROM reimburse_flags WHERE txn_id=%s",
            (tid,)).fetchone() is not None

    def test_unflag_with_the_hold_id_drops_the_posted_rows_flag(self):
        self.assertIsNone(data.flag_reimbursement(self.conn, "hold-1"))
        self._settle()
        out = api.reimburse_unflag("hold-1", user=self.user)
        self.assertTrue(out["removed"])
        self.assertEqual(out["id"], "post-1")
        self.assertFalse(self._flagged("post-1"))

    def test_flag_with_the_hold_id_flags_the_posted_row(self):
        self._settle()
        out = api.reimburse_flag("hold-1", user=self.user, body={})
        self.assertEqual(out["id"], "post-1")
        self.assertTrue(self._flagged("post-1"))
        self.assertFalse(self._flagged("hold-1"))

    def test_unlink_with_the_hold_id_removes_the_moved_pair(self):
        add_txn(self.conn, DAY, -45.60, "VENMO FROM SAM", account="chk",
                txn_id="dep-1")
        self.conn.execute("INSERT INTO reimbursements (expense_id, "
                          "reimburse_id, partial) VALUES ('hold-1','dep-1',0)")
        self._settle()
        out = api.reimburse_unlink(user=self.user, body={
            "expense_id": "hold-1", "reimburse_id": "dep-1"})
        self.assertTrue(out["removed"])
        self.assertEqual(out["expense_id"], "post-1")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM reimbursements WHERE reimburse_id='dep-1'"
        ).fetchone())

    def test_a_receipt_upload_answers_with_the_row_it_landed_on(self):
        from starlette.datastructures import Headers, UploadFile
        self._settle()
        up = UploadFile(io.BytesIO(_png()), filename="r.png",
                        headers=Headers({"content-type": "image/png"}))
        with mock.patch("oikonome.engine.llm_categorize.configured",
                        return_value=False):
            out = asyncio.run(api.receipt_upload(
                "hold-1", user=self.user, file=up, kind="receipt"))
        self.assertEqual(out["txn_id"], "post-1")
        self.assertEqual([r["id"] for r in out["receipts"]], [out["id"]])
        self.assertEqual(self.record.call_args.kwargs["target"], "post-1")
        # a refetch keyed on the hold's id shows the receipt too
        again = api.receipt_list("hold-1", user=self.user)
        self.assertEqual([r["id"] for r in again["receipts"]], [out["id"]])


if __name__ == "__main__":
    unittest.main()
