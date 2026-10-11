"""A receipt still waiting for its charge travels in the data ZIP and comes
back waiting — image, parse and the charges a person already took it off —
and a matched one comes back matched, saying who matched it.
"""
import datetime as dt
import unittest

from oikonome.engine import receipt_match, receipts
from oikonome.sync import export, restore

from .util import add_txn, make_db, write_config

PNG = b"\x89PNG-waiting-receipt"


class WaitingReceiptRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.src = make_db()
        write_config(self.src)

    def tearDown(self):
        self.src.close()

    def test_waiting_and_matched_receipts_round_trip(self):
        day = dt.date.today() - dt.timedelta(days=1)
        add_txn(self.src, day, 31.00, "QUILL AND INK", txn_id="rt-1")
        add_txn(self.src, day, 12.00, "HARBOR COFFEE", txn_id="rt-2")
        waiting = receipts.add(self.src, None, PNG, "image/png")
        self.src.execute(
            "UPDATE receipts SET status='parsed', parsed=%s::jsonb "
            "WHERE id=%s",
            ('{"merchant": "Quill and Ink", "total": 31.0, "date": "%s"}'
             % day.isoformat(), waiting))
        # a person already said this receipt is not rt-1
        self.assertTrue(receipt_match.match_by_hand(self.src, waiting, "rt-1"))
        self.assertEqual(receipt_match.unmatch(self.src, waiting), "rt-1")
        matched = receipts.add(self.src, None, PNG, "image/png")
        self.assertTrue(receipt_match.match_by_hand(self.src, matched, "rt-2"))

        data = export.build_zip(self.src)
        dest = make_db()
        try:
            counts = restore.restore_zip(dest, data)
            self.assertEqual(counts.get("receipts"), 2)
            w = dest.execute(
                "SELECT txn_id, status, parsed, image, match_method, "
                "matched_at, unmatched_txn_ids FROM receipts WHERE id=%s",
                (waiting,)).fetchone()
            self.assertIsNone(w["txn_id"])
            self.assertEqual(w["status"], "parsed")
            self.assertEqual(w["parsed"]["total"], 31.0)
            self.assertEqual(bytes(w["image"]), PNG)
            self.assertIsNone(w["match_method"])
            self.assertEqual(w["unmatched_txn_ids"], ["rt-1"])
            m = dest.execute(
                "SELECT txn_id, match_method, matched_at FROM receipts "
                "WHERE id=%s", (matched,)).fetchone()
            self.assertEqual((m["txn_id"], m["match_method"]),
                             ("rt-2", "manual"))
            self.assertIsNotNone(m["matched_at"])
            # and the restored waiting list still refuses to offer rt-1
            listed = receipt_match.waiting(dest)["waiting"]
            self.assertEqual([x["id"] for x in listed], [waiting])
            self.assertNotIn("rt-1", [c["txn_id"]
                                      for c in listed[0]["candidates"]])
        finally:
            dest.close()


def _jpeg() -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (40, 60), 200).save(buf, format="JPEG")
    return buf.getvalue()


class CleanedUpCopyRoundTripTests(unittest.TestCase):
    """The cleaned-up copy of a receipt photo travels in the data ZIP and
    comes back with it. Only an upload ever makes one, so a restore that
    dropped it would leave every restored receipt serving and re-reading
    the raw, uncropped photo for good."""

    def setUp(self):
        self.src = make_db()
        write_config(self.src)

    def tearDown(self):
        self.src.close()

    def _round_trip(self, opt, opt_mime):
        rid = receipts.add(self.src, None, PNG, "image/png")
        self.src.execute(
            "UPDATE receipts SET image_optimized=%s, optimized_mime=%s "
            "WHERE id=%s::uuid", (opt, opt_mime, rid))
        data = export.build_zip(self.src)
        dest = make_db()
        try:
            restore.restore_zip(dest, data)
            return dest.execute(
                "SELECT image, image_optimized, optimized_mime "
                "FROM receipts WHERE id=%s::uuid", (rid,)).fetchone()
        finally:
            dest.close()

    def test_the_cleaned_up_copy_is_restored(self):
        opt = _jpeg()
        row = self._round_trip(opt, "image/jpeg")
        self.assertEqual(bytes(row["image"]), PNG)
        self.assertEqual(bytes(row["image_optimized"]), opt)
        self.assertEqual(row["optimized_mime"], "image/jpeg")

    def test_a_copy_that_is_not_the_jpeg_an_upload_writes_is_dropped(self):
        # the stored type is what the image door serves the bytes back as,
        # so only the one type the optimizer writes is ever restored; the
        # receipt itself still comes back
        for opt, mime in ((_jpeg(), "text/html"),
                          (b"<html>not a jpeg</html>", "image/jpeg")):
            with self.subTest(mime=mime):
                row = self._round_trip(opt, mime)
                self.assertEqual(bytes(row["image"]), PNG)
                self.assertIsNone(row["image_optimized"])
                self.assertIsNone(row["optimized_mime"])


if __name__ == "__main__":
    unittest.main()
