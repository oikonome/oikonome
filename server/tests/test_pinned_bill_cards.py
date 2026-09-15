"""Bills pinned to the Today page ("pin to Today" bill setting).

A pinned bill gets its own card on the Today page and in the daily email:
an envelope pin answers "how much is left in the pool this period", a fixed
(occurrence) pin answers "how much of this bill is still to go out, and
when". Envelope bills may additionally match by CATEGORY (`match_category`):
spend of the bill's category counts toward the pool even when the merchants
share nothing — a pool whose money goes to several unrelated payees (a
service, ATM cash, P2P counterparties) that no merchant matcher can unify.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.engine.compat import as_dict
from oikonome.web import report

from .util import (TODAY, _ensure_db, add_bill, add_txn, make_db,
                   seed_accounts, write_config)


class CategoryEnvelopeTests(unittest.TestCase):
    """An envelope with match_category pools its category's spend."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Helper", 1600.0, bill_type="envelope",
                 frequency=None, interval=1, category="CHILD CARE",
                 match_category=True, show_today=True)

    def tearDown(self):
        self.conn.close()

    def status(self):
        return budget.month_status(self.conn, TODAY)

    def test_unrelated_merchants_pool_by_category(self):
        """THE POINT: three merchants sharing no token all draw the pool."""
        add_txn(self.conn, TODAY, 380.0, "ACME BANK ATM WITHDRAWAL",
                override="CHILD CARE")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 60.0,
                "VENMO PAYMENT SITTER", override="CHILD CARE")
        add_txn(self.conn, TODAY - dt.timedelta(days=4), 39.0,
                "ACME SITTERS", override="CHILD CARE")
        st = self.status()
        env = st["buckets"]["fixed"]["envelopes"][0]
        self.assertAlmostEqual(env["used"], 479.0, places=2)
        self.assertAlmostEqual(env["period_left"], 1600.0 - 479.0, places=2)
        # the rows are fixed spend now, not "everything else"
        self.assertEqual(st["buckets"]["other"]["actual"], 0.0)

    def test_other_categories_stay_variable(self):
        add_txn(self.conn, TODAY, 25.0, "TARGET", primary="GENERAL_MERCHANDISE")
        st = self.status()
        self.assertEqual(st["buckets"]["fixed"]["envelopes"][0]["used"], 0.0)
        self.assertAlmostEqual(st["buckets"]["other"]["actual"], 25.0, places=2)

    def test_merchant_match_beats_category_match(self):
        """A row matching another envelope's MERCHANT belongs to that
        envelope even when its category names this one (mirrors the custom
        buckets' merchant-over-category rule)."""
        add_bill(self.conn, "Corner Coffee", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        add_txn(self.conn, TODAY, 30.0, "CORNER COFFEE", override="CHILD CARE")
        st = self.status()
        by = {e["payee"]: e for e in st["buckets"]["fixed"]["envelopes"]}
        self.assertAlmostEqual(by["Corner Coffee"]["used"], 30.0, places=2)
        self.assertEqual(by["Helper"]["used"], 0.0)

    def test_occurrence_bill_still_wins_the_row(self):
        """A row already matched to a scheduled occurrence never leaks into
        a category envelope — occurrence matching runs first."""
        add_bill(self.conn, "Daycare Co", 500.0,
                 next_due=TODAY, last_seen=TODAY - dt.timedelta(days=31),
                 category="CHILD CARE")
        add_txn(self.conn, TODAY, 500.0, "DAYCARE CO", override="CHILD CARE")
        st = self.status()
        env = st["buckets"]["fixed"]["envelopes"][0]
        self.assertEqual(env["used"], 0.0)

    def test_without_flag_category_spend_stays_variable(self):
        add_bill(self.conn, "Helper", 1600.0, bill_type="envelope",
                 frequency=None, interval=1, category="CHILD CARE",
                 match_category=False)
        add_txn(self.conn, TODAY, 380.0, "ACME BANK ATM WITHDRAWAL",
                override="CHILD CARE")
        st = self.status()
        self.assertEqual(st["buckets"]["fixed"]["envelopes"][0]["used"], 0.0)
        self.assertAlmostEqual(st["buckets"]["other"]["actual"], 380.0,
                               places=2)


class CardCompositionTests(unittest.TestCase):
    """_bill_cards composes one card list all three surfaces render.

    Driven off month_status directly (fixed fixture date): report.gather
    would run this month historical=True — bills legitimately skipped — so
    the gather-path assertions live in the email tests below on the real
    date."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def cards(self):
        from oikonome.web.todayview import _bill_cards
        st = budget.month_status(self.conn, TODAY)
        st["today"] = TODAY
        return _bill_cards(st)

    def test_unpinned_bills_produce_no_cards(self):
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_bill(self.conn, "Corner Coffee", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        self.assertEqual(self.cards(), [])

    def test_envelope_card_says_whats_left_this_month(self):
        add_bill(self.conn, "Helper", 1600.0, bill_type="envelope",
                 frequency=None, interval=1, category="CHILD CARE",
                 match_category=True, show_today=True)
        add_txn(self.conn, TODAY, 380.0, "ACME BANK ATM", override="CHILD CARE")
        (c,) = self.cards()
        self.assertEqual(c["kind"], "envelope")
        self.assertEqual(c["label"], "Helper")
        self.assertAlmostEqual(c["left"], 1220.0, places=2)
        self.assertFalse(c["over"])
        self.assertEqual(c["sub"], "used $380 of $1,600 this month")
        # the pace tick sits at today's position in the MONTH (fixture 07/15)
        self.assertAlmostEqual(c["pace_frac"], 15 / 31, places=4)
        self.assertIsNone(c["status"])

    def test_annual_pool_paces_on_the_year(self):
        add_bill(self.conn, "Domain Registrar", 120.0, bill_type="envelope",
                 frequency=None, interval=12, show_today=True)
        (c,) = self.cards()
        self.assertAlmostEqual(c["pace_frac"],
                               TODAY.timetuple().tm_yday / 365, places=4)

    def test_spent_envelope_reports_the_overflow(self):
        add_bill(self.conn, "Helper", 100.0, bill_type="envelope",
                 frequency=None, interval=1, category="CHILD CARE",
                 match_category=True, show_today=True)
        add_txn(self.conn, TODAY, 150.0, "ACME BANK ATM", override="CHILD CARE")
        (c,) = self.cards()
        self.assertEqual(c["left"], 0.0)
        self.assertTrue(c["over"])
        self.assertEqual(c["status_tone"], "neg")
        self.assertIn("over by $50", c["status"])
        self.assertIn("variable", c["status"])

    def test_fixed_pin_unpaid_says_due(self):
        due = TODAY + dt.timedelta(days=3)
        add_bill(self.conn, "Rent", 2000.0, next_due=due,
                 last_seen=due - dt.timedelta(days=31), show_today=True)
        (c,) = self.cards()
        self.assertEqual(c["kind"], "bill")
        self.assertAlmostEqual(c["left"], 2000.0, places=2)
        self.assertEqual(c["sub"], "paid $0 of $2,000 this month")
        self.assertEqual(c["status"], f"due {due.strftime('%m/%d')}")
        self.assertAlmostEqual(c["pace_frac"], 15 / 31, places=4)
        self.assertFalse(c["over"])

    def test_fixed_pin_paid_says_all_paid(self):
        due = TODAY.replace(day=2)
        add_bill(self.conn, "Rent", 2000.0, next_due=due,
                 last_seen=due - dt.timedelta(days=31), show_today=True)
        add_txn(self.conn, due, 2000.0, "RENT", primary="RENT_AND_UTILITIES")
        (c,) = self.cards()
        self.assertEqual(c["left"], 0.0)
        self.assertEqual(c["status"], "all paid")
        self.assertEqual(c["status_tone"], "pos")

    def test_fixed_pin_overdue_is_loud(self):
        due = TODAY - dt.timedelta(days=3)
        add_bill(self.conn, "Rent", 2000.0, next_due=due,
                 last_seen=due - dt.timedelta(days=31), show_today=True)
        (c,) = self.cards()
        self.assertTrue(c["over"])
        self.assertEqual(c["status_tone"], "neg")
        self.assertEqual(c["status"],
                         f"overdue — was due {due.strftime('%m/%d')}")


class EmailMirrorTests(unittest.TestCase):
    """The daily email (HTML + plain) carries the same pinned cards.

    Runs on the REAL date: gather() treats any other month as historical and
    correctly drops the live bill schedule, which is not what's under test.
    An envelope pin has no occurrence-date dependence, so the real date is
    deterministic here."""

    NOW = dt.date.today()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Helper", 1600.0, bill_type="envelope",
                 frequency=None, interval=1, category="CHILD CARE",
                 match_category=True, show_today=True)
        add_txn(self.conn, self.NOW, 380.0, "ACME BANK ATM",
                override="CHILD CARE")
        self.d = report.gather(self.conn, self.NOW)

    def tearDown(self):
        self.conn.close()

    def test_html_has_the_card(self):
        _, _, html = report.build(self.d)
        self.assertIn("Pinned bills", html)
        self.assertIn("used $380 of $1,600 this month", html)

    def test_plain_has_the_card(self):
        _, plain, _ = report.build(self.d)
        self.assertIn("Pinned bills:", plain)
        self.assertIn("Helper $1,220 left", plain)

    def test_no_pins_no_section(self):
        add_bill(self.conn, "Helper", 1600.0, bill_type="envelope",
                 frequency=None, interval=1, category="CHILD CARE",
                 match_category=True, show_today=False)
        _, plain, html = report.build(report.gather(self.conn, self.NOW))
        self.assertNotIn("Pinned bills", html)
        self.assertNotIn("Pinned bills", plain)


class SaveBillFlagTests(unittest.TestCase):
    """The flags survive the save_bill raw rebuild (the lock_amount trap)."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def raw(self, payee="Helper"):
        return as_dict(self.conn.execute(
            "SELECT raw FROM bills WHERE payee=%s", (payee,)).fetchone()["raw"])

    def test_flags_survive_an_edit_that_omits_them(self):
        add_bill(self.conn, "Helper", 1600.0, bill_type="envelope",
                 frequency=None, interval=1, show_today=True,
                 match_category=True, category="CHILD CARE")
        # a later edit (amount tweak) that doesn't mention the flags
        add_bill(self.conn, "Helper", 1700.0, bill_type="envelope",
                 frequency=None, interval=1)
        r = self.raw()
        self.assertTrue(r.get("show_today"))
        self.assertTrue(r.get("match_category"))

    def test_explicit_false_clears(self):
        add_bill(self.conn, "Helper", 1600.0, bill_type="envelope",
                 frequency=None, interval=1, show_today=True)
        add_bill(self.conn, "Helper", 1600.0, bill_type="envelope",
                 frequency=None, interval=1, show_today=False)
        self.assertNotIn("show_today", self.raw())


class BillsApiTests(unittest.TestCase):
    """/api/bills/save round-trips the flags; /api/today/full ships cards."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"pin-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_save_and_read_back(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "Helper", "amount": "1600", "cadence": "ENVELOPE:1",
            "show_today": True, "match_category": True,
            "category": "CHILD CARE"})
        self.assertEqual(r.status_code, 200, r.text)
        d = self.client.get("/api/bills/bill?payee=Helper").json()
        self.assertTrue(d["show_today"])
        self.assertTrue(d["match_category"])
        self.assertEqual(d["category"], "CHILD CARE")
        # an edit that omits the keys keeps them (the `cap` contract)
        r = self.client.post("/api/bills/save", json={
            "payee": "Helper", "amount": "1700", "cadence": "ENVELOPE:1",
            "orig_payee": "Helper"})
        self.assertEqual(r.status_code, 200, r.text)
        d = self.client.get("/api/bills/bill?payee=Helper").json()
        self.assertTrue(d["show_today"])
        self.assertTrue(d["match_category"])
        self.assertEqual(d["category"], "CHILD CARE")

    def test_today_full_ships_cards(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "Sitter Pool", "amount": "200", "cadence": "ENVELOPE:1",
            "show_today": True, "match_category": True,
            "category": "CHILD CARE"})
        self.assertEqual(r.status_code, 200, r.text)
        cards = self.client.get("/api/today/full").json()["bill_cards"]
        self.assertTrue(any(c["label"] == "Sitter Pool"
                            and c["kind"] == "envelope" for c in cards))

    def test_category_length_capped(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "X", "amount": "10", "cadence": "ENVELOPE:1",
            "category": "C" * 65})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
