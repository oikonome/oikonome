"""Self-employed tax hygiene.

Mileage (standard rate), home-office (simplified method), quarterly estimated
taxes (SE + income), a simple balance sheet, and 1099-NEC vendor tracking. All
feed / lean on the P&L. Everything here is an ESTIMATE, clearly labeled as
one: Oikonome categorizes and estimates; the CPA files. Not tax advice.
"""
from __future__ import annotations

import datetime as dt
import math

# IRS standard mileage rate (business), by the date it takes effect. The
# rate is usually set each December for the coming year, but the IRS can
# revise it mid-year (2026: 72.5¢ from Jan 1, 76¢ from Jul 1), so the table is
# keyed by effective date, not tax year, and every trip is priced at the rate
# in force on its own date. A date past the last entry takes the latest known
# rate — the user can confirm.
_MILEAGE_RATE = (
    (dt.date(2023, 1, 1), 0.655),
    (dt.date(2024, 1, 1), 0.67),
    (dt.date(2025, 1, 1), 0.70),
    (dt.date(2026, 1, 1), 0.725),
    (dt.date(2026, 7, 1), 0.76),
)
SE_TAX_RATE = 0.153          # 12.4% Social Security + 2.9% Medicare (combined)
SS_RATE = 0.124             # Social Security portion — capped at the wage base
MEDICARE_RATE = 0.029       # Medicare portion — no cap
# Social Security wage base by year (published annually); a year not listed
# falls back to the latest known. Above the base only Medicare applies.
_SS_WAGE_BASE = {2023: 160200, 2024: 168600, 2025: 176100, 2026: 184500}
SE_BASE_FACTOR = 0.9235      # net SE earnings are 92.35% of net profit
HOME_OFFICE_RATE = 5.0       # simplified method: $5 / sq ft …
HOME_OFFICE_MAX_SQFT = 300   # … up to 300 sq ft ($1,500 cap)
# 1099-NEC reporting threshold by the calendar year the payments were made
# in. It sat at $600 for decades; payments made from 2026 on are reportable
# at $2,000, indexed for inflation from 2027. A year before the table takes
# $600, a year past it the latest known figure — the user can confirm.
_1099_THRESHOLD = {2025: 600.0, 2026: 2000.0}


def mileage_rate(on: dt.date | int | None = None) -> float:
    """The business rate in force on a date (default today). An int is a tax
    year and yields the rate in force at that year's end — the single figure
    to quote when a year needs one, e.g. a year with no trips yet."""
    if on is None:
        on = dt.date.today()
    elif isinstance(on, int):
        on = dt.date(on, 12, 31)
    rate = _MILEAGE_RATE[0][1]
    for start, r in _MILEAGE_RATE:
        if on >= start:
            rate = r
    return rate


def ss_wage_base(year: int) -> int:
    return _SS_WAGE_BASE.get(year, _SS_WAGE_BASE[max(_SS_WAGE_BASE)])


def reportable_1099_threshold(year: int) -> float:
    """What a contractor must be paid in `year` before a 1099-NEC is due."""
    if year < min(_1099_THRESHOLD):
        return _1099_THRESHOLD[min(_1099_THRESHOLD)]
    return _1099_THRESHOLD.get(year, _1099_THRESHOLD[max(_1099_THRESHOLD)])


# ---- mileage ---------------------------------------------------------------

