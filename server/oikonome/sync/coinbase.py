"""Coinbase connector — full wallet + transaction history for a linked
Coinbase account, via the Coinbase App API (/v2) using CDP ES256 API keys.

Read-only. Every Coinbase account is stored budget-excluded like any other
brokerage: type='investment', subtype='crypto', and every transaction is
mapped to TRANSFER_IN/OUT so it never touches spend math. Net worth comes
from the crypto_holdings table (positions × spot price), NOT from summing
transaction amounts — crypto txn signs are informational only, so the
source amounts are preserved verbatim (native_amount as-is; the rows are
spend-excluded by category, and reporting._coinbase_pl reads `raw`, not the
amount column). P/L stays anchored to FIAT flows in raw (buy/sell/
fiat_deposit/fiat_withdrawal) — pro_/exchange_ transfers are the user's own
money moving between venues, never new cost basis; counting them as basis
understates the return by an order of magnitude (see
reporting._coinbase_pl).

Ids are stable across re-syncs (and across a restore), so a re-sync
UPSERTS instead of duplicating:
  item/account  coinbase-<label>
  transactions  coinbase:<coinbase txn uuid>

Credentials: the raw CDP key file Coinbase hands out —
{"name": "organizations/.../apiKeys/...", "privateKey": PEM} — stored as
the item's access_token, encrypted at rest via db/crypto exactly like
Plaid tokens (see `link`).

Gotchas baked in:
  * The JWT `uri` claim must be METHOD + HOST + PATH only, NO query string,
    even though the request itself carries ?limit=/?starting_after=.
    Signing the query string yields 401.
  * Signature algorithm must be ES256 (ECDSA); Ed25519 keys are rejected
    by /v2.
  * /v2/accounts does not return native_balance, so holdings are valued
    via /v2/prices/<code>-USD/spot.
  * A failed spot-price lookup must abort the WHOLE holdings write: valuing
    that coin at $0 would silently overwrite the account balance with a
    partial total, which lands straight in net worth. Holdings are computed
    first, written only when complete.

Testable: pass `transport=` (httpx.MockTransport) — no live HTTP in tests.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import math
import secrets
import time
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from ..engine.compat import as_date, jsonb
from .base import get_access_token, log_sync, upsert_item

HOST = "api.coinbase.com"
INSTITUTION = "Coinbase"
AGGREGATOR = "coinbase"


def item_id(label: str) -> str:
    return f"coinbase-{label}"


def account_id(label: str) -> str:
    return f"coinbase-{label}"


# -- auth / client --------------------------------------------------------


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _jwt_es256(key, key_name: str, path_only: str) -> str:
    """CDP App API JWT, signed ES256 (raw r||s signature per RFC 7518).
    Hand-rolled on `cryptography` (already a dependency) rather than adding
    a JWT library for one signature shape."""
    now = int(time.time())
    header = {"typ": "JWT", "alg": "ES256", "kid": key_name,
              "nonce": secrets.token_hex(16)}
    payload = {"sub": key_name, "iss": "cdp", "nbf": now, "exp": now + 120,
               "uri": f"GET {HOST}{path_only}"}
    signing_input = (
        _b64u(json.dumps(header, separators=(",", ":")).encode()) + "." +
        _b64u(json.dumps(payload, separators=(",", ":")).encode()))
    der = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    size = (key.curve.key_size + 7) // 8
    return signing_input + "." + _b64u(r.to_bytes(size, "big") +
                                       s.to_bytes(size, "big"))


class CoinbaseClient:
    """Signs a fresh short-lived ES256 JWT per request (CDP App API auth)."""

    def __init__(self, key_name: str, priv_pem: str, transport=None):
        self.key_name = key_name
        self._key = serialization.load_pem_private_key(
            priv_pem.encode(), password=None)
        self._http = httpx.Client(transport=transport, timeout=30)

    def get(self, full_path: str, tries: int = 6) -> dict:
        path_only = urlparse(full_path).path  # uri claim: path only, never query
        last = ""
        for i in range(tries):
            tok = _jwt_es256(self._key, self.key_name, path_only)
            r = self._http.get(
                f"https://{HOST}{full_path}",
                headers={"Authorization": f"Bearer {tok}",
                         "Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            last = f"{r.status_code} {r.text[:200]}"
            if r.status_code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(min(2 ** i, 20))
                continue
            raise RuntimeError(f"Coinbase {full_path}: {last}")
        raise RuntimeError(f"Coinbase retries exhausted {full_path}: {last}")

    def paginate(self, path: str):
        """Yield every item across next_uri pages."""
        while path:
            body = self.get(path)
            yield from body.get("data", [])
            nxt = (body.get("pagination") or {}).get("next_uri")
            path = nxt or None

    def spot(self, code: str, cache: dict) -> float | None:
        if code in cache:
            return cache[code]
        try:
            v = float(self.get(f"/v2/prices/{code}-USD/spot")["data"]["amount"])
            # "NaN"/"Infinity" parse as floats and would poison every sum
            # they touch; a negative price is no price either. A literal
            # 0 is a real answer (a delisted asset) and stays one — turning
            # it into "missing" would stall the whole account's sync.
            if not math.isfinite(v) or v < 0:
                v = None
        except Exception:                        # noqa: BLE001 — None = unpriced
            v = None
        cache[code] = v
        return v


# -- linking ---------------------------------------------------------------


def link(conn, label: str, key_name: str, private_key_pem: str) -> str:
    """Store the CDP key encrypted (items.access_token via db/crypto, the
    same envelope as Plaid tokens) on item coinbase-<label>. Returns the
    item id. Re-linking the same label rotates the stored key in place."""
    iid = item_id(label)
    upsert_item(conn, iid, AGGREGATOR, INSTITUTION,
                json.dumps({"name": key_name, "privateKey": private_key_pem}),
                raw={"label": label})
    return iid


def _ensure_account(conn, label: str) -> str:
    """ON CONFLICT DO NOTHING so a re-link keeps the existing account row
    (display_name, user classification) exactly as it is."""
    aid = account_id(label)
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype, currency)
           VALUES (%s,%s,%s,'investment','crypto','USD')
           ON CONFLICT (tenant_id, id) DO NOTHING""",
        (aid, item_id(label), f"Coinbase ({label})"))
    return aid


