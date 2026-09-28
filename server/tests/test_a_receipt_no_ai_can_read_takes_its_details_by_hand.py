"""A receipt no AI can read still meets its charge, from details typed in.

An instance with no vision model configured stores a snapped receipt and can
never read it: it would sit
at 'uploaded' forever, with no total, date or store for the matcher to use.
The waiting list says so out loud — `ai_available` false, and the receipt
flagged `needs_details` — and a person types the total and date in; the
receipt then pairs with its charge at once. The same door corrects a parse
that failed or misread the total, and once a person has entered the details
no later parse writes over them — not a retry, not a parse that was already
reading when they saved.
"""
import datetime as dt
import json
import os
import unittest
from unittest import mock

import httpx

from oikonome.db import tenancy
from oikonome.engine import budget, receipt_match, receipts

from .test_waiting_receipt_doors import PNG, _app, _claim, _signup
from .util import add_txn, make_db, write_config

TODAY = dt.date.today()
_NO_ENV_LLM = {"OIKONOME_LLM_URL": "", "OIKONOME_LLM_MODEL": ""}


def _db(tid, sql, args=()):
    conn = tenancy.tenant_connect(tid)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


class NoAiDetailsByHandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env = mock.patch.dict(os.environ, _NO_ENV_LLM)
        cls._env.start()
        cls.app = _app()
        cls.owner, cls.tid = _signup(cls.app, "nai")
        conn = tenancy.tenant_connect(cls.tid)
        try:
            cls.txn = add_txn(conn, TODAY, 27.35, "LANTERN HARDWARE",
                              account="chk")
        finally:
            conn.close()
        cls.viewer = _claim(cls.app, cls.owner, "viewer")

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()

    def _snap(self):
        r = self.owner.post("/api/receipts", files={"file": PNG})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"], r.json()

    def _mine(self, listed, rid):
        return next(w for w in listed["waiting"] if w["id"] == rid)

    def test_the_list_says_there_is_no_ai_and_asks_for_details(self):
        rid, snapped = self._snap()
        self.assertIs(snapped["ai_available"], False)
        listed = self.owner.get("/api/receipts/waiting").json()
        self.assertIs(listed["ai_available"], False)
        mine = self._mine(listed, rid)
        self.assertEqual(mine["status"], "uploaded")
        self.assertIs(mine["needs_details"], True)
        self.assertIs(mine["manual"], False)

    def test_details_pair_the_receipt_with_its_charge_on_save(self):
        rid, _ = self._snap()
        r = self.owner.post(f"/api/receipts/{rid}/details", json={
            "total": "27.35", "date": TODAY.isoformat(),
            "merchant": "Lantern Hardware"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["txn_id"], self.txn)
        self.assertNotIn(rid, [w["id"] for w in r.json()["waiting"]])
        row = _db(self.tid, "SELECT txn_id, match_method, status, parsed"
                            " FROM receipts WHERE id=%s", (rid,))
        self.assertEqual((row["txn_id"], row["match_method"], row["status"]),
                         (self.txn, "auto", "parsed"))
        self.assertEqual(row["parsed"]["source"], "manual")
        self.assertEqual(row["parsed"]["total"], 27.35)
        acts = _db(self.tid, "SELECT count(*) AS n FROM activity_log"
                             " WHERE kind='receipt' AND action='details'")
        self.assertGreaterEqual(acts["n"], 1)
        # undo it so the charge is free for the other tests
        self.owner.post(f"/api/receipts/{rid}/unmatch")
        self.owner.delete(f"/api/receipts/{rid}")

    def test_saving_the_same_details_again_changes_nothing(self):
        rid, _ = self._snap()
        body = {"total": 12.5, "date": TODAY.isoformat()}
        first = self.owner.post(f"/api/receipts/{rid}/details", json=body)
        again = self.owner.post(f"/api/receipts/{rid}/details", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(again.status_code, 200, again.text)
        a, b = self._mine(first.json(), rid), self._mine(again.json(), rid)
        for k in ("status", "total", "date", "merchant", "needs_details",
                  "manual", "candidates"):
            self.assertEqual(a[k], b[k], k)
        self.assertEqual((b["status"], b["total"], b["needs_details"]),
                         ("parsed", 12.5, False))

    def test_details_correct_a_failed_parse(self):
        rid, _ = self._snap()
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("UPDATE receipts SET status='failed',"
                         " error='could not read it' WHERE id=%s", (rid,))
        finally:
            conn.close()
        mine = self._mine(self.owner.get("/api/receipts/waiting").json(), rid)
        self.assertIs(mine["needs_details"], True)
        r = self.owner.post(f"/api/receipts/{rid}/details", json={
            "total": 3.99, "date": TODAY.isoformat()})
        self.assertEqual(r.status_code, 200, r.text)
        mine = self._mine(r.json(), rid)
        self.assertEqual((mine["status"], mine["error"], mine["total"]),
                         ("parsed", None, 3.99))

    def test_bad_details_are_refused(self):
        rid, _ = self._snap()
        for body in ({"date": TODAY.isoformat()},
                     {"total": 0, "date": TODAY.isoformat()},
                     {"total": -4, "date": TODAY.isoformat()},
                     {"total": "nan", "date": TODAY.isoformat()},
                     {"total": 4, "date": "yesterday"},
                     {"total": 4, "date": (TODAY + dt.timedelta(days=5))
                      .isoformat()},
                     {"total": 4, "date": TODAY.isoformat(),
                      "merchant": "x" * 81}):
            self.assertEqual(self.owner.post(
                f"/api/receipts/{rid}/details", json=body).status_code,
                400, body)
        self.assertEqual(self.owner.post(
            "/api/receipts/not-a-uuid/details",
            json={"total": 4, "date": TODAY.isoformat()}).status_code, 404)

    def test_a_viewer_may_not_enter_details(self):
        rid, _ = self._snap()
        self.assertEqual(self.viewer.post(
            f"/api/receipts/{rid}/details",
            json={"total": 4, "date": TODAY.isoformat()}).status_code, 403)
        self.assertEqual(_db(self.tid, "SELECT status FROM receipts"
                                       " WHERE id=%s", (rid,))["status"],
                         "uploaded")


class DemoRefusesDetailsTests(unittest.TestCase):
    def test_a_demo_household_cannot_enter_details(self):
        app = _app()
        client, tid = _signup(app, "naid")
        rid = client.post("/api/receipts", files={"file": PNG}).json()["id"]
        from oikonome.web import demoguard
        conn = tenancy.tenant_connect(tid)
        try:
            cfg = budget.load_config(conn)
            cfg["demo_mode"] = True
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        demoguard._cache.clear()
        try:
            r = client.post(f"/api/receipts/{rid}/details",
                            json={"total": 4, "date": TODAY.isoformat()})
            self.assertEqual(r.status_code, 403, r.text)
        finally:
            demoguard._cache.clear()


def _vision(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(payload)}}]})
    return httpx.MockTransport(handler)


