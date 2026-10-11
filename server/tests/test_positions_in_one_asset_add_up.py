"""Two positions in the same asset are one holding worth both.

Holdings are keyed by (account, asset). A Coinbase account can hold the
same currency in two wallets (a wallet and a vault), and a brokerage feed
can report one ticker under two securities. Written one by one under that
key, the second position overwrites the first: the account balance counts
both, the holdings table keeps one, and everything that reads positions —
crypto P/L, the net-worth walk back through history, the holdings list —
is short by the position that was lost.

The invariant: the holdings stored for an account add up to what the feed
says the account holds.
"""

import json
import unittest

import httpx

from oikonome.sync import base as sync_base
from oikonome.sync import coinbase, plaid

from .test_coinbase import _PEM, KEY_NAME
from .util import make_db, write_config

WALLET = {"id": "w-btc", "balance": {"amount": "1.0", "currency": "BTC"},
          "currency": {"type": "crypto", "code": "BTC"}}
VAULT = {"id": "w-btc-vault", "balance": {"amount": "0.5", "currency": "BTC"},
         "currency": {"type": "crypto", "code": "BTC"}}


def _coinbase_transport():
    def handler(request):
        p = request.url.path
        if p == "/v2/accounts":
            return httpx.Response(200, json={"data": [WALLET, VAULT],
                                             "pagination": {}})
        if p == "/v2/prices/BTC-USD/spot":
            return httpx.Response(200, json={"data": {"amount": "40000.00"}})
        if p.endswith("/transactions"):
            return httpx.Response(200, json={"data": [], "pagination": {}})
        return httpx.Response(404, text=f"unmocked {p}")
    return httpx.MockTransport(handler)


class CoinbaseWalletsInOneCurrencyTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.iid = coinbase.link(self.conn, "two-wallets", KEY_NAME, _PEM)

    def test_holdings_add_up_to_the_balance(self):
        coinbase.sync(self.conn, self.iid, transport=_coinbase_transport())
        aid = coinbase._stored_account_id(self.conn, "two-wallets")
        rows = self.conn.execute(
            "SELECT currency, quantity, native_usd, raw FROM crypto_holdings "
            "WHERE account_id=%s", (aid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["quantity"], 1.5)
        self.assertAlmostEqual(rows[0]["native_usd"], 60000.0)
        bal = self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id=%s",
            (aid,)).fetchone()["balance_current"]
        self.assertAlmostEqual(sum(r["native_usd"] for r in rows), bal)
        # both wallets' source records are kept
        ids = {w["id"] for w in rows[0]["raw"]["wallets"]}
        self.assertEqual(ids, {"w-btc", "w-btc-vault"})


HOLDINGS = {
    "accounts": [{"account_id": "inv-1", "type": "investment"}],
    "securities": [
        {"security_id": "s1", "ticker_symbol": "VTI", "name": "Total Market"},
        # the same fund reported under a second security id
        {"security_id": "s1b", "ticker_symbol": "VTI", "name": "Total Market"}],
    "holdings": [
        {"account_id": "inv-1", "security_id": "s1", "quantity": 10.0,
         "institution_price": 250.0, "institution_value": 2500.0},
        {"account_id": "inv-1", "security_id": "s1b", "quantity": 4.0,
         "institution_price": 250.0, "institution_value": 1000.0}]}


def _plaid_transport():
    def handler(request):
        if request.url.path == "/investments/holdings/get":
            return httpx.Response(200, text=json.dumps(HOLDINGS))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class PlaidPositionsInOneTickerTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        sync_base.upsert_item(self.conn, "it-p", "plaid", "Demo",
                              "access-prod-abc")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,balance_current) "
            "VALUES ('inv-1','it-p','Brokerage','investment',3500)")

    def test_holdings_add_up_to_what_the_feed_reports(self):
        plaid.sync_holdings(self.conn, "it-p", transport=_plaid_transport())
        rows = self.conn.execute(
            "SELECT quantity, price, value FROM holdings "
            "WHERE account_id='inv-1' AND symbol='VTI'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["quantity"], 14.0)
        self.assertAlmostEqual(rows[0]["value"], 3500.0)
        self.assertAlmostEqual(rows[0]["price"], 250.0)

    def test_the_merged_position_keeps_its_cost_basis(self):
        """Lots worth $2,500 (basis $1,500) and $1,000 (basis $800) are one
        $3,500 position bought for $2,300: the brokerage P/L reads +$1,200,
        not the zero gain a position with no basis reads."""
        from oikonome.engine import reporting
        lots = [dict(h) for h in HOLDINGS["holdings"]]
        lots[0]["cost_basis"], lots[1]["cost_basis"] = 1500.0, 800.0
        feed = {**HOLDINGS, "holdings": lots}

        def handler(request):
            if request.url.path == "/investments/holdings/get":
                return httpx.Response(200, text=json.dumps(feed))
            return httpx.Response(404, text="{}")
        plaid.sync_holdings(self.conn, "it-p",
                            transport=httpx.MockTransport(handler))
        pl = next(r for r in reporting._brokerage_pl(self.conn)
                  if r["name"] == "Brokerage")
        self.assertEqual((pl["value"], pl["basis"], pl["pl"]),
                         (3500.0, 2300.0, 1200.0))


if __name__ == "__main__":
    unittest.main()
