"""Reporting engine: net worth, spending, cash flow & savings, fees.

Computes the four report families (net worth, spending, cash flow & savings,
investment fees). Reuses the project's spend definition and budget engine —
no business logic forked.

Net-worth CURRENT is exact (account balances). Net-worth OVER TIME is a
holdings-based reconstruction (see `_reconstruct_trend`): every account is
anchored to its exact current value and walked backward — investments through
their share-level transaction history valued at historical prices (so market
growth and drawdowns are captured), cash/cards through their own raw flows. It
is a reconstruction, not a recorded balance history, and labelled as such.

Structure worth knowing:
  * reports are computed on demand (web/reporting_api.py) plus a nightly
    snapshot job — there is no materialized report cache.
  * table DDL (holdings, crypto_holdings, networth_snapshot,
    networth_recorded, income_annual, merchant_canonical) lives in the
    schema/migrations — engine code never CREATEs tables.
  * merchant_dedup's MC_JOIN / DISPLAY_MERCHANT SQL fragments are imported
    from that module and re-exported below, so there is one definition.
"""
from __future__ import annotations

import bisect
import calendar
import datetime as dt
import hashlib
import json
import logging
import re

from . import budget

log = logging.getLogger(__name__)
from .compat import as_date, as_dict, jsonb

# ---- spend definition (mirrors budget._spend_rows) ----------------------
# shadow exclusion: drop transactions belonging to a
# linked account that isn't its group's primary — app.shadow_ids is set
# per tenant_connect (empty when nothing is linked → no-op).
_NOT_SHADOW = ("""
  AND ((SELECT NULLIF(current_setting('app.shadow_ids', true), '')) IS NULL
       OR NOT ({col} = ANY((SELECT string_to_array(
               current_setting('app.shadow_ids', true), ','))::text[])))""")
# The session variable is read through scalar subqueries on purpose: written
# bare, current_setting() is re-evaluated for every row of a full-table scan;
# as a subquery it becomes an InitPlan the planner evaluates once per
# statement. Same value, same meaning, a fraction of the per-row cost.
SPEND_WHERE = ("""t.removed = 0 AND t.amount > 0
  AND COALESCE(t.category_detailed,'') != 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
  AND COALESCE(t.category_override, t.category_primary, '')
      NOT IN ('TRANSFER_OUT','TRANSFER_IN')
  AND t.account_id NOT IN (SELECT id FROM accounts WHERE type='loan')"""
  + _NOT_SHADOW.format(col="t.account_id")
  # exclude business-entity money from personal reports (same
  # predicate _spend_rows uses — imported from budget so they can't drift).
  + budget.PERSONAL_ONLY_SQL)
# REPLACE spans the whole COALESCE so underscore-form overrides
# ('RENT_AND_UTILITIES') display-merge with native primaries ('RENT AND
# UTILITIES'); Amazon overrides carry no underscores so are unaffected.
EFF_CAT = "REPLACE(COALESCE(t.category_override, t.category_primary,'?'),'_',' ')"

# What the AGGREGATOR called the row, before any rule touched it: Plaid's own
# personal_finance_category primary (the raw payload outlives every later
# re-stamp), else the category_plaid column bank sync wrote. NULL for rows
# no aggregator labelled (CSV imports), so a predicate built on it must be
# a no-op there. `t` must be the transactions alias.
ORIGINAL_CAT = ("COALESCE(t.raw #>> '{personal_finance_category,primary}', "
                "t.category_plaid)")
# Money in is only INCOME when the aggregator did not call it a transfer. A
# user's merchant rule may reclassify a TRANSFER row into a spend category
# (a babysitter paid through Venmo is "Child care" to the household), and a
# merchant-wide rule reaches the occasional inflow under the same merchant
# too — the babysitter Venmo-ing $50 back. That inflow is no longer a
# transfer to the effective-category test, but it is still not income: a
# reclassified transfer counts as income only when the user explicitly made
# it INCOME (a per-row override — rules refuse flow categories).
NOT_RECLASSIFIED_TRANSFER_IN = f"""
  AND NOT (COALESCE({ORIGINAL_CAT}, '') IN ('TRANSFER_IN','TRANSFER_OUT')
           AND COALESCE(t.category_override, t.category_primary, '')
               <> 'INCOME')"""

# partial reimbursements net the received money off their expense
# row — the remainder stays real spend. Full pairs stay excluded by
# category (TRANSFER_*) as before; this expression only bites rows with
# partial pairs (correlated on the reimbursements PK prefix).
# partial-reimbursement net, floored at 0. Canonical definition
# lives in budget (reporting imports budget, not the reverse) — re-exported
# here under the name callers already use so the two cannot drift apart.
NET_AMOUNT = budget.NET_AMOUNT

# The same net, as a join. NET_AMOUNT's correlated subquery is re-run for
# every output row (a SubPlan), which on an all-history aggregate means tens
# of thousands of probes of a table that is usually empty. Pre-aggregating
# the partial reimbursements once and LEFT JOINing them on the expense id
# computes the identical value — SUM of the same rows, COALESCEd to 0 and
# floored at 0 — in a single hash probe per row. Use the pair together:
# `FROM transactions t {REIMB_PARTIAL_JOIN}` and `SUM({NET_AMOUNT_JOINED})`.
# Row-level readers (budget._spend_rows) keep NET_AMOUNT; the join form is
# for the full-scan aggregates in this module and the lenses.
REIMB_PARTIAL_JOIN = ("LEFT JOIN (SELECT expense_id, SUM(amount) AS s "
                      "FROM reimbursements WHERE partial = 1 "
                      "GROUP BY expense_id) pr ON pr.expense_id = t.id")
NET_AMOUNT_JOINED = "GREATEST(t.amount - COALESCE(pr.s, 0), 0)"

# merchant_dedup fragments — imported, ONE definition. Callers alias
# transactions as `t`, LEFT JOIN via MC_JOIN, group/label by DISPLAY_MERCHANT.
from . import merchant_dedup as _md  # noqa: E402
MC_JOIN = _md.MC_JOIN
DISPLAY_MERCHANT = _md.DISPLAY_MERCHANT
MERCHANT_LOGO = _md.MERCHANT_LOGO

# ROUND(double precision, n) doesn't exist on Postgres; the numeric
# round-trip keeps 2-decimal SQL rounding with float JSON output.
_R2 = "ROUND(({0})::numeric, 2)::float8"


def excluded_accounts_sql(conn, memo: dict | None = None) -> tuple[str, tuple]:
    """The Accounts page 'excl' toggle (budget config `excluded_accounts`) as
    a (SQL fragment, params) pair every report in this module appends to its
    predicate over `transactions t`.

    One helper, and every entry point calls it, because the exclusion used to
    live inside flow_breakdown alone: the Overview's "went out" dropped an
    excluded card's rows while the Spending tab's total for the SAME window
    kept them, so two figures on one page disagreed and neither was wrong by
    its own rules. The fragment is empty when nothing is excluded, so a
    caller splices both parts unconditionally:

        excl, ex_p = excluded_accounts_sql(conn)
        conn.execute(f"... WHERE {SPEND_WHERE} AND t.date >= %s {excl}",
                     (start, *ex_p))

    `memo` is the request-scoped budget cache, when the caller already has
    one — the config must be read once per call, or the spend total and the
    fixed split can be judged under two different configs."""
    excluded = list(budget.load_config(conn, memo=memo).get(
        "excluded_accounts") or [])
    return (("AND NOT (t.account_id = ANY(%s))", (excluded,)) if excluded
            else ("", ()))


def _iso(v) -> str:
    """DATE columns arrive as datetime.date; the trend machinery compares
    ISO strings. Normalize at the row boundary."""
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v)[:10]


def asset_class(typ: str, subtype: str | None) -> str:
    subtype = (subtype or "").lower()
    if typ == "depository":
        return "Cash"
    if typ == "credit":
        return "Card debt"
    if typ == "investment":
        if subtype == "crypto":
            return "Crypto"
        if subtype in ("401a", "403b", "457b", "401k", "457", "ira",
                       "roth", "roth ira", "roth 401k", "roth 403b",
                       "sep ira", "simple ira", "pension", "retirement",
                       "keogh", "sarsep", "profit sharing plan",
                       "thrift savings plan"):
            return "Retirement"
        return "Taxable investments"
    return "Other"


# ---- net worth ----------------------------------------------------------
def _live_accounts(conn, owner: str | None = None):
    """Accounts with a real current balance (exclude synthetic/historical items
    whose balances are NULL: mint/simplifi/betterment/closed lineages).
    Linked shadow accounts are excluded — the same real-world
    money must not be counted once per source. An owner filter restricts
    the result to that owner's accounts (a reporting lens, not a money
    change)."""
    from . import links
    shadows = links.shadow_ids(conn)
    q = """SELECT a.id, COALESCE(a.display_name, a.name) AS name,
                  a.type, a.subtype, a.balance_current AS bal,
                  i.institution_name AS inst
           FROM accounts a JOIN items i ON i.id=a.item_id
           WHERE a.balance_current IS NOT NULL
             AND (""" + budget.COMBINE_OR + """a.entity_id IS NULL)  -- business accounts out of personal net worth (unless combined)
             AND NOT (a.id = ANY(%s))"""
    args: list = [shadows]
    if owner:
        q += " AND a.owner = %s"
        args.append(owner)
    return conn.execute(q, args).fetchall()


def compute_networth(conn, owner: str | None = None,
                     today: dt.date | None = None) -> dict:
    rows = _live_accounts(conn, owner)
    by_inst, by_class = {}, {}
    total = 0.0
    live_loans = []          # loan accounts (e.g. mortgage) → property layer
    for r in rows:
        if r["type"] == "loan":
            live_loans.append(r)
            continue         # mortgages live in the property layer, not financial
        signed = (-r["bal"]) if r["type"] == "credit" else r["bal"]
        total += signed
        by_inst[r["inst"]] = by_inst.get(r["inst"], 0) + signed
        cls = asset_class(r["type"], r["subtype"])
        by_class[cls] = by_class.get(cls, 0) + signed
    total = round(total, 2)

    # ---- property layer: real estate + vehicles − mortgage ----------------
    # Values from tenant config `manual_assets` (user-editable). A live
    # aggregator loan account replaces the manual mortgage entry automatically
    # once linked.
    # property + the historical trend are HOUSEHOLD-level and not
    # owner-tagged, so an owner lens shows only that owner's live accounts.
    if owner:
        return {
            "current_total": total,
            "property_items": [], "property_net": 0.0,
            "full_total": total,
            "by_institution": sorted(([k, round(v, 2)] for k, v in by_inst.items()),
                                     key=lambda x: -x[1]),
            "by_asset_class": sorted(([k, round(v, 2)] for k, v in by_class.items()),
                                     key=lambda x: -x[1]),
            "trend": [], "coinbase_pl": [], "retirement_combined": {},
            "trend_estimated_until": 0, "snapshot_trend": [],
            "savings_trend": [], "owner": owner,
        }
    try:
        manual = budget.load_config(conn).get("manual_assets", [])
    except Exception:
        manual = []
    # A live MORTGAGE supersedes a manual mortgage entry — an auto or student
    # loan does not. Testing `live_loans` as a whole would let any loan of any
    # subtype delete the manual mortgage from net worth and then relabel that
    # unrelated loan "mortgage": a $300k home loan vanishing because a $20k
    # car loan arrived, overstating net worth by the mortgage and lying about
    # what the remaining row is.
    live_mortgages = [r for r in live_loans if (r["subtype"] or "") == "mortgage"]
    prop_items = []
    for m in manual:
        if not isinstance(m, dict):
            continue
        if m.get("kind") == "mortgage" and live_mortgages:
            continue                              # live balance supersedes
        # The Settings-save path runs every asset value through _num(), but a
        # restored/hand-edited config can carry a non-numeric value straight
        # into the tenant — a bad row must be skipped, not 500 the net-worth
        # dashboard (one of the hottest endpoints) and the AI assistant.
        try:
            value = round(float(m.get("value") or 0), 2)
        except (ValueError, TypeError):
            continue
        prop_items.append({"name": m.get("name", "?"), "kind": m.get("kind", "?"),
                           "value": value,
                           "source": f"manual ({m.get('as_of', '?')})"})
    for ln in live_loans:
        # Carry the real subtype through — NetWorth.tsx filters property_items
        # on kind == 'mortgage' for its "incl. mortgage payoff" caption, so a
        # mislabelled auto loan is a wrong number on the page, not just a tag.
        prop_items.append({"name": f"{ln['inst']} {ln['name']}",
                           "kind": (ln["subtype"] or "loan") or "loan",
                           "value": round(-(ln["bal"] or 0), 2), "source": "live (Plaid)"})
    property_net = round(sum(p["value"] for p in prop_items), 2)

    trend = _reconstruct_trend(conn, total, today=today)   # anchored-exact-today, back-cast
    return {
        "current_total": total,
        "property_items": prop_items,
        "property_net": property_net,
        "full_total": round(total + property_net, 2),
        "by_institution": sorted(([k, round(v, 2)] for k, v in by_inst.items()),
                                 key=lambda x: -x[1]),
        "by_asset_class": sorted(([k, round(v, 2)] for k, v in by_class.items()),
                                 key=lambda x: -x[1]),
        "trend": trend,
        "coinbase_pl": _coinbase_pl(conn),
        "retirement_combined": _retirement_combined(conn),
        # index up to which the trend is a reconstruction (best guess). Everything
        # before the first REAL nightly snapshot is estimated; the recorded tail
        # grows one point per night. Whole line is estimated until snapshots accrue.
        "trend_estimated_until": _trend_estimated_until(conn, [m for m, _ in trend]),
        "snapshot_trend": _snapshot_series(conn),          # real, accrues nightly
        "savings_trend": _cumulative_savings(conn),        # honest historical proxy
    }


