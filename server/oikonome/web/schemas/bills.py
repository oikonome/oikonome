"""Bills and recurring income: the list, one bill, its history, the
proposals and the merchant-wide category door (web/api_bills.py)."""

from __future__ import annotations

from typing import Literal

from . import Shape
from .ledger import HistoryTxn

Cadences = list[tuple[str, str]]
"""the cadence picker's options: [value, label]"""


class LedgerFit(Shape):
    """What the ledger's own charges say the bill is."""
    kind: str
    amount: float
    cadence: str
    next_due: str
    short: str
    label: str
    cycle_days: int = 0
    hits: int = 0
    recent_spread: float = 0.0
    by_month_day: list[int] | None = None
    """the two days a twice-a-month fit landed on"""


class SeenCharge(Shape):
    date: str
    amount: float


class Suggestion(Shape):
    """The one action the server's evidence supports; clients render it
    verbatim."""
    action: Literal["accept_amount", "disable", "review"]
    reason: str
    amount: float = 0.0


class BillProposal(Shape):
    id: str
    kind: str
    bill_type: str | None
    payee: str
    amount: float
    income: bool
    bill_payee: str | None
    """an "attach" (fee) or "merchant" proposal: the bill it would join"""
    cadence: str
    frequency: str | None
    interval: int | None
    by_month_day: list[int] | None
    next_due: str | None
    llm_tag: str | None
    summary: str
    change_text: str | None = None
    """what approving the offer does, in words ("new bill", "stop
    tracking", "add merchant"); clients print it as the row's pill"""
    tag_text: str | None = None
    """the advisory tag in plain words ("looks like a subscription")"""
    cadence_text: str | None = None
    """the offer's cadence in words ("every 2 weeks"), "varies month to
    month" for an envelope, null where the offer has no cadence"""
    upcoming_due: str | None = None
    """when the next charge is expected, never a day already past: the
    finder's next_due rolled forward by the cadence (bills.offer_upcoming_due);
    null for a fee, a merchant, a removal, an envelope or a one-time offer
    whose date has gone by. Clients print it as the row's "next" date"""


class Companion(Shape):
    """A fee that rides with the payment, attached to the same occurrence
    when it lands within window_days."""
    tokens: str
    amount: float
    window_days: int


class Bill(Shape):
    """One active bill or income on the list, with this month's stats and
    the edit form's prefill."""
    paid_text: str | None = None
    """the month's paid state in words ("paid 05/03 · next 06/03", "1 of
    2 paid", "received 05/15"); null while nothing of it is paid"""
    paid_on: str | None = None
    """the latest day a payment of this month's occurrences posted"""
    charges_category: str | None = None
    """an envelope: the category this month's charges carry most"""
    payee: str
    amount: float
    income: bool
    cadence: str
    by_month_day: list[int] | None
    due_on: str | None
    health: str | None
    last_seen: SeenCharge | None
    last_match: str | None
    cycle_days: int | None
    suggestion: Suggestion | None
    health_dismissed: bool
    disabled: bool
    fit: LedgerFit | None
    fit_diverges: bool
    hint_dismissed: bool
    cadence_value: str
    bill_type: str
    source: str
    category: str | None
    merchant: str | None
    cap: float | None
    show_today: bool
    match_category: bool
    txn_category: str
    merchants: list[str]
    companions: list[Companion]
    fee_total: float
    amount_with_fees: float | None = None
    """the figure the row prints (bills.row_amount): for a bill, what one
    payment charges — the amount plus the fees that ride with it; for an
    income series, the most recent deposit matched to it, to the cent,
    else its planned `amount`"""
    occurrences: int
    paid: int
    paid_amount: float
    used: float | None
    overflow: float | None
    pool: float | None
    period_months: int | None
    period_used: float | None
    period_left: float | None
    period_frac: float | None = None
    """an envelope's bar: how full its pool is, 0..1
    (budget.envelope_frac); null on a bill that is not an envelope"""
    pool_text: str | None = None
    """an envelope named on its own: everything its period charged at
    its merchants of the pool, and any spill past it (null on a bill that
    is not an envelope, or from an older server)"""
    next_unpaid: str | None
    withdrawn_on: str | None
    """this month's missed occurrence: when the bank feed withdrew it"""
    health_withdrawn_on: str | None
    due_label: str | None = None
    """the Due column: "today", "tomorrow", else the date MM/DD/YY
    (api_bills.due_words); null on a closed month, an envelope or a row
    with no due date"""
    due_text: str | None = None
    """the countdown beside the cycle bar: "due today", "in 12 days",
    "3 days late" (income: "today", "tomorrow"); null once the month's
    occurrences are paid, and wherever `due_label` is null"""
    due_tone: Literal["ok", "soon", "late", "withdrawn"] | None = None
    """the colour of `due_text` and the bar: late (red), withdrawn — late
    with the charge taken back by the bank feed — and soon (amber), ok
    (green); null with no `due_text`"""
    cycle_frac: float | None = None
    """the cycle bar's fill: how far through its cycle the row is, 0..1;
    null wherever `due_label` is null"""
    attention_text: str | None = None
    """the attention queue's line after the bill's name ("missed its
    ~03/14/26 cycle — nothing since 02/14/26"; api_bills.attention_words);
    null for a row whose health wants no decision. A dismissed or disabled
    row keeps it; the queue leaves those rows out"""


