"""A bill's cadence is bounded by how long its cycle is, not by its count.

An interval of 240 is twenty years for a MONTHLY bill but 240 years for a
YEARLY one. Accepting the latter let the nightly due-date roll carry the
bill's due date past the years the product can read; from then on every
night's bill maintenance failed on that one row and rolled back for the
whole household, and an export holding it could not be restored. So:

- a save is refused when the cycle is longer than about twenty years,
  whatever the frequency, and the machine never files or approves one;
- the nightly roll skips a bill whose stored or next due date is out of
  range instead of failing the pass;
- a restore keeps such a bill without its due date and says so, and
  brings an over-long cycle down to the longest one allowed;
- an amount-only edit never carries an over-long stored cycle forward.
"""

import csv
import datetime as dt
import io
import json
import os
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills
from oikonome.sync import restore

from .util import (TODAY, _ensure_db, add_bill, make_db, seed_accounts,
                   write_config)


def _make_client() -> TestClient:
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


class SavingABillBoundsItsCycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"cyc-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _save(self, payee, cadence):
        return self.client.post("/api/bills/save", json={
            "payee": payee, "amount": "120", "cadence": cadence,
            "next_due": TODAY.isoformat()})

    def test_a_cycle_past_twenty_years_is_refused(self):
        for cadence in ("YEARLY:240", "YEARLY:21", "MONTHLY:300",
                        "WEEKLY:2000", "DAILY:9000", "MONTHLY:0"):
            with self.subTest(cadence=cadence):
                r = self._save(f"Long Cycle {cadence}", cadence)
                self.assertEqual(r.status_code, 400, r.text)

    def test_a_cycle_up_to_twenty_years_is_accepted(self):
        for cadence in ("YEARLY:20", "MONTHLY:240", "WEEKLY:1043",
                        "DAILY:7300", "YEARLY:1"):
            with self.subTest(cadence=cadence):
                r = self._save(f"Ok Cycle {cadence}", cadence)
                self.assertEqual(r.status_code, 200, r.text)


