"""The Matched list reads down a column of charge dates, so it is sorted by
the charge's date; and a partial pair says what came back OF the charge.

Sorted by when each pair was made, a pair matched today against a charge
from months ago sat at the top of a column of recent dates and the list
looked unsorted. A partial row printed only what came back, with the
charge's own size in a hover title the phone does not have.
"""
import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import data as d
from oikonome.web import security

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config


class MatchedPairsOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"mp-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            recent = TODAY - dt.timedelta(days=3)
            e = add_txn(conn, recent, 40.0, "INVENTED KITE SHOP")
            p = add_txn(conn, recent, -40.0, "INVENTED KITE REFUND",
                        account="chk", primary="INCOME")
            d.link_reimbursement(conn, e, p)
            # matched LAST, on the oldest charge
            old = TODAY - dt.timedelta(days=60)
            e = add_txn(conn, old, 80.0, "INVENTED CLINIC")
            p = add_txn(conn, old + dt.timedelta(days=20), -30.0,
                        "INVENTED INSURER", account="chk", primary="INCOME")
            d.link_reimbursement(conn, e, p, partial=True)
            # repaid AHEAD: the money came in well before the charge
            chg = TODAY - dt.timedelta(days=20)
            e = add_txn(conn, chg, 55.0, "INVENTED SKATE SHOP")
            p = add_txn(conn, chg - dt.timedelta(days=9), -55.0,
                        "INVENTED SKATE FRIEND", account="chk",
                        primary="INCOME")
            d.link_reimbursement(conn, e, p)
        finally:
            conn.close()

    def setUp(self):
        security._limiter._hits.clear()

    def _pairs(self):
        r = self.client.get("/api/reimburse/pairs")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["pairs"]

    def test_the_list_is_sorted_by_the_charge_date_it_shows(self):
        dates = [p["expense_date"] for p in self._pairs()]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_a_pair_says_what_it_nets_and_a_partial_names_the_charge(self):
        by = {p["expense_payee"].upper(): p for p in self._pairs()}
        full = next(v for k, v in by.items() if "KITE" in k)
        part = next(v for k, v in by.items() if "CLINIC" in k)
        self.assertEqual(full["amount_label"], "$40.00")
        self.assertEqual(part["amount_label"], "$30.00 of $80.00")

    def test_a_repayment_dated_before_its_charge_says_so(self):
        """A deposit dated before the charge it repays is an advance (or a
        wrong pick): the line says how far ahead it came, so it never
        reads as "paid back" days before anything was spent. Within the
        posting allowance it is an ordinary repayment and says nothing."""
        by = {p["expense_payee"].upper(): p for p in self._pairs()}
        ahead = next(v for k, v in by.items() if "SKATE" in k)
        self.assertEqual(ahead["deposit_note"],
                         "9 days before the charge")
        for k, v in by.items():
            if "SKATE" not in k:
                self.assertIsNone(v["deposit_note"], k)
        pair = {"expense_date": dt.date(2026, 3, 9)}
        for dep, want in ((dt.date(2026, 3, 9), None),
                          (dt.date(2026, 3, 6), None),       # posting lag
                          (dt.date(2026, 3, 5), "4 days before the charge"),
                          (dt.date(2026, 3, 8), None),
                          (dt.date(2026, 3, 20), None)):
            self.assertEqual(
                d.pair_deposit_note({**pair, "deposit_date": dep}), want,
                dep)

    def test_how_long_a_charge_waited_is_the_households_day_count(self):
        today = dt.date(2026, 3, 10)
        self.assertEqual(d.waited_label("2026-03-10", today), "today")
        self.assertEqual(d.waited_label("2026-03-09", today), "waiting 1 day")
        self.assertEqual(d.waited_label(dt.date(2026, 2, 28), today),
                         "waiting 10 days")
        self.assertEqual(d.waited_label("2026-03-12", today), "today")


if __name__ == "__main__":
    unittest.main()