class ArchivedBill(Shape):
    payee: str
    amount: float
    frequency: str | None
    synced_at: str | None


class BillsHeroRow(Shape):
    """One figure of the Bills & Income headline, with its words
    (api_bills.bills_hero); clients print it as sent."""
    key: Literal["bills", "income", "left", "due"]
    label: str
    amount: int
    note: str
    tone: Literal["", "pos", "neg"]


class BillsTotals(Shape):
    hero: list[BillsHeroRow] | None = None
    """the headline, in the plan's terms: Bills, Income, Left after bills
    (to spend + excess); the clients print these and add nothing up"""
    bills_monthly: float
    income_monthly: float
    income_basis: Literal["actual", "schedule"]
    income_months: int | None
    week_total: float
    week_count: int
    week_text: str | None = None
    """the week ahead in words ("$165 across 2 bills"), printed after
    "Next 7 days"; null when nothing is due in it"""
    attention: int


class BillsCalendarEvent(Shape):
    label: str
    amount: float
    kind: Literal["bill", "income"]
    name: str
    """The event's name, as /api/calendar's events send it."""
    note: str | None
    """What the figure is, printed apart from the name (/api/calendar's
    `note`); null on a closed month's events."""


class BillsCalendarDay(Shape):
    """One day of a closed month's calendar, the shape /api/calendar's
    days have."""
    date: str
    total: float
    came_in: float
    went_out: float
    events: list[BillsCalendarEvent]


class BillsData(Shape):
    """GET /api/bills."""
    proposals: list[BillProposal]
    bills: list[Bill]
    archived: list[ArchivedBill]
    totals: BillsTotals
    cadences: Cadences
    month: str
    month_final: bool
    month_calendar: list[BillsCalendarDay] | None = None
    """a closed month only: its dated money — bills paid on the day they
    were paid, paychecks received, bills left unpaid on their due day"""


class BillDetail(Shape):
    """GET /api/bills/bill: one bill's editable fields."""
    payee: str
    amount: float
    income: bool
    cadence: str
    by_month_day: list[int] | None
    next_due: str
    bill_type: str
    source: str
    category: str | None
    merchant: str | None
    active: bool
    cap: float | None
    disabled: bool
    show_today: bool
    match_category: bool
    txn_category: str
    merchants: list[str]
    companions: list[Companion]
    fee_total: float
    cadences: Cadences


class HistoryMonth(Shape):
    month: str
    total: float
    count: int


class HistoryLifetime(Shape):
    total: float
    count: int
    first: str | None
    last: str | None
    active_months: int
    avg_monthly_active: float


class HistoryBill(Shape):
    amount: float
    bill_type: str
    source: str
    cadence: str
    by_month_day: list[int] | None
    due_on: str | None
    income: bool
    tol: float


class BillHealth(Shape):
    health: str
    last_match: str | None
    fit: LedgerFit | None
    matched: int
    cycle_days: int | None
    suggestion: Suggestion | None
    fit_diverges: bool
    withdrawn_on: str | None = None
    last_seen: SeenCharge | None = None
    last_match_amount: float | None = None
    """the amount of the payment `last_match` dates, to the cent; an
    income row prints it (bills.row_amount)"""


class HistoryProposal(Shape):
    id: str
    kind: str
    bill_type: str | None
    payee: str
    amount: float
    cadence: str
    by_month_day: list[int] | None
    summary: str