class TheEngineNeverWritesALongerCycle(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_save_bill_refuses_it(self):
        with self.assertRaises(ValueError):
            add_bill(self.conn, "Century Fee", 50.0, frequency="YEARLY",
                     interval=240, next_due=TODAY)
        add_bill(self.conn, "Score Fee", 50.0, frequency="YEARLY",
                 interval=20, next_due=TODAY)

    def test_no_offer_is_filed_for_it(self):
        self.assertFalse(bills._insert_proposal(
            self.conn, pid="prop:long", kind="add", payee="Century Fee",
            amount=50.0, frequency="YEARLY", interval=240, next_due=TODAY))
        self.assertTrue(bills._insert_proposal(
            self.conn, pid="prop:ok", kind="add", payee="Score Fee",
            amount=50.0, frequency="YEARLY", interval=20, next_due=TODAY))


class TheNightlySurvivesAnOutOfRangeBill(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _plant(self, bid, due, recurrence):
        # written straight to the table, as an older install could have
        self.conn.execute(
            """INSERT INTO bills (id, type, payee, amount, frequency,
                                  monthly_amount, due_on, active,
                                  is_completed, raw)
               VALUES (%s, 'BILL', %s, -50, %s, -1, %s, 1, 0, %s::jsonb)""",
            (bid, bid, recurrence["frequency"], due,
             json.dumps({"source": "manual", "bill_type": "occurrence",
                         "dueOn": due, "recurrence": recurrence})))

    def _due(self, bid):
        return self.conn.execute("SELECT due_on FROM bills WHERE id=%s",
                                 (bid,)).fetchone()["due_on"]

    def test_a_far_future_due_date_does_not_abort_the_pass(self):
        self._plant("far-future", "2266-03-01",
                    {"frequency": "YEARLY", "interval": 240})
        past = (TODAY - dt.timedelta(days=40)).isoformat()
        self._plant("ordinary", past, {"frequency": "MONTHLY", "interval": 1})
        bills.run(self.conn, TODAY)              # must not raise
        self.assertGreaterEqual(self._due("ordinary"), TODAY)
        self.assertEqual(self._due("far-future"), dt.date(2266, 3, 1))

    def test_a_roll_that_would_land_out_of_range_is_skipped(self):
        past = (TODAY - dt.timedelta(days=40)).isoformat()
        self._plant("century", past, {"frequency": "YEARLY", "interval": 240})
        stats = {"due_advanced": 0}
        bills._advance_due_dates(self.conn, TODAY, stats)
        self.assertEqual(self._due("century").isoformat(), past)
        self.assertEqual(stats["due_advanced"], 0)


class ARestoreKeepsTheBillWithoutTheDate(unittest.TestCase):
    COLS = ["id", "type", "payee", "amount", "frequency", "monthly_amount",
            "due_on", "category", "merchant", "is_completed", "active", "raw"]

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _zip(self, rows):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=self.COLS)
            w.writeheader()
            for b in rows:
                w.writerow({**{c: "" for c in self.COLS}, **b})
            z.writestr("bills.csv", s.getvalue())
        return buf.getvalue()

    def test_an_out_of_range_due_date_is_dropped_with_a_note(self):
        counts = restore.restore_zip(self.conn, self._zip([
            {"id": "b-far", "type": "bill", "payee": "Far Future Co",
             "amount": "-50", "frequency": "YEARLY", "active": "1",
             "due_on": "2266-03-01",
             "raw": json.dumps({"dueOn": "2266-03-01",
                                "recurrence": {"frequency": "YEARLY",
                                               "interval": 1}})},
            {"id": "b-ok", "type": "bill", "payee": "Ordinary Co",
             "amount": "-20", "frequency": "MONTHLY", "active": "1",
             "due_on": TODAY.isoformat(),
             "raw": json.dumps({"recurrence": {"frequency": "MONTHLY",
                                               "interval": 1}})}]))
        self.assertEqual(counts.get("bills"), 2, counts)
        rows = {r["id"]: r for r in self.conn.execute(
            "SELECT id, due_on, raw FROM bills").fetchall()}
        self.assertIsNone(rows["b-far"]["due_on"])
        raw = rows["b-far"]["raw"]
        raw = raw if isinstance(raw, dict) else json.loads(raw)
        self.assertNotIn("dueOn", raw)
        self.assertEqual(rows["b-ok"]["due_on"], TODAY)
        self.assertTrue(any("due date outside" in n
                            for n in counts.get("_notes", [])), counts)
        bills.run(self.conn, TODAY)              # and the nightly runs

    def test_a_cycle_past_twenty_years_comes_back_at_the_longest_allowed(self):
        # the count cap (240) is twenty years only for MONTHLY: a YEARLY:240
        # bill restored as-is would be skipped by every nightly roll and
        # read overdue for good
        for freq, kept in (("YEARLY", 20), ("WEEKLY", 240),
                           ("MONTHLY", 240)):
            with self.subTest(freq=freq):
                self.conn.execute("DELETE FROM bills")
                restore.restore_zip(self.conn, self._zip([
                    {"id": f"b-{freq}", "type": "bill", "payee": "Long Co",
                     "amount": "-50", "frequency": freq, "active": "1",
                     "due_on": TODAY.isoformat(),
                     "raw": json.dumps({"recurrence": {
                         "frequency": freq, "interval": 240}})}]))
                raw = self.conn.execute(
                    "SELECT raw FROM bills WHERE id=%s",
                    (f"b-{freq}",)).fetchone()["raw"]
                raw = raw if isinstance(raw, dict) else json.loads(raw)
                rec = raw["recurrence"]
                self.assertEqual(rec["interval"], kept)
                self.assertFalse(bills.cadence_too_long(rec["frequency"],
                                                        rec["interval"]))


class AnAmountOnlyEditOfALegacyLongCycle(unittest.TestCase):
    """The attention queue's Accept sends an amount alone and the route
    carries the bill's own cadence forward — but not one the save itself
    would refuse, or Accept fails until a new cadence is picked by hand."""

    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"cyc-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            for payee, iv in (("Legacy Long Co", 25), ("Legacy Ok Co", 3)):
                add_bill(conn, payee, 100.0, frequency="YEARLY",
                         next_due=TODAY)
                conn.execute(
                    "UPDATE bills SET raw = jsonb_set(raw, "
                    "'{recurrence,interval}', %s::jsonb) WHERE payee=%s",
                    (str(iv), payee))
        finally:
            conn.close()

    def _accept(self, payee):
        return self.client.post("/api/bills/save", json={
            "payee": payee, "orig_payee": payee, "amount": "130"})

    def _rec(self, payee):
        conn = tenancy.tenant_connect(self.tid)
        try:
            raw = bills.get_bill(conn, payee)["raw"]
        finally:
            conn.close()
        raw = raw if isinstance(raw, dict) else json.loads(raw)
        return raw.get("recurrence") or {}

    def test_an_over_long_stored_cadence_falls_back_to_the_default(self):
        r = self._accept("Legacy Long Co")
        self.assertEqual(r.status_code, 200, r.text)
        rec = self._rec("Legacy Long Co")
        self.assertFalse(bills.cadence_too_long(rec.get("frequency"),
                                                rec.get("interval")))

    def test_a_stored_cadence_inside_the_bound_is_kept(self):
        r = self._accept("Legacy Ok Co")
        self.assertEqual(r.status_code, 200, r.text)
        rec = self._rec("Legacy Ok Co")
        self.assertEqual((rec.get("frequency"), rec.get("interval")),
                         ("YEARLY", 3))


if __name__ == "__main__":
    unittest.main()
