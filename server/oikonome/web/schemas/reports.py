"""The analytics reports (web/reporting_api.py, built by engine/reporting):
Net Worth, Spending, Cash Flow and its windows, investment fees and the
cost of holding cash.

Most of these payloads are positional rows (`[label, amount, …]`) because
the charts read them that way; each tuple type below names its columns.
Every report carries `_built_at`, the time it was computed (a leading
underscore cannot be a field name, so it is declared through an alias).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from . import Shape

# [label, amount] — a month ("YYYY-MM"), a year, a category or an
# institution against its figure
Point = tuple[str, float]
# [payee or payer, amount, rows, previous window's amount (null when the
# range has no previous window), logo URL]
RankedPayee = tuple[str, float, int, float | None, str | None]
CashCostKind = Literal["interest", "fee"]


def _built_at():
    return Field(alias="_built_at")


class PropertyItem(Shape):
    """One row of the property layer: a manual asset or debt from Settings,
    or a linked loan (its balance as a negative value)."""
    name: str
    kind: str
    value: float
    source: str
    entered_text: str | None = None
    """For a hand-entered mortgage, when its balance was last stated:
    "entered 06/26", or "entered 06/26 — update" once a quarter has
    passed. Null for every other row."""
    update_due: bool = False
    """The hand-entered balance is a quarter old: `entered_text` asks for
    an update, and the clients tone it as a warning."""


class TrendWindow(Shape):
    """One Over-time timeframe over `full_trend`: the points drawn
    (start..end), the gain between them and its percent; `full` when the
    window is the whole series."""
    start: int
    end: int
    gain: float | None
    pct: float | None
    full: bool
    from_text: str | None = None
    """Where the gain is measured from, said when there is no percent to
    say it (the first point is not above zero): "from −$4,200 in
    03/08"."""
    note: str | None = None
    """Set when a hand-entered property item entered the series inside the
    window ("Home added 07/26 — the change is measured on the financial
    accounts"): `gain` and `pct` are then the financial accounts' alone.
    Printed beside the gain."""


class ShownRow(Shape):
    """One row of a breakdown list as the page prints it."""
    label: str
    dollars: int
    """Whole dollars; the rows add up to the list's shown net (a row may
    be a dollar off its own rounding)."""
    share: int | None
    """Whole percent of what the ring draws, the drawn rows adding up to
    100; null for a row the ring does not draw."""
    owed: bool
    """Listed under the ring as owed."""


class ShownList(Shape):
    """A breakdown list in the whole dollars the page prints: the rows
    (in the order of the list they come from), the sums of the drawn and
    of the owed rows, and the net they add up to — the Financial accounts
    figure in whole dollars."""
    rows: list[ShownRow]
    owned: int
    owed: int
    net: int


class RingFigures(Shape):
    """A breakdown ring: what it draws (owned), what is listed under it as
    owed, and the net."""
    owned: float
    owed: float
    net: float
    shown: ShownList


class NetWorthRings(Shape):
    by_asset_class: RingFigures
    by_institution: RingFigures


class CoinbasePl(Shape):
    """A crypto account's profit or loss against the cash put in and taken
    out; a last "Combined" row when there are several."""
    name: str
    cash_in: float
    cash_out: float
    value: float
    pl: float
    pct: float | None


class InvestedPl(Shape):
    """A long-term investment account: money put in (cost basis or
    contributions) against its value now."""
    name: str
    invested: float
    value: float
    pl: float
    pct: float | None


class InvestedGroup(Shape):
    """One institution's long-term accounts and their subtotal."""
    provider: str
    accounts: list[InvestedPl]
    invested: float
    value: float
    pl: float
    pct: float | None


class InvestedTotal(Shape):
    invested: float
    value: float
    pl: float
    pct: float | None


class RetirementCombined(Shape):
    """Every long-term account grouped by institution with a grand total;
    an empty object when there are none."""
    groups: list[InvestedGroup] = []
    total: InvestedTotal | None = None
    pl_note: str | None = None
    """Which accounts' gain is counted to an older holdings snapshot
    (the balance's move since counts as money in); null when none."""