def _trend_estimated_until(conn, months: list) -> int:
    """Index of the boundary between reconstructed (estimated) and recorded
    months. Recorded = the earlier of the first `networth_recorded` month
    (balance report) and the first nightly `networth_snapshot`."""
    firsts = []
    row = conn.execute("SELECT MIN(date) AS d FROM networth_snapshot").fetchone()
    if row and row["d"]:
        firsts.append(_iso(row["d"])[:7])
    # exclude rows an import marked 'historical…': reconstructed from old
    # statements or a register (honestly "estimated"), not the live recorded
    # era, so the solid/recorded boundary stays at the snapshot region.
    row = conn.execute("SELECT MIN(month) AS m FROM networth_recorded "
                       "WHERE COALESCE(source,'') NOT LIKE 'historical%'").fetchone()
    if row and row["m"]:
        firsts.append(row["m"])
    if not firsts or not months:
        return len(months) - 1
    fm = min(firsts)
    for i, m in enumerate(months):
        if m >= fm:
            return i
    return len(months) - 1


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _security(raw):
    """Normalize an investment transaction's raw JSON to (ticker, share_delta,
    price) across all sources (QFX/Plaid brokerages, workplace-plan feeds,
    robo-advisors, Coinbase). Returns None for non-investment rows."""
    d = as_dict(raw)
    if "native_amount" in d and isinstance(d.get("amount"), dict):        # coinbase
        qty = _f(d["amount"]["amount"]); nat = _f(d["native_amount"]["amount"])
        return (d["amount"]["currency"], qty or 0.0,
                (abs(nat / qty) if qty and nat is not None else None))
    if "shares" in d:                                                     # plan feeds, robo-advisors
        return (d.get("symbol") or d.get("fund"), _f(d["shares"]) or 0.0, _f(d.get("price")))
    if "units" in d:                                                      # QFX brokerages
        return (d.get("ticker") or d.get("cusip"), _f(d["units"]) or 0.0, _f(d.get("unitprice")))
    return None


# Large moves of money BETWEEN the user's own accounts (checking↔brokerage,
# brokerage→brokerage) are NOT net-worth changes — they're transfers. We net
# them out so a round-trip doesn't read as a bump.
_TRANSFER_MIN = 10_000


def _is_transfer(raw) -> bool:
    d = as_dict(raw)
    if "native_amount" in d:
        return str(d.get("type")) in ("pro_withdrawal", "pro_deposit", "exchange_deposit",
                                      "exchange_withdrawal", "fiat_deposit", "fiat_withdrawal")
    t = str(d.get("type") or "").lower()
    return any(k in t for k in ("invbanktran", "acat", "withdraw", "deposit from",
                                "transfer", "in-kind"))


def _transfer_dir(raw, amount: float) -> float:
    """Signed cash flow INTO the investment world (+ in, − out). Direction is read
    from the transaction TYPE, not the stored amount sign — importers disagree on
    sign (one robo-advisor stores deposits opposite to a QFX brokerage), so the type name is
    the only reliable cue."""
    t = str(as_dict(raw).get("type") or "").lower()
    mag = abs(amount)
    if any(k in t for k in ("withdraw", "liquidation", "redemption", "send")):
        return -mag
    if any(k in t for k in ("deposit", "contribution", "purchase")):
        return +mag
    return -amount            # invbanktran/transfer: negative amount = money in


def _reconstruct_trend(conn, today_total, today: dt.date | None = None):
    """Net worth over time, reconstructed from HOLDINGS, not cash flow.

    Anchors every account at its exact current value and walks backward:
      • Investments — each position's share count is walked back from today's
        holdings through the share-level transaction history (buys/sells/reinvests),
        then valued at that date's price (interpolated from every price the ledger
        observed for that security, plus today's live price). This captures market
        growth AND drawdowns. Clamped to each account's inception (it held
        nothing before it existed).
      • Cash & cards — a constant baseline at today's exact signed balance
        (pre-history unrecoverable; investment transfers handled by the offset).
      • The part of an investment balance its holdings do not explain is
        carried as a constant from the account's inception, like cash.

    Transfers between the user's own accounts (large deposits/withdrawals/ACATs)
    are netted out via `_transfer_dir` so a round-trip doesn't read as a
    net-worth bump — only market moves and genuine external flows change the
    line. Residual month spikes from transfer timing are smoothed.

    Monthly, exact today. It is a reconstruction, not a recorded balance
    history — months with short investment history are underdetermined;
    recorded month-ends and the nightly snapshot pin what they cover."""
    today_d = today or dt.date.today()
    from . import links
    shadows = set(links.shadow_ids(conn))   # exclude dual-source duplicates
    # the SAME entity filter _live_accounts applies to the current
    # total. Without it the historical cash baseline carries business
    # balances that today's anchor excludes, so the line sits high for every
    # past month and then steps to the real number at today — a fabricated
    # move the exact size of the entity's balances.
    accts = list(conn.execute(
        "SELECT id, type, subtype, balance_current AS bal FROM accounts"
        " WHERE (" + budget.COMBINE_OR + "entity_id IS NULL)"))
    # exclude linked shadow investment accounts (same set _live_accounts drops)
    # — a Plaid+MX brokerage link would otherwise double-count holdings + the
    # share-delta walk across the whole reconstructed trend
    inv_ids = {a["id"] for a in accts
               if (a["type"] == "investment" or a["subtype"] == "crypto")
               and a["id"] not in shadows}
    if not inv_ids:
        return [[today_d.strftime("%Y-%m"), today_total]]

    # current holdings anchor: aid -> {ticker: [shares_now, price_now]}
    hold_now: dict = {}
    for h in conn.execute("SELECT account_id, symbol, quantity, price FROM holdings"):
        hold_now.setdefault(h["account_id"], {})[h["symbol"]] = [h["quantity"] or 0.0, h["price"] or 0.0]
    for h in conn.execute("SELECT account_id, currency, quantity, native_usd FROM crypto_holdings"):
        q = h["quantity"] or 0.0
        hold_now.setdefault(h["account_id"], {})[h["currency"]] = [q, (h["native_usd"] / q if q else 0.0)]

    # per-ticker price curve from every observation in the ledger + today's price.
    # One fetch of the investment rows feeds everything below that needs them
    # — share deltas, price observations, transfer events and each account's
    # inception — so the ledger is read (and its raw JSON decoded) once, not
    # once per purpose.
    today = today_d.isoformat()
    obs: dict = {}
    inv_deltas: dict = {}     # (aid, ticker) -> [(date, share_delta)]
    events = []               # large own-money transfers: (date, signed flow)
    inception: dict = {}      # aid -> first ledger date
    for r in conn.execute(
            "SELECT account_id, date, amount, raw FROM transactions "
            "WHERE removed=0 AND account_id = ANY(%s)", (list(inv_ids),)):
        aid = r["account_id"]
        d_iso = _iso(r["date"])
        if d_iso < inception.get(aid, "9999"):
            inception[aid] = d_iso
        amt = r["amount"] or 0.0
        if abs(amt) >= _TRANSFER_MIN and _is_transfer(r["raw"]):
            events.append((d_iso, _transfer_dir(r["raw"], amt)))
        s = _security(r["raw"])
        if not s or not s[0]:
            continue
        inv_deltas.setdefault((aid, s[0]), []).append((d_iso, s[1]))
        if s[2] and s[2] > 0:
            obs.setdefault(s[0], []).append((d_iso, s[2]))
    for posn in hold_now.values():
        for tk, (sh, pr) in posn.items():
            if pr and pr > 0:
                obs.setdefault(tk, []).append((today, pr))
    curve = {tk: sorted(set(o)) for tk, o in obs.items()}
    curve_dates = {tk: [d for d, _ in c] for tk, c in curve.items()}
    # shares held after a date = today's shares minus every delta dated after
    # it. Sorted deltas with a suffix-sum table answer that with one bisect
    # instead of re-summing the account's whole history for each of the
    # ~300 months walked.
    delta_dates: dict = {}
    delta_after: dict = {}    # (aid, tk) -> suffix sums aligned to delta_dates
    for key, dl in inv_deltas.items():
        dl.sort(key=lambda x: x[0])
        delta_dates[key] = [d for d, _ in dl]
        acc = [0.0] * (len(dl) + 1)
        for i in range(len(dl) - 1, -1, -1):
            acc[i] = acc[i + 1] + dl[i][1]
        delta_after[key] = acc

    def price(tk, date):
        c = curve.get(tk)
        if not c:
            return None
        i = bisect.bisect_right(curve_dates[tk], date)
        if i == 0:
            return c[0][1]
        if i >= len(c):
            return c[-1][1]
        (d0, p0), (d1, p1) = c[i - 1], c[i]
        span = (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days or 1
        frac = (dt.date.fromisoformat(date) - dt.date.fromisoformat(d0)).days / span
        return p0 + (p1 - p0) * frac

    # cash/cards: a small constant baseline (today's exact signed balance). Its
    # early history is unrecoverable and swamped by the investment layer, so we
    # don't try to walk it — investment transfers are handled by the offset below.
    signed_now = {a["id"]: ((-(a["bal"] or 0.0)) if a["type"] == "credit" else (a["bal"] or 0.0))
                  for a in accts}
    # loans (mortgage/auto) live in the PROPERTY layer, not the financial
    # total — compute_networth skips them, so the trend must too, or a
    # linked mortgage would inflate the whole line by its balance
    loan_ids = {a["id"] for a in accts if a["type"] == "loan"}
    # Linked shadow accounts are excluded like every other
    # money aggregate — a dual-sourced checking/card pair would double the
    # baseline, and a shadow INVESTMENT account (excluded from inv_ids
    # above) would otherwise fall through into the cash sum
    cash_baseline = sum(v for aid, v in signed_now.items()
                        if aid not in inv_ids and aid not in loan_ids
                        and aid not in shadows)

    # transfer offset: cumulative external cash that flowed into the investment
    # world AFTER date t. Adding it makes contributions/withdrawals net-worth-
    # neutral (the money was in cash before it was invested).
    events.sort()
    ev_dates = [d for d, _ in events]
    ev_flows = [f for _, f in events]

    def offset(date):
        i = bisect.bisect_right(ev_dates, date)
        return sum(ev_flows[i:])

    acct_tickers: dict = {}
    for (aid, tk) in inv_deltas:
        acct_tickers.setdefault(aid, set()).add(tk)
    for aid, posn in hold_now.items():
        for tk in posn:
            acct_tickers.setdefault(aid, set()).add(tk)

    # The part of each investment balance the listed holdings do not
    # explain (a cash sweep, an unpriced fund, a plan reporting only a
    # total). It belongs to every month the account existed; carried as a
    # constant like cash, or the line would sit at cash + holdings for the
    # whole history and then jump by the residual in the pinned last month.
    residual = {}
    for a in accts:
        if a["id"] in inv_ids and a["bal"] is not None:
            held = sum(sh * pr for sh, pr in hold_now.get(a["id"], {}).values())
            residual[a["id"]] = signed_now[a["id"]] - held

    def nw(date):
        total = cash_baseline + offset(date)
        for aid in inv_ids:
            if inception.get(aid, "9999") > date:       # account didn't exist yet
                continue
            total += residual.get(aid, 0.0)
            posn = hold_now.get(aid, {})
            for tk in acct_tickers.get(aid, ()):
                dd = delta_dates.get((aid, tk))
                after = (delta_after[(aid, tk)][bisect.bisect_right(dd, date)]
                         if dd else 0.0)
                sh = posn.get(tk, [0.0, 0.0])[0] - after
                if abs(sh) < 1e-6:
                    continue
                p = price(tk, date)
                if p is not None:
                    total += sh * p
        return total

    # Investment accounts can exist (holdings) with zero ledger rows yet —
    # a brokerage linked before its transactions sync, or a holdings-only
    # fill. min() of an empty inception set would raise and 500
    # /api/reports/networth, so fall back to a single "today" point like
    # the no-inv case.
    starts = [inception[a] for a in inv_ids if a in inception]
    if not starts:
        return [[today_d.strftime("%Y-%m"), today_total]]
    start = min(starts)[:7]
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = today_d.year, today_d.month
    months = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        last = (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)).isoformat()
        months.append(min(last, today))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    vals = [nw(d) for d in months]
    # smooth residual month spikes from transfer timing (money briefly in-flight
    # between our own accounts). Iterated; replaces any point far from its
    # neighbours' midpoint. Real market moves are gradual and survive this.
    for _ in range(4):
        for i in range(1, len(vals) - 1):
            mid = (vals[i - 1] + vals[i + 1]) / 2
            if abs(mid) > 1 and abs(vals[i] - mid) > 0.30 * abs(mid):
                vals[i] = mid

    # ---- recorded overlay ------------------------------------------------
    # RECORDED month-end balances beat the reconstruction wherever they exist:
    # (a) `networth_recorded` — imported month-end balance reports (an
    #     import may also write reconstructed rows marked 'historical…');
    # (b) nightly `networth_snapshot`s, collapsed to month-end.
    # Recorded catches real cash moves the frozen-cash reconstruction cannot
    # see, so recorded wins.
    recorded = {r["month"]: r["total"] for r in conn.execute(
        "SELECT month, total FROM networth_recorded")}
    for r in conn.execute(
            "SELECT DISTINCT ON (to_char(date,'YYYY-MM')) "
            "       to_char(date,'YYYY-MM') AS m, total "
            "FROM networth_snapshot ORDER BY to_char(date,'YYYY-MM'), date DESC"):
        recorded.setdefault(r["m"], r["total"])
    ym = [d[:7] for d in months]
    # Recorded month-end balances pin the reconstruction. There can be MORE than
    # one recorded region (e.g. an older historical block reconstructed from
    # statements + register, PLUS a recent snapshot block). Correct the
    # reconstruction's DRIFT piecewise-linearly so its shape is preserved but it
    # passes through every recorded anchor: ramp toward the first anchor before
    # it, interpolate the delta between consecutive anchors, hold the last delta
    # after. With a single recent region this reduces to a single ramp.
    rec_idx = [i for i, m in enumerate(ym) if m in recorded]
    if rec_idx:
        deltas = {i: recorded[ym[i]] - vals[i] for i in rec_idx}
        first, last = rec_idx[0], rec_idx[-1]
        for i in range(first):                                  # before first anchor
            vals[i] += deltas[first] * (i / first if first else 0)
        for a, b in zip(rec_idx, rec_idx[1:]):                  # between anchors
            da, db = deltas[a], deltas[b]
            for i in range(a + 1, b):
                vals[i] += da + (db - da) * ((i - a) / (b - a))
        for i in range(last + 1, len(vals)):                    # after last anchor
            vals[i] += deltas[last]
        for i in rec_idx:                                       # anchors land exact
            vals[i] = recorded[ym[i]]
    if vals:
        vals[-1] = today_total                          # pin the last point exact
    return [[d[:7], round(v, 2)] for d, v in zip(months, vals)]


