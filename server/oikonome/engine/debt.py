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

# The rate a debt is walked at when neither the issuer nor the household
# has given one, by what kind of debt it is. A plan walked at 0% quotes a
# debt-free date that is too early and no interest at all, which reads as
# good news; a typical rate errs toward a debt that costs more, and the
# page marks it an estimate and asks for the real figure.
TYPICAL_APR = {"card": 22.0, "mortgage": 6.5, "auto": 7.5, "student": 6.5,
               "loan": 10.0}


def typical_apr(kind: str, subtype: str | None) -> float:
    """The typical rate for a debt of this kind (TYPICAL_APR): a loan's
    subtype picks the car or student rate where it names one."""
    if kind == "loan":
        sub = (subtype or "").lower()
        for word in ("auto", "student"):
            if sub.startswith(word):
                return TYPICAL_APR[word]
    return TYPICAL_APR.get(kind, TYPICAL_APR["loan"])


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


def _last_payment(raw: dict | None) -> tuple[int, int] | None:
    """The servicer's last payment as (months from year zero, day of
    month): the maturity date (a student loan's expected payoff date),
    else the origination date plus the loan term, paid on the
    origination's day. Kept in months, never built into a date: an
    origination near the calendar's last year plus the term is past what
    a date can hold, and building one would fail the whole plan on one
    bad row."""
    if not isinstance(raw, dict):
        return None
    end = (_iso_date(raw.get("maturity_date"))
           or _iso_date(raw.get("expected_payoff_date")))
    if end is not None:
        return end.year * 12 + end.month - 1, end.day
    start = _iso_date(raw.get("origination_date"))
    m = _TERM_RE.search(str(raw.get("loan_term") or ""))
    if start is None or m is None:
        return None
    n = float(m.group(1)) * (12 if m.group(2).lower() == "y" else 1)
    if not 0 < n <= MAX_MONTHS:
        return None
    return start.year * 12 + start.month - 1 + int(round(n)), start.day


def _due_through(day: int, today: dt.date) -> bool:
    """Whether a payment falling on `day` of the month is still ahead (or
    is today) — a day past this month's end falls on its last day."""
    return today.day <= min(day, calendar.monthrange(today.year,
                                                     today.month)[1])


def _this_months_payment_due(raw: dict, day: int, today: dt.date) -> bool:
    """Whether this month's payment is still to be made. The servicer's
    own dates decide when it sent them, and its next due date speaks
    first: a due date later this month (or today) is still owed whatever
    was paid earlier in the month (an extra principal payment, a late
    one for last month, half of a biweekly pair), and a due date in a
    later month means this month is done. With no usable due date, a
    payment made this month is in the balance already. Only when it sent
    neither (or nothing that reads as a date) does the schedule's payment
    day decide — and the maturity date's day of the month is often not
    the due day, so it is the fallback, never the first word."""
    month = (today.year, today.month)
    due = _iso_date(raw.get("next_payment_due_date"))
    if due is not None:
        if (due.year, due.month) == month:
            return due >= today
        if due > today:
            return False
    paid = _iso_date(raw.get("last_payment_date"))
    if paid is not None and (paid.year, paid.month) == month \
            and paid <= today:
        return False
    return _due_through(day, today)


def remaining_term_months(raw: dict | None, today: dt.date) -> int | None:
    """Payments left on an amortizing loan, from what the servicer sent.
    The maturity (or payoff) date is the day of the LAST payment, so its
    month counts, and this month's payment counts while it is still owed
    (_this_months_payment_due) — on its due day itself it is still owed.
    None when the servicer sent no term or the last payment has passed —
    the caller then estimates."""
    last = _last_payment(raw)
    if last is None:
        return None
    end, day = last
    left = end - (today.year * 12 + today.month - 1)
    if _this_months_payment_due(raw, day, today):
        left += 1
    return left if 0 < left <= MAX_MONTHS else None


def payment_still_due(raw: dict | None, today: dt.date) -> bool:
    """Whether this month's scheduled payment is still to be made: the
    loan has a dated term that has not ended and the payment is still
    owed by the same predicate remaining_term_months counts with. The
    walk then pays it this month (simulate's `due_now`), so the payments
    counted end on the servicer's date rather than a month past it."""
    last = _last_payment(raw)
    return (last is not None and remaining_term_months(raw, today) is not None
            and _this_months_payment_due(raw, last[1], today))


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
        # and the plan built on it never ends. Never under 2% either: at
        # a rate near 0 the 1% alone would tell a household (and the
        # continuity packet) to pay a third of what a typical card asks
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


