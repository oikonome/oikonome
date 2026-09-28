"""Plaid's recurring streams as a cross-check on bill detection: mature,
active outflow/inflow streams no active bill covers become PENDING
proposals that say they came from Plaid; covered merchants and pending
duplicates are skipped; unknown/semi-monthly cadences are skipped; the
pass is idempotent per stream; a Plaid error yields nothing."""

import datetime as dt
import json
import unittest

import httpx

from oikonome.engine import bills
from oikonome.sync import plaid

from .util import add_bill, make_db, write_config

TODAY = dt.date.today()
STREAMS = {
    "outflow_streams": [
        {"stream_id": "s-netflix", "merchant_name": "Netflix", "description": "NETFLIX.COM",
         "average_amount": {"amount": 15.49}, "last_amount": {"amount": 15.49},
         "frequency": "MONTHLY", "first_date": "2025-01-05", "last_date": "2026-08-05",
         "predicted_next_date": "2026-09-05", "is_active": True, "status": "MATURE",
         "transaction_ids": ["a", "b", "c", "d", "e"],
         "personal_finance_category": {"primary": "ENTERTAINMENT"}},
        {"stream_id": "s-gym", "merchant_name": "Gymblue Fitness", "description": "GYMBLUE",
         "average_amount": {"amount": 45.0}, "frequency": "MONTHLY",
         "last_date": "2026-08-01", "predicted_next_date": "2026-09-01",
         "is_active": True, "status": "MATURE", "transaction_ids": ["x", "y", "z"]},
        {"stream_id": "s-semi", "merchant_name": "Landlord", "description": "RENT",
         "average_amount": {"amount": 900.0}, "frequency": "SEMI_MONTHLY",
         "last_date": "2026-08-15", "is_active": True, "status": "MATURE",
         "transaction_ids": ["p", "q"]},
        {"stream_id": "s-old", "merchant_name": "Old Cable", "description": "OLDCABLE",
         "average_amount": {"amount": 60.0}, "frequency": "MONTHLY",
         "last_date": "2025-02-01", "is_active": False, "status": "MATURE",
         "transaction_ids": ["m"]}],
    "inflow_streams": [
        {"stream_id": "s-pay", "merchant_name": "Acme Payroll", "description": "ACME PAYROLL",
         "average_amount": {"amount": -2400.0}, "frequency": "BIWEEKLY",
         "last_date": "2026-08-14", "predicted_next_date": "2026-08-28",
         "is_active": True, "status": "MATURE", "transaction_ids": ["i1", "i2", "i3"]}],
}
ACCOUNTS = {"accounts": [
    {"account_id": "p-chk", "name": "Plaid Checking", "type": "depository",
     "subtype": "checking", "mask": "4242",
     "balances": {"current": 3000.0, "available": 2900.0, "iso_currency_code": "USD"}}]}


def _transport(fail=False):
    def handler(request):
        path = request.url.path
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps(ACCOUNTS))
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(
                {"added": [], "modified": [], "removed": [], "has_more": False,
                 "next_cursor": "c"}))
        if path == "/item/public_token/exchange":
            return httpx.Response(200, text=json.dumps(
                {"item_id": "plaid-item-1", "access_token": "access-prod-abc"}))
        if path == "/transactions/recurring/get":
            if fail:
                return httpx.Response(400, text=json.dumps(
                    {"error_code": "PRODUCTS_NOT_SUPPORTED", "error_type": "INVALID_REQUEST",
                     "error_message": "no"}))
            return httpx.Response(200, text=json.dumps(STREAMS))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class PlaidRecurringTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        plaid.link_item(self.conn, "public-xyz", "Demo", transport=_transport())

    def tearDown(self):
        self.conn.close()

    def _pending(self):
        return {r["payee"]: r for r in self.conn.execute(
            "SELECT payee, amount, frequency, interval, next_due, evidence "
            "FROM bill_proposals WHERE status='pending'").fetchall()}

    def test_streams_flatten_both_directions(self):
        st = plaid.recurring_streams(self.conn, "plaid-item-1", transport=_transport())
        by = {s["stream_id"]: s for s in st}
        self.assertEqual(by["s-netflix"]["amount"], 15.49)
        self.assertEqual(by["s-pay"]["direction"], "inflow")
        self.assertEqual(by["s-pay"]["amount"], 2400.0)

    def test_uncovered_mature_streams_become_proposals_covered_and_odd_ones_do_not(self):
        add_bill(self.conn, "Gymblue Fitness", 45)      # already a bill
        st = plaid.recurring_streams(self.conn, "plaid-item-1", transport=_transport())
        stats = bills.propose_from_plaid_streams(self.conn, st, TODAY)
        p = self._pending()
        self.assertIn("Netflix", p)
        self.assertEqual((p["Netflix"]["frequency"], p["Netflix"]["interval"]), ("MONTHLY", 1))
        self.assertEqual(str(p["Netflix"]["next_due"]), "2026-09-05")
        ev = p["Netflix"]["evidence"]
        self.assertEqual((ev["source"], ev["n"]), ("plaid_recurring", 5))
        self.assertIn("Acme Payroll", p)                # income, biweekly
        self.assertEqual((p["Acme Payroll"]["frequency"], p["Acme Payroll"]["interval"]),
                         ("WEEKLY", 2))
        self.assertTrue(p["Acme Payroll"]["evidence"]["income"])
        self.assertNotIn("Gymblue Fitness", p, "an active bill covers it")
        self.assertNotIn("Landlord", p, "semi-monthly has no shape here")
        self.assertNotIn("Old Cable", p, "inactive")
        self.assertEqual(stats["proposed"], 2)
        # idempotent per stream
        stats2 = bills.propose_from_plaid_streams(self.conn, st, TODAY)
        self.assertEqual(stats2["proposed"], 0)
        self.assertEqual(len(self._pending()), 2)

    def test_plaid_error_yields_nothing(self):
        st = plaid.recurring_streams(self.conn, "plaid-item-1", transport=_transport(fail=True))
        self.assertEqual(st, [])

    def test_approving_a_plaid_proposal_makes_the_bill(self):
        st = plaid.recurring_streams(self.conn, "plaid-item-1", transport=_transport())
        bills.propose_from_plaid_streams(self.conn, st, TODAY)
        pid = self.conn.execute(
            "SELECT id FROM bill_proposals WHERE payee='Netflix'").fetchone()["id"]
        bills.apply_proposal(self.conn, pid, "approve")
        b = bills.get_bill(self.conn, "Netflix")
        self.assertIsNotNone(b)
        self.assertAlmostEqual(abs(b["amount"]), 15.49)


if __name__ == "__main__":
    unittest.main()
