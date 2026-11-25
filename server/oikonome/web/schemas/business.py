"""Business books: flags, entities and their members, equity, the books
and the P&L, compliance, the self-employment figures (estimated tax,
balance sheet, mileage, 1099 vendors), and assigning accounts and rows to
an entity (web/api_business.py)."""

from __future__ import annotations

from typing import Literal

from . import Shape


class BizRow(Shape):
    id: str
    date: str
    amount: float
    pending: int
    payee: str
    category: str
    note: str | None
    flagged_at: str
    account: str | None


class BizData(Shape):
    """GET /api/business: rows flagged as business expenses."""
    rows: list[BizRow]
    total: float
    count: int
    years: list[int]


class EntityMember(Shape):
    id: str
    member_name: str
    ownership_pct: float | None
    is_manager: bool
    is_household: bool
    """the member this household is: at most one per business"""


class Capital(Shape):
    contributions: float
    paid_personally: float = 0.0
    """the part of `contributions` that is business costs paid on a
    personal card or account, counted as money the owner put in"""
    draws: float
    distributions: float
    reimbursements: float
    net_income: float
    movements_balance: float
    capital_balance: float
    household_share_pct: float | None
    household_capital: float | None


class OwnerOption(Shape):
    value: Literal["primary", "spouse", "unknown"]
    label: str


class Entity(Shape):
    """A business entity; the detail read adds its members and capital."""
    id: str
    name: str
    structure: str
    state: str | None
    formation_date: str | None
    business_start_date: str | None
    ein_last4: str | None
    registered_agent: str | None
    fiscal_year_end: str | None
    status: str
    home_office_sqft: float | None
    income_tax_rate: float | None
    tax_reserve_account_id: str | None
    """the account it keeps its tax money in; null = not nominated"""
    archived_at: str | None
    owner: Literal["primary", "spouse", "unknown"] = "unknown"
    """whose business it is: whose W-2 Social Security wages close its
    self-employment wage base ("unknown" nets none, so the estimate may
    read high)"""
    owner_options: list[OwnerOption] = []
    """the choices for `owner` with the labels the pickers print, in
    order"""
    created_at: str
    updated_at: str
    members: list[EntityMember] = []
    capital: Capital | None = None


class Entities(Shape):
    entities: list[Entity]


class EntityInput(Shape):
    """POST (create) and PATCH (update) /api/business/entities; a key left
    out of an update keeps its value. The EIN is stored encrypted and read
    back only as its last four."""
    name: str | None = None
    structure: str | None = None
    state: str | None = None
    formation_date: str | None = None
    business_start_date: str | None = None
    ein: str | None = None
    registered_agent: str | None = None
    fiscal_year_end: str | None = None
    home_office_sqft: float | str | None = None
    income_tax_rate: float | str | None = None
    tax_reserve_account_id: str | None = None
    owner: Literal["primary", "spouse", "unknown"] | None = None
    """whose business it is; left out of an update, it keeps its value"""


class EntityCreate(Shape):
    name: str
    structure: str
    state: str | None = None
    formation_date: str | None = None
    business_start_date: str | None = None
    ein: str | None = None
    registered_agent: str | None = None
    fiscal_year_end: str | None = None
    owner: Literal["primary", "spouse", "unknown"] | None = None


class EntityStatus(Shape):
    status: str


class Counts(Shape):
    members: int
    equity_movements: int
    compliance_obligations: int
    mileage_trips: int
    vendors_1099: int


class Detached(Shape):
    accounts: int
    transactions: int


class EntityDeleteImpact(Shape):
    """What deleting forever would destroy, and what would go back to
    personal."""
    id: str
    name: str
    destroyed: Counts
    detached: Detached


class EntityDeleteForever(Shape):
    name: str
    """typed back as the confirmation"""


class EntityDeleted(EntityDeleteImpact):
    ok: Literal[True]


class MemberInput(Shape):
    member_name: str | None = None
    ownership_pct: float | str | None = None
    is_manager: bool | None = None
    is_household: bool | None = None


class EquityMovement(Shape):
    id: str
    entity_id: str
    kind: str
    amount: float
    date: str | None
    member_id: str | None
    txn_id: str | None
    form: str | None
    note: str | None


class OwnerTransfer(Shape):
    txn_id: str
    date: str | None
    amount: float
    """the ledger's sign: negative is money into the business"""
    payee: str
    role: Literal["owner_in", "owner_out"]
    owner_recorded: str | None
    """the movement already recorded for it, or null"""
    offer: Literal["contribution", "draw"]
    """what recording it writes"""