class AParseNeverOverwritesDetailsTests(unittest.TestCase):
    MISREAD = {"merchant": "L4NTERN", "date": TODAY.isoformat(),
               "total": 72.35, "tax": 1.1, "tip": None,
               "items": [{"description": "nails", "amount": 72.35}]}

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        cfg = budget.load_config(self.conn)
        cfg["llm_url"] = "http://llm.test"
        cfg["llm_model"] = "vision-model"
        budget.save_config(self.conn, cfg)
        self.rid = receipts.add(self.conn, None, b"\x89PNG...", "image/png")

    def tearDown(self):
        # a pooled connection left open per test starves the tenant pool
        # for whatever module runs next
        self.conn.close()

    def _parsed(self):
        return self.conn.execute(
            "SELECT status, parsed FROM receipts WHERE id=%s",
            (self.rid,)).fetchone()

    def test_a_retry_after_manual_details_is_refused(self):
        receipts.set_details(self.conn, self.rid, total=27.35, date=TODAY)
        self.assertFalse(receipts.claim(self.conn, self.rid))
        out = receipts.parse_one(self.conn, self.rid,
                                 transport=_vision(self.MISREAD))
        self.assertEqual(out, {"status": "parsing"})
        row = self._parsed()
        self.assertEqual((row["status"], row["parsed"]["total"],
                          row["parsed"]["source"]),
                         ("parsed", 27.35, "manual"))

    def test_a_parse_in_flight_when_details_are_saved_keeps_them(self):
        self.assertTrue(receipts.claim(self.conn, self.rid))
        receipts.set_details(self.conn, self.rid, total=27.35, date=TODAY)
        out = receipts.parse_one(self.conn, self.rid, claimed=True,
                                 transport=_vision(self.MISREAD))
        self.assertEqual(out.get("kept"), "manual")
        row = self._parsed()
        self.assertEqual((row["status"], row["parsed"]["total"]),
                         ("parsed", 27.35))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM receipt_items WHERE receipt_id=%s",
            (self.rid,)).fetchone())

    def test_a_failing_parse_in_flight_does_not_mark_details_failed(self):
        self.assertTrue(receipts.claim(self.conn, self.rid))
        receipts.set_details(self.conn, self.rid, total=27.35, date=TODAY)

        def boom(request):
            return httpx.Response(500, json={"error": "down"})
        receipts.parse_one(self.conn, self.rid, claimed=True,
                           transport=httpx.MockTransport(boom))
        self.assertEqual(self._parsed()["status"], "parsed")

    def test_a_correction_keeps_what_the_ai_read_right(self):
        receipts.parse_one(self.conn, self.rid,
                           transport=_vision(self.MISREAD))
        receipts.set_details(self.conn, self.rid, total=27.35, date=TODAY)
        p = self._parsed()["parsed"]
        self.assertEqual((p["merchant"], p["tax"], p["total"]),
                         ("L4NTERN", 1.1, 27.35))


class NeedsDetailsIsPureTests(unittest.TestCase):
    def test_who_is_asked(self):
        nd = receipt_match.needs_details
        self.assertTrue(nd("uploaded", None, False))
        self.assertFalse(nd("uploaded", None, True))
        self.assertFalse(nd("parsing", None, False))
        self.assertTrue(nd("failed", None, True))
        self.assertTrue(nd("parsed", None, True))
        self.assertFalse(nd("parsed", 4.0, False))


if __name__ == "__main__":
    unittest.main()