def _retirement_pl(conn) -> list[dict]:
    """Workplace-plan P/L: contributions (the clean cost basis, straight
    off the payroll 'Contribution' rows) vs current value, per plan + a combined
    Total. Retirement accounts funded by rollovers or in-kind transfers are
    excluded — the transaction feed never tags those as deposits, so no honest
    cost basis exists for them."""
    out = []
    tot_c = tot_v = 0.0
    for a in conn.execute(
            """SELECT a.id, COALESCE(a.display_name, a.name) nm, a.balance_current v,
                      COALESCE(i.institution_name, 'Workplace plan') inst
               FROM accounts a JOIN items i ON i.id=a.item_id
               WHERE i.aggregator='plan_csv' AND a.balance_current IS NOT NULL
               ORDER BY a.balance_current DESC""").fetchall():
        c = conn.execute(
            """SELECT COALESCE(SUM((raw ->> 'amount')::float8),0) c
               FROM transactions WHERE account_id=%s AND removed=0
                 AND raw ->> 'type' = 'Contribution'""",
            (a["id"],)).fetchone()["c"]
        val = a["v"] or 0.0
        out.append({"name": a["nm"], "institution": a["inst"],
                    "contributed": round(c, 2),
                    "value": round(val, 2), "pl": round(val - c, 2),
                    "pct": round(100 * (val - c) / c, 1) if c else None})
        tot_c += c; tot_v += val
    if len(out) > 1:
        out.append({"name": "Total", "contributed": round(tot_c, 2),
                    "value": round(tot_v, 2), "pl": round(tot_v - tot_c, 2),
                    "pct": round(100 * (tot_v - tot_c) / tot_c, 1) if tot_c else None})
    return out


# Feeds whose accounts carry a P/L of their own — Coinbase's fiat-anchored
# one, a workplace plan's contributions — are left out of the holdings P/L.
_OWN_PL_AGGREGATORS = ("coinbase", "plan_csv")


def _brokerage_pl(conn) -> list[dict]:
    """Per-account gain vs money-in for brokerage accounts that report
    holdings, with the money-in source chosen per account:

    - Where the aggregator's per-holding COST BASIS is trustworthy (lots
      bought with cash at real prices), basis = Σcost_basis.
    - Where it is not (lots transferred in-kind arrive with a near-zero
      basis), a manual anchor takes over: config `investment_basis_override`
      keyed by account mask — the dollars actually put in, as the
      brokerage's own dashboard states them. Value stays live, so the return
      tracks going forward; only the deposit anchor is manual.

    A holding with no cost_basis is cash (CUR:USD, gain 0) or an untracked lot;
    those contribute value to BOTH sides (0 gain). Each row names its
    institution so the combined view can group by it."""
    override = budget.load_config(conn).get("investment_basis_override", {})
    per: dict = {}
    for h in conn.execute(
            """SELECT COALESCE(a.display_name, a.name) nm, a.mask, h.value, h.raw,
                      COALESCE(i.institution_name, 'Brokerage') inst
               FROM holdings h JOIN accounts a ON a.id=h.account_id
               JOIN items i ON i.id=a.item_id
               WHERE COALESCE(i.aggregator, '') <> ALL(%s)""",
            (list(_OWN_PL_AGGREGATORS),)):
        d = per.setdefault((h["inst"], h["nm"]), [0.0, 0.0, h["mask"]])   # [value, cost_basis, mask]
        val = h["value"] or 0.0
        d[0] += val
        try:
            cb = as_dict(h["raw"]).get("cost_basis")
        except (TypeError, ValueError):
            cb = None
        d[1] += cb if cb is not None else val      # no basis → basis=value (0 gain)
    out = []
    tot_v = tot_c = 0.0
    for (inst, nm), (v, c, mask) in sorted(per.items(), key=lambda x: -x[1][0]):
        if v < 1:                                  # skip drained/closed lineages
            continue
        if mask in override:                       # manual dashboard basis
            c = float(override[mask])
        out.append({"name": nm, "institution": inst, "basis": round(c, 2),
                    "value": round(v, 2), "pl": round(v - c, 2),
                    "pct": round(100 * (v - c) / c, 1) if c else None})
        tot_v += v; tot_c += c
    if len(out) > 1:
        out.append({"name": "Total", "basis": round(tot_c, 2), "value": round(tot_v, 2),
                    "pl": round(tot_v - tot_c, 2),
                    "pct": round(100 * (tot_v - tot_c) / tot_c, 1) if tot_c else None})
    return out


def _coinbase_pl(conn) -> list[dict]:
    """Per-account Coinbase profit/loss, external-cash anchored.

    Cost basis = FIAT flows only (buy/sell/fiat_deposit/fiat_withdrawal).
    pro_/exchange_ transfers are the user's own money moving between their
    Coinbase venues — counting them as flows buries the gains made there
    inside the cost basis, understating them badly. External crypto
    received via send/tx (unknown basis) counts as gain."""
    FIAT = {"buy", "sell", "fiat_deposit", "fiat_withdrawal"}
    out = []
    tot_in = tot_out = tot_val = 0.0
    for acct in conn.execute(
            """SELECT a.id, COALESCE(a.display_name, a.name) AS name FROM accounts a
               WHERE a.subtype='crypto' AND a.balance_current IS NOT NULL
               ORDER BY a.balance_current DESC""").fetchall():
        cash_in = cash_out = 0.0
        for r in conn.execute(
                "SELECT raw FROM transactions WHERE account_id=%s AND removed=0",
                (acct["id"],)):
            d = as_dict(r["raw"])
            if d.get("type") not in FIAT:
                continue
            try:
                usd = float((d.get("native_amount") or {}).get("amount") or 0)
            except (TypeError, ValueError):
                continue
            if usd > 0:
                cash_in += usd
            else:
                cash_out += -usd
        val = conn.execute(
            "SELECT COALESCE(SUM(native_usd),0) AS v FROM crypto_holdings WHERE account_id=%s",
            (acct["id"],)).fetchone()["v"] or 0.0
        if not cash_in and not cash_out and not val:
            # not a Coinbase-shaped account (no fiat flows, no
            # crypto_holdings) — this P/L method doesn't apply, and an
            # all-$0 row only reads as breakage
            continue
        pl = val + cash_out - cash_in
        out.append({"name": acct["name"], "cash_in": round(cash_in, 2),
                    "cash_out": round(cash_out, 2), "value": round(val, 2),
                    "pl": round(pl, 2),
                    "pct": round(100 * pl / cash_in, 1) if cash_in else None})
        tot_in += cash_in; tot_out += cash_out; tot_val += val
    if len(out) > 1:
        tpl = tot_val + tot_out - tot_in
        out.append({"name": "Combined", "cash_in": round(tot_in, 2),
                    "cash_out": round(tot_out, 2), "value": round(tot_val, 2),
                    "pl": round(tpl, 2),
                    "pct": round(100 * tpl / tot_in, 1) if tot_in else None})
    return out


