"""Self-employed tax hygiene.

Mileage (standard rate), home-office (simplified method), quarterly estimated
taxes (SE + income), a simple balance sheet, and 1099-NEC vendor tracking. All
feed / lean on the P&L. Everything here is an ESTIMATE, clearly labeled as
one: Oikonome categorizes and estimates; the CPA files. Not tax advice.
"""
from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

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
            "capital_account": cap["capital_balance"],
            # the household member's share of it, when one is marked and
            # carries an ownership percentage
            "household_share_pct": cap.get("household_share_pct"),
            "household_capital": cap.get("household_capital")}


# ---- quarterly estimated taxes ---------------------------------------------

def quarterly_deadlines(tax_year: int) -> list[dt.date]:
    """Federal 1040-ES due dates for a tax year (Q4 falls in January next year).
    Weekend/holiday shifts are ignored — an estimate."""
    return [dt.date(tax_year, 4, 15), dt.date(tax_year, 6, 15),
            dt.date(tax_year, 9, 15), dt.date(tax_year + 1, 1, 15)]


def _se_tax(se_base: float, year: int) -> float:
    """One person's self-employment tax on their net SE earnings.
    Social Security (12.4%) applies only up to the year's wage base and
    Medicare (2.9%) has no cap, so a flat 15.3% overstates it for higher
    earners. IRC §1402(b)(2): none at all under $400, or a first-year or
    hobby-scale entity is told to send quarterly payments it does not owe."""
    if se_base < 400:
        return 0.0
    ss = min(se_base, ss_wage_base(year)) * SS_RATE
    return round(ss + se_base * MEDICARE_RATE, 2)


def _member_shares(conn, entity_id: str) -> list[dict]:
    """The partners' ownership shares, when they are complete: every member
    carries a percentage and together they make 100. Anything less cannot
    say whose share the rest is, so the caller treats the profit as one."""
    rows = conn.execute(
        "SELECT member_name, ownership_pct, is_household FROM entity_membership "
        "WHERE entity_id = %s ORDER BY created_at, id",
        (entity_id,)).fetchall()
    if not rows or any(r["ownership_pct"] is None for r in rows):
        return []
    # summed exactly: three two-decimal thirds (33.33 each) add up in
    # binary floating point to a hair over a cent short of 100, which the
    # cent of rounding slack exists to accept
    total = sum(Decimal(str(r["ownership_pct"])) for r in rows)
    if abs(total - Decimal(100)) > Decimal("0.01"):
        return []
    return [{"member_name": r["member_name"],
             "ownership_pct": float(r["ownership_pct"]),
             "is_household": bool(r["is_household"])} for r in rows]


def _household_unavailable(conn, entity_id: str, structure: str | None,
                           is_household: bool) -> str | None:
    """Why an estimate that covers every member is not the household's
    share, or None when it is (or when there is only one owner):

      * "s_corp" — shareholders' tax is not split here at all, so no mark
        changes the figure;
      * "shares_incomplete" — a member is marked, but the shares are not
        all given or do not total 100%, so the marked member's share of
        the profit cannot be told from the others';
      * "none_marked" — the shares are complete and nobody is marked.
    The rule is _member_shares', stated so a screen never asks for a mark
    that would change nothing."""
    if is_household:
        return None
    if structure == "s_corp":
        return "s_corp"
    if structure != "multi_member_llc":
        return None
    marked = conn.execute(
        "SELECT 1 FROM entity_membership WHERE entity_id = %s "
        "AND is_household LIMIT 1", (entity_id,)).fetchone()
    return "shares_incomplete" if marked else "none_marked"