class NetWorthReport(Shape):
    """GET /api/reports/networth. With `?owner=` the report is that
    owner's live accounts only: no property layer, no history, and the
    owner named back."""
    current_total: float
    property_items: list[PropertyItem]
    property_net: float
    full_total: float
    by_institution: list[Point]
    by_asset_class: list[Point]
    month_change: float | None
    financial_month_change: float | None
    property_month_change: float | None
    month_change_text: str | None = None
    """`month_change` by part, the parts that moved only, in whole dollars
    adding up to it: "crypto −$600 · retirement −$300 · cash +$50". Null
    when there is no change or last month was not recorded by part."""
    month_withdrawn_text: str | None = None
    """This month's withdrawals to the bank that no linked account has
    received: "$250 withdrawn 03/02 has not shown up in a linked
    account". Null when there are none."""
    mortgage_payoff: float
    mortgage_entered_text: str | None = None
    """The oldest hand-entered mortgage's `entered_text`, for beside
    `mortgage_payoff`; null when no typed-in mortgage counts."""
    full_trend_estimated_until: int
    trend_windows: dict[str, TrendWindow]
    rings: NetWorthRings
    trend: list[Point]
    """Financial accounts only, month by month."""
    full_trend: list[Point]
    """The same months with the property layer added, ending at
    full_total."""
    coinbase_pl: list[CoinbasePl]
    retirement_combined: RetirementCombined
    trend_estimated_until: int
    """Index up to which `trend` is a reconstruction."""
    snapshot_trend: list[tuple[str, float | None]]
    savings_trend: list[Point]
    owner: str = ""
    """The owner lens asked for; absent on the household report."""
    built_at: str = _built_at()


class SpendingReport(Shape):
    """GET /api/reports/spending."""
    by_year: list[tuple[str, float, int]]
    """[year, spent, purchases]"""
    by_month: list[Point]
    top_merchants: list[tuple[str, float, int, str | None]]
    """The trailing twelve months: [payee, spent, purchases, logo URL]."""
    amazon: list[tuple[str, float, int]]
    category_totals: list[Point]
    yoy_years: list[str]
    yoy_cats: list[str]
    yoy_matrix: list[list[str | float]]
    """One row per category: the category, then its spend per year of
    `yoy_years`."""
    built_at: str = _built_at()


class FlowNodeShown(Shape):
    """One node of the flow picture as it is printed."""
    key: Literal["paychecks", "interest", "other", "overspent", "fixed",
                 "variable", "saved", "unsplit"]
    dollars: int
    """whole dollars; a side's parts add up to the total printed for it"""
    share: int | None
    """whole percent; null for the overspend and for a part that is not
    positive"""


class FlowIn(Shape):
    """Income by kind; every figure null when no bank feed covers the
    window."""
    total: float | None
    paychecks: float | None
    interest: float | None
    other: float | None
    shown: list[FlowNodeShown] | None
    """paychecks, interest, other (and the overspend when spending passed
    income) as printed: the dollars add up to the total, the shares to
    100; null when the income is unknown"""


class FlowOut(Shape):
    total: float
    fixed: float
    variable: float
    shown: list[FlowNodeShown]
    """fixed and variable (adding up to the total) and what stayed, as
    printed; a share is of the larger of in and out"""
    variable_scheduled: float
    """Variable spend over the months a bill schedule reached; equal to
    `variable` when nothing is unsplit."""
    unsplit: float
    """What went out in the months before the household's first bill
    began, which the picture draws apart (reporting._flow_split)."""
    unsplit_text: str | None
    """The caption under the picture when something is unsplit."""
    fixed_basis_text: str | None
    """What Fixed means on a window reaching back before most of today's
    bills began: "Fixed counts the bills you track today; 7 of these 12
    months are from before most of them began." Null when no split month
    of the window is (reporting._fixed_basis_words)."""
    split_shown: list[FlowNodeShown]
    """fixed, the scheduled months' variable, unsplit and what stayed, as
    printed — the nodes the current clients draw."""


