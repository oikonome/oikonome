"""The Retirement and Debt pages' shapes (web/api_retirement.py)."""

from __future__ import annotations

from typing import Literal

from . import Shape

DebtMethod = Literal["avalanche", "snowball"]


# ---- retirement projection --------------------------------------------------

class RetirementBuckets(Shape):
    """Live balances grouped by tax treatment, card debt netted out in the
    draw order."""
    cash: float
    taxable: float
    td: float
    """tax-deferred"""
    roth: float
    debt: float
    """card debt owed (positive)"""
    debt_apr: float | None
    """the balance-weighted rate the owed cards grow at, percent"""
    debt_residual: float
    """debt beyond every bucket, subtracted from `total`"""
    total: float
    cards_from_cash: float
    """card balances netted out of `cash` (cash before cards = cash + this)"""
    cards_from_taxable: float
    cards_from_td: float
    cards_from_roth: float


class RetirementRow(Shape):
    age: int
    """the retire age this row tries"""
    at_retire: float
    feasible: bool
    fail_age: int | None
    max_spend: float
    hist_pct: float | None
    """the share of historical paths (1928 on) that survive"""
    hist_lasts: bool
    """the replay lasts in at least verdict.threshold_pct of past
    sequences: the row's verdict colour"""
    meets_target: bool = False
    """max_spend covers the target spend: the figure's colour"""


class RetirementStress(Shape):
    label: str
    earliest: int | None
    max_spend_at_ref: float
    under_target: bool = False
    """max_spend_at_ref is below the target spend: the figure's colour"""


class RetirementHist(Shape):
    ok: int
    n: int
    pct: float
    spend90: float
    spend100: float
    stock_frac: float


class RetirementCatchUp(Shape):
    age: int
    extra_monthly: float


class RetirementVerdict(Shape):
    """The page's headline: the history replay (specs/retirement-figures.md)."""
    basis: Literal["history"]
    age: int | None
    """earliest retire age lasting in threshold_pct of past sequences;
    null when none does"""
    at_age: int
    """the age the counts below are for: `age`, else the last age tried"""
    ok: int
    n: int
    pct: float
    in_ten: int
    """ok × 10 ÷ n rounded down: lasts in 9 of 10 past markets"""
    threshold_pct: float
    tone: Literal["good", "bad"]
    steady_age: int | None
    """the flat-return ("steady markets") model's earliest age"""
    catch_up: list[RetirementCatchUp] | None
    """null when an age works; otherwise the extra monthly saving that
    would make each listed age last in threshold_pct of past sequences"""


class RetirementSavingPart(Shape):
    """One part of the monthly saving, by the tax treatment it gets."""
    kind: Literal["td", "roth", "taxable"]
    monthly: int
    text: str
    """the server's words for the part, printed as they are"""


class RetirementInputs(Shape):
    """What the projection was computed from, after the clamps and the
    snapping to the page's control grid."""
    age: int
    spend: int | None
    """null when spend_source is "needed\""""
    ret: float
    infl: float
    end: int
    ssage: int
    employer_mo: int
    """kept for older clients: the saving that is not taxable-account
    saving, $/mo — tax-deferred, plus the Roth part while that is the
    ledger's; a client that knows td_mo reads the split instead"""
    td_mo: int
    """saving into tax-deferred accounts, $/mo: what the tax-deferred box
    opens on (see saving_hint for a saved total split by the ledger)"""
    taxable_mo: int
    roth_mo: int
    """saving into Roth accounts, $/mo: what the Roth box opens on. Equal
    to the Roth saving projected, except while saving_hint offers to split
    a saved total; saving_parts always says what is projected"""
    resume: int
    stockpct: int
    cash: float | None
    """the cash bucket's nominal yield in %; null keeps pace with inflation"""
    saving_yr: int
    your_ss: float
    has_sched: bool
    spend_source: Literal["what_if", "saved", "ledger", "needed"]
    saving_source: Literal["what_if", "saved", "none"]
    """kept for older clients: a ledger figure reads as "saved" here"""
    saving_basis: Literal["what_if", "saved", "ledger", "none"]
    """where the saving came from; ledger = deposits into retirement
    accounts over the last 12 months"""
    saving_mo: int
    """the monthly saving the projection counts (tax-deferred + Roth +
    taxable)"""
    saving_parts: list[RetirementSavingPart]
    """each saving figure above $0, worded for the page; their monthly
    figures sum to saving_mo"""
    saving_hint: str | None
    """an offer of the ledger's Roth saving a saved answer leaves out;
    null otherwise"""
    saving_found_mo: int | None
    """what the ledger shows going into retirement accounts a month,
    whatever is typed; null when nothing arrived"""


