"""A waiting receipt's line items total under the ledger's name for the
store, and under a date the receipt could really carry.

The model reads the store as printed ("TRADER JOE'S #552"), while the
ledger shows that business under its display name ("Trader Joe's"). Filed
under the raw spelling, one store split into two rows on a screen that
exists to total it. The printed name goes through the canonicaliser the
ledger uses and takes the ledger's display name for that business; a store
the ledger has not seen still files every spelling of it on one row.

A printed date is believed only inside the window the matcher believes (a
year before the snap to a day after); a misread year otherwise filed the
purchase under a month it never happened in.
"""
import datetime as dt
import json
import unittest

import httpx

from oikonome.engine import receipts

from .test_receipts import _enable_llm
from .util import TODAY, add_txn, make_db, write_config


def _reply(payload):
    return httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(payload)}}]}))


def _receipt(merchant, total, date, desc, txn=None):
    return {"merchant": merchant, "date": date, "total": total,
            "tax": None, "tip": None,
            "items": [{"description": desc, "qty": 1, "amount": total}]}


class WaitingItemsTotalWithTheirStore(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def _add(self, txn, payload):
        rid = receipts.add(self.conn, txn, b"\x89PNG...", "image/png")
        out = receipts.parse_one(self.conn, rid, transport=_reply(payload))
        self.assertEqual(out["status"], "parsed")
        return rid

    def _snap_day(self, rid):
        return self.conn.execute(
            "SELECT created_at::date AS d FROM receipts WHERE id=%s::uuid",
            (rid,)).fetchone()["d"]

    def test_attached_and_waiting_receipts_of_one_store_are_one_group(self):
        txn = add_txn(self.conn, TODAY, 300.00, "Trader Joe's")
        self._add(txn, _receipt("Trader Joe's", 300.00, TODAY.isoformat(),
                                "GROCERIES"))
        waiting = self._add(None, _receipt(
            "TRADER JOE'S #552", 80.00,
            (TODAY - dt.timedelta(days=1)).isoformat(), "FLOWERS"))
        self.assertIsNone(self.conn.execute(
            "SELECT txn_id FROM receipts WHERE id=%s::uuid",
            (waiting,)).fetchone()["txn_id"])
        groups = receipts.search_items(self.conn, group="merchant")["groups"]
        self.assertEqual([(g["key"], g["total"]) for g in groups],
                         [("Trader Joe's", 380.00)])
        row = receipts.search_items(self.conn, q="flowers")["rows"][0]
        self.assertEqual(row["payee"], "Trader Joe's")
        receipts.set_tag(self.conn, waiting, 0, "gift")
        rep = receipts.report(self.conn, "gift", TODAY.year, TODAY.month)
        self.assertEqual(rep["rows"][0]["payee"], "Trader Joe's")

    def test_a_store_the_ledger_has_not_seen_still_files_on_one_row(self):
        self._add(None, _receipt("CORNER HARDWARE #12", 9.00,
                                 TODAY.isoformat(), "GLUE"))
        self._add(None, _receipt("Corner Hardware", 32.00,
                                 TODAY.isoformat(), "CLAMPS"))
        groups = receipts.search_items(self.conn, group="merchant")["groups"]
        self.assertEqual([(g["key"], g["total"]) for g in groups],
                         [("Corner Hardware", 41.00)])

    def test_a_misread_year_files_under_the_snap_month(self):
        rid = self._add(None, _receipt("CORNER HARDWARE", 41.00,
                                       "2020-09-20", "CLAMPS"))
        snap = self._snap_day(rid)
        row = receipts.search_items(self.conn, q="clamps")["rows"][0]
        self.assertEqual(row["date"], snap)
        months = receipts.search_items(self.conn, group="month")["groups"]
        self.assertEqual([g["key"] for g in months],
                         [snap.strftime("%Y-%m")])
        receipts.set_tag(self.conn, rid, 0, "shop")
        self.assertEqual(receipts.report(self.conn, "shop", 2020, 9)["count"],
                         0)
        self.assertEqual(receipts.report(
            self.conn, "shop", snap.year, snap.month)["count"], 1)

    def test_a_date_after_the_snap_day_is_not_believed(self):
        rid = self._add(None, _receipt("CORNER HARDWARE", 41.00,
                                       "2020-01-01", "CLAMPS"))
        snap = self._snap_day(rid)
        future = (snap + dt.timedelta(days=40)).isoformat()
        self.conn.execute(
            "UPDATE receipts SET parsed = jsonb_set(parsed, '{date}', %s)"
            " WHERE id=%s::uuid", (json.dumps(future), rid))
        row = receipts.search_items(self.conn, q="clamps")["rows"][0]
        self.assertEqual(row["date"], snap)


if __name__ == "__main__":
    unittest.main()