class IncomeDue(Shape):
    amount: float
    text: str


class FlowPeriod(Shape):
    """GET /api/reports/cashflow/flow?range=: where the money came from
    and where it went over one timeframe."""
    from_: str = Field(alias="from")
    to: str
    label: str
    live_month: bool
    """The ledger starts this month: the range is the month so far."""
    so_far: bool = False
    """The window runs to today: its figures are the month so far, and no
    per-month pace is printed (reporting.window_words)."""
    heading: str = ""
    """What the window is called above its figures: "Nov 1–17 so far" for
    the month so far, else the range label."""
    income_due: IncomeDue | None = None
    """The paychecks still due this month beside the month so far
    (reporting.income_still_due); null otherwise."""
    income_clipped: Literal[False]
    """Always false; kept for installed clients that read it."""
    income_gap_months: Literal[0]
    """Always 0; kept for installed clients that read it."""
    incomplete_months: list[str]
    dating: Literal["paid"]
    income_unknown: bool
    """No month of the window has known income: `in`, `saved` and `rate`
    are null."""
    in_: FlowIn = Field(alias="in")
    out: FlowOut
    saved: float | None
    rate: float | None
    saved_months_known: int
    saved_months_total: int
    invested: float = 0.0
    """Net money into investments over the window (the Records' investment
    funding for the same days); negative for a net withdrawal."""
    invested_text: str | None = None
    """`invested` as the line under what was left over: "moved to
    investments $2,400"; null when it rounds to $0."""
    built_at: str = _built_at()


class Change(Shape):
    """A figure against the same figure for an earlier period, as printed
    (reporting.change)."""
    amount: int
    """The shown figure minus the shown figure, in whole dollars."""
    pct: int | None
    """Whole percent of the earlier exact figure, halves to even, signed
    by the direction; null when unchanged or nothing came before."""
    unchanged: bool
    """The amount is $0."""


class SpendingMover(Shape):
    """One row of "What changed"."""
    kind: Literal["category", "merchant"]
    label: str
    """The stored category (the client labels it) or the merchant."""
    current: float
    previous: float
    change: Change


class SpendingWindow(Shape):
    """GET /api/reports/spending/window?range=."""
    from_: str = Field(alias="from")
    to: str
    label: str
    months: int
    live_month: bool
    so_far: bool = False
    """The window runs to today: its figures are the month so far, and no
    per-month pace is printed (reporting.window_words)."""
    heading: str = ""
    """What the window is called above its figures: "Nov 1–17 so far" for
    the month so far, else the range label."""
    prev_text: str | None = None
    """What the comparison is against: "vs Oct 1–17" for the month so far,
    "vs the year before" for a range; null without a previous window."""
    total: float
    prev_total: float | None
    by_month: list[Point]
    incomplete_months: list[str]
    dating: Literal["paid"]
    categories: list[tuple[str, float, float | None]]
    """[category, spent, previous window's spend]; categories spent on only
    in the previous window follow at 0. The category is the value the
    ledger stores (the client labels it), so it is also the `cat` filter
    that lists its rows."""
    category_shares: list[int | None]
    """Each category's whole-percent share of `total`, in the order of
    `categories`, adding up to 100 across every category spent on; null
    for one not spent on in this window."""
    per_month: float
    """`total` a month over the window's `months`."""
    per_month_basis: str | None = None
    """What `per_month` divides by, printed after "/mo", when that is
    fewer months than the heading names ("over the 20 months with
    spending"); null when it is the heading's own months."""
    per_month_by_category: list[float]
    """Each category's spend a month, in the order of `categories`."""
    change: Change | None
    """The window against the previous one; null with no previous
    window."""
    category_changes: list[Change | None]
    """In the order of `categories`."""
    merchants: list[RankedPayee]
    merchant_changes: list[Change | None]
    """In the order of `merchants`."""
    dropped_merchants: list[RankedPayee]
    """Merchants paid only in the previous window (amount 0)."""
    movers: list[SpendingMover]
    """What "What changed" prints, in order."""
    figures_open_rows: bool = False
    """Each figure opens the rows it summed through GET
    /api/transactions?spend= (specs/spending-drill.md). A server from
    before the drill sends nothing, and a client links no figure."""
    built_at: str = _built_at()