def _retirement_combined(conn) -> dict:
    """Long-term investment accounts grouped by institution — brokerages
    (cost basis / dashboard-anchored) and workplace plans (contributions) —
    each account with money-in, value now, P/L and return; per-institution
    subtotals and a blended grand Total. 'Invested' unifies the two basis
    concepts (both are 'money put in' against current value)."""
    by_inst: dict[str, list[dict]] = {}
    for c in _brokerage_pl(conn):
        if c["name"] != "Total":
            by_inst.setdefault(c["institution"], []).append(
                {"name": c["name"], "invested": c["basis"], "value": c["value"],
                 "pl": c["pl"], "pct": c["pct"]})
    for c in _retirement_pl(conn):
        if c["name"] != "Total":
            by_inst.setdefault(c["institution"], []).append(
                {"name": c["name"], "invested": c["contributed"],
                 "value": c["value"], "pl": c["pl"], "pct": c["pct"]})

    def _grp(name, rows):
        rows = sorted(rows, key=lambda r: -r["value"])
        inv = sum(r["invested"] for r in rows)
        val = sum(r["value"] for r in rows)
        return {"provider": name, "accounts": rows, "invested": round(inv, 2),
                "value": round(val, 2), "pl": round(val - inv, 2),
                "pct": round(100 * (val - inv) / inv, 1) if inv else None}

    groups = [_grp(n, r) for n, r in by_inst.items() if r]
    if not groups:
        return {}
    groups.sort(key=lambda g: -g["value"])
    ti = sum(g["invested"] for g in groups)
    tv = sum(g["value"] for g in groups)
    return {"groups": groups,
            "total": {"invested": round(ti, 2), "value": round(tv, 2),
                      "pl": round(tv - ti, 2),
                      "pct": round(100 * (tv - ti) / ti, 1) if ti else None}}


def snapshot_networth(conn):
    """Record today's exact net worth + asset-class split. Called nightly by
    the snapshot job so a REAL net-worth trend accrues going forward."""
    from .. import localtime
    # The row's date is the HOUSEHOLD's day, like every other day the app
    # keys on: the nightly job runs on the instance clock (UTC hosted), so
    # a snapshot taken at 01:00 UTC would land on tomorrow's date for a US
    # household and the trend would show a point it has not lived.
    today = localtime.now_local(budget.load_config(conn)).date()
    rows = _live_accounts(conn)
    total = 0.0; by_class = {}
    for r in rows:
        # Match compute_networth: loans (mortgages) live in the property
        # layer, not as positive financial assets.
        if r["type"] == "loan":
            continue
        signed = (-r["bal"]) if r["type"] == "credit" else r["bal"]
        total += signed
        cls = asset_class(r["type"], r["subtype"])
        by_class[cls] = by_class.get(cls, 0) + signed
    with conn.transaction():
        conn.execute(
            """INSERT INTO networth_snapshot (date, total, by_class) VALUES (%s,%s,%s)
               ON CONFLICT (tenant_id, date) DO UPDATE
               SET total=EXCLUDED.total, by_class=EXCLUDED.by_class""",
            (today, round(total, 2), jsonb(by_class)))


def _snapshot_series(conn):
    return [[_iso(r["date"]), r["total"]] for r in conn.execute(
        "SELECT date, total FROM networth_snapshot ORDER BY date")]


def _cumulative_savings(conn):
    """Reliable historical wealth-building proxy: cumulative (income − spend) by
    year. NOT exact net worth (no early balance snapshots) — labelled as such
    in the UI."""
    excl, ex_p = excluded_accounts_sql(conn)
    spend = {r["yr"]: r["amt"] for r in conn.execute(
        f"SELECT to_char(t.date,'YYYY') yr, SUM({NET_AMOUNT_JOINED}) amt "
        f"FROM transactions t {REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} {excl} GROUP BY yr", ex_p)}
    income = {r["p"]: r["amt"] for r in _income_by(conn, "year")}
    covered = _bank_coverage_years(conn)
    years = sorted(set(spend) | set(income))
    run = 0.0; out = []
    for y in years:
        if y not in covered:  # no bank data → net unknowable; hold the line flat
            out.append([y, round(run, 2)])
            continue
        run += (income.get(y, 0) or 0) - (spend.get(y, 0) or 0)
        out.append([y, round(run, 2)])
    return out


def _month_ends(start: dt.date, end: dt.date) -> list[str]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        last = (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1))
        out.append(min(last, end).isoformat())
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---- spending -----------------------------------------------------------
def compute_spending(conn, today: dt.date | None = None) -> dict:
    # The trailing windows are measured from the HOUSEHOLD's day (the
    # router passes its timezone's today), bound into the same interval
    # arithmetic CURRENT_DATE drives. Off the DB clock, a household west of
    # UTC watches a day fall out of its 12-month figures every evening while
    # the Cash Flow windows (spending_window) keep it.
    today = today or dt.date.today()
    amt2 = _R2.format(f"SUM({NET_AMOUNT_JOINED})")
    excl, ex_p = excluded_accounts_sql(conn)
    # One pass over all history serves the year totals, the year × category
    # matrix and the all-time category axis: GROUPING SETS aggregates each
    # grouping from the same scan, and each group's SUM is still rounded in
    # SQL exactly as the three separate queries rounded theirs. GROUPING()
    # tells the rows apart — 0 = (yr, cat), 1 = year only, 2 = category only.
    all_time = conn.execute(
        f"SELECT to_char(t.date,'YYYY') yr, {EFF_CAT} cat, {amt2} amt, COUNT(*) n, "
        f"GROUPING(to_char(t.date,'YYYY'), {EFF_CAT}) g "
        f"FROM transactions t {REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} {excl} "
        f"GROUP BY GROUPING SETS ((yr, cat), (yr), (cat))",
        ex_p or None).fetchall()
    by_year = sorted((r for r in all_time if r["g"] == 1), key=lambda r: r["yr"])
    by_year_cat = [r for r in all_time if r["g"] == 0]
    by_cat = [r for r in all_time if r["g"] == 2]
    by_month = conn.execute(
        f"SELECT to_char(t.date,'YYYY-MM') ym, {amt2} amt "
        f"FROM transactions t {REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} "
        f"AND t.date >= %s::date - INTERVAL '36 months' {excl} "
        f"GROUP BY ym ORDER BY ym", (today, *ex_p)).fetchall()
    # Trailing 12 months, not all time. An all-time toplist is
    # dominated by whatever you spent most on since the ledger began — a
    # mortgage paid off years ago outranks this year's groceries forever — and
    # a spending report is about behaviour you can still change. by_year
    # below keeps the long view; that is its job.
    top_merch = conn.execute(
        f"SELECT {DISPLAY_MERCHANT} payee, {amt2} amt, COUNT(*) n, "
        f"       max({MERCHANT_LOGO}) logo "
        f"FROM transactions t {MC_JOIN} {REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} "
        f"AND t.date >= %s::date - INTERVAL '12 months' {excl} "
        f"GROUP BY payee ORDER BY amt DESC LIMIT 40", (today, *ex_p)).fetchall()
    # Amazon's own sub-categories, all time — the category grouping above,
    # narrowed to the 'Amazon…' prefix (LIKE 'Amazon%' is a case-sensitive
    # prefix test, which startswith reproduces).
    amazon = sorted((r for r in by_cat if (r["cat"] or "").startswith("Amazon")),
                    key=lambda r: -r["amt"])
    # Two lists, on purpose. The DISPLAYED toplist is trailing-12-month for
    # the reason above; the matrix's category AXIS stays all-time, because
    # narrowing it would quietly drop a category that was large for years
    # from the year-over-year table — which is the one place you would go to
    # see exactly that.
    cat_tot = conn.execute(
        f"SELECT {EFF_CAT} cat, {amt2} amt FROM transactions t {REIMB_PARTIAL_JOIN} "
        f"WHERE {SPEND_WHERE} "
        f"AND t.date >= %s::date - INTERVAL '12 months' {excl} "
        f"GROUP BY cat ORDER BY amt DESC LIMIT 15", (today, *ex_p)).fetchall()
    cat_all = sorted(by_cat, key=lambda r: -r["amt"])[:15]
    years = sorted({r["yr"] for r in by_year_cat})
    cats = [r["cat"] for r in cat_all]
    matrix = {(r["yr"], r["cat"]): r["amt"] for r in by_year_cat}
    return {
        "by_year": [[r["yr"], r["amt"], r["n"]] for r in by_year],
        "by_month": [[r["ym"], r["amt"]] for r in by_month],
        # [payee, amount, count, logo_url|null] — the 4th element is new;
        # clients that read three still work
        "top_merchants": [[r["payee"], r["amt"], r["n"], r["logo"]] for r in top_merch],
        "amazon": [[r["cat"], r["amt"], r["n"]] for r in amazon],
        "category_totals": [[r["cat"], r["amt"]] for r in cat_tot],
        "yoy_years": years, "yoy_cats": cats,
        "yoy_matrix": [[c] + [matrix.get((y, c), 0) for y in years] for c in cats],
    }


# ---- cash flow & savings ------------------------------------------------
# investment-provider tokens as they appear in bank-transaction descriptors
# and institution names — the same list decides what is a bank, what is an
# own-money inflow and what is investment funding
_INV_PROVIDERS = ("wealthfront", "betterment", "coinbase", "robinhood",
                  "schwab", "vanguard", "fidelity", "sofi invest", "acorns")
_INV_PATTERNS = ["%" + p + "%" for p in _INV_PROVIDERS]
# own-money inflows a bank stamps INCOME: transfers back from an investment
# provider (stock sells moved to checking — the budget engine treats
# those as shortfall funding, never income) and "Ext Trans" (transfers
# between own accounts at different banks). Literal SQL so _INCOME_WHERE
# keeps its one parameter; the doubled percents survive the param step.
_NOT_OWN_MONEY_SQL = " ".join(
    f"AND LOWER(t.name) NOT LIKE '%%{tok}%%'" for tok in (*_INV_PROVIDERS, "ext trans"))


def _bank_account_ids(conn) -> list[str]:
    """Depository accounts at a real bank — not an investment provider's cash
    sweep (a robo-advisor's "Cash" account), which is depository-typed but
    isn't bank money. Reaching these rows by joining accounts and items inside a full
    scan of transactions costs the whole ledger; resolving the handful of
    account ids first lets the same predicate run as an index probe over the
    bank rows alone. A NULL institution name fails the LIKE test here exactly
    as it failed it inside the join."""
    return [r["id"] for r in conn.execute(
        """SELECT a.id FROM accounts a JOIN items i ON i.id=a.item_id
           WHERE a.type='depository'
             AND LOWER(i.institution_name) NOT LIKE ALL(%s)""", (_INV_PATTERNS,))]


# The income definition, as SQL over bank rows. Own-money inflows a bank
# stamps INCOME are excluded (_NOT_OWN_MONEY_SQL). Callers supply the bank
# account ids as the one parameter.
_INCOME_WHERE = f"""t.removed=0 AND t.amount < 0 AND t.account_id = ANY(%s)
              {_NOT_OWN_MONEY_SQL}
              AND COALESCE(t.category_override, t.category_primary, '')
                  NOT IN ('TRANSFER_IN','TRANSFER_OUT')
              {NOT_RECLASSIFIED_TRANSFER_IN}
              {_NOT_SHADOW.format(col="t.account_id")}
              {budget.PERSONAL_ONLY_SQL}"""


def _income_by(conn, grain: str):
    # Bank accounts only — see _bank_account_ids for why the ids are resolved
    # first and _INCOME_WHERE for what counts as income.
    col = "to_char(t.date,'%s')" % ("YYYY-MM" if grain == "month" else "YYYY")
    excl, ex_p = excluded_accounts_sql(conn)
    return conn.execute(
        f"""SELECT {col} p, {_R2.format("SUM(-t.amount)")} amt FROM transactions t
            WHERE {_INCOME_WHERE} {excl}
            GROUP BY p ORDER BY p""",
        (_bank_account_ids(conn), *ex_p)).fetchall()


def _income_year_month(conn) -> tuple[dict, dict]:
    """Both grains of _income_by from one pass: ({year: amt}, {month: amt}).
    GROUPING SETS rounds each grouping's SUM in SQL exactly as the two
    single-grain queries do, so the figures are the same — only the scan is
    shared. For callers that need both (cash flow, the year lens)."""
    years: dict = {}
    months: dict = {}
    excl, ex_p = excluded_accounts_sql(conn)
    for r in conn.execute(
            f"""SELECT to_char(t.date,'YYYY') yr, to_char(t.date,'YYYY-MM') p,
                       {_R2.format("SUM(-t.amount)")} amt
                FROM transactions t
                WHERE {_INCOME_WHERE} {excl}
                GROUP BY GROUPING SETS ((yr), (p))""",
            (_bank_account_ids(conn), *ex_p)):
        if r["p"] is None:
            years[r["yr"]] = r["amt"]
        else:
            months[r["p"]] = r["amt"]
    return years, months