def _rate_within_payment(kind: str, balance: float, payment: float,
                         apr: float) -> float:
    """A typical rate, lowered until a KNOWN payment retires the debt.

    The typical rate is a guess, the payment is a fact. A mortgage written
    at 3% pays less each month than the interest a 6.5% guess would
    charge, and the walk would then call a loan that is being paid down on
    schedule one that never ends. So where the payment does not retire
    the balance within thirty years at the typical rate, the rate is the
    highest one at which it does. One rule for every kind of debt, cards
    included: a rate that changed with a rule at some payment size would
    jump there, and a cent more of payment would cut the quoted interest
    to nothing. Thirty years is the bound, not the kind's usual term: a
    five-year bound would call any car or student loan with more than five
    years left a 0% loan, which is the good news the typical rate exists
    to stop printing.

    A payment that would not retire the debt in thirty years even at 0%
    is no evidence about the rate at all: the typical rate stands, and
    the walk reports a debt those payments never clear."""
    if balance <= 0 or payment <= 0:
        return apr
    months = 360
    if amortized_payment(balance, apr, months) <= payment:
        return apr
    if amortized_payment(balance, 0.0, months) > payment:
        return apr
    lo, hi = 0.0, apr
    for _ in range(40):
        mid = (lo + hi) / 2
        if amortized_payment(balance, mid, months) <= payment:
            lo = mid
        else:
            hi = mid
    return round(lo, 3)


# ---- the debts -------------------------------------------------------------