class EquityPage(Shape):
    movements: list[EquityMovement]
    capital: Capital
    owner_transfers: list[OwnerTransfer] = []
    """owner money moved in or out on the business's accounts, newest
    first, each offered as a contribution or a draw"""


class EquityInput(Shape):
    kind: str
    amount: float | str
    date: str
    member_id: str | None = None
    form: str | None = None
    note: str | None = None


class OwnerTransferInput(Shape):
    txn_id: str
    """an owner transfer on a business account (role owner_in / owner_out)"""


class EquityReimburse(Shape):
    txn_id: str
    mode: Literal["contribute", "reimburse"]
    member_id: str | None = None


class UnlinkedMovement(EquityMovement):
    entity_name: str


class UnlinkedMovements(Shape):
    """GET /api/doctor/unlinked-movements: equity movements whose
    transaction is gone."""
    movements: list[UnlinkedMovement]


class FlagsImported(Shape):
    ok: Literal[True]
    moved: int


class LineSuggestion(Shape):
    txn_id: str
    date: str | None
    amount: float
    payee: str
    category: str | None
    suggested_line: str | None
    source: Literal["merchant", "kind", "category"] | None
    """merchant: the owner's own earlier line for this merchant; kind: the
    built-in list of business kinds; category is no longer sent"""
    suggested_bucket: str
    kind_label: str | None = None
    """the business kind that matched when `source` is kind ("software
    and hosting")"""


class LineSuggestions(Shape):
    """GET …/suggestions: proposed Schedule C lines for a person to
    confirm; `unclassified` is the true total (the list is capped)."""
    lines: list[str]
    suggestions: list[LineSuggestion]
    unclassified: int


class BizTxn(Shape):
    id: str
    date: str | None
    amount: float
    net_amount: float | None
    """net of partial reimbursement links: what the P&L counts"""
    payee: str
    merchant_logo: str | None
    category: str | None
    cat_detailed: str | None
    cat_original: str | None
    note: str | None
    partly_claimed: bool | None
    bucket: str | None
    sched_c_line: str | None
    effective_bucket: str | None
    role: Literal["cost", "revenue", "refund", "owner_in", "owner_out",
                  "owner_side", "internal", "card_payment",
                  "owner_tax"] | None = None
    """what the row is on the books (books.row_roles): the Books list shows
    `cost` rows; owner_in / owner_out are offered as a contribution / draw"""
    paid_personally: bool = False
    """the row sits on an account that is not the business's"""
    owner_recorded: str | None = None
    """the equity movement recorded for this row, if any (contribution,
    draw, distribution, reimbursement)"""
    ledger_line: str | None = None
    """what the downloads print in their Schedule C line column"""
    account_id: str | None = None


class BizTxns(Shape):
    transactions: list[BizTxn]
    total: int = 0
    limit: int = 0
    offset: int = 0


class TxnClass(Shape):
    bucket: str | None = None
    sched_c_line: str | None = None
    note: str | None = None


class Deduction(Shape):
    total: float
    immediate: float
    amortizable: float
    monthly_amortization: float
    amortization_this_year: float = 0.0
    deductible_this_year: float = 0.0
    begins: str | None = None


class PeriodProfit(Shape):
    """books.period_profit: a period's operating profit by the P&L's
    rules."""
    period: str
    net_operating: float


class EntityCash(Shape):
    """books.entity_cash: the business's bank balances, a linked backup
    and a hidden account out."""
    total: float
    accounts: int
    """How many of the business's accounts count (every kind)."""


class Pnl(Shape):
    year: int | None
    revenue: float
    operating_expenses: float
    operating_by_line: dict[str, float]
    net_operating: float
    vehicle_expenses: float
    estimated_tax_paid: float
    organizational: float
    startup_195: float
    organizational_deduction: Deduction
    startup_deduction: Deduction
    capital: Capital
    this_month: PeriodProfit
    """The household's current month, by the same rules."""
    cash: EntityCash
    """The cash in the business."""


class Obligation(Shape):
    """A filing or fee due; the ones the state's rules supply have no
    id (they cannot be deleted)."""
    title: str
    due: str
    fee: float | None
    url: str | None
    note: str | None
    source: str
    recurrence: str
    id: str = ""


class Obligations(Shape):
    obligations: list[Obligation]


