"""A printed date like 03/04 is read against the day it was photographed.

03/04/2026 is March 4 in the US and April 3 almost everywhere else. Read
month-first unconditionally, a day-first receipt snapped on April 3 was
stored as March 4, a month before the photo — and the matcher then looked
for its charge in the wrong month. A receipt is printed on or before the
day it is photographed, usually within the week, so that day decides
whenever it can; only when it cannot does month-first stand."""

import datetime as dt
import json
import unittest

import httpx

from oikonome.engine import receipts

from .test_receipts import LLM_JSON, _enable_llm
from .util import add_txn, make_db, write_config


class Readings(unittest.TestCase):
    def test_a_day_first_receipt_photographed_that_week(self):
        self.assertEqual(
            receipts.iso_date("03/04/2026", dt.date(2026, 4, 3)),
            "2026-04-03")

    def test_a_month_first_receipt_photographed_that_week(self):
        self.assertEqual(
            receipts.iso_date("03/04/2026", dt.date(2026, 3, 5)),
            "2026-03-04")

    def test_a_reading_after_the_photo_is_never_taken(self):
        # snapped March 20: April 3 has not happened yet
        self.assertEqual(
            receipts.iso_date("03/04/2026", dt.date(2026, 3, 20)),
            "2026-03-04")
        # snapped April 1 with 04/03: March 4 is the only reading so far
        self.assertEqual(
            receipts.iso_date("04/03/2026", dt.date(2026, 4, 1)),
            "2026-03-04")

    def test_when_the_photo_day_cannot_tell_month_first_stands(self):
        # both readings months before the photo
        self.assertEqual(
            receipts.iso_date("03/04/2026", dt.date(2026, 9, 1)),
            "2026-03-04")
        # and with no photo day at all, as before
        self.assertEqual(receipts.iso_date("03/04/2026"), "2026-03-04")

    def test_unambiguous_dates_ignore_the_photo_day(self):
        near = dt.date(2026, 4, 3)
        self.assertEqual(receipts.iso_date("26.09.2026", near), "2026-09-26")
        self.assertEqual(receipts.iso_date("2026-03-04", near), "2026-03-04")
        self.assertEqual(receipts.iso_date("04/04/2026", near), "2026-04-04")


class ParseUsesTheAttachedCharge(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_stored_date_is_the_reading_near_the_charge(self):
        charge = dt.date(2026, 7, 3)
        printed = "03/07/2026"       # day-first; month-first is March 7
        txn = add_txn(self.conn, charge, 63.00, "BIGBOX WHOLESALE")
        rid = receipts.add(self.conn, txn, b"\x89PNG...", "image/png")
        payload = json.dumps({**LLM_JSON, "date": printed})
        receipts.parse_one(self.conn, rid, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={
                "choices": [{"message": {"content": payload}}]})))
        row = receipts.for_txn(self.conn, txn)[0]
        self.assertEqual(row["parsed"]["date"], charge.isoformat())

    def test_a_waiting_receipt_reads_against_the_households_photo_day(self):
        """The photo day is the household's calendar day, the one the
        matcher's "today" is kept in — not the UTC one. Snapped at 7pm on
        Nov 30 in Los Angeles (already Dec 1 in UTC), 02/12 cannot be
        Dec 2: that would be two days after the photo, where UTC's day
        would let it through as the nearer reading."""
        from oikonome.engine import budget
        cfg = budget.load_config(self.conn)
        cfg["timezone"] = "America/Los_Angeles"
        budget.save_config(self.conn, cfg)
        rid = receipts.add(self.conn, None, b"\x89PNG...", "image/png")
        self.conn.execute(
            "UPDATE receipts SET created_at = '2026-12-01 03:00:00+00'"
            " WHERE id = %s::uuid", (rid,))
        payload = json.dumps({**LLM_JSON, "date": "02/12/2026"})
        out = receipts.parse_one(self.conn, rid, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={
                "choices": [{"message": {"content": payload}}]})))
        self.assertEqual(out["status"], "parsed")
        self.assertEqual(out["date"], "2026-02-12")


if __name__ == "__main__":
    unittest.main()