def month_flows(conn, year: int, month: int) -> dict:
    """One month's household money in / out / net, by the SAME definitions
    the Cash Flow page uses (SPEND_WHERE / _INCOME_WHERE), so the ledger's
    headline and Cash Flow cannot disagree. This is what the Transactions
    month header states: a raw sum of the listed rows counts card spend
    twice (once at the swipe, once at the payment) and calls a transfer
    between own accounts money out, which makes the header useless for the
    question it exists to answer — did the month cost more than it paid."""
    start = dt.date(year, month, 1)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    excl, ex_p = excluded_accounts_sql(conn)
    out = conn.execute(
        f"""SELECT {_R2.format(f"COALESCE(SUM({NET_AMOUNT_JOINED}), 0)")} amt
            FROM transactions t {REIMB_PARTIAL_JOIN}
            WHERE {SPEND_WHERE} AND t.date >= %s AND t.date < %s {excl}""",
        (start, end, *ex_p)).fetchone()["amt"]
    inc = conn.execute(
        f"""SELECT {_R2.format("COALESCE(SUM(-t.amount), 0)")} amt
            FROM transactions t
            WHERE {_INCOME_WHERE} AND t.date >= %s AND t.date < %s {excl}""",
        (_bank_account_ids(conn), start, end, *ex_p)).fetchone()["amt"]
    return {"out": out, "in": inc, "net": round(inc - out, 2)}


_MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
        "Oct", "Nov", "Dec"]


# the one timeframe vocabulary every over-time toggle on the site speaks
# (Net Worth set it; Cash Flow and the rest follow): months back, None = all.
# Cash Flow adds two short windows: "cur" is the household's month so far
# (one month, open to today), "1m" is the last COMPLETE month — a window
# that closes before today, which no other key does (see _range_window).
FLOW_RANGES = {"cur": 1, "1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36,
               "5y": 60, "all": None}

# (tenant, inputs fingerprint, month-start iso) -> (fixed actual,
# expires-at). Closed months only — see _fixed_for. Ten minutes bounds a
# cache miss nobody notices; the fingerprint bounds the one they would.
# Keyed on time alone, a bill edit or a combined-entities toggle would
# leave the Cash Flow page showing the pre-edit fixed split for up to ten
# minutes, process-wide — the page disagreeing with the Budget page it
# promises to agree with. The stamp is the bills the match runs against
# plus the config it runs under, so any change to either misses instead.
_FIXED_MONTH_CACHE: dict[tuple[str, str, str], tuple[float, float]] = {}
_FIXED_CACHE_TTL = 600.0


def _fixed_inputs_stamp(conn, cfg: dict) -> str:
    """Fingerprint of everything a closed month's fixed total is computed
    from: the bill rows themselves and the budget config. One small
    aggregate over a table of tens of rows, once per call."""
    row = conn.execute(
        """SELECT COALESCE(md5(string_agg(
                 id || ':' || COALESCE(type, '') || ':'
                    || COALESCE(amount::text, '') || ':'
                    || COALESCE(monthly_amount::text, '') || ':'
                    || COALESCE(payee, '') || ':' || COALESCE(merchant, '')
                    || ':' || COALESCE(frequency, '') || ':'
                    || COALESCE(category, '') || ':'
                    || COALESCE(account_id, '') || ':'
                    || COALESCE(due_on::text, '') || ':'
                    || COALESCE(active::text, ''), '|' ORDER BY id)), '') AS h
             FROM bills""").fetchone()
    try:
        cfg_s = json.dumps(cfg, sort_keys=True, default=str)
    except Exception:                              # noqa: BLE001
        cfg_s = repr(sorted(cfg))
    return hashlib.md5(
        f"{row['h']}|{cfg_s}".encode(), usedforsecurity=False).hexdigest()


def _months_back(month_start: dt.date, n: int) -> dt.date:
    for _ in range(n):
        month_start = (month_start - dt.timedelta(days=1)).replace(day=1)
    return month_start


def _span_months(start: dt.date, last: dt.date) -> int:
    """Calendar months a window covers, first-of-month to first-of-month
    inclusive — the divisor behind every "/mo" pace the windowed tabs
    show. Counted from the CALENDAR, never from which months had a row:
    $300 a quarter over a year is $100/mo, and a divisor that shrinks to
    the four months with a deposit puts $300/mo on the Income headline.
    The live month counts whole, like every month the range label
    promises ("Sep 2025–Aug 2026" is twelve).

    `last` is the window's OWN last day, not the wall clock. "1m" closes
    before today (see _range_window), so measuring it to today added the
    in-progress month nobody asked for and halved the pace: $4,000 spent
    in August read as $2,000/mo all through September."""
    return (last.year - start.year) * 12 + last.month - start.month + 1


def _first_bank_month(conn, bank: list[str]) -> dt.date | None:
    """First of the month the ledger begins in, or None for an empty one.
    The `/mo` divisor is capped at this: uncapped, a household three weeks
    old on the default 1y range divides its spend by twelve — $2,400 reading
    as $200/mo on every windowed tab until the ledger catches up with the
    label."""
    excl, ex_p = excluded_accounts_sql(conn)
    first = conn.execute(
        f"SELECT MIN(date) AS d FROM transactions t WHERE account_id = ANY(%s) {excl}",
        (bank, *ex_p)).fetchone()["d"]
    return as_date(first).replace(day=1) if first else None


def _pace_months(conn, bank: list[str], start: dt.date, last: dt.date) -> int:
    first = _first_bank_month(conn, bank)
    # Never below one: a window that closes before the ledger even opens
    # (a brand-new household reading "1m") spans zero or fewer calendar
    # months, and a zero divisor is a crash or an infinity on the page.
    return max(1, _span_months(max(start, first) if first else start, last))


def _range_window(conn, today: dt.date, range_key: str, bank: list[str],
                  ) -> tuple[dt.date, dt.date, dt.date | None, str]:
    """(start, end, prev_start, label) for a site-wide range key. `end` is
    EXCLUSIVE: the day after today for every window that runs to now, the
    first of this month for "1m", the last complete month. `prev_start`
    opens the previous EQUAL window (ending where this one starts) — the
    thing "vs the year before" compares against; None for 'all', which has
    no previous window."""
    months = FLOW_RANGES[range_key]      # KeyError = caller's bug; route validates
    month_start = today.replace(day=1)
    end = today + dt.timedelta(days=1)
    last = today
    if months is None:
        excl, ex_p = excluded_accounts_sql(conn)
        first = conn.execute(
            f"SELECT MIN(date) AS d FROM transactions t WHERE account_id = ANY(%s) {excl}",
            (bank, *ex_p)).fetchone()["d"]
        start = (as_date(first).replace(day=1) if first else month_start)
        prev = None
    elif range_key == "1m":
        # the last complete month: closes where this month opens
        end = month_start
        last = month_start - dt.timedelta(days=1)
        start = last.replace(day=1)
        prev = _months_back(start, 1)
    else:
        start = _months_back(month_start, months - 1)
        prev = _months_back(start, months)
    if start.year == last.year:
        label = (f"{_MON[start.month]}–{_MON[last.month]} {last.year}"
                 if start.month != last.month
                 else f"{_MON[last.month]} {last.year}")
    else:
        label = (f"{_MON[start.month]} {start.year}–"
                 f"{_MON[last.month]} {last.year}")
    return start, end, prev, label


def _income_kind_fn(conn):
    """The income classifier the flow picture and the Income tab share.
    A paycheck is what the product already calls one: a deposit that
    matches an INCOME bill's merchant key (the same series the forecast
    projects and the Bills page tracks), or one the aggregator tagged as
    wages. Interest and dividends by their tag or their words."""
    pay_keys = {k for k in (budget._key_token(r["payee"] or "") for r in conn.execute(
        "SELECT payee FROM bills WHERE amount > 0 AND active = 1")) if k}
    interest_tags = {"INCOME_INTEREST_EARNED", "INCOME_DIVIDENDS"}

    def _kind(name: str, detailed: str) -> str:
        if detailed == "INCOME_WAGES":
            return "paychecks"
        if detailed in interest_tags:
            return "interest"
        toks = budget._tokens(name or "")
        if pay_keys & toks:
            return "paychecks"
        if toks & {"interest", "dividend", "dividends"}:
            return "interest"
        return "other"
    return _kind


def _mkey(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower()) or (name or "")


def _fold_merchants(rows, limit: int | None) -> list[tuple[str, dict]]:
    """Display names that differ only in punctuation/case are one merchant
    the canonical map hasn't caught up with — over a wide window "H-E-B"
    and "H E B" both survive their own eras and rank as two rows. Fold by
    the letters alone; the bigger variant keeps the name and logo."""
    folded: dict[str, dict] = {}
    for r in rows:
        k = _mkey(r["payee"])
        f = folded.get(k)
        if f is None:
            folded[k] = {"payee": r["payee"], "amt": float(r["amt"] or 0),
                         "n": r["n"], "logo": r["logo"]}
        else:
            if float(r["amt"] or 0) > f["amt"] and r["payee"]:
                f["payee"], f["logo"] = r["payee"], r["logo"] or f["logo"]
            f["amt"] += float(r["amt"] or 0)
            f["n"] += r["n"]
            f["logo"] = f["logo"] or r["logo"]
    out = sorted(folded.items(), key=lambda x: -x[1]["amt"])
    return out[:limit] if limit else out


def spending_window(conn, today: dt.date, range_key: str) -> dict:
    """The Spending tab's windowed view: total, monthly pace, categories and
    merchants ranked over one site-wide timeframe, each with its total over
    the PREVIOUS equal window so the page can lead with what changed. Pure
    SQL aggregation — no month walk — so every range is cheap."""
    bank = _bank_account_ids(conn)
    start, end, prev, label = _range_window(conn, today, range_key, bank)
    # the last day the window covers — today for every range open to now,
    # the end of last month for "1m", which closes before today. Both the
    # reported end and the "/mo" divisor are the WINDOW's, never the wall
    # clock's (flow_breakdown reads the same day off `end`).
    last_day = end - dt.timedelta(days=1)
    amt2 = _R2.format(f"SUM({NET_AMOUNT_JOINED})")
    excl, ex_p = excluded_accounts_sql(conn)

    def _cats(a: dt.date, b: dt.date) -> dict[str, float]:
        return {r["cat"]: float(r["amt"] or 0) for r in conn.execute(
            f"SELECT {EFF_CAT} cat, {amt2} amt FROM transactions t "
            f"{REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} "
            f"AND t.date >= %s AND t.date < %s {excl} GROUP BY cat",
            (a, b, *ex_p))}

    cats = _cats(start, end)
    prev_cats = _cats(prev, start) if prev else {}
    by_month = [[r["ym"], float(r["amt"] or 0)] for r in conn.execute(
        f"SELECT to_char(t.date,'YYYY-MM') ym, {amt2} amt FROM transactions t "
        f"{REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} "
        f"AND t.date >= %s AND t.date < %s {excl} GROUP BY ym ORDER BY ym",
        (start, end, *ex_p))]
    def _merchants(a: dt.date, b: dt.date, limit: int | None):
        rows = conn.execute(
            f"SELECT {DISPLAY_MERCHANT} payee, {amt2} amt, COUNT(*) n, "
            f"       max({MERCHANT_LOGO}) logo "
            f"FROM transactions t {MC_JOIN} {REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} "
            f"AND t.date >= %s AND t.date < %s {excl} "
            f"GROUP BY payee ORDER BY amt DESC", (a, b, *ex_p)).fetchall()
        return _fold_merchants(rows, limit)

    merch = _merchants(start, end, 25)
    prev_merch = ({k: round(v["amt"], 2) for k, v in _merchants(prev, start, None)}
                  if prev else {})
    total = round(sum(cats.values()), 2)
    prev_total = round(sum(prev_cats.values()), 2) if prev else None
    return {
        "from": start.isoformat(), "to": last_day.isoformat(), "label": label,
        "months": _pace_months(conn, bank, start, last_day),
        "total": total, "prev_total": prev_total,
        "by_month": by_month,
        # [category, amount, previous-window amount|null], ranked
        "categories": [[c, round(v, 2),
                        round(prev_cats[c], 2) if prev and c in prev_cats
                        else (0.0 if prev else None)]
                       for c, v in sorted(cats.items(), key=lambda x: -x[1])],
        # [payee, amount, visits, previous-window amount|null, logo] —
        # prev looked up by the same fold key, so a renamed variant still
        # compares against its own history
        "merchants": [[f["payee"], round(f["amt"], 2), f["n"],
                       (prev_merch.get(k, 0.0) if prev else None),
                       f["logo"]] for k, f in merch],
    }