class ObligationInput(Shape):
    title: str
    due_date: str
    fee: float | str | None = None
    url: str | None = None
    note: str | None = None
    recurrence: str | None = None


class Created(Shape):
    id: str


class MemberShare(Shape):
    member_name: str
    ownership_pct: float
    is_household: bool
    share_of_profit: float
    se_tax: float


class HouseholdShare(MemberShare):
    income_tax: float | None
    total: float


class ReserveAccount(Shape):
    id: str
    name: str | None
    mask: str | None


class TaxSetAside(Shape):
    """selfemploy.tax_set_aside: the "Set aside for tax" card, on the
    estimate's footing."""
    still_due: float
    paid: float
    rate_of_profit: int | None
    held: float
    account: ReserveAccount | None
    stale: bool
    covered: int
    """Whole dollars: the printed held less the printed still to pay;
    negative is short."""


class EstimatedTax(Shape):
    year: int
    startup_label: str = "start-up costs"
    """what `startup_deduction` is made of: "organizational costs",
    "start-up costs" or both"""
    loss_note: str | None = None
    """the sentence under a year that ends in a loss: whose return the
    loss lands on"""
    net_profit: float
    covered_profit: float
    covered_tax_paid: float
    mileage_deduction: float
    mileage_logged: float
    vehicle_method: Literal["standard_mileage", "actual"] | None
    vehicle_expenses_added_back: float
    home_office_deduction: float
    startup_deduction: float
    taxable_profit: float
    se_tax: float
    se_tax_applies: bool
    ss_wages_note: str | None = None
    """the sentence printed under the self-employment tax line, exactly as
    sent: whose W-2 wages the Social Security part was figured after (and
    whose business that assumes) and how much of the wage base they left,
    or that W-2 wages the app cannot tell to be the owner's were not
    counted so the estimate may be high; null when no W-2 wages for the
    year are on file"""
    structure: str | None
    covers: Literal["owner", "all_members", "household"]
    household: HouseholdShare | None
    household_unavailable: Literal["s_corp", "shares_incomplete",
                                   "none_marked"] | None
    business_total: float | None
    members: list[MemberShare]
    income_tax_rate: float | None
    income_tax: float | None
    total_estimated: float
    annualized: bool
    annualized_taxable_profit: float
    annualized_total: float
    quarterly: float
    basis: Literal["projected", "prior_year", "year"]
    estimated_tax_paid: float
    next_deadline: str | None
    next_payment: float | None
    deadlines: list[str]
    set_aside: TaxSetAside


class NamedAmount(Shape):
    name: str
    amount: float


class BalanceSheet(Shape):
    assets: list[NamedAmount]
    liabilities: list[NamedAmount]
    total_assets: float
    total_liabilities: float
    net: float
    capital_account: float
    household_share_pct: float | None
    household_capital: float | None
    capital_gap: float = 0.0
    """net less the capital account"""
    reconcile: list[str] = []
    """why capital and net assets differ, in the sentences every surface
    prints"""


class MileageTrip(Shape):
    id: str
    date: str | None
    miles: float
    purpose: str | None
    note: str | None


class RateMiles(Shape):
    rate: float
    miles: float


class MileageSummary(Shape):
    year: int
    miles: float
    rate: float
    rates: list[RateMiles]
    deduction: float


class Mileage(Shape):
    trips: list[MileageTrip]
    summary: MileageSummary


class TripInput(Shape):
    date: str
    miles: float | str
    purpose: str | None = None
    note: str | None = None


class Vendor(Shape):
    merchant: str
    paid: float
    paid_by_card: float
    reportable: bool
    tin_last4: str | None
    needs_1099: bool


class Vendors(Shape):
    vendors: list[Vendor]
    threshold: float


class VendorMark(Shape):
    """Left out, `reportable` is true and the TIN on file is kept; a TIN
    sent ("" or null clears it) keeps only its digits."""
    merchant: str
    reportable: bool | None = None
    tin_last4: str | int | None = None


class BusinessSummary(Shape):
    entities: list[Entity]
    flagged_unassigned: int
    combined: bool


class Combine(Shape):
    on: bool


class Combined(Shape):
    combined: bool


class EntityAssign(Shape):
    """null takes it back to personal."""
    entity_id: str | None = None


class AccountEntitySet(Shape):
    ok: Literal[True]
    account_id: str
    entity_id: str | None


class TxnEntitySet(Shape):
    ok: Literal[True]
    txn_id: str
    entity_id: str | None