class IncomeKinds(Shape):
    paychecks: float
    interest: float
    other: float


class IncomeKindChanges(Shape):
    paychecks: Change | None
    interest: Change | None
    other: Change | None


class SourcePaycheck(Shape):
    """One income-by-source row's paycheck verdict."""
    key: str
    """The payer's key — its display name's letters and digits — which
    POST /api/income/payer takes."""
    paycheck: bool
    """Its deposits count as paychecks."""
    override: Literal["paycheck", "never"] | None
    """The household's word; null while the rules decide."""


class IncomeWindow(Shape):
    """GET /api/reports/income/window?range=."""
    from_: str = Field(alias="from")
    to: str
    label: str
    months: int
    live_month: bool
    so_far: bool = False
    """The window runs to today: its figures are the month so far, and no
    per-month pace is printed (reporting.window_words)."""
    heading: str = ""
    """What the window is called above its figures: "Nov 1–17 so far" for
    the month so far, else the range label."""
    prev_text: str | None = None
    """What the comparison is against: "vs Oct 1–17" for the month so far,
    "vs the year before" for a range; null without a previous window."""
    income_due: IncomeDue | None = None
    """The paychecks still due this month beside the month so far
    (reporting.income_still_due); null otherwise."""
    incomplete_months: list[str]
    total: float
    per_month: float
    per_month_basis: str | None = None
    """What `per_month` divides by, printed after "/mo", when that is
    fewer months than the heading names ("over the 18 months since the
    bank feed began"); null when it is the heading's own months."""
    paycheck_share: int | None
    """The paychecks' share of the total in whole percent; null when
    nothing came in."""
    prev_total: float | None
    kinds: IncomeKinds
    kinds_shown: dict[str, int]
    """Each kind of `kinds` in the whole dollars it prints, adding up to
    `total` as printed: the Overview picture's figures for the window."""
    prev_kinds: IncomeKinds | None
    change: Change | None
    """The window against the previous one; null with no previous
    window."""
    kind_changes: IncomeKindChanges | None
    by_month: list[Point]
    sources: list[RankedPayee]
    source_changes: list[Change | None]
    """In the order of `sources`."""
    source_shares: list[int | None]
    """Each listed payer's whole-percent share of `total`, in the order of
    `sources`, decided over every payer so the shares of all of them add
    up to 100."""
    dropped_sources: list[RankedPayee]
    """Payers of the previous window who paid nothing in this one, largest
    first, each with its previous-window amount."""
    dropped_text: str | None = None
    """Those payers in one sentence under the headline, beside the change
    they explain: "Nothing this time from Acme Payroll ($900), paid the
    year before." (reporting.dropped_words); null when none dropped out"""
    source_paychecks: list[SourcePaycheck] = []
    """In the order of `sources`: whether each payer counts as paychecks
    and the household's word on it (paychecks.payer_verdicts)."""
    built_at: str = _built_at()


class SavedExtremes(Shape):
    """A timeframe's best and worst saved month, [YYYY-MM, saved]; both
    null when its window holds fewer than two finished months."""
    best: Point | None
    worst: Point | None


class GraphWindow(Shape):
    """The months a timeframe draws under "Left over, by month": every
    slot of the graph (a finished month, the month in progress, a
    forecast month) from `first` through `last`, both 'YYYY-MM'."""
    first: str
    last: str
    note: str
    """The line under the chart, as printed."""


class CashGraph(Shape):
    """Monthly net cash flow: the saved history, then the forecast from
    `forecast_from`."""
    points: list[Point]
    forecast_from: int
    current: Point | None
    """The month in progress, saved so far; null when its income is not
    known."""
    forecast_days: int
    extremes: dict[str, SavedExtremes]
    """Per timeframe key, the best and worst month of its window among the
    finished months of `points`."""
    windows: dict[str, GraphWindow]
    """Per timeframe key, the months it draws (reporting._drawn_windows):
    a timeframe of complete months ends with its last one; the month in
    progress and the forecast follow only one that includes this month."""