def load_debts(conn, cfg: dict, plan: dict | None = None, *,
               today: dt.date | None = None) -> list[dict]:
    """Every card and loan carrying a balance, in the scope every report
    uses (the Accounts page's exclusions, entity and shadow accounts
    dropped), with its rate and minimum resolved: issuer → the saved
    override → an estimate (for a rate, the typical one for that kind of
    debt), each remembered as `apr_source` / `min_source` so the page can
    say which.

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
        got = _resolve(kind, r["subtype"], bal, raw, ov, today)
        # what the plan walks with no override from the household: the
        # figure the page's empty boxes show in their place
        bare = got if not ov else _resolve(kind, r["subtype"], bal, raw,
                                           {}, today)
        out.append({
            "id": r["id"], "name": r["name"], "kind": kind,
            "mask": r["mask"], "institution": r["institution"],
            "balance": bal,
            "apr": got["apr"], "apr_source": got["apr_source"],
            "apr_issuer": got["apr_issuer"],
            "apr_default": bare["apr"],
            "min_payment": got["min_payment"],
            "min_source": got["min_source"],
            "min_issuer": got["min_issuer"],
            "min_default": bare["min_payment"],
            "payment_reported": got["payment_reported"],
            # walked at a typical rate or an estimated minimum: the page
            # labels it so it gets corrected, not trusted
            "estimated": got["apr_source"] == "typical"
                         or got["min_source"] == "estimate",
            "skip": bool(ov.get("skip")),
            "due_now": kind in ("loan", "mortgage")
                       and payment_still_due(raw, today),
        })
    return out


def card_aprs(conn, cfg: dict, cards: list[tuple[str, str | None, float]],
              *, today: dt.date | None = None) -> dict[str, float]:
    """The rate the Debt page walks each of these cards at — issuer, the
    household's override, or the typical rate (`_resolve`) — for cards
    the caller scoped itself (the retirement projection counts a card the
    payoff plan leaves out). `cards` is (account id, subtype, balance)."""
    today = today or dt.date.today()
    overrides = plan_settings(cfg).get("debts") or {}
    ids = [c[0] for c in cards]
    raws = {r["account_id"]: r["raw"] for r in conn.execute(
        "SELECT account_id, raw FROM liabilities WHERE account_id = ANY(%s)",
        (ids,)).fetchall()} if ids else {}
    out = {}
    for aid, sub, bal in cards:
        raw = raws.get(aid)
        out[aid] = _resolve("card", sub, bal,
                            raw if isinstance(raw, dict) else None,
                            overrides.get(aid) or {}, today)["apr"]
    return out


def _resolve(kind: str, subtype: str | None, bal: float, raw: dict | None,
             ov: dict, today: dt.date) -> dict:
    """One debt's rate and minimum: issuer → the household's override `ov`
    → an estimate, each with where it came from (see load_debts)."""
    apr_issuer = issuer_apr(raw, subtype)
    min_issuer = issuer_minimum(raw)
    if ov.get("apr") is not None:
        apr, apr_src = float(ov["apr"]), "you"
    elif apr_issuer is not None:
        apr, apr_src = apr_issuer, "issuer"
    else:
        apr, apr_src = typical_apr(kind, subtype), "typical"
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
    if apr_src == "typical" and min_src != "estimate":
        apr = _rate_within_payment(kind, bal, min(mp, bal), apr)
    return {"apr": round(apr, 3), "apr_source": apr_src,
            "apr_issuer": apr_issuer,
            "min_payment": round(min(mp, bal), 2), "min_source": min_src,
            "min_issuer": min_issuer, "payment_reported": reported}


# ---- what the plan walks (specs/debt-plan.md) -------------------------------

# the window a card's own ledger is read over for "paid in full"
PAID_WINDOW_DAYS = 90

# an interest charge on a card, as the feeds name it
_INTEREST_SQL = (
    "(COALESCE(t.category_detailed, '') = 'BANK_FEES_INTEREST_CHARGE'"
    " OR t.name ILIKE '%%interest charge%%'"
    " OR t.name ~* '^(purchase |cash advance |cash )?interest\\M')")
# a payment INTO a card (a negative row on the card account)
_CARD_PAYMENT_SQL = (
    "(COALESCE(t.category_detailed, '') = 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'"
    " OR COALESCE(t.category_override, t.category_primary, '')"
    "    IN ('TRANSFER_IN', 'LOAN_PAYMENTS')"
    " OR t.name ILIKE '%%payment%%')")


def card_ledger(conn, ids: list[str], today: dt.date) -> dict[str, dict]:
    """Per card: the first day of its ledger, whether interest was charged
    in the window, what was paid into it over the window and in how many
    calendar months."""
    if not ids:
        return {}
    since = today - dt.timedelta(days=PAID_WINDOW_DAYS)
    rows = conn.execute(
        f"""SELECT t.account_id AS id, MIN(t.date) AS first,
                  BOOL_OR(t.amount > 0 AND t.date >= %s
                          AND {_INTEREST_SQL}) AS interest,
                  COALESCE(SUM(-t.amount) FILTER (
                      WHERE t.amount < 0 AND t.date >= %s
                        AND {_CARD_PAYMENT_SQL}), 0) AS paid,
                  COUNT(DISTINCT date_trunc('month', t.date)) FILTER (
                      WHERE t.amount < 0 AND t.date >= %s
                        AND {_CARD_PAYMENT_SQL}) AS months
             FROM transactions t
            WHERE t.removed = 0 AND t.account_id = ANY(%s)
            GROUP BY t.account_id""",
        (since, since, since, ids)).fetchall()
    out = {}
    for r in rows:
        out[r["id"]] = {"first": _iso_date(r["first"]), "interest": bool(r["interest"]),
                        "paid": float(r["paid"] or 0), "months":
                        int(r["months"] or 0)}
    return out


def paid_in_full(raw: dict | None, ledger: dict | None, balance: float,
                 today: dt.date) -> dict | None:
    """Whether a card is cleared every month, and on what evidence, or
    None when it carries a balance from month to month
    (specs/debt-plan.md). Interest charged in the window always says it
    revolves; then the issuer's last statement decides when it sent one,
    and the card's own ledger when it did not."""
    led = ledger or {}
    if led.get("interest"):
        return None
    raw = raw if isinstance(raw, dict) else {}
    stmt = _num(raw.get("last_statement_balance"))
    last = _num(raw.get("last_payment_amount"))
    if stmt is not None:
        if stmt <= 0.005:
            return {"basis": "issuer", "statement_balance": round(stmt, 2),
                    "last_payment": last}
        paid_on = _iso_date(raw.get("last_payment_date"))
        issued = _iso_date(raw.get("last_statement_issue_date"))
        if paid_on is not None and issued is not None and paid_on < issued:
            # the last payment cleared the cycle before this statement,
            # which is not due yet: the previous cycle decides — the
            # ledger's evidence when it has any, else no interest charged
            # and a due date still ahead
            if _ledger_cleared(led, balance, today):
                return {"basis": "ledger", "statement_balance": None,
                        "last_payment": None}
            if _not_due_yet(raw, issued, today):
                return {"basis": "issuer",
                        "statement_balance": round(stmt, 2),
                        "last_payment": None if last is None
                        else round(last, 2)}
            return None
        if last is not None and last >= stmt - 0.005:
            return {"basis": "issuer", "statement_balance": round(stmt, 2),
                    "last_payment": round(last, 2)}
        return None
    if _ledger_cleared(led, balance, today):
        return {"basis": "ledger", "statement_balance": None,
                "last_payment": None}
    return None


