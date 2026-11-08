"""Debt payoff planner — every card and loan on one schedule.

The cash forecast (forecast.py) answers "does this month's money reach the
next paycheck", and pays each card off in full at its due date to find
out. This module answers the longer question a household with a balance
that does not clear each month actually asks: **when is the last payment,
and what does the order cost?**

The model is the ordinary planner arithmetic, made explicit:

  * Every open debt accrues one month of simple interest on its balance
    (APR / 12), then receives its minimum payment.
  * One monthly POOL is on top: the household's chosen extra, plus the
    minimum of every debt already retired (the "rolling" payment that
    makes the plan accelerate). The pool goes to ONE target at a time —
    the next debt in the method's order — and spills over to the next
    target the month the first one clears.
  * **avalanche** orders by APR, highest first (least interest paid);
    **snowball** orders by balance, smallest first (the earliest visible
    win). Ties fall back to the other key. The order is fixed on day one
    from the balances the plan starts with, as the method is usually
    taught; the two schedules are always computed together so the page
    can show what the preference costs.
  * A minimum that does not cover the month's interest lets the balance
    grow; the walk stops at MAX_MONTHS and reports no debt-free date
    rather than searching forever. Such a schedule carries
    `finishes: False`, and its interest total — fifty years of compounding
    on a balance nobody will carry that long — is not a figure to quote.

Where the inputs come from, in order: the aggregator's liabilities pull
(a card's purchase APR and minimum payment, a mortgage's rate and the
principal-and-interest its term implies — see load_debts on escrow — a
student loan's rate), then the household's own overrides saved
with the plan, then an ESTIMATE the page labels as such — for a card the
month's interest plus 1% of the balance (at least $25), the way issuers
compute a minimum, a 5-year amortization for another loan, 30 years for a
mortgage. Every estimate covers the month's interest, so an estimated plan
always finishes; a flat percentage would not, since 2% of the balance
falls short of the month's interest once the APR passes 24%. The estimate
exists so the page is never blank
for a self-hoster whose bank sends no liabilities data; it is shown as an
estimate so it gets corrected, not trusted.

Minimum payments are held constant for the whole walk. A card issuer's
real minimum shrinks with the balance, which stretches the minimums-only
baseline out for decades; holding it flat is the conservative reading of
"keep paying what you pay now", and the comparison the page draws is
against that, not against the issuer's floor.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
import re

from .forecast import CARD_SCOPE_SQL

METHODS = ("avalanche", "snowball")
MAX_MONTHS = 600          # fifty years: past this the answer is "never"
MAX_EXTRA = 1_000_000.0
MAX_APR = 100.0
MAX_PAYMENT = 10_000_000.0
# the plan document's per-debt map is bounded so a hand-edited settings
# blob cannot carry an unbounded dictionary into every planner request
MAX_OVERRIDES = 200
PLAN_KEY = "debt_plan"

# loan subtypes the estimate amortizes as a mortgage (30y) rather than a
# five-year loan; Plaid's `subtype` for a home loan is "mortgage",
# MX maps its MORTGAGE type onto the same word (sync/mx.py)
_MORTGAGE_SUBTYPES = ("mortgage", "home equity", "home")


def _typed(v: str) -> float | None:
    """A hand-typed plan figure as a number, or None when it is not one.
    Either separator convention is read the way the CSV importer reads it
    ("1,234.56", "1.234,56", "12,50"): stripping every comma would save a
    decimal-comma "12,50" extra payment as 1250 a month and a "19,99" APR
    as 1999 % — the first passes the range check, with nothing to say so.
    A blank is the caller's to handle."""
    from ..sync.csvimport import parse_money_text
    try:
        return parse_money_text(v)
    except ValueError:
        return None