class BillsHistory(Shape):
    """GET /api/bills/history: one payee's ledger rows and what they say."""
    payee: str
    txns: list[HistoryTxn]
    monthly: list[HistoryMonth]
    logo: str | None
    fit: LedgerFit | None
    lifetime: HistoryLifetime
    inflow: bool
    bill: HistoryBill | None
    health: BillHealth | None
    proposals: list[HistoryProposal]
    covered_by: str | None
    hint_dismissed: bool
    bill_archived: bool
    merchant_in_use: bool
    cadences: Cadences
    scope_line: str | None = None
    """what the page lists, in words, under its header: every row filed
    under the name, not only the rows a spending figure counted"""


class BillsDetected(Shape):
    proposed_add: int
    proposed_income: int
    drift_applied: int


class ProposalAdjust(Shape):
    name: str | None = None
    amount: float | str | None = None
    cadence: str | None = None
    due_day: int | None = None
    by_month_day: list[int] | None = None


class ProposalAnswer(Shape):
    pid: str
    action: Literal["approve", "reject"]
    adjust: ProposalAdjust | None = None


class ProposalAnswered(Shape):
    ok: Literal[True]
    payee: str
    note: str = ""


class ApproveFailure(Shape):
    pid: str
    error: str
    superseded: bool


class ApproveNote(Shape):
    payee: str | None
    note: str


class ProposalsApproveAll(Shape):
    """POST /api/bills/proposals/approve-all: `pids` are the offers the
    client showed; only those still listed are approved. Left out (an
    older client), every listed offer is approved."""
    pids: list[str] | None = None


class ProposalsApproved(Shape):
    ok: Literal[True]
    approved: int
    payees: list[str]
    failed: list[ApproveFailure]
    notes: list[ApproveNote]


class CompanionInput(Shape):
    tokens: str
    amount: float | str
    window_days: int | str | None = None


class BillSave(Shape):
    """POST /api/bills/save: a key left out keeps the bill's value."""
    payee: str
    orig_payee: str | None = None
    amount: float | str = 0
    income: bool | None = None
    cadence: str | None = None
    by_month_day: list[int] | None = None
    next_due: str | None = None
    cap: int | str | None = None
    category: str | None = None
    merchant: str | None = None
    merchants: list[str] | None = None
    companions: list[CompanionInput] | None = None
    show_today: bool | None = None
    match_category: bool | None = None
    txn_category: str | None = None
    restore: bool = False
    """true: bring an archived bill under this payee back"""


class BillSaved(Shape):
    ok: Literal[True]
    payee: str
    restored: bool = False


class BillDelete(Shape):
    payee: str
    mode: Literal["archive", "purge"] = "archive"


class BillDeleted(Shape):
    ok: Literal[True]
    mode: Literal["archive", "purge"]
    affected: int


class BillPayee(Shape):
    payee: str


class BillRestored(Shape):
    ok: Literal[True]
    affected: int


class BillToggle(Shape):
    payee: str
    disabled: bool | None = None


class BillToggled(Shape):
    ok: Literal[True]
    payee: str
    disabled: bool


class IncomePayerSet(Shape):
    """POST /api/income/payer: the household's word on whether one payer's
    deposits are paychecks."""
    key: str
    """The payer's key, as `source_paychecks` sends it."""
    count_as: Literal["paycheck", "never", "auto"]
    """"auto" forgets the word and lets the rules decide."""
    name: str | None = None
    """The payer's name as the row shows it, for the activity log."""


class IncomePayerSaved(Shape):
    ok: Literal[True]
    key: str
    override: Literal["paycheck", "never"] | None
    """The word now stored; null when the rules decide."""


class BillHint(Shape):
    payee: str
    action: Literal["dismiss", "restore"] = "dismiss"
    target: Literal["fit", "health"] = "fit"


class BillHinted(Shape):
    ok: Literal[True]
    payee: str
    dismissed: bool


class MerchantCategory(Shape):
    payee: str
    category: str


class MerchantCategoryPreview(MerchantCategory):
    canon_only: bool = False


class MerchantCategoryPreviewed(Shape):
    merchant: str
    count: int
    variants: list[str]
    flow_rows: int
    skipped_override: int


class CategoryUndo(Shape):
    """The reference an undo hands back."""
    ref: str
    category: str | None


class MerchantCategorySet(Shape):
    merchant: str
    count: int
    undo: CategoryUndo


class MerchantCategoryUndo(Shape):
    undo: CategoryUndo


class MerchantCategoryUndone(Shape):
    ok: Literal[True]
    restored: int