def _tax_on(taxable: float, year: int, rate: float | None,
            structure: str | None, members: list[dict]) -> dict:
    """SE + income tax on one year's taxable business profit.

    What is modelled, by structure:
      * sole_prop / single_member_llc — the owner's SE tax on the whole
        profit.
      * multi_member_llc — each member's SE tax on their share, each with
        their own wage base; the total covers EVERY member. Without a
        complete set of shares the profit is taxed as one person's.
      * s_corp — no SE tax: a shareholder's Social Security and Medicare go
        through payroll on wages, which the books already carry as an
        expense. Payroll tax itself and reasonable-compensation rules are
        not modelled.
    Income tax is the entity's assumed effective rate on the profit less
    the deductible half of the SE tax.

    When one member is marked as the HOUSEHOLD (is_household), the
    headline figures — se_tax, income_tax, total — are that member's
    share: their SE tax on their share of the profit, and the rate on
    that share less half of it; the whole business's figures ride along
    as `business`. Nothing is modelled for the other members' own
    returns beyond their SE tax, which the business total still carries."""
    se_applies = structure != "s_corp"
    shares = []
    if not se_applies:
        se_tax = 0.0
    elif structure == "multi_member_llc" and members:
        se_tax = 0.0
        for m in members:
            share = round(taxable * m["ownership_pct"] / 100.0, 2)
            t = _se_tax(round(share * SE_BASE_FACTOR, 2), year)
            shares.append({**m, "share_of_profit": share, "se_tax": t})
            se_tax += t
        se_tax = round(se_tax, 2)
    else:
        se_tax = _se_tax(round(taxable * SE_BASE_FACTOR, 2), year)
    income_tax = None
    if rate is not None:
        income_base = max(0.0, taxable - se_tax / 2)
        income_tax = round(income_base * rate / 100.0, 2)
    business = {"se_tax": se_tax, "se_tax_applies": se_applies,
                "income_tax": income_tax, "members": shares,
                "total": round(se_tax + (income_tax or 0.0), 2)}
    ours = next((m for m in shares if m.get("is_household")), None)
    if ours is None:
        return business
    h_income = None
    if rate is not None:
        h_base = max(0.0, ours["share_of_profit"] - ours["se_tax"] / 2)
        h_income = round(h_base * rate / 100.0, 2)
    return {"se_tax": ours["se_tax"], "se_tax_applies": se_applies,
            "income_tax": h_income, "members": shares,
            "total": round(ours["se_tax"] + (h_income or 0.0), 2),
            "household": {**ours, "income_tax": h_income,
                          "total": round(ours["se_tax"] + (h_income or 0.0), 2)},
            "business": business}


# The shortest stretch of a year a projection is made from: the IRS's own
# annualized-installment method (Form 2210, Schedule AI) never projects
# from less than the first three months. Projected from a handful of days,
# one early invoice reads as a six-figure year and a quarterly payment
# many times the real one.
MIN_PROJECTION_DAYS = 90