def _num(v) -> float | None:
    """A finite number, or None. `True` is an int to float() and a
    restored blob can carry "N/A" — neither is a rate or a payment."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: a JSON integer too wide for a double
        return None
    return f if math.isfinite(f) else None


def issuer_apr(raw: dict | None, subtype: str | None) -> float | None:
    """The rate the aggregator reported for this debt, as a percentage.

    A card carries `aprs[]` — purchase / cash / balance-transfer, each
    with the balance it applies to. Purchase is the one a revolving
    balance pays; failing that, the rate with money under it, then the
    highest. A mortgage carries `interest_rate.percentage`, a student loan
    `interest_rate_percentage`; both are also accepted flat, so a hand
    restore that wrote the number at the top level reads the same way.
    Out-of-range values (negative, over 100%) read as absent: they are
    data errors, not rates."""
    if not isinstance(raw, dict):
        return None
    cands: list[float] = []
    aprs = raw.get("aprs")
    if isinstance(aprs, list):
        purchase = None
        with_bal: list[tuple[float, float]] = []
        for a in aprs:
            if not isinstance(a, dict):
                continue
            pct = _num(a.get("apr_percentage"))
            if pct is None:
                continue
            if a.get("apr_type") == "purchase_apr" and purchase is None:
                purchase = pct
            bal = _num(a.get("balance_subject_to_apr")) or 0.0
            with_bal.append((bal, pct))
        if purchase is not None:
            cands.append(purchase)
        elif with_bal:
            with_bal.sort(reverse=True)
            cands.append(with_bal[0][1] if with_bal[0][0] > 0
                         else max(p for _, p in with_bal))
    ir = raw.get("interest_rate")
    if isinstance(ir, dict):
        pct = _num(ir.get("percentage"))
        if pct is not None:
            cands.append(pct)
    for key in ("interest_rate_percentage", "apr", "apr_percentage"):
        pct = _num(raw.get(key))
        if pct is not None:
            cands.append(pct)
    for pct in cands:
        if 0.0 <= pct <= MAX_APR:
            return pct
    return None


def issuer_minimum(raw: dict | None) -> float | None:
    """The monthly payment the aggregator reported: a card's minimum, a
    mortgage's next monthly payment. Zero or negative is "not reported"."""
    if not isinstance(raw, dict):
        return None
    for key in ("minimum_payment_amount", "next_monthly_payment"):
        v = _num(raw.get(key))
        if v is not None and 0.0 < v <= MAX_PAYMENT:
            return v
    return None


_TERM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(y|m)", re.I)


def _iso_date(v) -> dt.date | None:
    if isinstance(v, dt.date):
        return v
    if not isinstance(v, str):
        return None
    try:
        return dt.date.fromisoformat(v.strip()[:10])
    except ValueError:
        return None


def remaining_term_months(raw: dict | None, today: dt.date) -> int | None:
    """Payments left on an amortizing loan, from what the servicer sent:
    the maturity date (a student loan's expected payoff date), else the
    origination date plus the loan term ("30 year", "360 months"). None
    when none is there or the loan is already past its end — the caller
    then estimates."""
    if not isinstance(raw, dict):
        return None
    end = (_iso_date(raw.get("maturity_date"))
           or _iso_date(raw.get("expected_payoff_date")))
    if end is not None:
        left = (end.year - today.year) * 12 + (end.month - today.month)
    else:
        start = _iso_date(raw.get("origination_date"))
        m = _TERM_RE.search(str(raw.get("loan_term") or ""))
        if start is None or m is None:
            return None
        n = float(m.group(1)) * (12 if m.group(2).lower() == "y" else 1)
        if not 0 < n <= MAX_MONTHS:
            return None
        # counted in months, never through a date: an origination near
        # the calendar's last year plus the term is past what a date can
        # hold, and building one would fail the whole plan on one bad row
        left = ((start.year - today.year) * 12 + (start.month - today.month)
                + int(round(n)))
    return left if 0 < left <= MAX_MONTHS else None


def _up_to_cent(x: float) -> float:
    """Up to the cent, as a servicer rounds a payment: rounded down, the
    schedule leaves a few dollars for one payment past the term."""
    return math.ceil(x * 100 - 1e-6) / 100


