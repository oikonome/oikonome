"""A waiting receipt with no believable printed date files under the day it
was snapped ON THE HOUSEHOLD'S CALENDAR, not the database's.

The database session runs in UTC, so a receipt photographed at 6:30pm on
the US west coast is already "tomorrow" there. Casting its timestamp to a
date in SQL would put September's last purchase on October's expense report
and in October's month group, and would shift the window the printed date
is believed in by a day. The matcher reads the same snap day, so it must
agree."""

import datetime as dt
import json
import unittest
import zoneinfo

import httpx

from oikonome.engine import receipt_match, receipts

from .test_receipts import _enable_llm
from .util import make_db, write_config

ZONE = "America/Los_Angeles"
# 6:30pm Pacific on 09/30 — already 10/01 in UTC
SNAP_UTC = "2026-10-01 01:30:00+00"


def _reply(payload):
    return httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(payload)}}]}))


FADED = {"merchant": "CORNER HARDWARE", "total": 41.00, "tax": None,
         "tip": None,
         "items": [{"description": "WOOD GLUE", "qty": 1, "amount": 9.00},
                   {"description": "CLAMPS", "qty": 2, "amount": 32.00}]}


class HouseholdSnapDay(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, timezone=ZONE)
        _enable_llm(self.conn)
        self.rid = receipts.add(self.conn, None, b"\x89PNG...", "image/png")
        out = receipts.parse_one(self.conn, self.rid,
                                 transport=_reply(FADED))
        self.assertEqual(out["status"], "parsed")
        self.conn.execute("UPDATE receipts SET created_at = %s "
                          "WHERE id = %s::uuid", (SNAP_UTC, self.rid))
        for r in self.conn.execute(
                "SELECT line FROM receipt_items WHERE receipt_id = %s::uuid",
                (self.rid,)).fetchall():
            receipts.set_tag(self.conn, self.rid, r["line"], "business")

    def tearDown(self):
        self.conn.close()

    def _printed(self, day):
        self.conn.execute(
            "UPDATE receipts SET parsed = jsonb_set(parsed, '{date}', %s)"
            " WHERE id = %s::uuid", (json.dumps(day), self.rid))

    def test_the_expense_report_files_it_in_the_households_month(self):
        sep = receipts.report(self.conn, "business", 2026, 9)
        oct_ = receipts.report(self.conn, "business", 2026, 10)
        self.assertEqual(sep["count"], 2)
        self.assertEqual(sep["total"], 41.00)
        self.assertEqual(oct_["count"], 0)
        self.assertEqual(str(sep["rows"][0]["date"]), "2026-09-30")

    def test_the_month_grouping_and_rows_use_the_households_day(self):
        out = receipts.search_items(self.conn, q="glue", group="month")
        self.assertEqual(str(out["rows"][0]["date"]), "2026-09-30")
        self.assertEqual([g["key"] for g in out["groups"]], ["2026-09"])

    def test_the_printed_date_window_is_reckoned_from_the_households_day(self):
        # a day after the household's snap day is believed; two days after
        # is a misread and the snap day stands in
        self._printed("2026-10-01")
        row = receipts.search_items(self.conn, q="glue")["rows"][0]
        self.assertEqual(str(row["date"]), "2026-10-01")
        self._printed("2026-10-02")
        row = receipts.search_items(self.conn, q="glue")["rows"][0]
        self.assertEqual(str(row["date"]), "2026-09-30")

    def test_the_matcher_reads_the_same_snap_day(self):
        snapped = dt.datetime(2026, 10, 1, 1, 30, tzinfo=dt.timezone.utc)
        row = {"parsed": {"total": 41.0}, "created_at": snapped}
        f = receipt_match.facts(row, tz=zoneinfo.ZoneInfo(ZONE))
        self.assertEqual(f["date"], dt.date(2026, 9, 30))
        # and the window around it
        row["parsed"]["date"] = "2026-10-02"
        f = receipt_match.facts(row, tz=zoneinfo.ZoneInfo(ZONE))
        self.assertEqual(f["date"], dt.date(2026, 9, 30))

    def test_the_waiting_list_shows_the_households_day(self):
        listed = [r for r in receipt_match.waiting(self.conn)["waiting"]
                  if r["id"] == self.rid]
        self.assertEqual(listed[0]["date"], "2026-09-30")


if __name__ == "__main__":
    unittest.main()
