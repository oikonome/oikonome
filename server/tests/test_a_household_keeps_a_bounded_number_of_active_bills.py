"""A household keeps a bounded number of active bills.

Every Today load, the hourly email sweep and the nightly pass expand every
active bill into its occurrences and match them against the month's
spending, in a process every household shares. With no ceiling, one
household could save (or restore) thousands of daily bills and make each
of those requests build millions of occurrences. So:

- a save that adds an active bill past the ceiling is refused, through
  the Bills page's door as a 400 that names the limit;
- an edit of a bill already counted, and archiving one, still work at the
  ceiling — a household at the limit can fix what it has;
- bringing an archived bill back counts as adding one;
- approving a detected offer past the ceiling leaves the offer pending;
- restoring an archived bill from the Bills page past the ceiling is
  refused (a 400 naming the limit) and changes nothing;
- a backup restore brings the active bills past the ceiling back
  archived, and says how many.
"""

import csv
import io
import json
import os
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills
from oikonome.engine.compat import jsonb
from oikonome.sync import restore

from .util import TODAY, _ensure_db, add_bill, make_db, write_config

CAP = bills.MAX_ACTIVE_BILLS


def _fill(conn, n, *, active=1, prefix="filler"):
    """Plant `n` plain monthly bills directly — the ceiling is about how
    many rows exist, not how they were made."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO bills (id, type, payee, amount, frequency, "
            "monthly_amount, due_on, is_completed, active, synced_at, raw) "
            "VALUES (%s,'BILL',%s,-10,'MONTHLY',-10,%s,0,%s,now(),%s)",
            [(bills._slug(f"{prefix.title()} Co {i}"),
              f"{prefix.title()} Co {i}", TODAY, active,
              jsonb({"recurrence": {"frequency": "MONTHLY", "interval": 1,
                                    "byMonthDay": [TODAY.day]}}))
             for i in range(n)])


class SavingPastTheCeiling(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_new_bill_past_the_ceiling_is_refused(self):
        _fill(self.conn, CAP - 1)
        add_bill(self.conn, "Last Room Co", 12.0)
        self.assertEqual(bills.active_bill_count(self.conn), CAP)
        with self.assertRaisesRegex(ValueError, str(CAP)):
            add_bill(self.conn, "One Too Many Co", 12.0,
                     frequency="DAILY")
        self.assertIsNone(bills.get_bill(self.conn, "One Too Many Co"))

    def test_an_edit_at_the_ceiling_still_saves(self):
        _fill(self.conn, CAP - 1)
        add_bill(self.conn, "Edited Co", 12.0)
        add_bill(self.conn, "Edited Co", 15.0)
        amt = self.conn.execute("SELECT amount FROM bills WHERE payee=%s",
                                ("Edited Co",)).fetchone()["amount"]
        self.assertAlmostEqual(abs(amt), 15.0)

    def test_archived_bills_do_not_count_but_reviving_one_does(self):
        _fill(self.conn, CAP)
        _fill(self.conn, 1, active=0, prefix="archived")
        payee = "Archived Co 0"
        # an edit that keeps it archived is not an addition
        add_bill(self.conn, payee, 20.0)
        with self.assertRaisesRegex(ValueError, str(CAP)):
            add_bill(self.conn, payee, 20.0, revive=True)
        self.conn.execute("UPDATE bills SET active=0 WHERE payee='Filler Co 0'")
        add_bill(self.conn, payee, 20.0, revive=True)
        self.assertEqual(bills.active_bill_count(self.conn), CAP)

    def test_an_offer_past_the_ceiling_stays_pending(self):
        _fill(self.conn, CAP)
        bills._insert_proposal(self.conn, pid="p-over", kind="add",
                               payee="Offered Co", amount=9.0,
                               frequency="MONTHLY", interval=1)
        r = bills.apply_proposal(self.conn, "p-over", "approve")
        self.assertIn(str(CAP), r.get("error") or "", r)
        st = self.conn.execute("SELECT status FROM bill_proposals "
                               "WHERE id='p-over'").fetchone()["status"]
        self.assertEqual(st, "pending")


class TheSaveDoorNamesTheLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"cap-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            write_config(conn)
            _fill(conn, CAP)
        finally:
            conn.close()

    def test_an_add_past_the_ceiling_is_a_400_naming_it(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "Door Co", "amount": 5, "cadence": "DAILY:1",
            "next_due": ""})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn(str(CAP), r.json()["detail"])

    def test_restoring_an_archived_bill_past_the_ceiling_is_a_400(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            _fill(conn, 1, active=0, prefix="shelved")
            before = bills.active_bill_count(conn)
        finally:
            conn.close()
        r = self.client.post("/api/bills/restore",
                             json={"payee": "Shelved Co 0"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn(str(CAP), r.json()["detail"])
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertEqual(bills.active_bill_count(conn), before)
            act = conn.execute("SELECT active FROM bills WHERE payee=%s",
                               ("Shelved Co 0",)).fetchone()["active"]
            self.assertEqual(act, 0)
        finally:
            conn.close()

    def test_an_edit_at_the_ceiling_goes_through(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "Filler Co 1", "orig_payee": "Filler Co 1",
            "amount": 11, "cadence": "MONTHLY:1", "next_due": ""})
        self.assertEqual(r.status_code, 200, r.text)


class RestoringPastTheCeiling(unittest.TestCase):
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

    def _row(self, i, active="1"):
        return {"id": f"r-{i}", "type": "BILL", "payee": f"Restored Co {i}",
                "amount": "-5", "frequency": "DAILY", "active": active,
                "raw": json.dumps({"recurrence": {"frequency": "DAILY",
                                                  "interval": 1}})}

    def test_active_bills_past_the_ceiling_come_back_archived(self):
        _fill(self.conn, CAP - 2)
        counts = restore.restore_zip(self.conn, self._zip(
            [self._row(i) for i in range(5)]
            + [self._row(f"a{i}", active="0") for i in range(3)]))
        self.assertEqual(bills.active_bill_count(self.conn), CAP)
        self.assertEqual(counts.get("bills"), 5 + 3, counts)
        archived = self.conn.execute(
            "SELECT count(*) AS n FROM bills WHERE active=0 "
            "AND payee LIKE 'Restored Co %%'").fetchone()["n"]
        self.assertEqual(archived, 3 + 3)
        self.assertTrue(any("3 active bills past the limit" in n
                            and "archived" in n
                            for n in counts.get("_notes", [])), counts)


class RestoringAnArchivedBillPastTheCeiling(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_it_is_refused_and_nothing_changes(self):
        _fill(self.conn, CAP)
        _fill(self.conn, 1, active=0, prefix="archived")
        with self.assertRaisesRegex(ValueError, str(CAP)):
            bills.restore_bill(self.conn, "Archived Co 0")
        self.assertEqual(bills.active_bill_count(self.conn), CAP)
        act = self.conn.execute("SELECT active FROM bills WHERE payee=%s",
                                ("Archived Co 0",)).fetchone()["active"]
        self.assertEqual(act, 0)

    def test_restoring_an_already_active_bill_at_the_ceiling_is_a_no_op(self):
        _fill(self.conn, CAP)
        self.assertEqual(bills.restore_bill(self.conn, "Filler Co 0"), 1)
        self.assertEqual(bills.active_bill_count(self.conn), CAP)

    def test_below_the_ceiling_it_comes_back(self):
        _fill(self.conn, CAP - 1)
        _fill(self.conn, 1, active=0, prefix="archived")
        self.assertEqual(bills.restore_bill(self.conn, "Archived Co 0"), 1)
        self.assertEqual(bills.active_bill_count(self.conn), CAP)