def term_payment(raw: dict | None, balance: float, apr: float,
                 today: dt.date) -> float | None:
    """A non-mortgage loan's monthly payment over the payments its servicer
    says are left, or None when it sent no term — or too short a term to
    plan by. A loan reporting no payment a month or two from its payoff
    date (a student loan in deferment reports $0) is not going to be paid
    off in one or two instalments of half the balance; the caller's
    estimate is the safer minimum there."""
    left = remaining_term_months(raw, today)
    if left is None or left <= TERM_PAYMENT_MIN_MONTHS:
        return None
    return _up_to_cent(amortized_payment(balance, apr, left))


# fewer payments left than this, and a payment worked out from the term is
# most of the balance: no minimum to plan by
TERM_PAYMENT_MIN_MONTHS = 6


def mortgage_principal_and_interest(raw: dict | None, balance: float,
                                    apr: float, today: dt.date) -> float | None:
    """A mortgage's monthly principal and interest from what the servicer
    sent, or None when it sent too little to tell.

    With a maturity (or origination plus term) the balance is amortized
    over the payments left — at 0% (no rate sent) that is balance / months
    left, a lower bound that still ends the loan on its real date. With no
    term but the original principal and a rate, it is the original 30-year
    schedule's payment, which is what a seasoned loan still pays. What it
    never does is amortize TODAY's balance over a fresh 30 years: on a loan
    ten years in that understates the payment by a third and adds ten
    years and tens of thousands in interest to the plan."""
    up = _up_to_cent
    left = remaining_term_months(raw, today)
    if left is not None:
        return up(amortized_payment(balance, apr, left))
    orig = _num(raw.get("origination_principal_amount")) \
        if isinstance(raw, dict) else None
    if orig is not None and orig > 0 and apr > 0:
        return up(amortized_payment(orig, apr, 360))
    return None


def debt_kind(acct_type: str | None, subtype: str | None) -> str:
    if acct_type == "credit":
        return "card"
    sub = (subtype or "").lower()
    if any(sub.startswith(m) for m in _MORTGAGE_SUBTYPES):
        return "mortgage"
    return "loan"


def amortized_payment(balance: float, apr: float, months: int) -> float:
    r = apr / 1200.0
    if months <= 0:
        return balance
    # 1 - (1 + r)^-n through log1p/expm1: for a rate so small that 1 + r
    # rounds to 1 the plain form divides by zero and fails the plan
    denom = -math.expm1(-months * math.log1p(r)) if r > 0 else 0.0
    if denom <= 0:
        return balance / months
    return balance * r / denom


def estimated_minimum(kind: str, balance: float, apr: float) -> float:
    """What a debt with no reported minimum is assumed to be paid each
    month. Labelled an estimate wherever it is shown."""
    if kind == "card":
        # interest plus 1% of the balance, as issuers set it: a flat share
        # of the balance falls below the interest at a high enough rate,
        # and the plan built on it never ends. Never under 2% either: an
        # unknown rate reads as 0, and the 1% alone would then tell a
        # household (and the continuity packet) to pay a third of what a
        # typical card asks
        return min(balance, max(25.0, balance * apr / 1200.0 + 0.01 * balance,
                                0.02 * balance))
    if kind == "mortgage":
        return min(balance, amortized_payment(balance, apr, 360))
    return min(balance, amortized_payment(balance, apr, 60))


# ---- the plan document -----------------------------------------------------

