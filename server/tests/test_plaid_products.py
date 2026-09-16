"""Plaid non-transaction products: /liabilities/get → `liabilities` rows
(what forecast.py's LEFT JOIN reads) and
/investments/holdings/get → `holdings` replace-per-account. Mocked HTTP
throughout (test_plaid.py pattern)."""

import json
import unittest

import httpx

from oikonome.engine import forecast
from oikonome.sync import base as sync_base
from oikonome.sync import plaid

from .util import TODAY, make_db, write_config

LIABILITIES = {"liabilities": {
    "credit": [{"account_id": "card",
                "next_payment_due_date": "2026-07-20",
                "last_statement_balance": 180.0,
                "minimum_payment_amount": 35.0}],
    "mortgage": [{"account_id": "mort-1",
                  "next_payment_due_date": "2026-08-01"}],
    "student": None}}

HOLDINGS = {
    "accounts": [{"account_id": "inv-1", "type": "investment"}],
    "securities": [
        {"security_id": "s1", "ticker_symbol": "VTI",
         "name": "Vanguard Total Stock Market ETF"},
        {"security_id": "s2", "ticker_symbol": None, "cusip": "922908363",
         "name": "Some Bond Fund"}],
    "holdings": [
        {"account_id": "inv-1", "security_id": "s1", "quantity": 10.0,
         "institution_price": 250.0, "institution_value": 2500.0},
        {"account_id": "inv-1", "security_id": "s2", "quantity": 5.0,
         "institution_price": 100.0, "institution_value": 500.0}]}

NOT_SUPPORTED = {"error_code": "PRODUCTS_NOT_SUPPORTED",
                 "error_type": "ITEM_ERROR",
                 "error_message": "nope"}