class RetirementData(Shape):
    """GET /api/retirement. With no spend to plan for (inputs.spend_source
    "needed") every spend-based figure is empty."""
    buckets: RetirementBuckets
    rows: list[RetirementRow]
    earliest: int | None
    real_return: float
    cash_real_return: float
    rmd_start: int
    spend: int | None
    spend_now: float | None
    growth_path: list[tuple[str, float]]
    """[YYYY-MM, balance] from this month to the end age: the median of
    every past sequence, retiring at growth_path_retire_age"""
    growth_path_retire_age: int | None
    grow_to_67: float | None
    stress: list[RetirementStress]
    stress_ref_age: int | None
    stress_floor_spend: float | None
    stress_floor_vs_spend: float | None
    hist: RetirementHist | None
    catch_up: list[RetirementCatchUp] | None
    """null while some age works; otherwise the extra monthly saving that
    would make each listed age work (empty: no saving would)"""
    verdict: RetirementVerdict | None
    """the headline; null when nothing is projected"""
    inputs: RetirementInputs


# ---- debt payoff plan -------------------------------------------------------

class DebtRow(Shape):
    id: str
    name: str
    kind: Literal["card", "mortgage", "loan"]
    mask: str | None
    institution: str | None
    balance: float
    apr: float
    apr_source: Literal["issuer", "you", "typical"]
    apr_issuer: float | None
    apr_default: float
    """what the plan walks with no override: the box's placeholder"""
    min_payment: float
    min_source: Literal["issuer", "you", "estimate"]
    min_issuer: float | None
    min_default: float
    payment_reported: float | None
    """a mortgage servicer's whole monthly payment, escrow included"""
    estimated: bool
    skip: bool
    due_now: bool
    """a loan or mortgage whose payment this month is still due"""
    origin: Literal["account", "hand_entered"]
    """hand_entered: a loan typed in on Net Worth (id `manual:<name>`)"""
    min_basis: Literal["issuer", "you", "estimate", "ledger"]
    """where the minimum came from; `ledger` is the matching recurring
    payment (min_source reads `estimate` for it)"""
    rate_needed: bool
    """a hand-entered loan whose rate nobody has typed yet: the page asks"""


class DebtScheduleDebt(Shape):
    id: str
    order: int
    paid_off: str | None
    interest: float
    paid: float
    remaining: float


class DebtSchedule(Shape):
    method: DebtMethod
    extra: float
    months: int | None
    """null: not within fifty years"""
    debt_free: str | None
    total_interest: float
    total_paid: float
    monthly: float
    starting: float
    growing: bool
    finishes: bool
    """false: the walk stopped at fifty years and the totals are a cut-off"""
    series: list[tuple[str, float]]
    debts: list[DebtScheduleDebt]


class DebtSaved(Shape):
    method: DebtMethod
    extra_monthly: float


class DebtOverride(Shape):
    """A stored per-debt override; an absent key is no override."""
    apr: float | None = None
    min_payment: float | None = None
    skip: bool = False


class DebtPaidInFull(Shape):
    """A card cleared every statement, left out of the plan
    (specs/debt-plan.md)."""
    id: str
    name: str
    mask: str | None
    institution: str | None
    balance: float
    basis: Literal["issuer", "ledger"]
    """issuer: the last statement was paid; ledger: the card's own
    payments cover its balance and no interest was charged"""
    statement_balance: float | None
    last_payment: float | None


class DebtPlan(Shape):
    """GET and POST /api/debt-plan."""
    today: str
    method: DebtMethod
    extra_monthly: float
    saved: DebtSaved
    debts: list[DebtRow]
    overrides: dict[str, DebtOverride]
    """every stored override, listed debt or not"""
    plan: DebtSchedule
    alt: DebtSchedule
    minimums: DebtSchedule
    estimated: bool
    saves_vs_minimums: float | None
    alt_extra_interest: float | None
    paid_in_full: list[DebtPaidInFull]
    """cards paid in full every month: not in `debts`, listed under the
    plan as no interest"""
    alt_same_path: bool
    """the other method's balance curve is this one's, month for month"""


class DebtOverrideInput(Shape):
    """One debt's override as a client sends it: a typed figure may arrive
    as text, and a blank or null clears it."""
    apr: float | str | None = None
    min_payment: float | str | None = None
    skip: bool = False


class DebtPlanSave(Shape):
    """POST /api/debt-plan: the whole plan, which replaces the saved one; a
    key left out takes the plan's default. A null method or extra payment is
    refused; a blank extra payment is zero."""
    method: DebtMethod = "avalanche"
    extra_monthly: float | str = 0
    debts: dict[str, DebtOverrideInput | None] | None = None
    """per-debt overrides keyed by account id; a null entry is no override"""