def normalize_plan(body) -> dict:
    """The saved plan in its one shape, or ValueError with a sentence.

    {"method": "avalanche"|"snowball", "extra_monthly": float,
     "debts": {account_id: {"apr": float|None, "min_payment": float|None,
                            "skip": bool}}}
    Blank strings and nulls clear an override; a field the caller omits
    is left absent (the reader treats absent as "no override")."""
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ValueError("the plan must be an object")
    out: dict = {}
    method = body.get("method", "avalanche")
    if method not in METHODS:
        raise ValueError("method must be avalanche or snowball")
    out["method"] = method
    extra = body.get("extra_monthly", 0)
    if isinstance(extra, str):
        extra = _typed(extra) if extra.strip() else 0.0
    ev = _num(extra)
    if ev is None or ev < 0 or ev > MAX_EXTRA:
        raise ValueError("extra monthly payment must be a number from 0 "
                         "to 1,000,000")
    out["extra_monthly"] = round(ev, 2)
    debts = body.get("debts")
    if debts is None:
        debts = {}
    if not isinstance(debts, dict):
        raise ValueError("debts must be an object keyed by account id")
    if len(debts) > MAX_OVERRIDES:
        raise ValueError(f"at most {MAX_OVERRIDES} debts can carry overrides")
    clean: dict = {}
    for aid, ov in debts.items():
        if not isinstance(aid, str) or not aid or len(aid) > 128:
            raise ValueError("account ids must be short strings")
        if ov is None:
            continue
        if not isinstance(ov, dict):
            raise ValueError("each debt override must be an object")
        row: dict = {}
        for key, hi, what in (("apr", MAX_APR, "APR"),
                              ("min_payment", MAX_PAYMENT, "minimum payment")):
            if key not in ov:
                continue
            v = ov[key]
            if isinstance(v, str):
                v = v.replace("%", "").strip()
                if v:
                    v = _typed(v)
                    if v is None:
                        raise ValueError(f"{what} must be a number from 0 "
                                         f"to {hi:g}")
            if v is None or v == "":
                row[key] = None
                continue
            f = _num(v)
            if f is None or f < 0 or f > hi:
                raise ValueError(f"{what} must be a number from 0 to {hi:g}")
            row[key] = round(f, 2)
        if "skip" in ov:
            row["skip"] = bool(ov["skip"])
        if row:
            clean[aid] = row
    out["debts"] = clean
    return out


def plan_settings(cfg: dict) -> dict:
    """The saved plan, read defensively: a settings blob is a document a
    restore writes verbatim, so a malformed value reads as the default
    rather than failing every planner request."""
    try:
        return normalize_plan(cfg.get(PLAN_KEY))
    except ValueError:
        return normalize_plan({})


# ---- the debts -------------------------------------------------------------

def load_debts(conn, cfg: dict, plan: dict | None = None, *,
               today: dt.date | None = None) -> list[dict]:
    """Every card and loan carrying a balance, in the scope every report
    uses (the Accounts page's exclusions, entity and shadow accounts
    dropped), with its rate and minimum resolved: issuer → the saved
    override → an estimate, each remembered as `apr_source` /
    `min_source` so the page can say which.

    A mortgage is the exception to "the issuer's payment": what a servicer
    reports as the monthly payment carries the escrow (property tax and
    insurance), which never reduces the loan, and walking all of it
    against the balance retires a 30-year loan in about half the time and
    quotes about half the interest. So a mortgage's minimum is its
    principal and interest where the servicer sent enough to derive it
    (mortgage_principal_and_interest), never more than the reported
    payment; with too little to derive it, the reported payment stands —
    an upper bound, where a fresh 30-year amortization of today's balance
    would understate a seasoned loan by years. The reported payment is
    carried as `payment_reported` so the page can show what the servicer
    collects."""
    from . import budget
    today = today or dt.date.today()
    plan = plan if plan is not None else plan_settings(cfg)
    overrides = plan.get("debts") or {}
    excluded = budget.excluded_account_ids(cfg)
    rows = conn.execute(
        f"""SELECT a.id, COALESCE(a.display_name, a.name) AS name,
                  a.type, a.subtype, a.mask,
                  COALESCE(a.balance_current, 0) AS bal,
                  i.institution_name AS institution,
                  l.raw AS lraw
           FROM accounts a
           LEFT JOIN items i ON i.id = a.item_id
           LEFT JOIN liabilities l ON l.account_id = a.id
           WHERE a.type IN ('credit', 'loan')
             AND COALESCE(a.balance_current, 0) > 0.5
             {CARD_SCOPE_SQL}
           ORDER BY a.type, name, a.id
        """, (excluded,)).fetchall()
    out: list[dict] = []
    for r in rows:
        kind = debt_kind(r["type"], r["subtype"])
        bal = round(float(r["bal"]), 2)
        ov = overrides.get(r["id"]) or {}
        raw = r["lraw"] if isinstance(r["lraw"], dict) else None
        apr_issuer = issuer_apr(raw, r["subtype"])
        min_issuer = issuer_minimum(raw)
        if ov.get("apr") is not None:
            apr, apr_src = float(ov["apr"]), "you"
        elif apr_issuer is not None:
            apr, apr_src = apr_issuer, "issuer"
        else:
            apr, apr_src = 0.0, "none"
        reported = None
        # what the servicer's own term says the loan pays: a mortgage's
        # principal and interest, another loan's balance amortized over the
        # payments left. Derived whether or not a payment was reported — a
        # loan that sent its maturity but no payment is planned to end on
        # that maturity, not on a fresh 30 or 5 years
        if kind == "mortgage":
            termed = mortgage_principal_and_interest(raw, bal, apr, today)
        elif kind == "loan":
            termed = term_payment(raw, bal, apr, today)
        else:
            termed = None
        if kind == "mortgage" and min_issuer is not None:
            reported = min_issuer
            if termed is not None:
                min_issuer = min(termed, reported)
        if ov.get("min_payment") is not None:
            mp, min_src = float(ov["min_payment"]), "you"
        elif min_issuer is not None:
            mp, min_src = min_issuer, "issuer"
        elif termed is not None:
            mp, min_src = termed, "estimate"
        else:
            mp, min_src = estimated_minimum(kind, bal, apr), "estimate"
        out.append({
            "id": r["id"], "name": r["name"], "kind": kind,
            "mask": r["mask"], "institution": r["institution"],
            "balance": bal,
            "apr": round(apr, 3), "apr_source": apr_src,
            "apr_issuer": apr_issuer,
            "min_payment": round(min(mp, bal), 2), "min_source": min_src,
            "min_issuer": min_issuer,
            "payment_reported": reported,
            "skip": bool(ov.get("skip")),
        })
    return out