def income_window(conn, today: dt.date, range_key: str) -> dict:
    """The Income tab's windowed view: total in, the kind split (paychecks /
    interest & dividends / other — the flow picture's classifier), the
    monthly series, and the previous equal window's figures for the delta.
    Pure SQL plus a per-row classify pass — cheap at any range."""
    bank = _bank_account_ids(conn)
    start, end, prev, label = _range_window(conn, today, range_key, bank)
    # the window's own last day, as on the Spending tab — "1m" ends where
    # last month did, so its pace divides by that one month
    last_day = end - dt.timedelta(days=1)
    _kind = _income_kind_fn(conn)
    excl, ex_p = excluded_accounts_sql(conn)

    def _kinds(a: dt.date, b: dt.date) -> dict[str, float]:
        out = {"paychecks": 0.0, "interest": 0.0, "other": 0.0}
        for r in conn.execute(
                f"""SELECT t.name, COALESCE(t.category_detailed,'') AS d,
                          -t.amount AS amt
                     FROM transactions t
                    WHERE {_INCOME_WHERE} AND t.date >= %s AND t.date < %s
                    {excl}""",
                (bank, a, b, *ex_p)):
            out[_kind(r["name"], r["d"])] += float(r["amt"] or 0)
        return {k: round(v, 2) for k, v in out.items()}

    kinds = _kinds(start, end)
    prev_kinds = _kinds(prev, start) if prev else None
    by_month = [[r["ym"], round(float(r["amt"] or 0), 2)] for r in conn.execute(
        f"""SELECT to_char(t.date,'YYYY-MM') ym,
                   SUM(-t.amount) amt FROM transactions t
            WHERE {_INCOME_WHERE} AND t.date >= %s AND t.date < %s {excl}
            GROUP BY ym ORDER BY ym""", (bank, start, end, *ex_p))]

    # WHO paid — the window's income grouped by payer, punctuation-folded
    # like the Spending tab's merchants, each with its previous-window
    # figure. "By source" names the employer, the bank paying interest,
    # the person Venmo-ing you back — not just the three kinds.
    def _sources(a: dt.date, b: dt.date, limit: int | None):
        rows = conn.execute(
            f"""SELECT {DISPLAY_MERCHANT} payee,
                       {_R2.format('SUM(-t.amount)')} amt, COUNT(*) n,
                       max({MERCHANT_LOGO}) logo
                 FROM transactions t {MC_JOIN}
                WHERE {_INCOME_WHERE} AND t.date >= %s AND t.date < %s {excl}
                GROUP BY payee ORDER BY amt DESC""",
            (bank, a, b, *ex_p)).fetchall()
        return _fold_merchants(rows, limit)

    src = _sources(start, end, 15)
    prev_src = ({k: round(v["amt"], 2) for k, v in _sources(prev, start, None)}
                if prev else {})
    return {
        "from": start.isoformat(), "to": last_day.isoformat(), "label": label,
        "months": _pace_months(conn, bank, start, last_day),
        "total": round(sum(kinds.values()), 2),
        "prev_total": (round(sum(prev_kinds.values()), 2)
                       if prev_kinds is not None else None),
        "kinds": kinds, "prev_kinds": prev_kinds,
        "by_month": by_month,
        # [payer, amount, deposits, previous-window amount|null, logo]
        "sources": [[f["payee"], round(f["amt"], 2), f["n"],
                     (prev_src.get(k, 0.0) if prev else None),
                     f["logo"]] for k, f in src],
    }


def flow_breakdown(conn, today: dt.date, range_key: str) -> dict:
    """Where the money came from and where it went, for one timeframe from
    FLOW_RANGES (the site-wide 3m/6m/1y/3y/5y/all vocabulary). Computed per
    range on demand — not bundled into the report — because the fixed split
    walks month_status month by month, and 'all' over a decade of history
    is a couple of seconds nobody should pay just to see the default view.
    Sources are income split by kind (paychecks, interest and dividends,
    everything else); sinks are spend split into FIXED (rows a recurring
    bill matched) and VARIABLE (the rest), with what is left as saved.
    Same predicates as every other figure on the page (_INCOME_WHERE /
    SPEND_WHERE), so the picture and the table agree."""
    bank = _bank_account_ids(conn)
    start, end, _prev, label = _range_window(conn, today, range_key, bank)
    # the last day the window covers — today, or the end of the last
    # complete month for "1m"
    last_day = end - dt.timedelta(days=1)
    periods = {range_key: (start, label)}
    _kind = _income_kind_fn(conn)

    # Fixed is what the budget engine counts as fixed for each month —
    # the charges its recurring bills matched (envelope use included) —
    # so the picture agrees with the Budget page and the daily verdict.
    # Matched against TODAY's live bills for past months too (the
    # `historical` flag drops the bills altogether, which is right for a
    # closed month's verdict and wrong for this picture: a charge is
    # "fixed" because a bill claims it, whenever it landed).
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    # One memo for the whole call: bills, config and envelope priors load
    # once, so a 5y window costs seconds, not tens of seconds — and the
    # config read here is the one month_status reads below, so the spend
    # total and the fixed split cannot be judged under two configs.
    memo: dict = {}
    # The budget's excluded_accounts (the Accounts page 'excl' toggle)
    # apply to the spend TOTAL exactly as month_status applies them to the
    # fixed side. Left off, a bill paid from an excluded card was invisible
    # to the fixed match yet still counted in total_out, so its dollars
    # surfaced as "variable" on a picture that promises to agree with the
    # Budget page. Same helper as every other report in this module — the
    # picture and the Spending tab must not exclude different ledgers.
    cfg = budget.load_config(conn, memo=memo)
    excl_sql, ex_p = excluded_accounts_sql(conn, memo=memo)

    stamp = _fixed_inputs_stamp(conn, cfg)

    def _fixed_for(start: dt.date) -> float:
        # CLOSED months land in a short-lived process cache — an
        # all-history window on a decades-long ledger is hundreds of
        # month_status calls (seconds), and a closed month's fixed
        # total only moves when bills change, which the TTL bounds.
        total, cur = 0.0, start
        month_start = today.replace(day=1)
        now = dt.datetime.now().timestamp()
        while cur <= last_day:
            key = (tid, stamp, cur.isoformat())
            hit = _FIXED_MONTH_CACHE.get(key)
            if hit is not None and cur < month_start and hit[1] > now:
                total += hit[0]
                cur = (cur + dt.timedelta(days=32)).replace(day=1)
                continue
            last = min(last_day, (cur + dt.timedelta(days=32)).replace(day=1)
                       - dt.timedelta(days=1))
            try:
                st = budget.month_status(conn, last, historical=False,
                                         memo=memo)
                v = float(st["buckets"]["fixed"]["actual"] or 0)
                total += v
                if cur < month_start:              # the live month stays live
                    if len(_FIXED_MONTH_CACHE) > 100_000:
                        _FIXED_MONTH_CACHE.clear()
                    _FIXED_MONTH_CACHE[key] = (v, now + _FIXED_CACHE_TTL)
            except Exception:                      # noqa: BLE001
                log.exception("fixed-spend month %s failed", cur)
            cur = (cur + dt.timedelta(days=32)).replace(day=1)
        return total

    out: dict = {}
    for key, (start, label) in periods.items():
        inc = {"paychecks": 0.0, "interest": 0.0, "other": 0.0}
        for r in conn.execute(
                f"""SELECT t.name, COALESCE(t.category_detailed,'') AS d,
                          -t.amount AS amt
                     FROM transactions t
                    WHERE {_INCOME_WHERE} AND t.date >= %s AND t.date < %s
                    {excl_sql}""",
                (bank, start, end, *ex_p)):
            # the same exclusion on the IN side as on the OUT side below —
            # judged under different rules, an excluded account that takes
            # $6k in and pays $5.5k out reads as "+$5,500 stayed"
            inc[_kind(r["name"], r["d"])] += float(r["amt"] or 0)
        total_out = float(conn.execute(
            f"""SELECT {_R2.format(f"COALESCE(SUM({NET_AMOUNT_JOINED}), 0)")} amt
                 FROM transactions t {REIMB_PARTIAL_JOIN}
                WHERE {SPEND_WHERE} AND t.date >= %s AND t.date < %s
                {excl_sql}""",
            (start, end, *ex_p)).fetchone()["amt"] or 0)
        fixed = min(_fixed_for(start), total_out)
        sp = {"fixed": round(fixed, 2), "variable": round(total_out - fixed, 2)}
        total_in = round(sum(inc.values()), 2)
        total_out = round(total_out, 2)
        saved = round(total_in - total_out, 2)
        out[key] = {
            "from": start.isoformat(), "to": last_day.isoformat(),
            "label": label,
            "in": {"total": total_in, **{k: round(v, 2) for k, v in inc.items()}},
            "out": {"total": total_out, **{k: round(v, 2) for k, v in sp.items()}},
            "saved": saved,
            "rate": round(100 * saved / total_in, 1) if total_in else None,
        }
    return out[range_key]


def _bank_coverage_years(conn) -> set[str]:
    """Years with any bank-account (real-bank depository) rows at all.
    Years with card spend but no checking data have unknown income, not $0.
    Excluded accounts don't count as coverage: their rows are invisible to
    the income figure, so a year covered only by an excluded checking
    account would report $0 income as if it were measured."""
    excl, ex_p = excluded_accounts_sql(conn)
    return {r["yr"] for r in conn.execute(
        f"""SELECT DISTINCT to_char(t.date,'YYYY') yr FROM transactions t
           WHERE t.removed=0 AND t.account_id = ANY(%s) {excl}
             {budget.PERSONAL_ONLY_SQL}""",
        (_bank_account_ids(conn), *ex_p))}




def _investment_funding(conn) -> list[list]:
    """Net external cash into investments per year (+ in, − out).

    Measured from the BANK side — money that actually left the user's real
    depository accounts for an investment provider, netted against money that
    came back — PLUS workplace-plan payroll contributions (which bypass
    the bank entirely). Two reasons this is the right lens:
 • Provider-to-provider rollovers net to ~0: the checking→provider leg
    (+) is offset by the provider→checking leg (−), leaving only the
    genuine new money.
 • It is immune to a provider's internal INVBANKTRAN/BUYMF churn, which
    never touches the bank.
    Investment-provider depository accounts (a robo-advisor's cash sweep) are
    excluded so only the real-bank side of each transfer is counted once.
    Negative years are genuine net withdrawals.

    Shadow-excluded like every other money aggregate — a checking account
    fed by two linked sources would otherwise report each transfer
    to the investment provider twice."""
    by_year: dict[str, float] = {}
    # Real-bank depository accounts, resolved first so the ledger is read by
    # account id (an index probe) rather than joined inside a full scan; the
    # provider-name test runs in SQL as the same substring match. NULL
    # institution names fail the NOT IN test here as they did in the join.
    bank_ids = [r["id"] for r in conn.execute(
        """SELECT a.id FROM accounts a JOIN items i ON i.id = a.item_id
           WHERE a.type = 'depository'
             AND LOWER(i.institution_name) NOT LIKE ALL(%s)""", (_INV_PATTERNS,))]
    for r in conn.execute(
            f"""SELECT to_char(t.date,'YYYY') yr, SUM(t.amount) amt
               FROM transactions t
               WHERE t.removed = 0 AND t.account_id = ANY(%s)
                 AND LOWER(t.name) LIKE ANY(%s)
                 {_NOT_SHADOW.format(col="t.account_id")}
                 {budget.PERSONAL_ONLY_SQL}
               GROUP BY yr""",
            (bank_ids, _INV_PATTERNS)):
        # Plaid sign: +amount = money OUT of the bank = INTO investments.
        by_year[r["yr"]] = by_year.get(r["yr"], 0.0) + r["amt"]
    # workplace-plan payroll contributions: the plan-CSV door writes its
    # rows under its items' accounts, so the account ids make this an index
    # probe; the row's own type says it was a contribution
    plan_ids = [r["id"] for r in conn.execute(
        """SELECT a.id FROM accounts a JOIN items i ON i.id = a.item_id
           WHERE i.aggregator = 'plan_csv'""")]
    for r in conn.execute(
            """SELECT to_char(t.date,'YYYY') yr, SUM(ABS(t.amount)) amt
               FROM transactions t
               WHERE t.account_id = ANY(%s) AND t.removed = 0
                 AND LOWER(t.raw ->> 'type') LIKE '%%contribution%%'
               GROUP BY yr""", (plan_ids,)):
        by_year[r["yr"]] = by_year.get(r["yr"], 0.0) + r["amt"]
    return [[y, round(by_year[y], 2)] for y in sorted(by_year)]


