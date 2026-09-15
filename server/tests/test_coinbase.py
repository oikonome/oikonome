"""Coinbase connector: CDP ES256 JWT auth (path-only uri claim), pagination,
holdings partial-total guard, id/sign semantics, bank-flows-anchored P/L
consumption by reporting. Mocked HTTP — no live API."""

import base64
import datetime as dt
import json
import unittest

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from oikonome.engine import reporting
from oikonome.sync import base as sync_base
from oikonome.sync import coinbase

from .util import make_db

_KEY = ec.generate_private_key(ec.SECP256R1())
_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode()
KEY_NAME = "organizations/test-org/apiKeys/test-key"

W_BTC = {"id": "w-btc", "balance": {"amount": "0.5", "currency": "BTC"},
         "currency": {"type": "crypto", "code": "BTC"}}
W_USD = {"id": "w-usd", "balance": {"amount": "100.00", "currency": "USD"},
         "currency": {"type": "fiat", "code": "USD"}}
W_EMPTY = {"id": "w-eth", "balance": {"amount": "0", "currency": "ETH"},
           "currency": {"type": "crypto", "code": "ETH"}}

TXNS_BTC = [
    # buy: fiat in — counts toward cost basis
    {"id": "tx-buy", "type": "buy", "status": "completed",
     "created_at": "2024-01-05T12:00:00Z",
     "amount": {"amount": "0.5", "currency": "BTC"},
     "native_amount": {"amount": "500.00", "currency": "USD"}},
    # pro transfer: user's own money between venues — NEVER cost basis
    {"id": "tx-pro", "type": "pro_deposit", "status": "completed",
     "created_at": "2024-02-01T12:00:00Z",
     "amount": {"amount": "0.1", "currency": "BTC"},
     "native_amount": {"amount": "200.00", "currency": "USD"}},
    # sell: fiat out
    {"id": "tx-sell", "type": "sell", "status": "completed",
     "created_at": "2024-03-01T12:00:00Z",
     "amount": {"amount": "-0.1", "currency": "BTC"},
     "native_amount": {"amount": "-150.00", "currency": "USD"}},
]
TXNS_ETH = [
    {"id": "tx-pending", "type": "send", "status": "pending",
     "created_at": "2024-04-01T12:00:00Z",
     "amount": {"amount": "1.0", "currency": "ETH"},
     "native_amount": {"amount": "50.00", "currency": "USD"}},
]


def _transport(spot_status=200, seen=None, fail_accounts=False):
    """Fixture API: /v2/accounts paginates (page 1 → next_uri → page 2)."""
    def handler(request):
        if seen is not None:
            seen.append(request)
        p = request.url.path
        if fail_accounts and p == "/v2/accounts":
            return httpx.Response(401, text="Unauthorized")
        if p == "/v2/accounts":
            if "starting_after" in str(request.url.query):
                return httpx.Response(200, json={
                    "data": [W_USD, W_EMPTY], "pagination": {}})
            return httpx.Response(200, json={
                "data": [W_BTC],
                "pagination": {"next_uri": "/v2/accounts?limit=100&starting_after=w-btc"}})
        if p == "/v2/prices/BTC-USD/spot":
            if spot_status != 200:
                return httpx.Response(spot_status, text="no price")
            return httpx.Response(200, json={"data": {"amount": "40000.00"}})
        if p == "/v2/accounts/w-btc/transactions":
            return httpx.Response(200, json={"data": TXNS_BTC, "pagination": {}})
        if p == "/v2/accounts/w-usd/transactions":
            return httpx.Response(200, json={"data": [], "pagination": {}})
        if p == "/v2/accounts/w-eth/transactions":
            return httpx.Response(200, json={"data": TXNS_ETH, "pagination": {}})
        return httpx.Response(404, text=f"unmocked {p}")
    return httpx.MockTransport(handler)


class CoinbaseSyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.iid = coinbase.link(self.conn, "original", KEY_NAME, _PEM)

    def tearDown(self):
        self.conn.close()

    def _sync(self, **kw):
        return coinbase.sync(self.conn, self.iid, transport=_transport(**kw))

    def test_link_stores_credentials_round_trip(self):
        self.assertEqual(self.iid, "coinbase-original")
        stored = sync_base.get_access_token(self.conn, self.iid)
        creds = json.loads(stored)
        self.assertEqual(creds["name"], KEY_NAME)
        self.assertIn("PRIVATE KEY", creds["privateKey"])
        row = self.conn.execute(
            "SELECT aggregator, institution_name FROM items WHERE id=%s",
            (self.iid,)).fetchone()
        self.assertEqual(row["aggregator"], "coinbase")
        self.assertEqual(row["institution_name"], "Coinbase")

    def test_jwt_es256_path_only_uri_claim(self):
        seen = []
        self._sync(seen=seen)
        # first request: GET /v2/accounts?limit=100 — the uri claim must be
        # method + host + PATH only, no query string (401 otherwise), and
        # the signature must verify against the key
        first = seen[0]
        self.assertIn("limit=100", str(first.url.query))
        tok = first.headers["Authorization"].split()[1]
        h, p, s = tok.split(".")
        pad = lambda x: x + "=" * (-len(x) % 4)      # noqa: E731
        payload = json.loads(base64.urlsafe_b64decode(pad(p)))
        self.assertEqual(payload["uri"], "GET api.coinbase.com/v2/accounts")
        self.assertEqual(payload["sub"], KEY_NAME)
        self.assertEqual(payload["iss"], "cdp")
        header = json.loads(base64.urlsafe_b64decode(pad(h)))
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(header["kid"], KEY_NAME)
        self.assertTrue(header["nonce"])
        sig = base64.urlsafe_b64decode(pad(s))
        self.assertEqual(len(sig), 64)               # raw r||s, not DER
        _KEY.public_key().verify(
            encode_dss_signature(int.from_bytes(sig[:32], "big"),
                                 int.from_bytes(sig[32:], "big")),
            f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256()))

    def test_holdings_and_balance(self):
        res = self._sync()
        self.assertEqual(res["wallets"], 3)          # pagination followed
        # 0.5 BTC × 40000 + fiat wallet at its own USD; zero-qty ETH skipped
        self.assertEqual(res["holdings_usd"], 20100.0)
        rows = {r["currency"]: r for r in self.conn.execute(
            "SELECT currency, quantity, native_usd FROM crypto_holdings "
            "WHERE account_id='coinbase-original'").fetchall()}
        self.assertEqual(set(rows), {"BTC", "USD"})
        self.assertEqual(rows["BTC"]["native_usd"], 20000.0)
        self.assertEqual(rows["USD"]["native_usd"], 100.0)
        acct = self.conn.execute(
            "SELECT type, subtype, balance_current FROM accounts "
            "WHERE id='coinbase-original'").fetchone()
        self.assertEqual(acct["type"], "investment")
        self.assertEqual(acct["subtype"], "crypto")   # _coinbase_pl keys on this
        self.assertEqual(acct["balance_current"], 20100.0)

    def test_transaction_id_and_sign_semantics(self):
        res = self._sync()
        self.assertEqual(res["transactions"], 4)
        rows = {r["id"]: r for r in self.conn.execute(
            "SELECT id, date, amount, name, merchant_name, category_primary, "
            "category_detailed, pending FROM transactions "
            "WHERE id LIKE 'coinbase:%'").fetchall()}
        # stable id scheme: coinbase:<native transaction id>
        self.assertIn("coinbase:tx-buy", rows)
        buy = rows["coinbase:tx-buy"]
        # native_amount is stored AS-IS (informational only; spend-excluded
        # by TRANSFER_* category)
        self.assertEqual(buy["amount"], 500.0)
        self.assertEqual(buy["category_primary"], "TRANSFER_OUT")
        self.assertEqual(buy["category_detailed"], "COINBASE_BUY")
        self.assertEqual(buy["name"], "Coinbase buy BTC")
        self.assertEqual(buy["merchant_name"], "Coinbase")
        self.assertEqual(buy["date"], dt.date(2024, 1, 5))
        self.assertEqual(rows["coinbase:tx-sell"]["amount"], -150.0)
        self.assertEqual(rows["coinbase:tx-sell"]["category_primary"], "TRANSFER_IN")
        # zero-balance wallets still contribute history; pending flagged
        self.assertEqual(rows["coinbase:tx-pending"]["pending"], 1)

    def test_pl_is_bank_flows_anchored(self):
        """Buy/sell are fiat flows; a pro_deposit venue transfer is the
        holder's own money moving and is NOT cost basis — counted as one,
        it understates the return badly. reporting._coinbase_pl consumes
        the rows unchanged."""
        self._sync()
        pl = reporting._coinbase_pl(self.conn)
        self.assertEqual(len(pl), 1)
        row = pl[0]
        self.assertEqual(row["cash_in"], 500.0)      # buy only, NOT +200 pro
        self.assertEqual(row["cash_out"], 150.0)     # sell
        self.assertEqual(row["value"], 20100.0)
        self.assertEqual(row["pl"], 20100.0 + 150.0 - 500.0)

    def test_idempotent_resync(self):
        self._sync()
        self._sync()
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM transactions WHERE id LIKE 'coinbase:%'"
        ).fetchone()["n"]
        self.assertEqual(n, 4)
        h = self.conn.execute(
            "SELECT COUNT(*) n FROM crypto_holdings "
            "WHERE account_id='coinbase-original'").fetchone()["n"]
        self.assertEqual(h, 2)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id=%s",
            (self.iid,)).fetchone()["status"], "ok")

    def test_missing_spot_price_keeps_previous_holdings(self):
        """Partial-total guard: one failed price lookup must not overwrite
        the balance with a partial sum — a whole holding would vanish from
        net worth. 404 = non-retryable → spot() returns None → abort."""
        self._sync()                                 # good pass first
        with self.assertRaises(RuntimeError) as cm:
            self._sync(spot_status=404)
        self.assertIn("no spot price", str(cm.exception))
        # previous holdings + balance intact
        self.assertEqual(self.conn.execute(
            "SELECT native_usd FROM crypto_holdings WHERE account_id="
            "'coinbase-original' AND currency='BTC'").fetchone()["native_usd"],
            20000.0)
        self.assertEqual(self.conn.execute(
            "SELECT balance_current FROM accounts WHERE id='coinbase-original'"
        ).fetchone()["balance_current"], 20100.0)

    def test_api_error_marks_item_and_logs(self):
        with self.assertRaises(RuntimeError):
            self._sync(fail_accounts=True)
        self.assertTrue(self.conn.execute(
            "SELECT status FROM items WHERE id=%s",
            (self.iid,)).fetchone()["status"].startswith("error:"))
        err = self.conn.execute(
            "SELECT error FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIn("401", err["error"])

    def test_rollback_removes_everything(self):
        self._sync()
        n = coinbase.rollback(self.conn, "original")
        self.assertEqual(n, 4)
        for table, col, val in (("transactions", "account_id", "coinbase-original"),
                                ("crypto_holdings", "account_id", "coinbase-original"),
                                ("accounts", "id", "coinbase-original"),
                                ("items", "id", "coinbase-original")):
            self.assertEqual(self.conn.execute(
                f"SELECT COUNT(*) n FROM {table} WHERE {col}=%s",
                (val,)).fetchone()["n"], 0, table)

    def test_migrated_account_row_preserved(self):
        """An existing account row keeps its display_name/classification:
        _ensure_account is ON CONFLICT DO NOTHING."""
        self.conn.execute(
            """INSERT INTO accounts (id, item_id, name, display_name, type, subtype)
               VALUES ('coinbase-original', 'coinbase-original',
                       'Coinbase (original)', 'My Coinbase', 'investment', 'crypto')""")
        self._sync()
        row = self.conn.execute(
            "SELECT display_name, balance_current FROM accounts "
            "WHERE id='coinbase-original'").fetchone()
        self.assertEqual(row["display_name"], "My Coinbase")
        self.assertEqual(row["balance_current"], 20100.0)  # balance still updates


if __name__ == "__main__":
    unittest.main()