# ---- the walk --------------------------------------------------------------

def _add_months(day: dt.date, n: int) -> dt.date:
    y, m = divmod(day.month - 1 + n, 12)
    y += day.year
    m += 1
    return dt.date(y, m, min(day.day, calendar.monthrange(y, m)[1]))


def order_for(debts: list[dict], method: str) -> list[dict]:
    """The payoff order the method dictates, fixed from today's balances."""
    if method == "snowball":
        key = lambda d: (d["balance"], -d["apr"], d["name"], d["id"])
    else:
        key = lambda d: (-d["apr"], d["balance"], d["name"], d["id"])
    return sorted(debts, key=key)


def simulate(debts: list[dict], *, method: str = "avalanche",
             extra: float = 0.0, start: dt.date, rollover: bool = True,
             max_months: int = MAX_MONTHS) -> dict:
    """Walk the debts month by month until the last one clears.

    `start` is today; the first payment lands on the first of next month
    and every point in `series` is a month's first. `rollover=False` is
    the minimums-only baseline: no extra, and a retired debt's minimum is
    not redirected — the comparison the page draws against."""
    if method not in METHODS:
        raise ValueError("method must be avalanche or snowball")
    live = [dict(d) for d in debts if not d.get("skip") and d["balance"] > 0.005]
    ordered = order_for(live, method)
    bal = {d["id"]: float(d["balance"]) for d in ordered}
    paid_off: dict[str, str | None] = {d["id"]: None for d in ordered}
    interest_by: dict[str, float] = {d["id"]: 0.0 for d in ordered}
    paid_by: dict[str, float] = {d["id"]: 0.0 for d in ordered}
    first = _add_months(start.replace(day=1), 1)
    total0 = sum(bal.values())
    series: list[tuple[str, float]] = [(start.isoformat(), round(total0, 2))]
    months = 0
    total_interest = 0.0
    total_paid = 0.0
    freed = 0.0             # minimums of retired debts, rolling forward
    peak = total0
    debt_free: dt.date | None = None
    if total0 <= 0.005:
        debt_free = start
    while debt_free is None and months < max_months:
        months += 1
        day = _add_months(first, months - 1)
        pool = (float(extra) + freed) if rollover else 0.0
        # interest first, then every minimum
        for d in ordered:
            b = bal[d["id"]]
            if b <= 0.005:
                continue
            i = b * d["apr"] / 1200.0
            b += i
            interest_by[d["id"]] += i
            total_interest += i
            pay = min(d["min_payment"], b)
            b -= pay
            paid_by[d["id"]] += pay
            total_paid += pay
            # a minimum larger than what was left joins this month's pool
            if rollover:
                pool += d["min_payment"] - pay
            bal[d["id"]] = b
        # the pool to the target, spilling to the next when one clears
        for d in ordered:
            if pool <= 0.005:
                break
            b = bal[d["id"]]
            if b <= 0.005:
                continue
            pay = min(pool, b)
            b -= pay
            pool -= pay
            paid_by[d["id"]] += pay
            total_paid += pay
            bal[d["id"]] = b
        # retire what cleared this month; its minimum rolls from next month
        for d in ordered:
            if bal[d["id"]] <= 0.005 and paid_off[d["id"]] is None:
                paid_off[d["id"]] = day.isoformat()
                bal[d["id"]] = 0.0
                freed += d["min_payment"]
        total = sum(bal.values())
        peak = max(peak, total)
        series.append((day.isoformat(), round(total, 2)))
        if total <= 0.005:
            debt_free = day
    per = []
    for rank, d in enumerate(ordered, 1):
        per.append({"id": d["id"], "order": rank,
                    "paid_off": paid_off[d["id"]],
                    "interest": round(interest_by[d["id"]], 2),
                    "paid": round(paid_by[d["id"]], 2),
                    "remaining": round(bal[d["id"]], 2)})
    monthly = sum(d["min_payment"] for d in ordered) + (float(extra) if rollover else 0.0)
    return {
        "method": method,
        "extra": round(float(extra), 2) if rollover else 0.0,
        "months": months if debt_free is not None else None,
        "debt_free": debt_free.isoformat() if debt_free is not None else None,
        "total_interest": round(total_interest, 2),
        "total_paid": round(total_paid, 2),
        "monthly": round(monthly, 2),
        "starting": round(total0, 2),
        "growing": peak > total0 + 0.005 and debt_free is None,
        # False when the walk hit max_months: total_interest / total_paid
        # are then a cut-off figure, never "what the plan costs"
        "finishes": debt_free is not None,
        "series": series,
        "debts": per,
    }