def _cash_graph(conn, income_m: dict, spend_m: dict,
                today: dt.date | None = None) -> dict:
    """One continuous monthly net-cash-flow series — every
    historical month with bank income data (the same income − spend the
    per-year charts render), then the forward forecast (forecast.HORIZON_DAYS)
    folded into calendar months. The forward part is DERIVED from engine/forecast's
    autopay-statement balance series (balance delta over each month =
    net flow), so it reconciles with the Today page by construction —
    no second money model. The current month's actual-so-far is measured
    CASH-basis to match (see the cash-basis comment below). Returns
 {points: [[YYYY-MM, net]…],
    forecast_from: index of the first month carrying forecast}."""
    from . import forecast
    # the household's day when the caller has it (the API resolves it per
    # request); the process clock is the fallback for legacy callers
    today = today or dt.date.today()
    cur_key = today.strftime("%Y-%m")
    points = [[k, round(income_m[k] - spend_m.get(k, 0.0), 2)]
              for k in sorted(income_m) if k < cur_key]
    forecast_from = len(points)
    try:
        # forecast.HORIZON_DAYS, not a second hard-coded horizon. A longer
        # horizon here than the rest of the product measures would render
        # days no backtest covers — the walk's optimism grows with the
        # horizon, so the furthest days are the least trustworthy part of
        # the picture while looking exactly as confident as day 1.
        fc = forecast.build(conn, today=today, days=forecast.HORIZON_DAYS)
    except Exception:
        fc = None
    if fc is None:
        return {"points": points, "forecast_from": forecast_from}
    # month-by-month balance deltas along the forecast walk; the current
    # month combines actual-so-far with its forecast remainder
    start_bal = {cur_key: fc["checking"]}
    end_bal: dict[str, float] = {}
    prev = fc["checking"]
    for d_iso, bal in fc["pace_stmt"]["series"]:
        mk = d_iso[:7]
        start_bal.setdefault(mk, prev)
        end_bal[mk] = bal
        prev = bal
    # The current month's actual-so-far, CASH basis. The forward deltas
    # come from a CASH walk of checking (pace_stmt pays the card balance at
    # its autopay due date), so the actual added to the SAME month must be
    # cash too. An economic MTD (income − spend, where spend counts card
    # purchases at CHARGE time) double-counts card spend:
    #   $300 charged this month, still on the card
    #     economic MTD:      −300   (the charge, in spend_m)
    #     forecast autopay:  −300   (the walk pays the same balance off)
    #     graph:             −600   for $300 of economic outflow.
    # Cash basis on the checking side instead:
    #   + income deposits (income_m — depository-only, shadow-excluded)
    #   − spend on NON-credit accounts (a debit purchase left cash the
    #     moment it posted; a card purchase did not)
    #   − card payments that POSTED this month (that is when card spend
    #     becomes cash out; the walk only schedules what is STILL owed, so
    #     a payment already made is never re-scheduled)
    # Invariant: each card dollar reaches the current month exactly once —
    # via the posted payment if the card was paid this month, via the
    # walk's autopay if not. Dropping credit spend WITHOUT adding posted
    # payments back would count a charge-paid-this-month zero times.
    # (A charge from a PRIOR month settled this month appears in that
    # month's economic point and again here as the cash leaves — that is
    # the fixed seam where the economic history meets the cash forecast,
    # not an intra-model double count.)
    # Excluded accounts are invisible here too, on BOTH legs — the income
    # and spend series this point sits in already drop them, and dropping
    # one leg only would charge the month a settlement whose purchases it
    # never counted.
    excl, ex_p = excluded_accounts_sql(conn)
    mtd_spend_cash = conn.execute(
        f"""SELECT COALESCE(SUM({NET_AMOUNT_JOINED}), 0) AS amt
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            {REIMB_PARTIAL_JOIN}
            WHERE {SPEND_WHERE} AND a.type != 'credit'
              AND to_char(t.date,'YYYY-MM') = %s {excl}""",
        (cur_key, *ex_p)).fetchone()["amt"]
    pay_rows = conn.execute(
        f"""SELECT t.date, t.amount
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE t.removed = 0 AND t.amount > 0
              AND a.type NOT IN ('credit', 'loan')
              AND COALESCE(t.category_detailed,'')
                  = 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
              AND to_char(t.date,'YYYY-MM') = %s {excl}
              {_NOT_SHADOW.format(col="t.account_id")}
              {budget.PERSONAL_ONLY_SQL}""",
        (cur_key, *ex_p)).fetchall()
    mtd_card_pay = sum(float(r["amount"]) for r in pay_rows)
    # The category is not the only way a settlement shows up. A
    # sparse source (SimpleFIN, bare CSV) files the checking-side autopay
    # ("CHASE AUTOPAY") as TRANSFER_OUT — the cash still left checking, and
    # the walk won't re-schedule it (the card balance already dropped), so
    # missing it under-counts the month by the whole payment. Doctrine:
    # a checking-side TRANSFER_OUT IS a card settlement when a credit-
    # account inflow pairs with it — same amount to the cent, a few days
    # apart, each credit inflow backing at most ONE checking leg (the
    # category-marked payments claim theirs first, so a coincidental
    # same-amount transfer can't inherit an already-counted settlement).
    # The pairing is the evidence; an unpaired TRANSFER_OUT stays a plain
    # own-money move. Credit inflows are flow-shaped rows only (transfer/
    # payment categories or uncategorized) — a merchant refund must not
    # back a "payment".
    xfer_rows = conn.execute(
        f"""SELECT t.date, t.amount
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE t.removed = 0 AND t.amount > 0
              AND a.type NOT IN ('credit', 'loan')
              AND COALESCE(t.category_override, t.category_primary, '')
                  = 'TRANSFER_OUT'
              AND COALESCE(t.category_detailed,'')
                  != 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
              AND to_char(t.date,'YYYY-MM') = %s {excl}
              {_NOT_SHADOW.format(col="t.account_id")}
              {budget.PERSONAL_ONLY_SQL}""",
        (cur_key, *ex_p)).fetchall()
    if xfer_rows:
        month_start = today.replace(day=1)
        credit_in = [(r["date"], float(-r["amount"])) for r in conn.execute(
            f"""SELECT t.date, t.amount
                FROM transactions t JOIN accounts a ON a.id = t.account_id
                WHERE t.removed = 0 AND t.amount < 0 AND a.type = 'credit'
                  AND (COALESCE(t.category_override,
                                t.category_primary, '') = ''
                       OR COALESCE(t.category_override, t.category_primary)
                          IN ('TRANSFER_IN', 'TRANSFER_OUT',
                              'LOAN_PAYMENTS'))
                  AND t.date >= %s AND t.date <= %s {excl}
                  {_NOT_SHADOW.format(col="t.account_id")}
                  {budget.PERSONAL_ONLY_SQL}""",
            (month_start - dt.timedelta(days=5),
             today + dt.timedelta(days=5), *ex_p)).fetchall()]

        def _claim(d: dt.date, amt: float) -> bool:
            for i, (cd, ca) in enumerate(credit_in):
                if abs(ca - amt) <= 0.01 and abs((cd - d).days) <= 5:
                    del credit_in[i]
                    return True
            return False

        for r in pay_rows:
            _claim(r["date"], float(r["amount"]))
        for r in xfer_rows:
            if _claim(r["date"], float(r["amount"])):
                mtd_card_pay += float(r["amount"])
    # Posted savings-goal transfers, symmetric with the walk.
    # forecast.build schedules each goal's plan NET of what already
    # posted this month (posted = max(0, _matched_net since the 1st)), and
    # the posted money is already out of live checking — the walk's start —
    # so without this term it appears in neither the walk's deltas nor the
    # MTD figure: counted ZERO times. Subtracting the SAME per-goal posted amount
    # here makes the month's savings-out exactly
    #   posted (MTD) + max(0, plan − posted) (walk) = max(plan, posted)
    # — once, never zero, never twice. Scope mirrors the walk precisely:
    # goals with a plan the walk schedules, skipping plan-only goals (no
    # ledger to observe; the walk carries their full plan instead).
    # Both sides read posted_for_plan — the same helper the walk nets against —
    # so checking-leg-only transfers count here exactly once too, and
    # token-less goals sharing an account partition instead of each
    # claiming the full net.
    from . import savings as _savings
    cfg = budget.load_config(conn)
    first = today.replace(day=1)
    mtd_savings = sum(
        max(0.0, _savings.posted_for_plan(conn, cfg, g, since=first))
        for g in _savings.goals(cfg)
        if float(g.get("monthly_plan") or 0) > 0
        and not _savings.is_plan_only(g))
    mtd_actual = (income_m.get(cur_key, 0.0) - mtd_spend_cash
                  - mtd_card_pay - mtd_savings)
    for mk in sorted(end_bal):
        net = end_bal[mk] - start_bal[mk]
        if mk == cur_key:
            net += mtd_actual
        points.append([mk, round(net, 2)])
    return {"points": points, "forecast_from": forecast_from}


def compute_cashflow(conn, today: dt.date | None = None) -> dict:
    # Year and month spend from one scan: GROUPING SETS rounds each grouping's
    # SUM in SQL as the two separate queries did (a year is NOT the sum of its
    # rounded months), so the figures are unchanged and the scan is shared.
    spend_y: dict = {}
    spend_m: dict = {}
    excl, ex_p = excluded_accounts_sql(conn)
    for r in conn.execute(
            f"SELECT to_char(t.date,'YYYY') yr, to_char(t.date,'YYYY-MM') p, "
            f"{_R2.format(f'SUM({NET_AMOUNT_JOINED})')} amt "
            f"FROM transactions t {REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} {excl} "
            f"GROUP BY GROUPING SETS ((yr), (p))", ex_p or None):
        if r["p"] is None:
            spend_y[r["yr"]] = r["amt"]
        else:
            spend_m[r["p"]] = r["amt"]
    income_y, income_m = _income_year_month(conn)
    covered = _bank_coverage_years(conn)
    years = sorted(set(spend_y) | set(income_y))
    savings = []
    for y in years:
        sp = spend_y.get(y, 0)
        if y not in covered:  # card spend exists but no bank data → income unknown
            savings.append([y, None, round(sp, 2), None, None])
            continue
        inc = income_y.get(y, 0)
        rate = round(100 * (inc - sp) / inc, 1) if inc else None
        savings.append([y, round(inc, 2), round(sp, 2), round(inc - sp, 2), rate])
    # per-year monthly net cash flow (income − spend), for the expandable
    # per-year line chart. Keyed only on months that HAVE bank income data
    # (avoids a phantom -spend dip in a covered year's pre-coverage months).
    # Month labels are abbreviations so the chart's x-axis ticks read by month.
    _MO = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    monthly_by_year: dict = {}
    for y in years:
        if y not in covered:
            continue
        pts = []
        for mo in range(1, 13):
            key = f"{y}-{mo:02d}"
            if key not in income_m:
                continue
            pts.append([_MO[mo - 1],
                        round(income_m[key] - spend_m.get(key, 0.0), 2)])
        if pts:
            monthly_by_year[y] = pts
    # investment funding: net external cash into investments per year
    funding = _investment_funding(conn)
    # investment-funding shortfall signal (budget.cash_events, current
    # month; guarded — a cash_events failure must not sink the page)
    today = today or dt.date.today()
    try:
        # `until` is exclusive: month_status passes tomorrow so a pull that
        # landed TODAY — the one the Today page is flagging this morning —
        # counts month-to-date here too, not from tomorrow
        ce = budget.cash_events(conn, today.replace(day=1),
                                today + dt.timedelta(days=1))
    except Exception:
        ce = {}
    # career income reference: reported 1040 total (joint if MFJ) plus wage /
    # investment / tax detail. Include wage-only years (W-2 on record, no filed
    # 1040 in our set) so the reference is complete; those show wages with a
    # blank Total income.
    reported = [
        [r["year"], r["total_income"], r["wages"], r["investment_income"],
         r["tax_paid"], r["joint"]]
        for r in conn.execute(
            "SELECT year, total_income, wages, investment_income, tax_paid, joint "
            "FROM income_annual "
            "WHERE (total_income IS NOT NULL AND total_income > 0) "
            "   OR (wages IS NOT NULL AND wages > 0) ORDER BY year DESC")]
    # full career-earnings spine: uncapped Medicare wages where SSA has them
    # (best true-wage figure), else SS earnings / reported wages. One point per
    # year, so the chart reaches back to the first-ever paycheck.
    career = [
        [r["year"], r["inc"]] for r in conn.execute(
            "SELECT year, COALESCE(medicare_earnings, ss_earnings, primary_wages, wages) inc "
            "FROM income_annual "
            "WHERE COALESCE(medicare_earnings, ss_earnings, primary_wages, wages) IS NOT NULL "
            "ORDER BY year")]
    return {
        "savings_by_year": savings,
        "monthly_by_year": monthly_by_year,
        "cash_graph": _cash_graph(conn, income_m, spend_m, today=today),
        "reported_income": reported,
        "career_income": career,
        "income_by_month": [[p, income_m[p]] for p in sorted(income_m)],
        "investment_funding": funding,
        "wf_funding_mtd": ce.get("funding_mtd_total", 0),
        "wf_funding_ytd": ce.get("funding_ytd_total", 0),
    }