def estimated_tax(conn, entity_id: str, year: int | None = None, *,
                  today: dt.date | None = None, _lifetime=None) -> dict:
    """Estimate self-employment + income tax on the business's net profit, net
    of the mileage, home-office and start-up/organizational deductions. Income tax uses the entity's
    assumed effective rate (income_tax_rate); without it only SE tax is shown.
    Estimate only — not tax advice.

    `se_tax`, `income_tax` and `total_estimated` are owed on the profit
    earned so far in `year`. For the year in progress that is not the
    year's profit, so `quarterly` is a quarter of `annualized_total`: the
    same tax on the year-to-date profit projected over the whole year by
    the share of the year elapsed. A finished year is not projected.
    Until MIN_PROJECTION_DAYS have passed the projection stands on last
    year's tax when the business had one (`basis` "prior_year": paying a
    quarter of last year's tax is also the IRS's safe harbour), else on
    the year so far spread over at least that many days ("projected").
    `next_deadline` is the next 1040-ES due date and `next_payment` the
    installment it is for: from January 1 to 15 that is LAST year's fourth
    installment — a quarter of last year's tax, never more than is still
    unpaid — not a quarter of the new year's projection.
    `covers` says whose tax the figure is: `owner`, or `all_members` for a
    structure that can have several owners."""
    from . import books
    today = today or dt.date.today()
    year = year or today.year
    ent = conn.execute(
        "SELECT income_tax_rate, home_office_sqft, structure "
        "FROM business_entity WHERE id = %s", (entity_id,)).fetchone()
    structure = ent["structure"] if ent else None
    # the P&L without its capital block: the estimate reads net profit and
    # the §195/§248 share only, and the capital account costs one more
    # scan of the entity's whole history
    # The entity's whole history is read once and shared with the prior
    # year's estimate this one may need: each figure is that history
    # filtered by year, the same rows entity_transactions(year) returns.
    lifetime = (_lifetime if _lifetime is not None
                else books.entity_transactions(conn, entity_id, None))
    ytxns = [t for t in lifetime
             if t["date"] and int(t["date"][:4]) == year]
    p = books._pnl_core(conn, entity_id, year, ytxns, lifetime=lifetime)
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
    # The standard mileage rate REPLACES actual vehicle operating costs
    # (parking, tolls and fares for vehicles the owner does not drive are
    # allowed on top — books._is_vehicle_cost), and net_operating has
    # already deducted the actual ones. So the estimate takes one method:
    # the larger, as a filer free to choose would. Standard: the actual car
    # costs are added back and the mileage taken. Actual: the logged miles
    # are not deducted at all.
    logged = mileage
    car = float(p.get("vehicle_expenses") or 0.0)
    if logged > 0 and logged > car:
        method, added_back = "standard_mileage", car
    else:
        method, added_back, mileage = ("actual" if car > 0 else None), 0.0, 0.0
    vehicle_adj = round(mileage - added_back, 2)
    taxable = max(0.0, round(p["net_operating"] - vehicle_adj - home
                             - startup, 2))
    rate = float(ent["income_tax_rate"]) if ent and ent["income_tax_rate"] \
        is not None else None
    members = (_member_shares(conn, entity_id)
               if structure == "multi_member_llc" else [])
    so_far = _tax_on(taxable, year, rate, structure, members)

    # Projection for the year in progress. Profit that accrues through the
    # year (net of the vehicle deduction) scales with the year's elapsed share; the home
    # office and start-up allowances are annual amounts and do not.
    annualized = year == today.year
    basis = "projected" if annualized else "year"
    prior = None
    if annualized:
        start = dt.date(year, 1, 1)
        days = (dt.date(year + 1, 1, 1) - start).days
        elapsed = (today - start).days + 1
        if elapsed < MIN_PROJECTION_DAYS or today <= dt.date(year, 1, 15):
            prior = estimated_tax(conn, entity_id, year - 1, today=today,
                                  _lifetime=lifetime)
        if elapsed < MIN_PROJECTION_DAYS and prior["total_estimated"] > 0:
            basis = "prior_year"
            a_taxable = prior["taxable_profit"]
            projected = {"total": prior["total_estimated"]}
        else:
            factor = days / max(elapsed, MIN_PROJECTION_DAYS)
            a_taxable = max(0.0, round(
                (p["net_operating"] - vehicle_adj) * factor - home
                - startup, 2))
            projected = _tax_on(a_taxable, year, rate, structure, members)
    else:
        a_taxable, projected = taxable, so_far
    quarterly = round(projected["total"] / 4, 2)
    # Last year's fourth installment is this year's next payment only while
    # it is still ahead in the year it falls in. For a future year every
    # deadline of the year before is "ahead" too, and reaching back from
    # each such year to the one before it walked down one level per year
    # to the present, re-reading the ledger at every step.
    years = (year - 1, year) if year <= today.year else (year,)
    upcoming = [(d, yr) for yr in years
                for d in quarterly_deadlines(yr) if d >= today]
    next_deadline, next_payment = None, None
    if upcoming:
        nd, nyr = min(upcoming)
        next_deadline = nd.isoformat()
        if nyr == year:
            next_payment = quarterly
        else:
            prior = prior or estimated_tax(conn, entity_id, nyr, today=today,
                                           _lifetime=lifetime)
            # set against the payments on the same footing as the estimate:
            # the household's share of them when the estimate is the
            # household's share
            left = max(0.0, prior["total_estimated"]
                       - prior.get("covered_tax_paid",
                                   prior.get("estimated_tax_paid", 0.0)))
            next_payment = round(min(prior["quarterly"], left), 2)
    # The figures total_estimated is measured against. When it is the
    # household member's share, the profit it is a rate of and the
    # estimates already paid toward it are that member's share too: the
    # business's own profit would show half the real rate, and payments the
    # business made for every partner would come off one partner's bill.
    # Payments out of the business account are taken as split by ownership
    # — a guess that can only make the amount still to pay larger, never
    # smaller, when the business paid one partner's estimates alone.
    paid = p.get("estimated_tax_paid", 0.0)
    unavailable = _household_unavailable(conn, entity_id, structure,
                                          bool(so_far.get("household")))
    share = (so_far["household"]["ownership_pct"]
             if so_far.get("household") else None)
    covered_profit = (round(p["net_operating"] * share / 100.0, 2)
                      if share is not None else p["net_operating"])
    covered_paid = (round(paid * share / 100.0, 2)
                    if share is not None else paid)
    return {"year": year, "net_profit": p["net_operating"],
            "covered_profit": covered_profit,
            "covered_tax_paid": covered_paid,
            # what is deducted for the vehicle: `mileage_deduction` is the
            # standard-rate figure actually taken (0 when the actual costs
            # are larger), `vehicle_expenses_added_back` the actual costs it
            # replaced, `mileage_logged` the log's own figure
            "mileage_deduction": mileage, "mileage_logged": logged,
            "vehicle_method": method,
            "vehicle_expenses_added_back": added_back,
            "home_office_deduction": home,
            "startup_deduction": startup,
            "taxable_profit": taxable, "se_tax": so_far["se_tax"],
            "se_tax_applies": so_far["se_tax_applies"],
            "structure": structure,
            # whose tax the figure is: the owner's; the household member's
            # share, when one member is marked as the household; or every
            # member's, when nothing says which member this household is
            "covers": ("household" if so_far.get("household")
                       else "all_members"
                       if structure in ("multi_member_llc", "s_corp")
                       else "owner"),
            "household": so_far.get("household"),
            # why the figure is not the household's share, when it covers
            # every member: so a screen can say what would change it
            "household_unavailable": unavailable,
            "business_total": (so_far["business"]["total"]
                               if so_far.get("business") else None),
            "members": so_far["members"],
            "income_tax_rate": rate, "income_tax": so_far["income_tax"],
            "total_estimated": so_far["total"],
            "annualized": annualized,
            "annualized_taxable_profit": a_taxable,
            "annualized_total": projected["total"],
            "quarterly": quarterly,
            "basis": basis,
            "estimated_tax_paid": paid,
            "next_deadline": next_deadline,
            "next_payment": next_payment,
            "deadlines": [d.isoformat() for d in quarterly_deadlines(year)]}