def build(conn, today: dt.date, *, method: str | None = None,
          extra: float | None = None) -> dict:
    """The page's payload: the debts with their resolved inputs, the plan
    under the chosen method, the same money under the other method, and
    the minimums-only baseline. `method` / `extra` override the saved
    plan for a what-if without saving it."""
    from . import budget
    cfg = budget.load_config(conn)
    plan = plan_settings(cfg)
    method = method if method in METHODS else plan["method"]
    if extra is None or not math.isfinite(extra):
        extra = plan["extra_monthly"]
    extra = max(0.0, min(float(extra), MAX_EXTRA))
    debts = load_debts(conn, cfg, plan, today=today)
    other = "snowball" if method == "avalanche" else "avalanche"
    chosen = simulate(debts, method=method, extra=extra, start=today)
    alt = simulate(debts, method=other, extra=extra, start=today)
    base = simulate(debts, method=method, extra=0.0, start=today,
                    rollover=False)
    return {
        "today": today.isoformat(),
        "method": method,
        "extra_monthly": round(extra, 2),
        "saved": {"method": plan["method"],
                  "extra_monthly": plan["extra_monthly"]},
        "debts": debts,
        # every stored override, listed debt or not: the planner lists only
        # debts carrying a balance, and the save replaces the whole plan, so
        # a client rebuilding the map from the list alone would erase the
        # rate or skip mark of a card paid to zero this cycle
        "overrides": plan["debts"],
        "plan": chosen,
        "alt": alt,
        "minimums": base,
        "estimated": any(d["min_source"] == "estimate" or
                         d["apr_source"] == "none"
                         for d in debts if not d["skip"]),
    }