# -- sync -------------------------------------------------------------------


def sync(conn, item_id_: str, *, transport=None, tries: int = 6) -> dict:
    """Full pull for one linked Coinbase account: wallets → crypto_holdings
    (all-or-nothing, see docstring) + balance, transactions across ALL
    wallets (zero-balance wallets still hold history) → idempotent upserts."""
    label = item_id_.removeprefix("coinbase-")
    stored = get_access_token(conn, item_id_)
    if not stored:
        raise RuntimeError(f"no Coinbase credentials stored for {item_id_}")
    creds = json.loads(stored)
    client = CoinbaseClient(creds["name"], creds["privateKey"],
                            transport=transport)
    aid = _ensure_account(conn, label)
    spot_cache: dict[str, float | None] = {}

    try:
        wallets = list(client.paginate("/v2/accounts?limit=100"))

        # holdings (refresh from scratch each run) + total USD. Compute
        # FIRST, write only if complete (the partial-total guard).
        total_usd = 0.0
        priced_rows: list[tuple] = []
        missing_price: list[str] = []
        for w in wallets:
            code = w["balance"]["currency"]
            qty = float(w["balance"]["amount"])
            if qty == 0:
                continue
            is_crypto = (w.get("currency") or {}).get("type") == "crypto"
            # fiat wallet = its own USD
            px = client.spot(code, spot_cache) if is_crypto else 1.0
            if px is None:
                missing_price.append(code)
                continue
            usd = qty * px
            total_usd += usd
            priced_rows.append((aid, code, qty, round(usd, 2)))
        if missing_price:
            raise RuntimeError(
                f"coinbase {label}: no spot price for {missing_price} — "
                f"keeping previous holdings/balance rather than writing a "
                f"partial total")

        txns = [t for w in wallets for t in client.paginate(
            f"/v2/accounts/{w['id']}/transactions?limit=100")]
        # attach raw wallet objects for the holdings rows (kept out of the
        # priced tuple above so the guard math stays readable)
        raw_by_code = {w["balance"]["currency"]: w for w in wallets}
    except Exception as e:                       # noqa: BLE001
        log_sync(conn, item_id_, 0, error=f"{type(e).__name__}: {e}")
        conn.execute("UPDATE items SET status=%s WHERE id=%s",
                     (f"error:{type(e).__name__}", item_id_))
        raise

    now = dt.datetime.now(dt.timezone.utc)
    with conn.transaction():
        conn.execute("DELETE FROM crypto_holdings WHERE account_id=%s", (aid,))
        for haid, code, qty, usd in priced_rows:
            conn.execute(
                """INSERT INTO crypto_holdings
                       (account_id, currency, quantity, native_usd, as_of, raw)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, account_id, currency) DO UPDATE SET
                       quantity=EXCLUDED.quantity,
                       native_usd=EXCLUDED.native_usd,
                       as_of=EXCLUDED.as_of, raw=EXCLUDED.raw""",
                (haid, code, qty, usd, now, jsonb(raw_by_code.get(code) or {})))
        conn.execute(
            "UPDATE accounts SET balance_current=%s, updated_at=%s WHERE id=%s",
            (round(total_usd, 2), now, aid))
        n_txn = 0
        for t in txns:
            _upsert_txn(conn, aid, t)
            n_txn += 1
        # this pull is COMPLETE by construction — every wallet,
        # every page, or the except above re-raised before any write. So a
        # coinbase: row this account still shows live that the pull no
        # longer carries was restated away at the source: soft-retire it,
        # mirroring the push door's full_replace semantics. removed=1 is
        # reversible — a returning id hits _upsert_txn's ON CONFLICT,
        # which sets removed=0. A partial/failed pull never reaches here,
        # so it can never retire anything.
        pulled = {f"coinbase:{t['id']}" for t in txns}
        stale = [r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE account_id=%s"
            " AND id LIKE 'coinbase:%%' AND removed=0", (aid,)).fetchall()
            if r["id"] not in pulled]
        if stale:
            conn.execute(
                "UPDATE transactions SET removed=1 WHERE id = ANY(%s)",
                (stale,))

    log_sync(conn, item_id_, n_txn)
    conn.execute("UPDATE items SET status='ok' WHERE id=%s", (item_id_,))
    return {"label": label, "wallets": len(wallets),
            "holdings_usd": round(total_usd, 2), "transactions": n_txn,
            "stale_removed": len(stale)}


