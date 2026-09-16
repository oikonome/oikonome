"""Companion charges: the fee that rides with a bill's payment.

A bill is one transaction per occurrence; the $1 convenience fee a
processor posts beside the water bill belongs to that bill; otherwise it
is proposed as its own $1 bill and missing from the bill's cost. A companion
attaches to the SAME occurrence when it lands within its window, counts
toward what was paid, is never proposed alone once attached, and is
offered as an attachment when the detector sees it riding with a bill.
"""
import datetime as dt
import unittest

from oikonome.engine import bills, budget
from oikonome.engine.compat import as_dict

from .util import TODAY, add_bill, add_txn, make_db, write_config


def _bill_with_fee(conn, **kw):
    add_bill(conn, "Zenith Water", 90.00, frequency="MONTHLY",
             next_due=dt.date(TODAY.year, TODAY.month, 5),
             companions=[{"tokens": "servicing surcharge", "amount": 1.0,
                          "window_days": 3}], **kw)


class CompanionMatchingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _occs(self):
        cfg = budget.load_config(self.conn)
        bl = budget._recurring_bills(self.conn, caps=cfg.get("occurrence_caps"))
        rows = budget._spend_rows(self.conn, TODAY.replace(day=1),
                                  TODAY + dt.timedelta(days=1))
        occs = budget.month_occurrences(bl, TODAY.year, TODAY.month)
        matched, occs = budget.match_occurrences(rows, occs)
        return matched, occs, rows

    def test_companion_attaches_within_window_and_plans_on_top(self):
        _bill_with_fee(self.conn)
        d = dt.date(TODAY.year, TODAY.month, 5)
        add_txn(self.conn, d, 90.00, "ZENITH WATER 0123",
                primary="RENT_AND_UTILITIES")
        add_txn(self.conn, d + dt.timedelta(days=1), 1.00, "SERVICING SURCHARGE",
                primary="GENERAL_SERVICES")
        matched, occs, rows = self._occs()
        occ = next(o for o in occs if o["bill"]["payee"] == "Zenith Water")
        self.assertEqual(occ["planned"], 91.00)          # charge + fee
        self.assertIsNotNone(occ["txn"])
        self.assertEqual([r["amount"] for r in occ["companions"]], [1.0])
        self.assertEqual(len(matched), 2)                # both rows claimed

    def test_companion_outside_the_window_is_not_attached(self):
        _bill_with_fee(self.conn)
        d = dt.date(TODAY.year, TODAY.month, 5)
        add_txn(self.conn, d, 90.00, "ZENITH WATER 0123")
        add_txn(self.conn, d + dt.timedelta(days=9), 1.00, "SERVICING SURCHARGE")
        matched, occs, _ = self._occs()
        occ = next(o for o in occs if o["bill"]["payee"] == "Zenith Water")
        self.assertEqual(occ["companions"], [])
        self.assertEqual(len(matched), 1)

    def test_paid_amount_counts_both_legs(self):
        from oikonome.web import api as web_api
        _bill_with_fee(self.conn)
        d = dt.date(TODAY.year, TODAY.month, 5)
        add_txn(self.conn, d, 90.00, "ZENITH WATER 0123")
        add_txn(self.conn, d, 1.00, "SERVICING SURCHARGE")
        stats = web_api._month_bill_stats(self.conn, budget.load_config(self.conn),
                                          TODAY)
        self.assertEqual(stats["Zenith Water"]["paid_amount"], 91.00)

    def test_fee_row_inherits_the_bills_transaction_category(self):
        _bill_with_fee(self.conn, txn_category="Utilities")
        d = dt.date(TODAY.year, TODAY.month, 5)
        add_txn(self.conn, d, 90.00, "ZENITH WATER 0123",
                txn_id="main")
        add_txn(self.conn, d, 1.00, "SERVICING SURCHARGE", txn_id="fee")
        bills.apply_txn_categories(self.conn)
        cats = {r["id"]: r["category_override"] for r in self.conn.execute(
            "SELECT id, category_override FROM transactions "
            "WHERE id IN ('main','fee')").fetchall()}
        self.assertEqual(cats, {"main": "Utilities", "fee": "Utilities"})

    def test_save_keeps_companions_unless_told(self):
        _bill_with_fee(self.conn)
        bills.save_bill(self.conn, payee="Zenith Water", amount=92,
                        frequency="MONTHLY", next_due=TODAY)
        raw = as_dict(bills.get_bill(self.conn, "Zenith Water")["raw"])
        self.assertEqual(len(raw["companions"]), 1)
        self.assertEqual(bills.fee_total(raw), 1.0)
        bills.save_bill(self.conn, payee="Zenith Water", amount=92,
                        frequency="MONTHLY", next_due=TODAY, companions=[])
        raw = as_dict(bills.get_bill(self.conn, "Zenith Water")["raw"])
        self.assertNotIn("companions", raw)


class CompanionDetectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _seed_months(self, n=5, fee=True):
        for i in range(n, 0, -1):
            y, m = TODAY.year, TODAY.month - i
            while m < 1:
                y, m = y - 1, m + 12
            d = dt.date(y, m, 5)
            add_txn(self.conn, d, 90.00, "ZENITH WATER 0123",
                    primary="RENT_AND_UTILITIES")
            if fee:
                add_txn(self.conn, d + dt.timedelta(days=1), 1.00,
                        "SERVICING SURCHARGE", primary="GENERAL_SERVICES")

    def _pending(self):
        return [(p["kind"], p["payee"], as_dict(p["evidence"]))
                for p in bills.pending_proposals(self.conn)]

    def test_a_riding_fee_is_offered_as_an_attachment_not_a_bill(self):
        add_bill(self.conn, "Zenith Water", 90.00, frequency="MONTHLY",
                 next_due=dt.date(TODAY.year, TODAY.month, 5))
        self._seed_months()
        bills.run(self.conn, TODAY)
        kinds = {(k, p) for k, p, _ in self._pending()}
        self.assertNotIn(("add", "Servicing Surcharge"), kinds, kinds)
        att = [(k, p, ev) for k, p, ev in self._pending() if k == "attach"]
        self.assertEqual(len(att), 1, self._pending())
        _, payee, ev = att[0]
        self.assertEqual(ev["bill_payee"], "Zenith Water")
        self.assertGreaterEqual(ev["hits"], 3)
        # approving it attaches the companion and the fee stops being a
        # candidate of its own
        pid = bills.pending_proposals(self.conn)[0]["id"]
        out = bills.apply_proposal(self.conn, pid, "approve")
        self.assertEqual(out.get("kind"), "attach", out)
        raw = as_dict(bills.get_bill(self.conn, "Zenith Water")["raw"])
        self.assertEqual(bills.fee_total(raw), 1.0)
        bills.run(self.conn, TODAY)
        self.assertFalse([p for p in self._pending()
                          if p[1].lower().startswith("servicing surcharge")])

    def test_an_attached_fee_is_never_proposed_alone(self):
        _bill_with_fee(self.conn)
        self._seed_months()
        bills.run(self.conn, TODAY)
        self.assertFalse([p for p in self._pending()
                          if "convenience" in p[1].lower()], self._pending())


class CompanionDoorTests(unittest.TestCase):
    """/api/bills/save takes and validates companions; /api/bills and
    /api/bills/bill report them."""

    @classmethod
    def setUpClass(cls):
        from .util import _ensure_db
        _ensure_db()

    def setUp(self):
        import uuid
        from fastapi.testclient import TestClient

        from oikonome.auth import sessions
        from oikonome.db import tenancy
        from oikonome.web.app import app
        from .util import TEST_DB, _admin_dsn
        self.client = TestClient(app)
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"comp-{uuid.uuid4().hex[:8]}"))
        u = self.admin.execute(
            "INSERT INTO users (tenant_id, email, role, password_hash) "
            "VALUES (%s, %s, 'owner', 'x') RETURNING id",
            (self.tid, f"comp-{uuid.uuid4().hex[:6]}@example.dev")).fetchone()
        token = sessions.create_session(self.admin, u["id"], self.tid, "ua")
        self.client.cookies.set(sessions.COOKIE_NAME, token)

    def tearDown(self):
        self.admin.close()

    def test_save_lists_and_reads_back(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "Zenith Storage", "amount": 500, "cadence": "MONTHLY:1",
            "next_due": TODAY.isoformat(),
            "companions": [{"tokens": "payportal", "amount": 2.49,
                            "window_days": 2}]})
        self.assertEqual(r.status_code, 200, r.text)
        rows = self.client.get("/api/bills").json()["bills"]
        b = next(x for x in rows if x["payee"] == "Zenith Storage")
        self.assertEqual(b["fee_total"], 2.49)
        self.assertEqual(b["companions"][0]["tokens"], "payportal")
        one = self.client.get("/api/bills/bill",
                              params={"payee": "Zenith Storage"}).json()
        self.assertEqual(one["fee_total"], 2.49)

    def test_save_refuses_a_fee_that_is_really_a_bill(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "Zenith Storage", "amount": 500, "cadence": "MONTHLY:1",
            "next_due": TODAY.isoformat(),
            "companions": [{"tokens": "payportal", "amount": 120}]})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()


class CompanionFeeInMonthStatusTests(unittest.TestCase):
    """The month's fixed ACTUAL counts the same money as its planned.

    Planned carries the companion's fee, so the actual must add it too;
    otherwise every companion-carrying bill reports a phantom negative drift
    equal to its own fee, and the Today page's pinned 'paid' understates
    what actually left the account."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _bill_with_fee(self.conn, show_today=True)
        d = dt.date(TODAY.year, TODAY.month, 5)
        add_txn(self.conn, d, 90.00, "ZENITH WATER 0123",
                primary="RENT_AND_UTILITIES")
        add_txn(self.conn, d + dt.timedelta(days=1), 1.00, "SERVICING SURCHARGE",
                primary="GENERAL_SERVICES")

    def tearDown(self):
        self.conn.close()

    def test_actual_and_drift_count_the_fee(self):
        ms = budget.month_status(self.conn, dt.date(TODAY.year, TODAY.month, 20))
        fx = ms["buckets"]["fixed"]
        self.assertAlmostEqual(fx["actual"], 91.00, places=2)
        self.assertNotIn("Zenith Water", ms["drifts"],
                         "a paid fee is not drift — planned already carries it")
        pin = ms["pinned_bills"]
        row = next((p for p in (pin.values() if isinstance(pin, dict) else pin)
                    if p["payee"] == "Zenith Water"), None)
        if row is not None:
            self.assertAlmostEqual(row["paid"], 91.00, places=2)
