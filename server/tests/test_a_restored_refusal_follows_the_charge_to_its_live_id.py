"""A waiting receipt's refusals name charges by id. When a restore lands an
archived charge on the row this household already holds for the same line
(under another id), the refusal must follow it to that live id — otherwise
it names an id nothing holds, and the matcher pairs the receipt straight
back onto the charge the person took it off.
"""
import datetime as dt
import unittest

from oikonome.engine import receipt_match, receipts
from oikonome.sync import export, restore

from .util import add_txn, make_db, write_config

PNG = b"\x89PNG-refusal-follows"
ARCHIVED = "csv:" + "a" * 40
LIVE = "csv:" + "b" * 40
OTHER = "csv:" + "c" * 40


class RefusalFollowsCarriedChargeTests(unittest.TestCase):
    def setUp(self):
        self.src = make_db()
        write_config(self.src)
        self.dest = make_db()
        write_config(self.dest)

    def tearDown(self):
        self.src.close()
        self.dest.close()

    def test_a_refusal_names_the_live_row_its_charge_landed_on(self):
        day = dt.date.today() - dt.timedelta(days=1)
        add_txn(self.src, day, 40.00, "LANTERN BOOKS", txn_id=ARCHIVED)
        add_txn(self.src, day, 9.00, "LANTERN BOOKS", txn_id=OTHER)
        waiting = receipts.add(self.src, None, PNG, "image/png")
        self.src.execute(
            "UPDATE receipts SET status='parsed', parsed=%s::jsonb, "
            "unmatched_txn_ids=%s WHERE id=%s",
            ('{"merchant": "Lantern Books", "total": 40.0, "date": "%s"}'
             % day.isoformat(), [ARCHIVED, OTHER, ARCHIVED], waiting))
        data = export.build_zip(self.src)
        # the household already holds the same line under another id
        add_txn(self.dest, day, 40.00, "LANTERN BOOKS", txn_id=LIVE)

        restore.restore_zip(self.dest, data)

        held = {r["id"] for r in self.dest.execute(
            "SELECT id FROM transactions WHERE amount = 40").fetchall()}
        self.assertEqual(held, {LIVE})  # the archived line was carried
        row = self.dest.execute(
            "SELECT txn_id, unmatched_txn_ids FROM receipts WHERE id=%s",
            (waiting,)).fetchone()
        self.assertEqual(row["unmatched_txn_ids"], [LIVE, OTHER])
        receipt_match.match_waiting(self.dest)
        row = self.dest.execute(
            "SELECT txn_id FROM receipts WHERE id=%s", (waiting,)).fetchone()
        self.assertIsNone(row["txn_id"])


if __name__ == "__main__":
    unittest.main()