# ---- 1099-NEC vendor tracking ----------------------------------------------

def vendor_totals(conn, entity_id: str, year: int | None = None) -> list[dict]:
    """Every payee the business paid (expenses), with the 1099-NEC amount
    (`paid`: everything not paid by card), what was paid by card, credit or
    debit (`paid_by_card`: the card processor reports it on a 1099-K; a
    checking-account row counts as a card payment only when the aggregator
    says it was paid in store), and
    whether it's marked 1099-reportable + `paid` is at or over that year's
    reporting threshold (reportable_1099_threshold). The whole amount paid
    is `paid + paid_by_card`. Scoped to one tax year — the test is per
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
    # A payment made by card is not reportable on a 1099-NEC: the card
    # processor reports it on a 1099-K. Every row on a credit account is a
    # card payment. On a checking account only a positive card signal
    # counts, and the only one the ledger carries is the aggregator's
    # 'in store' channel. Its 'online' channel covers ACH debits and bill
    # pay as well as web card purchases, and an ACH payment to a contractor
    # is exactly what the 1099-NEC reports, so an 'online' checking row
    # stays in the form amount. Wrongly leaving in a card payment overstates
    # the amount the business then checks; wrongly leaving out an ACH
    # payment hides a form it owes.
    by_card = ("(t.account_id IN "
               "(SELECT id FROM accounts WHERE type = 'credit') "
               "OR (t.account_id IN "
               "(SELECT id FROM accounts WHERE type = 'depository') "
               "AND lower(COALESCE(t.payment_channel, '')) = 'in store'))")
    # a payment to a payee, any year: what the form counts in its own year
    # and what a refund is paired with (_vendor_refunds)
    is_payment = f"""{_BIZ_TXN}
              AND t.removed = 0 AND t.amount > 0
              AND {pre_link_cat} NOT IN ('TRANSFER_OUT','TRANSFER_IN')
              AND COALESCE(t.category_detailed,'')
                  NOT IN ('LOAN_PAYMENTS_CREDIT_CARD_PAYMENT',
                          -- a tax payment is the owner's, not a payee's
                          -- fee: the books keep it off the P&L the same way
                          'GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT')"""
    rows = conn.execute(
        f"""SELECT {DISPLAY_MERCHANT} AS payee,
                   COALESCE(SUM({paid_to_vendor})
                            FILTER (WHERE NOT {by_card}), 0) AS paid,
                   COALESCE(SUM({paid_to_vendor})
                            FILTER (WHERE {by_card}), 0) AS paid_by_card,
                   array_agg(DISTINCT t.merchant_id::text)
                       FILTER (WHERE t.merchant_id IS NOT NULL) AS mids
            FROM transactions t {MC_JOIN}
            WHERE {is_payment}
              AND EXTRACT(YEAR FROM t.date) = %(yr)s
            GROUP BY {DISPLAY_MERCHANT} HAVING SUM({paid_to_vendor}) > 0
            ORDER BY paid DESC""", params).fetchall()
    back = _vendor_refunds(conn, entity_id, year, by_card, is_payment)
    marks = _marks(conn, entity_id)
    threshold = reportable_1099_threshold(year)
    out = []
    for r in rows:
        m = _mark_for(marks, r["payee"], r["mids"] or [])
        refund, refund_card = back.get(r["payee"], (0.0, 0.0))
        paid = round(max(0.0, float(r["paid"]) - refund), 2)
        by_card_paid = round(max(0.0, float(r["paid_by_card"]) - refund_card),
                             2)
        if paid <= 0 and by_card_paid <= 0:
            continue                    # the vendor gave it all back
        out.append({"merchant": r["payee"], "paid": paid,
                    "paid_by_card": by_card_paid,
                    "reportable": bool(m and m["reportable"]),
                    "tin_last4": m["tin_last4"] if m else None,
                    "needs_1099": bool(m and m["reportable"]
                                       and paid >= threshold)})
    out.sort(key=lambda v: -v["paid"])
    return out


def _vendor_refunds(conn, entity_id: str, year: int, by_card: str,
                    is_payment: str) -> dict:
    """payee -> (refunded off the form amount, refunded by card): the
    year's deposits from a vendor that the P&L nets off that vendor's
    cost as a refund (books._inflow_role), and that no reimbursement link
    already ties to a charge (those are netted charge by charge in
    vendor_totals). The 1099 amount is what the business paid the payee,
    so a vendor's own refund comes off it the way it comes off the books;
    a refund to a card comes off the card part.

    Only a refund of a payment made in the same year comes off. The form
    counts each calendar year's payments gross, so a January refund of a
    December payment would otherwise erase part of a different year's
    payments — and can drop a contractor under the threshold for the year
    they were paid in full. The payment a refund reverses is found the way
    the P&L finds the cost it reverses (books._reversed_cost: the payee's
    payment on or before the refund within a year, the exact amount if
    one matches, else the most recent); a refund with no such payment is
    not netted, since the form then has nothing it could reverse."""
    from . import books, reporting
    from .merchant_dedup import DISPLAY_MERCHANT, MC_JOIN
    from .books import _BIZ_TXN
    rows = conn.execute(
        f"""SELECT {DISPLAY_MERCHANT} AS payee, t.amount, t.date,
                   COALESCE(t.category_override, t.category_primary) AS category,
                   t.category_detailed AS cat_detailed,
                   {reporting.ORIGINAL_CAT} AS cat_original,
                   {reporting.PARTLY_CLAIMED_DEPOSIT} AS partly_claimed,
                   c.sched_c_line, ({by_card}) AS card
              FROM transactions t {MC_JOIN}
              LEFT JOIN business_txn_class c ON c.txn_id = t.id
             WHERE {_BIZ_TXN} AND t.removed = 0 AND t.amount < 0
               AND EXTRACT(YEAR FROM t.date) = %(yr)s
               AND NOT EXISTS (SELECT 1 FROM reimbursements pr
                                WHERE pr.reimburse_id = t.id)""",
        {"eid": entity_id, "yr": year}).fetchall()
    # every payment a refund in this year could reverse
    payments: dict = {}
    for p in conn.execute(
            f"""SELECT {DISPLAY_MERCHANT} AS payee, t.date, t.amount
                  FROM transactions t {MC_JOIN}
                 WHERE {is_payment}
                   AND t.date >= %(lo)s AND t.date <= %(hi)s
                 ORDER BY t.date""",
            {"eid": entity_id, "lo": dt.date(year, 1, 1)
             - books._REFUND_LOOKBACK,
             "hi": dt.date(year, 12, 31)}).fetchall():
        payments.setdefault(p["payee"], []).append(
            (p["date"], round(float(p["amount"]), 2)))
    out: dict = {}
    for r in rows:
        t = dict(r)
        t["partly_claimed"] = bool(t["partly_claimed"])
        if books._inflow_role(t) != "refund" or not t["payee"]:
            continue
        paid = books._reversed_cost(t, payments)
        if paid is None or paid[0].year != year:
            continue
        plain, card = out.get(t["payee"], (0.0, 0.0))
        amt = -float(t["amount"])
        out[t["payee"]] = ((plain, card + amt) if t["card"]
                           else (plain + amt, card))
    return out


def _marks(conn, entity_id: str) -> list[dict]:
    """The entity's vendor marks, each with the live merchant it now
    follows: a mark made for a merchant that has since been merged away
    belongs to the merchant its rows went to."""
    from . import merchant_identity
    out = []
    for r in conn.execute(
            "SELECT id, merchant, merchant_id, reportable, tin_last4, "
            "merged_from FROM vendor_1099 WHERE entity_id = %s "
            "ORDER BY created_at, id", (entity_id,)).fetchall():
        m = dict(r)
        sid = (merchant_identity._survivor(conn, r["merchant_id"])
               if r["merchant_id"] else None)
        m["follows"] = str(sid) if sid else None
        out.append(m)
    return out


def _mark_for(marks: list[dict], payee: str, mids) -> dict | None:
    """The mark for one payee: by the merchant its rows belong to first,
    else by the name for a mark no merchant stands behind. Two marks that
    reach one merchant (made before a merge) read as one: reportable when
    either was, and the first TIN on file."""
    mids = {str(i) for i in mids}
    hit = [m for m in marks if m["follows"] and m["follows"] in mids]
    if not hit:
        hit = [m for m in marks if m["merchant"] == payee
               and (m["follows"] is None or not mids)]
    if not hit:
        return None
    return {"reportable": any(m["reportable"] for m in hit),
            "tin_last4": next((m["tin_last4"] for m in hit
                               if m["tin_last4"]), None)}


def _merchant_named(conn, name: str) -> str | None:
    """The one merchant the payee shown as `name` is, when exactly one
    stands behind it: the merchant its rows are filed under, else the one
    live merchant of that name."""
    from .merchant_dedup import DISPLAY_MERCHANT, MC_JOIN
    ids = [r["id"] for r in conn.execute(
        f"SELECT DISTINCT t.merchant_id::text AS id FROM transactions t "
        f"{MC_JOIN} WHERE t.removed = 0 AND t.merchant_id IS NOT NULL "
        f"AND {DISPLAY_MERCHANT} = %s LIMIT 2", (name,)).fetchall()]
    if not ids:
        ids = [r["id"] for r in conn.execute(
            "SELECT id::text AS id FROM merchants WHERE name = %s "
            "AND merged_into IS NULL LIMIT 2", (name,)).fetchall()]
    return ids[0] if len(ids) == 1 else None


def mark_vendor(conn, entity_id: str, merchant: str, *, reportable: bool = True,
                tin_last4=None) -> None:
    """Mark the payee shown as `merchant` 1099-reportable (or not). The
    mark is kept for the merchant behind that name when there is one, so a
    rename, merge or string clean carries it along (follow_merchant_rename);
    the name is what the screens show."""
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
    mid = _merchant_named(conn, merchant)
    with conn.transaction():
        same = None
        if mid is not None:
            # a mark already made for this merchant under an older name.
            # Two can follow it when a merge left one behind for each side
            # (a restored archive carries whatever it was given); only one
            # mark per merchant may stand, so the one already holding the
            # merchant's id is kept and the rest fold into it
            following = [m for m in _marks(conn, entity_id)
                         if m["follows"] == mid]
            if following:
                same = next((m for m in following
                             if str(m["merchant_id"]) == mid), following[0])
                extra = [m["id"] for m in following if m is not same]
                if extra:
                    _fold_marks(conn, same["id"], extra)
        if same is not None:
            # the payee's current name replaces the old one; a name-only
            # mark already standing under the current name is this one
            conn.execute(
                "DELETE FROM vendor_1099 WHERE entity_id = %s AND merchant = %s "
                "AND id <> %s AND merchant_id IS NULL",
                (entity_id, merchant, same["id"]))
            clash = conn.execute(
                "SELECT 1 FROM vendor_1099 WHERE entity_id = %s "
                "AND merchant = %s AND id <> %s",
                (entity_id, merchant, same["id"])).fetchone()
            conn.execute(
                "UPDATE vendor_1099 SET merchant = %s, merchant_id = %s, "
                "reportable = %s, tin_last4 = %s WHERE id = %s",
                (same["merchant"] if clash else merchant, mid, reportable,
                 tin_last4, same["id"]))
            return
        conn.execute(
            """INSERT INTO vendor_1099 (entity_id, merchant, merchant_id,
                                        reportable, tin_last4)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id, entity_id, merchant) DO UPDATE SET
                 reportable = EXCLUDED.reportable,
                 tin_last4 = EXCLUDED.tin_last4,
                 merchant_id = COALESCE(vendor_1099.merchant_id,
                                        EXCLUDED.merchant_id)""",
            (entity_id, merchant, mid, reportable, tin_last4))


def _fold_marks(conn, keep_id, extra_ids: list) -> None:
    """Fold extra marks into the one kept for their merchant, the way
    follow_merchant_rename folds a merge: each folded mark is journalled
    in the kept mark's `merged_from`, with its flags and the raw strings
    that carried its merchant here, so undoing that merge brings it back;
    its own journal comes along, so undos it was holding still work; and
    the kept mark takes the first TIN and note on file. Then the extras
    go."""
    from .compat import jsonb
    keep = conn.execute(
        "SELECT id, reportable, tin_last4, note, merged_from FROM vendor_1099 "
        "WHERE id = %s FOR UPDATE", (keep_id,)).fetchone()
    notes = list(keep["merged_from"] or [])
    tin, note = keep["tin_last4"], keep["note"]
    prior = {"reportable": keep["reportable"], "tin_last4": keep["tin_last4"]}
    for m in conn.execute(
            "SELECT id, merchant, merchant_id::text AS mid, reportable, "
            "tin_last4, note, merged_from FROM vendor_1099 "
            "WHERE id = ANY(%s) ORDER BY created_at, id FOR UPDATE",
            (list(extra_ids),)).fetchall():
        # the strings whose rows were this mark's merchant before the merge
        # that brought it here: still filed under that merchant's id, or
        # journalled as renamed away from its name
        raws = sorted({r["raw"] for r in conn.execute(
            "SELECT raw_merchant AS raw FROM merchant_canonical "
            "WHERE merchant_id::text = %s "
            "UNION SELECT raw_merchant FROM merchant_renames "
            "WHERE lower(from_canonical) = lower(%s)",
            (m["mid"] or "", m["merchant"])).fetchall()})
        notes.extend(m["merged_from"] or [])
        notes.append({"from": m["merchant"], "id": m["mid"], "raws": raws,
                      "had_into": True,
                      "mark": {"reportable": m["reportable"],
                               "tin_last4": m["tin_last4"]},
                      "into_prior": prior})
        tin = tin or m["tin_last4"]
        note = note or m["note"]
    conn.execute("DELETE FROM vendor_1099 WHERE id = ANY(%s)",
                 (list(extra_ids),))
    conn.execute(
        "UPDATE vendor_1099 SET tin_last4 = %s, note = %s, merged_from = %s "
        "WHERE id = %s", (tin, note, jsonb(notes), keep_id))


# ---- marks follow the merchant ---------------------------------------------
# merchant_dedup.rename calls follow_merchant_rename after it moves rows
# (inside its transaction), and merchant_dedup.undo calls
# unfollow_merchant_rename after it puts one raw string back — the same
# pair bills keeps for bill chips.

def follow_merchant_rename(conn, display: str, old_ids, raws: list[str]) -> int:
    """Re-key every vendor mark made for the payee `display` to the merchant
    its rows were just filed under. A plain rename keeps the merchant and
    only the name changes. A merge onto a merchant that has no mark of its
    own moves the mark; onto one that has, folds into it (reportable when
    either was, the TIN on file kept), and the folded mark is journalled in
    `merged_from` so undo can put it back. Returns how many marks changed."""
    from .bills import _merchant_of_raw
    survivor = _merchant_of_raw(conn, raws[0]) if raws else None
    if survivor is None:
        return 0
    return follow_merchant(conn, display, old_ids, survivor, raws)


def follow_merchant(conn, display: str, old_ids, survivor: tuple,
                    raws: list[str]) -> int:
    """follow_merchant_rename for a caller that already knows the merchant
    the marks go to, as (id, name). `raws` journals which strings moved
    (empty when the move is not one a person can undo)."""
    sid, sname = str(survivor[0]), survivor[1]
    old = {str(i) for i in old_ids if i}
    rows = conn.execute(
        "SELECT id, entity_id, merchant, merchant_id::text AS mid, "
        "reportable, tin_last4, merged_from FROM vendor_1099 "
        "WHERE merchant_id::text = ANY(%s) OR merchant_id::text = %s "
        "OR (merchant_id IS NULL AND lower(merchant) IN (lower(%s), lower(%s))) "
        "ORDER BY created_at, id FOR UPDATE",
        (sorted(old), sid, display, sname)).fetchall()
    by_ent: dict = {}
    for r in rows:
        by_ent.setdefault(str(r["entity_id"]), []).append(dict(r))
    changed = 0
    for ms in by_ent.values():
        moving = [m for m in ms if m["mid"] in old or (
            m["mid"] is None and m["merchant"].lower() == display.lower())]
        into = [m for m in ms if m not in moving]
        if not moving:
            continue
        if not into:
            into = [moving.pop(0)]
            tgt = into[0]
            notes = list(tgt["merged_from"] or [])
            if tgt["mid"] != sid:
                notes.append({"from": tgt["merchant"], "id": tgt["mid"],
                              "raws": sorted(raws), "had_into": False})
            tgt.update(mid=sid, merged_from=notes)
        else:
            # only one mark per merchant may stand: the one already holding
            # the survivor is kept, and any other mark of the business on
            # its name (a name-only one an earlier merge left beside it)
            # folds into it first, as mark_vendor folds them
            tgt = next((m for m in into if m["mid"] == sid), into[0])
            extra = [m["id"] for m in into if m is not tgt]
            if extra:
                _fold_marks(conn, tgt["id"], extra)
                tgt = dict(conn.execute(
                    "SELECT id, entity_id, merchant, "
                    "merchant_id::text AS mid, reportable, tin_last4, "
                    "merged_from FROM vendor_1099 WHERE id = %s",
                    (tgt["id"],)).fetchone())
        for m in moving:
            tgt["merged_from"] = list(tgt["merged_from"] or []) + [{
                "from": m["merchant"], "id": m["mid"], "raws": sorted(raws),
                "had_into": True,
                "mark": {"reportable": m["reportable"],
                         "tin_last4": m["tin_last4"]},
                "into_prior": {"reportable": tgt["reportable"],
                               "tin_last4": tgt["tin_last4"]}}]
            tgt["reportable"] = tgt["reportable"] or m["reportable"]
            tgt["tin_last4"] = tgt["tin_last4"] or m["tin_last4"]
            conn.execute("DELETE FROM vendor_1099 WHERE id = %s", (m["id"],))
            changed += 1
        from .compat import jsonb
        # another merchant's mark that happens to share the name keeps it;
        # this one is found by its merchant either way
        clash = conn.execute(
            "SELECT 1 FROM vendor_1099 WHERE entity_id = %s AND merchant = %s "
            "AND id <> %s", (tgt["entity_id"], sname, tgt["id"])).fetchone()
        conn.execute(
            "UPDATE vendor_1099 SET merchant = %s, merchant_id = %s, "
            "reportable = %s, tin_last4 = %s, merged_from = %s WHERE id = %s",
            (tgt["merchant"] if clash else sname, sid, tgt["reportable"],
             tgt["tin_last4"], jsonb(tgt["merged_from"] or []), tgt["id"]))
        changed += 1
    return changed


def unfollow_merchant_rename(conn, raw_key: str, into: str) -> int:
    """The undo of follow_merchant_rename for one raw string whose rename
    to `into` was just reversed. A mark that only moved goes back to the
    merchant the string is filed under again; a mark that was folded into
    another comes back as it was, and the one it folded into gets its own
    flags back — once no other string of that merge still files there (an
    undo runs one string at a time). Names follow merchants: a plain
    rename's mark simply takes the restored name. Returns marks changed."""
    from .bills import _merchant_of_raw
    from .compat import jsonb
    back = _merchant_of_raw(conn, raw_key)
    if back is None:
        return 0
    bid, bname = back
    changed = 0
    for m in conn.execute(
            "SELECT id, entity_id, merchant, merchant_id::text AS mid, "
            "reportable, tin_last4, merged_from FROM vendor_1099 "
            "WHERE merged_from @> %s OR merchant_id::text = %s "
            "ORDER BY created_at, id FOR UPDATE",
            (jsonb([{"raws": [raw_key]}]), bid)).fetchall():
        notes = list(m["merged_from"] or [])
        mine = [n for n in notes if raw_key in (n.get("raws") or [])]
        if not mine:
            # the merchant itself was renamed back: its mark takes the name
            if m["mid"] == bid and m["merchant"] != bname:
                taken = conn.execute(
                    "SELECT 1 FROM vendor_1099 WHERE entity_id = %s "
                    "AND merchant = %s AND id <> %s",
                    (m["entity_id"], bname, m["id"])).fetchone()
                if not taken:
                    conn.execute("UPDATE vendor_1099 SET merchant = %s "
                                 "WHERE id = %s", (bname, m["id"]))
                    changed += 1
            continue
        keep = [n for n in notes if n not in mine]
        rep, tin = m["reportable"], m["tin_last4"]
        mid, name = m["mid"], m["merchant"]
        for n in mine:
            left = [r for r in n.get("raws") or [] if r != raw_key
                    and (_merchant_of_raw(conn, r) or ("",))[0] == m["mid"]]
            if left:
                keep.append({**n, "raws": left})
                continue
            if n.get("had_into"):
                prior = n.get("into_prior") or {}
                rep = bool(prior.get("reportable", rep))
                tin = prior.get("tin_last4", tin)
                was = n.get("mark") or {}
                conn.execute(
                    """INSERT INTO vendor_1099 (entity_id, merchant,
                           merchant_id, reportable, tin_last4)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (m["entity_id"], bname, bid,
                     bool(was.get("reportable", True)), was.get("tin_last4")))
            else:
                mid, name = bid, bname
        if name != m["merchant"] and conn.execute(
                "SELECT 1 FROM vendor_1099 WHERE entity_id = %s "
                "AND merchant = %s AND id <> %s",
                (m["entity_id"], name, m["id"])).fetchone():
            name = m["merchant"]
        conn.execute(
            "UPDATE vendor_1099 SET merchant = %s, merchant_id = %s, "
            "reportable = %s, tin_last4 = %s, merged_from = %s WHERE id = %s",
            (name, mid, rep, tin, jsonb(keep), m["id"]))
        changed += 1
    return changed