# ---- investment fees ----------------------------------------------------
# investment accounts ONLY — advisory / admin / participant /
# account fees on brokerage, retirement and crypto accounts. Bank, card
# and ATM fees are ordinary spending and belong on Spending; folding them
# in makes the total meaningless next to the balances they erode (this
# report renders as a section of Net Worth).
# word-boundary fee match: '%fee%' also matches coffee/toffee in names AND
# FOOD_AND_DRINK_COFFEE in category_detailed. Pad names with spaces and
# categories with underscores so only the standalone tokens FEE/FEES qualify.
_FEE_WHERE = r"""t.removed=0 AND a.type='investment' AND (
      ('_' || UPPER(COALESCE(t.category_detailed,'')) || '_') LIKE '%\_FEE\_%' ESCAPE '\'
      OR ('_' || UPPER(COALESCE(t.category_detailed,'')) || '_') LIKE '%\_FEES\_%' ESCAPE '\'
      OR (' ' || LOWER(t.name) || ' ') LIKE '% fee %'
      OR (' ' || LOWER(t.name) || ' ') LIKE '% fees %')"""


def _advisory_fee_estimate(conn, today: dt.date | None = None) -> dict[str, dict[str, float]]:
    """Estimated advisory fees by platform and year, for the robo-advised
    accounts the household names in config: `advisory_fees` maps a lowercase
    fragment of the institution's name to the yearly rate as a fraction
    (0.0025 for 0.25%), and `advisory_fee_exempt_masks` lists the account
    masks at those institutions that are self-directed and pay no fee. A
    robo-advisor's exports carry NO fee rows — the cut comes out of the
    balance — so an estimate is the only honest way to show it. Returns
    {platform label: {year: fee}}; nothing when no fee is configured."""
    from . import budget, links
    cfg = budget.load_config(conn)
    fees = cfg.get("advisory_fees") or {}
    if not isinstance(fees, dict) or not fees:
        return {}
    exempt = [str(m) for m in (cfg.get("advisory_fee_exempt_masks") or [])]
    # the shadow exclusion compute_fees applies to the itemized rows: a
    # dual-linked account would otherwise be walked twice and estimate twice
    # the advisory cut on one real balance — and the same entity separation,
    # for the same reason compute_fees applies it
    shadows = links.shadow_ids(conn)
    out: dict[str, dict[str, float]] = {}
    for token, rate in fees.items():
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            continue
        if not str(token).strip() or rate <= 0:
            continue
        rows = conn.execute(
            f"""SELECT a.id, i.institution_name inst
               FROM accounts a JOIN items i ON i.id=a.item_id
               WHERE lower(i.institution_name) LIKE %s
                 AND a.type='investment'
                 AND NOT (COALESCE(a.mask, '') = ANY(%s))
                 AND NOT (a.id = ANY(%s))
                 AND ({budget.COMBINE_OR} a.entity_id IS NULL)""",
            ("%" + str(token).lower() + "%", exempt, shadows)).fetchall()
        if not rows:
            continue
        est = _managed_fee_by_year(conn, [r["id"] for r in rows], rate, today)
        if est:
            out[f"{rows[0]['inst']} advisory (estimated {rate * 100:g}%)"] = est
    return out


def _managed_fee_by_year(conn, managed: list[str], rate: float,
                         today: dt.date | None = None) -> dict[str, float]:
    """`rate` × the average managed value at the start and end of each year,
    reconstructed from holdings and transactions the way the net-worth
    trend is; the current year prorated, years under $1 dropped."""
    # per-account share walk-back (same machinery as the net-worth trend)
    hold_now: dict = {}
    for h in conn.execute("SELECT account_id, symbol, quantity, price FROM holdings"):
        hold_now.setdefault(h["account_id"], {})[h["symbol"]] = [h["quantity"] or 0.0, h["price"] or 0.0]
    obs: dict = {}
    deltas: dict = {}
    first = None              # earliest managed-account ledger date, from the same rows
    for r in conn.execute(
            "SELECT account_id, date, raw FROM transactions "
            "WHERE removed=0 AND account_id = ANY(%s)", (managed,)):
        d_iso = _iso(r["date"])
        if first is None or d_iso < first:
            first = d_iso
        s = _security(r["raw"])
        if not s or not s[0]:
            continue
        deltas.setdefault((r["account_id"], s[0]), []).append((d_iso, s[1]))
        if s[2] and s[2] > 0:
            obs.setdefault(s[0], []).append((d_iso, s[2]))
    today_d = today or dt.date.today()
    today = today_d.isoformat()
    for posn in hold_now.values():
        for tk, (sh, pr) in posn.items():
            if pr and pr > 0:
                obs.setdefault(tk, []).append((today, pr))
    curve = {tk: sorted(set(o)) for tk, o in obs.items()}
    curve_dates = {tk: [d for d, _ in c] for tk, c in curve.items()}
    # sorted deltas + suffix sums: shares after a date in one bisect (the
    # same device _reconstruct_trend uses) instead of re-summing per year
    delta_dates: dict = {}
    delta_after: dict = {}
    acct_tickers: dict = {}
    for key, dl in deltas.items():
        acct_tickers.setdefault(key[0], set()).add(key[1])
        dl.sort(key=lambda x: x[0])
        delta_dates[key] = [d for d, _ in dl]
        acc = [0.0] * (len(dl) + 1)
        for i in range(len(dl) - 1, -1, -1):
            acc[i] = acc[i + 1] + dl[i][1]
        delta_after[key] = acc

    def price(tk, date):
        c = curve.get(tk)
        if not c:
            return None
        i = bisect.bisect_right(curve_dates[tk], date)
        if i == 0:
            return c[0][1]
        if i >= len(c):
            return c[-1][1]
        (d0, p0), (d1, p1) = c[i - 1], c[i]
        span = (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days or 1
        return p0 + (p1 - p0) * ((dt.date.fromisoformat(date) - dt.date.fromisoformat(d0)).days / span)

    if not first:
        return {}

    def managed_value(date):
        tot = 0.0
        for aid in managed:
            posn = hold_now.get(aid, {})
            tks = acct_tickers.get(aid, set()) | set(posn)
            for tk in tks:
                dd = delta_dates.get((aid, tk))
                after = (delta_after[(aid, tk)][bisect.bisect_right(dd, date)]
                         if dd else 0.0)
                sh = posn.get(tk, [0.0, 0.0])[0] - after
                if abs(sh) < 1e-6:
                    continue
                p = price(tk, date)
                if p is not None:
                    tot += sh * p
        return tot

    est = {}
    y0, y1 = int(first[:4]), today_d.year
    prev = managed_value(f"{y0 - 1}-12-31")
    for y in range(y0, y1 + 1):
        end = min(f"{y}-12-31", today)
        cur = managed_value(end)
        # elapsed fraction against the year's real length — a fixed 365
        # reads 366/365 on a leap year's Dec 31 and overstates the fee
        _t = today_d
        _ylen = 366 if calendar.isleap(_t.year) else 365
        frac = 1.0 if y < y1 else (_t.timetuple().tm_yday / _ylen)
        fee = rate * ((prev + cur) / 2) * frac
        if fee >= 1:
            est[str(y)] = round(fee, 2)
        prev = cur
    return est


def compute_fees(conn, today: dt.date | None = None) -> dict:
    """Investment fee drag by platform and by year (see `_FEE_WHERE`).

    Summed SIGNED, not ABS: positive is money out (the house convention), so
    a refunded or rebated fee arrives negative and belongs NETTED off the
    drag. ABS would turn a $25 reversal into $25 more drag — answering "what
    did the platform charge and give back" with the sum of both."""
    # Investment accounts are a handful; naming their ids lets the ledger be
    # read by account id (an index probe over the investment rows) instead of
    # a full scan joined to accounts. _FEE_WHERE keeps the type test, and its
    # literal LIKE percents are doubled because a parameter now rides along.
    # Shadow exclusion, the same one _live_accounts and _reconstruct_trend
    # apply to the balances beside this number: a brokerage reached through
    # two aggregators is two account rows carrying one fee history, so
    # summing over both reports twice what the platform charged.
    from . import budget, links
    shadows = links.shadow_ids(conn)
    # and the entity separation every sibling aggregate applies: a
    # brokerage assigned to a business is business money, out of the
    # personal fee picture unless the household combines entities
    inv_ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM accounts WHERE type='investment'"
        f" AND NOT (id = ANY(%s))"
        f" AND ({budget.COMBINE_OR} entity_id IS NULL)", (shadows,))]
    fee_where = _FEE_WHERE.replace("%", "%%")
    by_platform = conn.execute(
        f"""SELECT i.institution_name inst, {_R2.format('SUM(t.amount)')} amt, COUNT(*) n
           FROM transactions t JOIN accounts a ON a.id=t.account_id JOIN items i ON i.id=a.item_id
           WHERE t.account_id = ANY(%s) AND {fee_where}
           GROUP BY inst ORDER BY amt DESC""", (inv_ids,)).fetchall()
    by_year = {r["yr"]: r["amt"] for r in conn.execute(
        f"""SELECT to_char(t.date,'YYYY') yr, {_R2.format('SUM(t.amount)')} amt
           FROM transactions t JOIN accounts a ON a.id=t.account_id
           WHERE t.account_id = ANY(%s) AND {fee_where}
           GROUP BY yr""", (inv_ids,))}
    advisory = _advisory_fee_estimate(conn, today=today)
    for est in advisory.values():
        for y, v in est.items():
            by_year[y] = round(by_year.get(y, 0) + v, 2)
    platforms = [[r["inst"], r["amt"], r["n"]] for r in by_platform]
    for label, est in advisory.items():
        platforms.append([label, round(sum(est.values()), 2), len(est)])
    if advisory:
        platforms.sort(key=lambda p: -p[1])
    return {
        "by_platform": platforms,
        "by_year": sorted(([y, v] for y, v in by_year.items())),
        "total": round(sum(p[1] for p in platforms), 2),
    }


REPORTS = {
    "networth": compute_networth,
    "spending": compute_spending,
    "cashflow": compute_cashflow,
    "fees": compute_fees,
}