# the longest a statement can wait for its due date when the issuer sends
# none: card grace periods run 21 to 25 days
GRACE_DAYS = 25


def _ledger_cleared(led: dict, balance: float, today: dt.date) -> bool:
    """The card's own ledger shows it cleared each month: a full window of
    history, payments in at least two months, covering the balance."""
    first = led.get("first")
    return (first is not None
            and first <= today - dt.timedelta(days=PAID_WINDOW_DAYS)
            and led.get("months", 0) >= 2
            and led.get("paid", 0.0) >= balance - 0.005)


def _not_due_yet(raw: dict, issued: dt.date, today: dt.date) -> bool:
    """The latest statement's due date is still ahead: the issuer's own
    date when it sends one, else within the longest grace period."""
    due = _iso_date(raw.get("next_payment_due_date"))
    if due is not None:
        return due >= today
    return today <= issued + dt.timedelta(days=GRACE_DAYS)


# words in a loan's name that say what it is, not whose it is: they match
# every mortgage payment in the ledger, so they never pick one
_GENERIC_WORDS = frozenset((
    "mortgage", "mortgages", "loan", "loans", "home", "house", "heloc",
    "equity", "auto", "car", "vehicle", "student", "bank", "the", "and",
    "payment", "payments", "servicing", "services", "lending", "lender",
    "financial", "finance", "credit", "union", "first", "national",
    "second", "line", "personal", "boat", "truck"))
LEDGER_PAYMENT_DAYS = 100
LEDGER_PAYMENT_SPREAD = 0.15


def _name_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
            if len(w) >= 4 and w not in _GENERIC_WORDS]


def ledger_payment(conn, name: str, today: dt.date) -> float | None:
    """The ledger's recurring payment for a hand-entered loan: outgoing
    rows of the last LEDGER_PAYMENT_DAYS whose name or merchant carries a
    distinctive word of the loan's name, in at least two months at amounts
    within LEDGER_PAYMENT_SPREAD of the latest. The latest amount, or None
    (specs/debt-plan.md)."""
    words = _name_words(name)
    if not words:
        return None
    pats = [f"%{w}%" for w in words]
    rows = conn.execute(
        """SELECT t.date, t.amount, t.name, t.merchant_name
             FROM transactions t
             JOIN accounts a ON a.id = t.account_id
            WHERE t.removed = 0 AND t.pending = 0 AND t.amount > 0
              AND a.type = 'depository'
              AND t.date >= %s AND t.date <= %s
              AND (t.name ILIKE ANY(%s) OR t.merchant_name ILIKE ANY(%s))
            ORDER BY t.date DESC, t.id""",
        (today - dt.timedelta(days=LEDGER_PAYMENT_DAYS), today, pats,
         pats)).fetchall()
    pat = re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")",
                     re.I)
    hits = [r for r in rows
            if pat.search(f"{r['name'] or ''} {r['merchant_name'] or ''}")]
    if not hits:
        return None
    latest = float(hits[0]["amount"])
    months = {(_iso_date(r["date"]) or today).replace(day=1) for r in hits
              if abs(float(r["amount"]) - latest)
              <= LEDGER_PAYMENT_SPREAD * latest}
    if len(months) < 2 or not 0 < latest <= MAX_PAYMENT:
        return None
    return round(latest, 2)


def _manual_loans(conn, cfg: dict) -> list[dict]:
    """The loans typed in on Net Worth: kind mortgage, or any negative
    value, minus a mortgage a linked mortgage account replaces (the rule
    net worth applies to it)."""
    manual = cfg.get("manual_assets")
    if not isinstance(manual, list):
        return []
    # the accounts net worth counts, so a mortgage the Net Worth page
    # keeps is the one the plan keeps: a business, linked-twin or
    # balance-less mortgage replaces nothing on either page
    from .reporting import _live_accounts
    live_mortgage = any(r["type"] == "loan"
                        and (r["subtype"] or "") == "mortgage"
                        for r in _live_accounts(conn))
    out, seen = [], set()
    for m in manual[:30]:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip()[:60]
        value = _num(m.get("value"))
        kind = str(m.get("kind") or "")
        if not name or value is None:
            continue
        if kind != "mortgage" and value >= 0:
            continue
        if kind == "mortgage" and live_mortgage:
            continue
        bal = round(abs(value), 2)
        aid = f"manual:{name.lower()}"
        if bal <= 0.5 or aid in seen:
            continue
        seen.add(aid)
        out.append({"id": aid, "name": name,
                    "kind": "mortgage" if kind == "mortgage" else "loan",
                    "balance": bal})
    return out