class YearDollars(Shape):
    income: int | None
    spend: int
    saved: int | None


class CashflowReport(Shape):
    """GET /api/reports/cashflow."""
    dating: Literal["paid"]
    savings_by_year: list[tuple[str, float | None, float, float | None,
                                float | None]]
    """[year, income, spending, saved, rate]; saved and rate are over the
    year's months with complete income, and income, saved and rate are
    null for a year with none."""
    savings_by_year_months: dict[str, tuple[int, int]]
    """Per year: [months with bank data, months]."""
    savings_by_year_dollars: dict[str, YearDollars]
    """Per year: income, spending and left over in the whole dollars the
    row prints (reporting.year_dollars, the Year lens's rule): when every
    month's income is known, left over is printed income less printed
    spending."""
    savings_by_year_notes: dict[str, str]
    """Per year holding a month with incomplete income, the words printed
    under its left-over figure ("income incomplete — left over counts 8 of 12
    months"); the row is drawn greyed."""
    monthly_by_year: dict[str, list[Point]]
    """Per year, [month label, saved] for its months with complete
    income."""
    months_by_year: dict[str, list[tuple[str, float | None]]]
    """Per year, [month label, saved] for every month from the ledger's
    first through this one; saved is null for a month with incomplete
    income, drawn as a grey slot named by `incomplete_label`."""
    incomplete_months: list[str]
    """'YYYY-MM' of every month, ledger start through this one, whose
    income the bank feed does not measure: counted in and out, left out
    of saved and the rate."""
    incomplete_label: str
    """The words for one such month."""
    cash_graph: CashGraph
    reported_income: list[tuple[int, float | None, float | None,
                                float | None, float | None, int | None]]
    """[year, total income, wages, investment income, tax paid, joint]"""
    career_income: list[tuple[int, float]]
    income_by_month: list[Point]
    investment_funding: list[Point]
    brokerage_funding_mtd: float
    brokerage_funding_ytd: float
    built_at: str = _built_at()


class FeesReport(Shape):
    """GET /api/reports/fees: investment fee drag."""
    by_platform: list[tuple[str | None, float, int]]
    """[institution, fees, rows]; an estimated advisory fee counts its
    years as rows. The institution is null when the connection has no
    name."""
    platform_counts: list[str]
    """In the order of `by_platform`, what each row's count counts: "40
    charges", or "5 years, estimated" for an estimated advisory fee."""
    by_year: list[Point]
    total: float
    built_at: str = _built_at()


class CashCostYearShown(Shape):
    """A year's cells in the whole dollars they print: interest + fees −
    refunds = net."""
    interest: int
    fees: int
    refunds: int
    net: int


class CashCostTilesShown(Shape):
    """The card's tiles in whole dollars; `vs_earned_ytd` is `earned_ytd`
    less `paid_ytd` as printed."""
    paid_ytd: int
    earned_ytd: int
    vs_earned_ytd: int


class CashCostYear(Shape):
    year: str
    interest: float
    fees: float
    refunds: float
    net: float
    biggest: tuple[str, float, str, CashCostKind] | None
    """[name, amount, date, kind] of the year's largest charge."""
    shown: CashCostYearShown
    biggest_text: str | None
    """The largest charge as the row prints it, dated in full: "Service fee
    $35 · 05/14/24"; null for a year with no charge."""


class CashCostsReport(Shape):
    """GET /api/reports/cash-costs: interest and fees paid on bank and
    card accounts, by year. An empty `by_year` means nothing to show."""
    by_year: list[CashCostYear]
    ytd: CashCostYear | None
    paid_ytd: float
    """This year's interest and fees less refunds; 0 with nothing
    charged."""
    interest_ever: float
    earned_ytd: float
    vs_earned_ytd: float
    shown: CashCostTilesShown
    latest: tuple[str, float, str, CashCostKind, str] | None
    """[name, amount, date, kind, account] of the newest charge."""
    built_at: str = _built_at()