def add_trip(conn, entity_id: str, *, date, miles, purpose=None,
             note=None) -> dict:
    from .compat import req_date
    date = req_date(date)
    try:
        m = round(float(miles), 1)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("miles must be a number")
    # NaN passes both range checks (its comparisons are all False)
    if not math.isfinite(m):
        raise ValueError("miles must be a number")
    if m <= 0:
        raise ValueError("miles must be positive")
    # NUMERIC(10,1) tops out just under a billion — past that psycopg raises
    # NumericValueOutOfRange, a 500 where the caller deserves the same 400
    # the "must be positive" check gives.
    if m > 999_999_999.9:
        raise ValueError("miles is too large")
    r = conn.execute(
        """INSERT INTO mileage_log (entity_id, date, miles, purpose, note)
           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
        (entity_id, date, m, purpose, note)).fetchone()
    return {"id": str(r["id"])}


def list_trips(conn, entity_id: str, year: int | None = None) -> list[dict]:
    q = "SELECT id, date, miles, purpose, note FROM mileage_log WHERE entity_id = %s"
    params: list = [entity_id]
    if year:
        q += " AND EXTRACT(YEAR FROM date) = %s"
        params.append(year)
    q += " ORDER BY date DESC"
    return [{"id": str(r["id"]),
             "date": r["date"].isoformat() if r["date"] else None,
             "miles": float(r["miles"]), "purpose": r["purpose"],
             "note": r["note"]} for r in conn.execute(q, params).fetchall()]


def delete_trip(conn, trip_id: str, entity_id: str) -> bool:
    return conn.execute(
        "DELETE FROM mileage_log WHERE id = %s AND entity_id = %s",
        (trip_id, entity_id)).rowcount > 0


def mileage_deduction(conn, entity_id: str, year: int | None = None) -> dict:
    """One tax year's miles priced trip by trip at the rate in force on each
    trip's date — a year can carry two rates (2026 did), so a single year
    rate over the year's total is wrong for every trip on the other side of
    the change. `rate` is the effective (deduction ÷ miles) rate so that
    "miles × rate = deduction" still reads true; `rates` lists each rate that
    applied and the miles under it, in date order."""
    year = year or dt.date.today().year
    rows = conn.execute(
        "SELECT date, miles FROM mileage_log "
        "WHERE entity_id = %s AND EXTRACT(YEAR FROM date) = %s ORDER BY date",
        (entity_id, year)).fetchall()
    miles = deduction = 0.0
    by_rate: dict[float, float] = {}
    for r in rows:
        m = float(r["miles"])
        rate = mileage_rate(r["date"])
        miles += m
        deduction += m * rate
        by_rate[rate] = by_rate.get(rate, 0.0) + m
    rates = [{"rate": k, "miles": round(v, 1)} for k, v in by_rate.items()]
    effective = round(deduction / miles, 4) if miles else mileage_rate(year)
    return {"year": year, "miles": round(miles, 1), "rate": effective,
            "rates": rates, "deduction": round(deduction, 2)}


# ---- home office (simplified method) ---------------------------------------

def home_office_deduction(sqft) -> float:
    if not sqft or sqft <= 0:
        return 0.0
    return round(HOME_OFFICE_RATE * min(int(sqft), HOME_OFFICE_MAX_SQFT), 2)


# ---- balance sheet (not double-entry — a snapshot) -------------------------

def balance_sheet(conn, entity_id: str) -> dict:
    """Business account balances as assets/liabilities + the capital account.
    Not a double-entry balance sheet; assets − liabilities need not equal the
    capital account (there's no journal here) — shown side by side honestly."""
    from . import equity, reporting
    # Shadow exclusion, same rule as every other balance sum. A business
    # account linked through two aggregators leaves two rows sharing one
    # group, and both can carry entity_id — without it one real balance
    # counts twice in total_assets and net.
    rows = conn.execute(
        f"""SELECT COALESCE(a.display_name, a.name) AS name, a.type,
                   a.balance_current AS bal
            FROM accounts a WHERE a.entity_id = %s
              AND a.balance_current IS NOT NULL
              {reporting._NOT_SHADOW.format(col="a.id")}""",
        (entity_id,)).fetchall()
    assets, liabilities = [], []
    ta = tl = 0.0
    for r in rows:
        bal = float(r["bal"])
        if r["type"] in ("credit", "loan"):
            liabilities.append({"name": r["name"], "amount": round(bal, 2)})
            tl += bal
        else:
            assets.append({"name": r["name"], "amount": round(bal, 2)})
            ta += bal
    cap = equity.capital_summary(conn, entity_id)
    return {"assets": assets, "liabilities": liabilities,
            "total_assets": round(ta, 2), "total_liabilities": round(tl, 2),
            "net": round(ta - tl, 2),
            "capital_account": cap["capital_balance"]}


# ---- quarterly estimated taxes ---------------------------------------------

def quarterly_deadlines(tax_year: int) -> list[dt.date]:
    """Federal 1040-ES due dates for a tax year (Q4 falls in January next year).
    Weekend/holiday shifts are ignored — an estimate."""
    return [dt.date(tax_year, 4, 15), dt.date(tax_year, 6, 15),
            dt.date(tax_year, 9, 15), dt.date(tax_year + 1, 1, 15)]


def estimated_tax(conn, entity_id: str, year: int | None = None) -> dict:
    """Estimate self-employment + income tax on the business's net profit, net
    of the mileage, home-office and start-up/organizational deductions. Income tax uses the entity's
    assumed effective rate (income_tax_rate); without it only SE tax is shown.
    Estimate only — not tax advice."""
    from . import books
    year = year or dt.date.today().year
    ent = conn.execute(
        "SELECT income_tax_rate, home_office_sqft FROM business_entity "
        "WHERE id = %s", (entity_id,)).fetchone()
    # the P&L without its capital block: the estimate reads net profit and
    # the §195/§248 share only, and the capital account costs one more
    # scan of the entity's whole history
    p = books._pnl_core(conn, entity_id, year)
    mileage = mileage_deduction(conn, entity_id, year)["deduction"]
    home = home_office_deduction(ent["home_office_sqft"] if ent else None)
    # §195/§248: the year's share of the start-up and organizational
    # allowance (the immediate part in the year the business began, plus
    # that year's amortization). _pnl_core keeps those costs out of
    # net_operating, so without this the estimate taxes profit the same
    # page calls deductible.
    startup = round(
        p["organizational_deduction"].get("deductible_this_year", 0.0)
        + p["startup_deduction"].get("deductible_this_year", 0.0), 2)
    taxable = max(0.0, round(p["net_operating"] - mileage - home - startup,
                             2))

    se_base = round(taxable * SE_BASE_FACTOR, 2)
    # Social Security (12.4%) applies only up to the year's wage base; Medicare
    # (2.9%) has no cap. A flat 15.3% overstates SE tax for higher earners.
    # IRC §1402(b)(2): no SE tax at all when net earnings from
    # self-employment are under $400. Without the floor a first-year or
    # hobby-scale entity is told to send the IRS quarterly payments it
    # does not owe.
    if se_base >= 400:
        ss = min(se_base, ss_wage_base(year)) * SS_RATE
        se_tax = round(ss + se_base * MEDICARE_RATE, 2)
    else:
        se_tax = 0.0
    rate = float(ent["income_tax_rate"]) if ent and ent["income_tax_rate"] \
        is not None else None
    income_tax = None
    if rate is not None:
        # half the SE tax is deductible for income tax
        income_base = max(0.0, taxable - se_tax / 2)
        income_tax = round(income_base * rate / 100.0, 2)
    total = round(se_tax + (income_tax or 0.0), 2)
    return {"year": year, "net_profit": p["net_operating"],
            "mileage_deduction": mileage, "home_office_deduction": home,
            "startup_deduction": startup,
            "taxable_profit": taxable, "se_tax": se_tax,
            "income_tax_rate": rate, "income_tax": income_tax,
            "total_estimated": total, "quarterly": round(total / 4, 2),
            "deadlines": [d.isoformat() for d in quarterly_deadlines(year)]}


# ---- 1099-NEC vendor tracking ----------------------------------------------

def vendor_totals(conn, entity_id: str, year: int | None = None) -> list[dict]:
    """Every payee the business paid (expenses), with the total paid and whether
    it's marked 1099-reportable + at or over that year's reporting threshold
    (reportable_1099_threshold). Scoped to one tax year — the test is per
    calendar year — defaulting to the current year."""
    year = year or dt.date.today().year
    params: dict = {"eid": entity_id, "yr": year}
    # group by the CANONICAL merchant (like every other reporting surface) so
    # split descriptors for one vendor can't each stay under the threshold
    from .merchant_dedup import DISPLAY_MERCHANT, MC_JOIN
    # Same ownership rule as the P&L (books._BIZ_TXN): a row explicitly
    # assigned to ANOTHER entity stays off this one's 1099 list even when it
    # sits in this entity's account, and non-primary linked (shadow) rows are
    # dropped so a dual-sourced account cannot double a vendor past the threshold.
    from .books import _BIZ_TXN
    from .merchant_sql import raw_key
    # Net of the part the VENDOR paid back, and only that. The form reports
    # what the business paid the payee: a partial refund from the vendor
    # shrinks that, but a client or anyone else repaying the business
    # (a billed-back expense) does not — the contractor was still paid in
    # full, and netting it would drop a contractor under the line who is
    # owed a form. The P&L nets both, because it counts the business's
    # cost, not its payments to one payee. A deposit is the vendor's when
    # it resolves to the same merchant the charge does. That holds for a
    # full pair too: it repays the charge with the whole deposit, so only
    # a full pair with the vendor itself takes the deposit off.
    paid_to_vendor = f"""GREATEST(t.amount - COALESCE((
        SELECT SUM(CASE WHEN pr.partial = 1 THEN pr.amount
                        ELSE ABS(d.amount) END)
          FROM reimbursements pr
          JOIN transactions d ON d.id = pr.reimburse_id
          LEFT JOIN merchant_canonical dmc
                 ON dmc.raw_merchant = {raw_key("d")}
          LEFT JOIN merchants dm ON dm.id = d.merchant_id
         WHERE pr.expense_id = t.id AND d.removed = 0
           AND lower(COALESCE(dm.name, dmc.canonical, {raw_key("d")}))
               = lower({DISPLAY_MERCHANT})), 0), 0)"""
    # A full reimbursement link stamps the charge TRANSFER_OUT so spend
    # math skips it, but the payee was still paid: the transfer test reads
    # the category the charge had before the link (the person's own pin
    # the link set aside, else the aggregator's). A transfer the person or
    # the aggregator set stays off the list.
    pre_link_cat = """COALESCE((
        SELECT COALESCE(NULLIF(lk.link_prior, ''), t.category_primary, '')
          FROM manual_categories lk
         WHERE lk.transaction_id = t.id AND lk.link_made),
        t.category_override, t.category_primary, '')"""
    rows = conn.execute(
        f"""SELECT {DISPLAY_MERCHANT} AS payee,
                   COALESCE(SUM({paid_to_vendor}),0) AS paid
            FROM transactions t {MC_JOIN}
            WHERE {_BIZ_TXN}
              AND t.removed = 0 AND t.amount > 0
              AND {pre_link_cat} NOT IN ('TRANSFER_OUT','TRANSFER_IN')
              AND COALESCE(t.category_detailed,'')
                  != 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
              AND EXTRACT(YEAR FROM t.date) = %(yr)s
            GROUP BY {DISPLAY_MERCHANT} HAVING SUM({paid_to_vendor}) > 0
            ORDER BY paid DESC""", params).fetchall()
    marks = {r["merchant"]: r for r in conn.execute(
        "SELECT merchant, reportable, tin_last4 FROM vendor_1099 "
        "WHERE entity_id = %s", (entity_id,)).fetchall()}
    threshold = reportable_1099_threshold(year)
    out = []
    for r in rows:
        m = marks.get(r["payee"])
        paid = round(float(r["paid"]), 2)
        out.append({"merchant": r["payee"], "paid": paid,
                    "reportable": bool(m and m["reportable"]),
                    "tin_last4": m["tin_last4"] if m else None,
                    "needs_1099": bool(m and m["reportable"]
                                       and paid >= threshold)})
    return out


def mark_vendor(conn, entity_id: str, merchant: str, *, reportable: bool = True,
                tin_last4=None) -> None:
    # store ONLY the last 4 — never a full TIN/SSN in this column. Accept a
    # full number but keep only its last 4 digits; reject junk.
    if tin_last4 not in (None, ""):
        import re as _re
        digits = _re.sub(r"\D", "", str(tin_last4))
        if not digits:
            raise ValueError("TIN must contain digits")
        tin_last4 = digits[-4:]
    else:
        tin_last4 = None
    conn.execute(
        """INSERT INTO vendor_1099 (entity_id, merchant, reportable, tin_last4)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (tenant_id, entity_id, merchant) DO UPDATE SET
             reportable = EXCLUDED.reportable, tin_last4 = EXCLUDED.tin_last4""",
        (entity_id, merchant, reportable, tin_last4))