def _transport(liabilities=LIABILITIES, holdings=HOLDINGS,
               fail: dict | None = None):
    def handler(request):
        path = request.url.path
        if fail and path in fail:
            return httpx.Response(400, text=json.dumps(fail[path]))
        if path == "/liabilities/get":
            return httpx.Response(200, text=json.dumps(liabilities))
        if path == "/investments/holdings/get":
            return httpx.Response(200, text=json.dumps(holdings))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class PlaidProductTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        sync_base.upsert_item(self.conn, "it-p", "plaid", "Demo",
                              "access-prod-abc")
        # move the fixture accounts onto the plaid item + add an investment
        self.conn.execute("UPDATE accounts SET item_id='it-p' "
                          "WHERE id IN ('chk','card')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,balance_current) "
            "VALUES ('inv-1','it-p','Brokerage','investment',3000)")

    def tearDown(self):
        self.conn.close()

    def test_liabilities_written_and_upserted(self):
        n = plaid.sync_liabilities(self.conn, "it-p",
                                   transport=_transport())
        self.assertEqual(n, 2)                   # credit + mortgage
        row = self.conn.execute(
            "SELECT raw ->> 'next_payment_due_date' AS due, "
            "       (raw ->> 'last_statement_balance')::float AS stmt "
            "FROM liabilities WHERE account_id='card'").fetchone()
        self.assertEqual(row["due"], "2026-07-20")
        self.assertEqual(row["stmt"], 180.0)
        # re-run: upsert, not duplicate
        plaid.sync_liabilities(self.conn, "it-p", transport=_transport())
        n_rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM liabilities").fetchone()["n"]
        self.assertEqual(n_rows, 2)

    def test_liabilities_feed_the_forecast_due_date(self):
        """With liabilities synced, forecast's 'full balance at due date'
        scenario uses the real due date instead of the tomorrow-fallback."""
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox", checking_account_id="chk")
        f = forecast.build(self.conn, today=TODAY)
        # without liabilities the card payoff lands tomorrow (fallback)
        self.assertEqual(f["card_events"][0][0],
                         (TODAY.replace(day=TODAY.day + 1)).isoformat())
        plaid.sync_liabilities(self.conn, "it-p", transport=_transport())
        f = forecast.build(self.conn, today=TODAY)
        self.assertEqual(f["card_events"][0][0], "2026-07-20")
        # statement scenario picks up last_statement_balance
        self.assertEqual(f["card_events_stmt"][0][1], -180.0)

    def test_holdings_written_then_replaced(self):
        n = plaid.sync_holdings(self.conn, "it-p", transport=_transport())
        self.assertEqual(n, 2)
        rows = {r["symbol"]: r for r in self.conn.execute(
            "SELECT symbol, name, quantity, price, value FROM holdings "
            "WHERE account_id='inv-1'").fetchall()}
        self.assertEqual(rows["VTI"]["value"], 2500.0)
        self.assertEqual(rows["922908363"]["name"], "Some Bond Fund")
        # a sold position must disappear on the next sync (delete-then-insert)
        smaller = dict(HOLDINGS)
        smaller["holdings"] = HOLDINGS["holdings"][:1]
        n = plaid.sync_holdings(self.conn, "it-p",
                                transport=_transport(holdings=smaller))
        self.assertEqual(n, 1)
        syms = [r["symbol"] for r in self.conn.execute(
            "SELECT symbol FROM holdings WHERE account_id='inv-1'").fetchall()]
        self.assertEqual(syms, ["VTI"])

    def test_holdings_skip_without_investment_accounts(self):
        self.conn.execute("DELETE FROM accounts WHERE id='inv-1'")
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, text="{}")
        n = plaid.sync_holdings(self.conn, "it-p",
                                transport=httpx.MockTransport(handler))
        self.assertEqual(n, 0)
        self.assertEqual(calls, [])              # no API call at all

    def test_unsupported_products_skip_quietly(self):
        t = _transport(fail={"/liabilities/get": NOT_SUPPORTED,
                             "/investments/holdings/get": NOT_SUPPORTED})
        self.assertEqual(plaid.sync_liabilities(self.conn, "it-p",
                                                transport=t), 0)
        self.assertEqual(plaid.sync_holdings(self.conn, "it-p",
                                             transport=t), 0)

    def test_the_reason_a_product_is_missing_is_recorded_and_served(self):
        """CONSENT (the user never granted the product — a re-link can
        attach it) and NOT_SUPPORTED (the bank cannot provide it) are
        different answers, and the connection card must be able to say
        which. Without the recorded reason a card with no due date simply
        draws no autopay line on the forecast, with nothing saying why."""
        consent = {"error_code": "ADDITIONAL_CONSENT_REQUIRED",
                   "error_type": "INVALID_INPUT",
                   "error_message": "consent required"}
        t = _transport(fail={"/liabilities/get": consent,
                             "/investments/holdings/get": NOT_SUPPORTED})
        self.assertEqual(plaid.sync_liabilities(self.conn, "it-p",
                                                transport=t), 0)
        self.assertEqual(plaid.sync_holdings(self.conn, "it-p",
                                             transport=t), 0)
        pi = self.conn.execute(
            "SELECT raw->'product_issues' AS pi FROM items "
            "WHERE id='it-p'").fetchone()["pi"]
        self.assertEqual(pi["liabilities"]["cause"], "consent")
        self.assertEqual(pi["investments"]["cause"], "not_supported")
        # the connections payload — the card's data source — carries it
        from oikonome.web.pages import _connections
        row = next(r for r in _connections(self.conn) if r["id"] == "it-p")
        self.assertEqual(row["product_issues"]["liabilities"]["cause"],
                         "consent")

    def test_a_granted_consent_heals_the_notice(self):
        consent = {"error_code": "ADDITIONAL_CONSENT_REQUIRED",
                   "error_type": "INVALID_INPUT",
                   "error_message": "consent required"}
        t = _transport(fail={"/liabilities/get": consent})
        plaid.sync_liabilities(self.conn, "it-p", transport=t)
        # the user re-links and grants the product: the very next
        # successful pull clears the notice, no separate code path
        plaid.sync_liabilities(self.conn, "it-p", transport=_transport())
        pi = self.conn.execute(
            "SELECT raw->'product_issues' AS pi FROM items "
            "WHERE id='it-p'").fetchone()["pi"]
        self.assertNotIn("liabilities", pi or {})

    def test_real_errors_raise(self):
        t = _transport(fail={"/liabilities/get": {
            "error_code": "INTERNAL_SERVER_ERROR", "error_type": "API_ERROR",
            "error_message": "boom"}})
        with self.assertRaises(plaid.PlaidError):
            plaid.sync_liabilities(self.conn, "it-p", transport=t)

    def test_sync_products_combined(self):
        out = plaid.sync_products(self.conn, "it-p", transport=_transport())
        self.assertEqual(out, {"liabilities": 2, "holdings": 2})


if __name__ == "__main__":
    unittest.main()
