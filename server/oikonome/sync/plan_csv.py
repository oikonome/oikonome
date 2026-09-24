"""Workplace-plan activity importer — the parsing and upsert half of a
pipeline whose other half is a host-side collector that downloads a plan
portal's "account activity" CSV and POSTs it into the product's import API.

Most 401(k)/403(b)/457(b) portals export the same seven columns: Trade
Date, Investments (fund), Ticker (ticker or CUSIP), Transaction, Transaction
Amount, Share Price, Total shares. There is rarely an automated
holdings/transactions API for these plans, so the portal CSV is the source
of truth. The columns are mapped by header name when a header row is
present, positionally otherwise.

Stored budget-excluded like other brokerage connectors: one item per
provider (`provider` is the caller's slug for the plan administrator, one
INVESTMENT account per plan under it), every transaction mapped to
TRANSFER_* so it never touches spend math. Per-fund positions (net shares ×
last price) land in `holdings`. reporting._retirement_pl reads
raw->>'type'='Contribution' rows for the cost basis, so `raw` keeps the CSV
row (with its ORIGINAL positive-into-plan sign) verbatim.

Idempotent via DATE-SET REPLACE, upsert-then-prune: importing a CSV
upserts every row (content-hashed ids reproduce, and the conflict update
leaves the user's category_override alone), then soft-retires (removed=1)
only rows on DATES the CSV restates that the CSV no longer carries — so a
one-time full history load and a daily 30-day refresh both converge
without duplicates or eaten overrides, and a sparse CSV (portal glitch)
can't wipe the history between its anchors. Transaction ids are
`<provider>:<sha1 of the row key>`, so a re-import replaces rather than
duplicates.

import_csv takes CSV TEXT (the import hub's convention) rather than a
filesystem path; an unknown plan raises ValueError rather than exiting, so
one bad plan cannot kill a server worker; `looks_like_plan_activity(header)`
is the import-hub detection hook (mint/ynab/competitors style).
"""
from __future__ import annotations

import csv as csvmod
import datetime as dt
import hashlib
import io
import json
import re
from collections import defaultdict

from ..engine.compat import as_date, jsonb
from . import heartbeat
from .base import MERCHANT_NAME_ON_CONFLICT

# items.aggregator for every plan-CSV item, whatever the provider slug
AGGREGATOR = "plan_csv"
DEFAULT_PROVIDER = "plan"
DEFAULT_INSTITUTION = "Workplace plan"
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Same DoS cap as the coinbase push door — this endpoint takes
# script-token input, and a real plan CSV is a few hundred rows, so 20k is
# generous headroom, not a limit anyone hits legitimately.
MAX_ROWS = 20_000

# Workplace plan codes the import door accepts. Account ids are
# <provider>-<code>, stable so a re-import upserts rather than duplicating.
# Metadata is DERIVED from the code — nothing employer-specific ships in
# this file: default display names are generic, a tenant labels their
# plans via config `plan_names` (e.g. {"401K": "Acme Corp 401K"}), and an
# existing account row is never renamed by an import anyway (ON CONFLICT DO
# NOTHING).
PLAN_CODES = ("401A", "401K", "403B", "457B")

PLAN_COLUMNS = {"trade date", "investments", "ticker", "transaction",
                "transaction amount", "share price", "total shares"}
# header name → row key, for the header-mapped parse
_COLUMN_KEYS = {"trade date": "date", "investments": "fund", "ticker": "symbol",
                "transaction": "type", "transaction amount": "amount",
                "share price": "price", "total shares": "shares"}


def provider_slug(value) -> str:
    """The caller's slug for the plan administrator, or the default; a
    ValueError for anything that could not be an item id."""
    slug = (str(value or "").strip().lower()) or DEFAULT_PROVIDER
    if not _PROVIDER_RE.match(slug):
        raise ValueError("provider must be a short lowercase slug "
                         "(letters, digits, - or _)")
    return slug


def account_id(provider: str, plan: str) -> str:
    return f"{provider}-{plan.lower()}"


def _plan_name(conn, plan: str, institution: str) -> str:
    """Tenant-config label for the plan, generic default otherwise."""
    from ..engine import budget
    names = budget.load_config(conn).get("plan_names") or {}
    return str(names.get(plan) or f"{institution} {plan}").strip()