def _upsert_txn(conn, aid: str, t: dict) -> None:
    nat = t.get("native_amount") or {}
    amount = float(nat.get("amount") or 0)   # Coinbase sign: + = value into wallet
    code = (t.get("amount") or {}).get("currency")
    ttype = (t.get("type") or "unknown")
    # Both TRANSFER_IN/OUT are spend-excluded; sign only drives which label.
    cat = "TRANSFER_OUT" if amount > 0 else "TRANSFER_IN"
    date = as_date((t.get("created_at") or "")[:10] or None)
    pending = 0 if t.get("status") == "completed" else 1
    name = f"Coinbase {ttype}" + (f" {code}" if code else "")
    conn.execute(
        """INSERT INTO transactions
               (id, account_id, date, amount, name, merchant_name,
                category_primary, category_detailed, pending, removed, raw,
                category_source)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,'flow')
           ON CONFLICT (tenant_id, id) DO UPDATE SET
               date=EXCLUDED.date, amount=EXCLUDED.amount,
               name=EXCLUDED.name, merchant_name=EXCLUDED.merchant_name,
               category_primary=EXCLUDED.category_primary,
               category_source=EXCLUDED.category_source,
               category_detailed=EXCLUDED.category_detailed,
               pending=EXCLUDED.pending, removed=0, raw=EXCLUDED.raw""",
        (f"coinbase:{t['id']}", aid, date, amount, name, "Coinbase",
         cat, f"COINBASE_{ttype.upper()}", pending, jsonb(t)))
    # category_override deliberately untouched — sacred, never synced


def rollback(conn, label: str) -> int:
    """Remove all data for one linked Coinbase account (unlink)."""
    aid = account_id(label)
    with conn.transaction():
        n = conn.execute(
            "DELETE FROM transactions WHERE account_id=%s", (aid,)).rowcount
        conn.execute("DELETE FROM crypto_holdings WHERE account_id=%s", (aid,))
        conn.execute("DELETE FROM accounts WHERE id=%s", (aid,))
        conn.execute("DELETE FROM items WHERE id=%s", (item_id(label),))
    return n