def load_plan_debts(conn, cfg: dict, plan: dict | None = None, *,
                    today: dt.date | None = None
                    ) -> tuple[list[dict], list[dict]]:
    """The debts the payoff plan walks, and the cards it leaves out as paid
    in full every month — the ONE place both are decided
    (specs/debt-plan.md). Every account debt from load_debts, less the
    cards paid in full, plus the loans typed in on Net Worth."""
    today = today or dt.date.today()
    plan = plan if plan is not None else plan_settings(cfg)
    overrides = plan.get("debts") or {}
    rows = load_debts(conn, cfg, plan, today=today)
    cards = [d["id"] for d in rows if d["kind"] == "card"]
    raws = {r["account_id"]: r["raw"] for r in conn.execute(
        "SELECT account_id, raw FROM liabilities WHERE account_id = ANY(%s)",
        (cards,)).fetchall()} if cards else {}
    ledger = card_ledger(conn, cards, today)
    debts, cleared = [], []
    for d in rows:
        d = {**d, "origin": "account", "rate_needed": False,
             "min_basis": d["min_source"]}
        if d["kind"] == "card":
            got = paid_in_full(raws.get(d["id"]), ledger.get(d["id"]),
                               d["balance"], today)
            if got is not None:
                cleared.append({"id": d["id"], "name": d["name"],
                                "mask": d["mask"],
                                "institution": d["institution"],
                                "balance": d["balance"], **got})
                continue
        debts.append(d)
    for m in _manual_loans(conn, cfg):
        ov = overrides.get(m["id"]) or {}
        paid = ledger_payment(conn, m["name"], today)
        # the ledger's payment stands where an issuer's would: the
        # household's typed figure beats it, an estimate stands in for it
        raw = {"minimum_payment_amount": paid} if paid is not None else None
        got = _resolve(m["kind"], None, m["balance"], raw, ov, today)
        bare = got if not ov else _resolve(m["kind"], None, m["balance"],
                                           raw, {}, today)
        basis = got["min_source"]
        if basis == "issuer":
            basis = "ledger"
        debts.append({
            "id": m["id"], "name": m["name"], "kind": m["kind"],
            "mask": None, "institution": None, "balance": m["balance"],
            "apr": got["apr"], "apr_source": got["apr_source"],
            "apr_issuer": None, "apr_default": bare["apr"],
            "min_payment": got["min_payment"],
            # an older client reads three sources; the ledger's figure is
            # not the bank's, so it reads as an estimate there
            "min_source": "estimate" if basis == "ledger" else basis,
            "min_issuer": None, "min_default": bare["min_payment"],
            "payment_reported": None,
            "estimated": got["apr_source"] == "typical"
                         or basis == "estimate",
            "skip": bool(ov.get("skip")), "due_now": False,
            "origin": "hand_entered",
            "rate_needed": got["apr_source"] == "typical",
            "min_basis": basis,
        })
    return debts, cleared


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
    and every point in `series` is a month's first — except a debt whose
    payment this month is still due (`due_now`, a loan before or on its
    payment day), which makes that payment at `start` first: its payments
    left were counted with this month in, and walking them from next
    month would end the loan a month past the servicer's date. That
    payment is its own minimum only — extra money and a rolled minimum
    start next month — and it is not counted in `months`. `rollover=False` is
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
    else:
        for d in ordered:
            if not d.get("due_now"):
                continue
            b = bal[d["id"]]
            i = b * d["apr"] / 1200.0
            b += i
            interest_by[d["id"]] += i
            total_interest += i
            pay = min(d["min_payment"], b)
            b -= pay
            paid_by[d["id"]] += pay
            total_paid += pay
            bal[d["id"]] = b
            if b <= 0.005:
                paid_off[d["id"]] = start.isoformat()
                bal[d["id"]] = 0.0
                freed += d["min_payment"]
        if sum(bal.values()) <= 0.005:
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
    debts, cleared = load_plan_debts(conn, cfg, plan, today=today)
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
        "estimated": any(d["estimated"] for d in debts if not d["skip"]),
        # cards cleared every statement: out of the plan, listed under it
        "paid_in_full": cleared,
        "alt_same_path": alt["series"] == chosen["series"],
        # the two differences the page quotes, None where one side is a
        # fifty-year cut-off rather than a cost
        "saves_vs_minimums": (
            round(base["total_interest"] - chosen["total_interest"], 2)
            if base["finishes"] and chosen["finishes"] else None),
        "alt_extra_interest": (
            round(alt["total_interest"] - chosen["total_interest"], 2)
            if alt["finishes"] and chosen["finishes"] else None),
    }