def looks_like_plan_activity(header: list[str]) -> bool:
    """Import-hub detection hook (same signature family as looks_like_mint
    et al.). The CSV carries no plan identifier — the /import flow must ask
    which plan (401A/403B/457B) before calling import_csv."""
    return PLAN_COLUMNS <= {h.strip().lower().lstrip('"') for h in header}


def _ensure_item_account(conn, provider: str, institution: str, plan: str,
                         name: str, mask: str | None = None) -> str:
    """ON CONFLICT DO NOTHING keeps existing item/account rows
    (name, status, display_name) intact. access_token is a plain marker,
    not a secret — nothing to encrypt.

    `mask` is the plan number's last digits, when the caller knows them.
    It is the one field an import DOES restate on an existing row: an
    aggregator that reaches the same plan (Plaid gives these accounts a
    balance and nothing else) reports the same digits, and the account-
    link suggestions pair accounts on mask + type — so a mask here is what
    lets the owner confirm the two as one account instead of the plan
    counting twice in net worth."""
    aid = account_id(provider, plan)
    # aggregator='plan_csv': the honest source, not 'csv'; status
    # 'archived' keeps the item out of the hourly pull loop and the
    # stale-connection checks (script heartbeats own its freshness)
    conn.execute(
        """INSERT INTO items (id, aggregator, institution_name, access_token, status)
           VALUES (%s,%s,%s,%s,'archived')
           ON CONFLICT (tenant_id, id) DO NOTHING""",
        (provider, AGGREGATOR, institution, f"{provider}:csv"))
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype, currency)
           VALUES (%s,%s,%s,'investment',%s,'USD')
           ON CONFLICT (tenant_id, id) DO NOTHING""",
        (aid, provider, name, plan.lower()))
    if mask:
        conn.execute(
            "UPDATE accounts SET mask=%s WHERE id=%s AND mask IS DISTINCT FROM %s",
            (mask, aid, mask))
    return aid


def _num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _parse(text: str) -> list[dict]:
    """Rows keyed date/fund/symbol/type/amount/price/shares. A header row
    maps the columns by name; without one the seven columns are read in
    the order the portals share."""
    rows = []
    order = ["date", "fund", "symbol", "type", "amount", "price", "shares"]
    for r in csvmod.reader(io.StringIO(text)):
        if not r or not r[0]:
            continue
        head = [h.strip().lower().lstrip('"') for h in r]
        if PLAN_COLUMNS <= set(head):
            order = [_COLUMN_KEYS.get(h, f"_{i}") for i, h in enumerate(head)]
            continue
        cell = dict(zip(order, r))
        try:
            date = dt.datetime.strptime(
                (cell.get("date") or "").strip(), "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        rows.append({
            "date": date, "fund": (cell.get("fund") or "").strip(),
            "symbol": (cell.get("symbol") or "").strip(),
            "type": (cell.get("type") or "").strip(),
            "amount": _num(cell.get("amount")), "price": _num(cell.get("price")),
            "shares": _num(cell.get("shares")),
        })
    return rows


def _txn_id(provider: str, plan: str, row: dict, occ: int) -> str:
    # The key format is fixed — ids must reproduce so re-imports upsert
    # over existing rows.
    key = f"{plan}|{row['date']}|{row['symbol']}|{row['type']}|{row['amount']}|{row['shares']}|{row['price']}|{occ}"
    return f"{provider}:" + hashlib.sha1(key.encode()).hexdigest()[:20]


def _recompute_holdings(conn, provider: str, plan: str) -> float:
    """Derive per-fund net shares + last price from ALL of this plan's
    imported transactions in the DB, and rewrite the holdings rows."""
    aid = account_id(provider, plan)
    net_shares: dict[str, float] = defaultdict(float)
    last_px: dict[str, tuple] = {}
    names: dict[str, str] = {}
    # this door's rows only — another importer's row on the plan account
    # has no share/price payload (and may have no raw at all), so it must
    # neither feed nor crash the recomputation.
    for r in conn.execute(
            "SELECT date, raw FROM transactions WHERE account_id=%s"
            " AND id LIKE %s AND removed=0",
            (aid, f"{provider}:%")):
        raw = r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"] or "{}")
        sym = raw.get("symbol") or raw.get("fund")
        if not sym:
            continue
        names[sym] = raw.get("fund") or sym
        if raw.get("shares") is not None:
            net_shares[sym] += raw["shares"]
        if raw.get("price") is not None:
            prev = last_px.get(sym)
            if prev is None or as_date(r["date"]) >= prev[0]:
                last_px[sym] = (as_date(r["date"]), raw["price"])
    conn.execute("DELETE FROM holdings WHERE account_id=%s", (aid,))
    total = 0.0
    now = dt.datetime.now(dt.timezone.utc)
    for sym, shares in net_shares.items():
        # A portal CSV rounds each row's Total shares to 3 decimals
        # independently, so a fully-liquidated fund nets to a few
        # thousandths of a share instead of 0 (a position bought at +30.640
        # and sold at -30.642; a more-traded one drifts further). Skip these
        # closed positions so no phantom sub-dollar dust row lands in
        # holdings. 0.05 covers ~100 rounding events yet sits far below any
        # real position, so it never touches a live holding.
        if abs(shares) < 0.05:
            continue
        px = last_px.get(sym, (None, None))[1]
        val = round(shares * px, 2) if px is not None else None
        if val:
            total += val
        conn.execute(
            """INSERT INTO holdings
                   (account_id, symbol, name, quantity, price, value, as_of, raw)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id, account_id, symbol) DO UPDATE SET
                   name=EXCLUDED.name, quantity=EXCLUDED.quantity,
                   price=EXCLUDED.price, value=EXCLUDED.value,
                   as_of=EXCLUDED.as_of, raw=EXCLUDED.raw""",
            (aid, sym, names.get(sym, sym), round(shares, 4), px, val, now,
             jsonb({"net_shares": shares, "last_price": px})))
    return round(total, 2)


def import_csv(conn, plan: str, text: str, balance: float | None = None,
               mask: str | None = None, *, provider: str = DEFAULT_PROVIDER,
               institution: str = DEFAULT_INSTITUTION) -> dict:
    """Date-set-replace import of one plan's activity CSV. `balance` (the
    portal's own current balance, read alongside the CSV) overrides the
    holdings-derived account balance when given; `mask` (the plan number's
    last digits) is stamped on the account so a second source for the same
    plan can be linked to it — see _ensure_item_account. `provider` names
    the plan administrator (the item and id namespace), `institution` how
    it is shown."""
    plan = plan.upper()
    if plan not in PLAN_CODES:
        raise ValueError(f"Unknown plan '{plan}'. Use one of {', '.join(PLAN_CODES)}")
    provider = provider_slug(provider)
    institution = (institution or "").strip() or DEFAULT_INSTITUTION
    rows = _parse(text)
    if not rows and balance is None:
        # ValueError, not a fatal error type: the host-side collector's
        # per-plan `except Exception` must catch this so one empty CSV
        # doesn't abort the remaining plans.
        raise ValueError("No transactions parsed from the plan activity CSV")
    if not rows:
        # A plan nobody is contributing to reports NO activity and a
        # balance — which is not an error, it is the normal state of a
        # dormant 403(b), so the push is accepted.
        # Apply the balance the portal DID give us and say so.
        plan_name = _plan_name(conn, plan, institution)
        with conn.transaction():
            aid = _ensure_item_account(conn, provider, institution, plan,
                                       plan_name, mask)
            holdings_val = _recompute_holdings(conn, provider, plan)
            conn.execute(
                "UPDATE accounts SET balance_current=%s, updated_at=now() "
                "WHERE id=%s", (round(balance, 2), aid))
            heartbeat.stamp(conn, provider, rows=0, label=institution)
        return {"plan": plan, "rows": 0, "inserted": 0, "span": None,
                "holdings_value": holdings_val,
                "balance": round(balance, 2), "balance_only": True}
    if len(rows) > MAX_ROWS:
        # rejected before any DB write, like the coinbase door
        raise ValueError(f"too many transactions (max {MAX_ROWS})")
    dates = [r["date"] for r in rows]
    lo, hi = min(dates), max(dates)
    plan_name = _plan_name(conn, plan, institution)

    with conn.transaction():
        aid = _ensure_item_account(conn, provider, institution, plan,
                                   plan_name, mask)
        # Window replace as UPSERT-then-PRUNE: a DELETE-then-INSERT would
        # recreate every row in the span, and a user category_override on
        # those rows would die on every push. Ids
        # are content-hashed and reproduce, so re-imported rows hit the
        # ON CONFLICT DO UPDATE below — which deliberately leaves
        # category_override alone — and only rows the CSV no longer
        # carries inside its own span are deleted afterwards.
        occ: dict[tuple, int] = defaultdict(int)
        inserted = 0
        pushed_ids: list[str] = []
        for r in rows:
            k = (r["date"], r["symbol"], r["type"], r["amount"], r["shares"])
            occ[k] += 1
            tid = _txn_id(provider, plan, r, occ[k])
            amt = r["amount"] or 0.0
            cat = "TRANSFER_IN" if amt >= 0 else "TRANSFER_OUT"  # both spend-excluded
            name = f"{plan_name}: {r['type']}" + (
                f" ({r['fund']})" if r["fund"] else "")
            # Engine-wide Plaid sign convention: positive = money OUT.
            # A plan CSV is positive = money into the plan, so flip
            # (raw preserves the original for _retirement_pl / _security).
            conn.execute(
                """INSERT INTO transactions
                       (id, account_id, date, amount, name, merchant_name,
                        category_primary, category_detailed, pending, removed, raw,
                        category_source)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,'flow')
                   ON CONFLICT (tenant_id, id) DO UPDATE SET
                       date=EXCLUDED.date, amount=EXCLUDED.amount,
                       name=EXCLUDED.name,
                       """ + MERCHANT_NAME_ON_CONFLICT + """,
                       category_primary=EXCLUDED.category_primary,
               category_source=EXCLUDED.category_source,
                       category_detailed=EXCLUDED.category_detailed,
                       pending=0, removed=0, raw=EXCLUDED.raw""",
                (tid, aid, as_date(r["date"]), -amt, name, institution,
                 cat, "PLAN_" + r["type"].upper().replace(" ", "_"),
                 jsonb(r)))
            inserted += 1
            pushed_ids.append(tid)
        # prune: rows the source no longer reports — per restated DATE,
        # not the CSV's min..max span: a sparse CSV (portal
        # glitch, filtered view) must not retire every row between its
        # anchors. A date the CSV carries is restated in full; dates it
        # omits keep their rows. Soft-retire like the coinbase door,
        # never DELETE: removed=1 is reversible (a row's id
        # reappearing in a later CSV hits the upsert above, which sets
        # removed=0) and preserves any category_override on the pruned
        # row. _recompute_holdings and reporting only read removed=0.
        # Only rows THIS door wrote (id '<provider>:…') are candidates: a
        # CSV/OFX import that also landed on the plan account for a
        # restated date is somebody else's data, and the coinbase door
        # already namespaces its prune the same way.
        conn.execute(
            "UPDATE transactions SET removed=1 WHERE account_id=%s"
            " AND id LIKE %s"
            " AND date = ANY(%s) AND NOT (id = ANY(%s))",
            (aid, f"{provider}:%", sorted({as_date(r["date"]) for r in rows}),
             pushed_ids))
        holdings_val = _recompute_holdings(conn, provider, plan)
        bal = balance if balance is not None else holdings_val
        conn.execute(
            "UPDATE accounts SET balance_current=%s, updated_at=now() WHERE id=%s",
            (round(bal, 2), aid))
        heartbeat.stamp(conn, provider, rows=inserted, label=institution)

    return {"plan": plan, "rows": len(rows), "inserted": inserted,
            "span": f"{lo}..{hi}", "holdings_value": holdings_val,
            "balance": round(bal, 2)}


def rollback(conn, plan: str | None = None, *,
             provider: str = DEFAULT_PROVIDER) -> int:
    provider = provider_slug(provider)
    plans = [plan.upper()] if plan else list(PLAN_CODES)
    n = 0
    with conn.transaction():
        for p in plans:
            aid = account_id(provider, p)
            n += conn.execute(
                "DELETE FROM transactions WHERE account_id=%s", (aid,)).rowcount
            conn.execute("DELETE FROM holdings WHERE account_id=%s", (aid,))
            conn.execute("DELETE FROM accounts WHERE id=%s", (aid,))
        if not plan:
            conn.execute("DELETE FROM items WHERE id=%s", (provider,))
    return n
