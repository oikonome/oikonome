"""Today, the month and year lenses, budget snapshots and suggestions, the
activity log, alerts and the calendar (web/api_today.py).

These replies carry the figures people read most. Each model is what the
handler and the engine it reads send today; where a client reads a field
the server does not send, that is recorded by the client's own type, not
added here.
"""

from __future__ import annotations

from typing import Any, Literal

from . import Shape
from .ledger import Txn
from .reports import Change

Verdictish = str
"""the verdict word the engine chose ("on track", …)"""


# ---- pieces shared by Today and the lenses ---------------------------------

class Chip(Shape):
    text: str
    tone: str


class SimpleFace(Shape):
    """The simple Today face's server-composed pieces, shared with the
    email."""
    left_today: float
    chips: list[Chip]


class VariableScale(Shape):
    factor: float
    static_total: float
    available: float


class PlanShares(Shape):
    """Each leg's whole-percent share of the income, by largest
    remainder: they add up to 100 when the legs are the whole income;
    null for a leg that is not positive."""
    bills: int | None
    to_spend: int | None
    saved: int | None
    excess: int | None


class PlanLine(Shape):
    """The plan line in whole dollars: income − bills − to_spend − saved
    = excess as printed. `saved` is null when the line has no savings
    leg."""
    income: int
    bills: int
    to_spend: int
    saved: int | None
    excess: int
    shares: PlanShares


class PlanLegs(Shape):
    """The plan line's legs in whole dollars, for every plan
    (budget.plan_legs): plan_line's terms when the plan has an income;
    without one, `income` and `excess` are null and each other leg is its
    own figure. `saved` is null when the line has no savings leg."""
    income: int | None
    bills: int
    to_spend: int
    saved: int | None
    excess: int | None


class PlanExcessWords(Shape):
    text: str
    """"planned excess", or "planned excess · none this month" once the
    month's spending is past its budget"""
    tone: Literal["pos", "mut", "neg"]


class FixedBucket(Shape):
    actual: float
    expected: float
    month_budget: float


class TodayBucketChild(Shape):
    """A carve-out budget inside Food or Other."""
    name: str
    actual: float
    expected: float
    month_budget: float
    paced_budget: float


class TodayVariableBucket(Shape):
    actual: float
    expected: float
    month_budget: float
    paced_budget: float
    children: list[TodayBucketChild]
    display_actual: float
    display_expected: float
    remaining_budget: float
    paced_remaining_budget: float


class TodayBuckets(Shape):
    fixed: FixedBucket
    food: TodayVariableBucket
    other: TodayVariableBucket


OverdueEntry = tuple[str, float, str, str | None]
"""[payee, amount, due date, the date the feed withdrew its charge]"""


# ---- /api/today/full -------------------------------------------------------

class PlanGoal(Shape):
    name: str
    monthly_plan: float


class RowStatus(Shape):
    """What a money-map row says beside its figures
    (todayview.row_status)."""
    kind: Literal["over", "ahead", "left", "due"]
    amount: float
    """over: past the budget; ahead: more than planned; left: left;
    due: still due."""
    pending: float
    """due: awaiting (0 otherwise)."""
    vs_pace: float | None
    """Today's judged rows: spent less what should have been spent by
    today (None elsewhere)"""
    text: str
    """the words every surface prints beside the row"""
    tone: Literal["neg", "pos", "mut"]
    """their colour: bad news, good news, neither"""


class BillsLateItem(Shape):
    """A bill of an earlier month still unpaid, which the running month
    carries as still due."""
    payee: str
    amount: int
    """whole dollars"""
    due: str
    """the date it was due"""
    y: int
    m: int
    """the year and month it was due in"""
    inside_plan: bool
    """on the Year's row: planned by one of the year's budgeted months
    (false: outside this year's plan, said apart)"""


class BillsUnpaidItem(Shape):
    """An occurrence of a month that is over that no payment met."""
    payee: str
    amount: int
    """whole dollars"""
    due: str


class BillsTableRow(Shape):
    """One row of the Bills row's "How this adds up" table."""
    label: str
    """what the row is, in plain words"""
    amount: int
    """whole dollars, never below nothing (the sign says which way)"""
    sign: Literal["+", "-", "="]
    """"+" added, "-" taken off, "=" the total the rows above add up to"""
    figure: str
    """the amount as printed, with its sign ("+$1,000", "−$35")"""


class BillsTable(Shape):
    """How the Bills row's figures add up to the plan
    (bills_breakdown.table): the signed `rows` add up to the last one,
    the plan as printed. A bill late from an earlier month is still due
    and in none of the plan: it is listed under `outside_heading` in
    `outside`, in no sum."""
    rows: list[BillsTableRow]
    outside_heading: str | None
    outside: list[BillsTableRow]


class BillsBreakdown(Shape):
    """A month's due-dated bills in parts that add up to its plan, in the
    whole dollars every surface prints (bills_breakdown):
    plan = posted − refunds + due + not_paid + under (+ moved and the late
    bills inside the plan, on the Year). `late` is still due and in none
    of this month's plan. No envelope is in any of it: the pools are the
    Envelopes row's (EnvelopeBreakdown)."""
    kind: Literal["running", "closed", "future", "year"]
    plan: int
    posted: int
    refunds: int = 0
    """by how much the bill refunds came to more than the charges (posted
    is then nothing): a part of its own, taken off the plan's sum"""
    due: int
    """this month's own bills not paid yet, through month end"""
    awaiting: int
    """the part of `due` that is due already and not posted"""
    late: list[BillsLateItem]
    not_paid: int
    """a month that is over: the plan no payment met"""
    unpaid: list[BillsUnpaidItem]
    under: int
    """what the plan had left after the bills that posted for less or
    more than planned; below zero, paid over plan"""
    moved: int = 0
    """the Year's row: a closed month's unpaid bill whose due date has
    moved into this month since, planned there and owed here once"""
    still_due: int
    """due and the late bills: the one still-due figure"""
    total: int
    """the Bills row's right figure: posted + still due on a running
    month, otherwise the plan"""
    text: str | None
    """the words every surface prints beside the row: its still-due
    figure, or nothing"""
    settled: bool = False
    """nothing more will post against it: a month that is over, or a
    year that is finished"""
    y: int | None = None
    """the Year: its year (None on a month)"""
    past: bool = False
    """the Year: a year that is over, named by its number"""
    table: BillsTable | None = None
    """how posted and still due add up to the plan, which the row opens
    (absent from an older server: no table to open)"""


class EnvelopeItem(Shape):
    """One pool on the Envelopes row (envelope_breakdown), whole
    dollars."""
    uid: str | None
    """the pool's stable id (none on a month frozen before pools had
    one)"""
    payee: str
    period_months: int
    """12 for a yearly pool, 1 for a monthly one"""
    pool: int
    drawn: int
    """what the pool paid out in the period (what the matcher booked to
    it; spend past the pool is `overflow`, counted in spending)"""
    left: int
    """what is left in the pool at the period's end (now, on a running
    month)"""
    cap: int
    """the bar's right figure: drawn + left (a monthly pool's Year: its
    months' pools)"""
    year_drawn: int
    """a yearly pool's year so far, as the matcher booked it"""
    overflow: int
    """what the pool's own period spent past it — counted in Food or
    Everything else, never in this row's figures"""
    unspent: int
    """a settled period: what the pool did not spend"""
    outside: int
    """the Year, a yearly pool: the pool less its cap, signed — used
    before the plan's months or before it was set up, or resized"""
    gone: bool
    """the Year: the pool is no longer tracked"""
    shown_used: int
    shown_of: int
    """the pool's own pair, printed wherever it is named: its period's
    spend of its size (budget.envelope_shown)"""
    shown_period: str
    """that pair's period, said after it: this year, this month, in a
    past year, or over the plan's months"""
    pair_spent: int
    """the line's first figure: on a month, the spent-of-cap pair's used
    (budget.envelope_shown); on the Year, everything the Year's months
    charged at the pool's merchants"""
    pair_of: int
    """the pool (a monthly pool's months' pools on the Year)"""
    pair_past: int
    """what the matcher sent to variable spending for this pool: this
    month's on a month, the Year's months' on the Year — never pair_spent
    less pair_of"""
    pair_text: str
    """the pool's line as every surface prints it (pool_line)"""


class EnvelopeTableLine(Shape):
    label: str
    text: str


class EnvelopeTableFigure(Shape):
    label: str
    amount: int
    figure: str
    sign: Literal["+", "-"] | None = None


class EnvelopeTable(Shape):
    """The list the Envelopes row opens (envelope_breakdown.table): one
    line per pool, on the Year a signed line for each cause a yearly
    pool did not draw all of it, and the plan's set-aside. A list, not a
    sum."""
    rows: list[EnvelopeTableLine]
    outside: list[EnvelopeTableFigure]
    total: EnvelopeTableFigure


class EnvelopeBreakdown(Shape):
    """A period's pools, monthly and yearly, as the Envelopes row prints
    them (envelope_breakdown): a pool gauge, drawn / cap, never past its
    right figure; "over" only from `overflow`."""
    kind: Literal["running", "closed", "future", "year"]
    drawn: int
    cap: int
    set_aside: int
    """the plan's monthly share of the pools (the caption's "set aside")"""
    overflow: int
    reserve: int
    """a running month: set aside to month end (the envelopes half of
    month_end_parts)"""
    unspent: int
    settled: bool
    past: bool
    y: int | None
    text: str | None
    items: list[EnvelopeItem]
    table: EnvelopeTable


class ServerPlanRow(Shape):
    """One plan row as the server builds it (todayview._plan_rows)."""
    label: str
    judged: bool
    to: str | None
    actual: float
    expected: float
    budget: float
    pending: float
    caption: str | None
    actual_label: str | None
    status: RowStatus | None
    breakdown: BillsBreakdown | EnvelopeBreakdown | None = None
    """the Bills row's parts (None on a past day), the Envelopes row's
    pools; None on every other row"""


class MonthEndParts(Shape):
    """What the month still owes through its end, the figure the cash
    tiles subtract, and its two halves in whole dollars that add up to it
    as printed."""
    total: float
    bills: int
    envelopes: int
    text: str | None
    """"bills $X + envelopes $Y" under the "After cards & bills" tile, or
    null when the month has nothing set aside in a pool"""


class PlanTotal(Shape):
    label: str
    judged: bool
    actual: float
    expected: float
    budget: float
    status: RowStatus | None


class IrregularBill(Shape):
    payee: str
    amount: float
    """what it cost once paid, else its plan"""
    label: str
    """the cadence ("annual"), with ", paid" once it is"""
    paid: bool
    due: str


class Allowance(Shape):
    """One everyday bucket's day: what is left today and how the month
    stands."""
    rate: float
    plan: float
    spent_today: float
    today_allowance: float
    left_today: float
    month_budget: float
    month_spent: float
    empty: bool
    zero_day: bool
    daily_note: str | None
    recover_days: int | None
    recover_note: str | None
    day_line: str
    """the line under the day's figure ("of $40 today · nothing spent
    yet"), printed alike on every surface and width"""
    rate_dollars: int = 0
    """the rate in the whole dollars every surface prints (floored)"""
    today_allowance_dollars: int = 0
    """today's allowance in printed whole dollars (floored)"""
    left_dollars: int = 0
    """what is left of today in printed whole dollars (floored; $0 once
    over) — the tile's figure"""


class KidAllowance(Shape):
    """A carve-out budget's own day tile."""
    name: str
    rate: float
    rate_days: int
    plan: float
    spent_today: float
    today_allowance: float
    left_today: float
    month_budget: float
    month_spent: float
    zero_day: bool
    daily_note: str | None
    daily_note_tone: str | None
    recover_days: int | None
    recover_note: str | None
    day_line: str
    to: str
    rate_dollars: int = 0
    """the rate in the whole dollars every surface prints (floored)"""
    today_allowance_dollars: int = 0
    """today's allowance in printed whole dollars (floored)"""
    left_dollars: int = 0
    """what is left of today in printed whole dollars (floored; $0 once
    over) — the tile's figure"""


class BillCard(Shape):
    kind: Literal["envelope", "bill"]
    label: str
    left: float
    over: bool
    sub: str
    frac: float
    pace_frac: float
    status: str | None
    status_tone: Literal["neg", "pos", "warn"] | None


class WhyChip(Shape):
    payee: str
    amount: float
    count: int


class Reason(Shape):
    bucket: str
    variance: float
    pace: str
    detail: list[str]


class WhyTop(Shape):
    payee: str
    amount: float
    count: int


class WhyEntry(Shape):
    name: str
    tone: Literal["over", "on", "under"]
    text: str
    spent: float
    budget: float
    spendable: float
    rate: float
    rate_days: int
    variance: float
    top: list[WhyTop]
    to: str


class Why(Shape):
    headline: str
    entries: list[WhyEntry]
    recovery: str | None


class Alert(Shape):
    kind: str
    severity: str
    message: str
    link: str | None = None


class Runway(Shape):
    checking: float | None
    card_debt: float
    due_total: float
    balance_unreliable: bool
    no_checking: bool


class CashNotice(Shape):
    """Today's cash notice: `kind` picks the sentence, `amount` the dollar
    it quotes, `until` the date it runs to; `ask` replaces the alarm with
    the missing setup step."""
    state: Literal["critical", "tight"] | None
    kind: Literal["today", "date", "forecast", "headroom"] | None
    amount: float | None
    slow_to_per_day: float | None
    until: str | None
    ask: str | None
    runs_out: str | None
    """the first day the forecast is below $0, when it is"""
    text: str | None
    """the notice in the words every surface prints (None when quiet)"""
    scheduled: bool = False
    """the dip comes from bills and card payments due, not from spending:
    no spending advice (slow_to_per_day is null)"""
    card_debt: float | None = None
    """the card debt a today or headroom notice assumes paid off now: the
    cash card's "card debt", posted balances and pending charges
    (forecast.card_debt_total); null for the other kinds and from an
    older server"""


class DayMeter(Shape):
    spent: float
    allow: float


DatedAmount = tuple[str, float, str]
"""[date, amount, what]"""


class Series(Shape):
    """One balance walk of the forecast."""
    series: list[tuple[str, float]]
    min: float
    min_date: str
    negative_date: str | None
    first_neg_amount: float | None
    end: float
    rate: float
    sweep_events: list[DatedAmount]


class PaceBasis(Shape):
    """The daily pace the forecast walks burn, before the envelope drip
    (forecast.forecast_pace)."""
    basis: Literal["trailing", "plan"]
    """trailing: the last `days` finished days' variable spending less
    their one-offs; plan: the plan's daily, on a ledger under a week old"""
    days: int
    daily: float


class OneOffCharge(Shape):
    """A charge the forecast's daily pace leaves out
    (forecast.window_one_offs)."""
    txn_id: str
    payee: str
    amount: float
    date: str


class ScenarioRow(Shape):
    """One dated row of a card scenario's cash timeline."""
    date: str
    amount: float | None
    """null on the month-end row (a balance, not an event)"""
    label: str
    balance: float | None
    """the scenario's balance at the end of that day"""
    kind: Literal["event", "month_end"]
    card: str | None
    """the card a bill is charged to: it moves no checking balance on its
    day, it leaves with the card's payment"""
    estimated: bool = False
    """the amount is the forecast's estimate (a seasonal bill, a
    paycheck's yearly pattern); the label says so"""
    after_cards: float | None = None
    """cash after card balances that day (forecast pace_now): checking
    less everything owed on cards — the list's second column, headed
    Forecast.after_cards_label"""


class DueMark(Shape):
    """A card's statement due date on the cash chart
    (todayview.due_marks): the same dates in every scenario."""
    date: str
    """the real due date (a payment due on a weekend lands after it)"""
    card: str
    label: str
    """the chart's caption ("Harbor card due")"""
    words: str
    """the hovered day's line in this scenario ("Harbor card due — $412
    owed", "… — paid", "… — carried balance")"""


class BandEdges(Shape):
    """A walk's shaded band from the forecast's own past misses
    (forecast_score.band): every day of the chart, low edge and high."""
    lo: list[tuple[str, float]]
    hi: list[tuple[str, float]]


class CashScenario(Shape):
    """One card scenario of the cash timeline (todayview.CASH_SCENARIOS):
    usual (each card paid the way the household's own payments show,
    never after its due date — the headline), now (pay all cards today,
    then your usual: one payoff on the walk's first day, then the usual
    walk), due (every card paid at its statements' due dates)."""
    key: Literal["usual", "now", "due"]
    label: str
    """the words every surface labels it with ("Your usual")"""
    series: list[tuple[str, float]]
    min: float
    min_date: str
    negative_date: str | None
    low_words: str
    """the low point in words ("$3,270 on 05/12/27", "· below $0 from
    …" when it goes negative), printed side by side for the three"""
    low_cards_note: str | None = None
    """what the same walk still holds on the cards on its low day ("with
    $2,950 still on your cards then"), printed under the low; null with
    nothing on the cards and from an older server"""
    runs_out_label: str
    """the chart's mark ("runs out MM/DD/YY"), empty above $0"""
    marks: list[DatedAmount]
    """dated payments of a card paid by autopay (todayview.autopay_marks);
    an older client draws them as "autopay" lines"""
    due_marks: list[DueMark] | None = None
    """each card's statement due dates inside the chart, drawn as lines in
    every scenario; an older server sends none (the clients draw `marks`)"""
    rows: list[ScenarioRow]
    """the timeline after today, through the month end or two weeks,
    whichever is later, and never short of this scenario's low point
    (todayview.timeline_horizon)"""
    heavy: list[tuple[str, float]] | None = None
    """the same walk in a heavier month (forecast.heavy_month_factor),
    drawn beside it"""
    heavy_label: str | None = None
    """the heavier line's legend words"""
    band: BandEdges | None = None
    """the range the forecast's own past misses suggest around this
    walk, once enough nights are graded (specs/forecast-grading.md); the
    clients draw it shaded in place of the heavier line"""
    band_text: str | None = None
    """the band's sentence, printed under the chart while it is drawn"""
    band_label: str | None = None
    """the band's legend words, printed as sent"""
    cards_after_horizon: float = 0.0
    """card spending the walk still owes on its last day"""
    cards_note: str | None = None
    """that sum in words, printed under the chart when over $0.50"""


class CashBand(Shape):
    """The quiet cash line (todayview.cash_band): the usual walk stays at
    or above $0, but cash after card balances (kind after_cards: card
    balances more than checking on some day), a heavier month (kind
    heavier) or the low edge of the forecast's own band (kind band) does
    not. Kind now is an older server's name for after_cards."""
    kind: Literal["now", "heavier", "after_cards", "band"]
    low: float
    date: str
    text: str


class ForecastScore(Shape):
    """How close the forecast has landed on this household
    (todayview.score_sentences); every sentence printed as sent."""
    text: str | None
    """"Over the last 3 months, the 30-day forecast landed within $250 of
    your real balance 4 times in 5.", once enough nights are graded"""
    bias_text: str | None
    """only when the misses lean one way"""
    note: str | None
    """the first weeks' "checks itself" line (web and phone only)"""
    horizon: int | None
    basis: Literal["own", "none"]
    """own: the band on the chart is the forecast's own misses"""


class Forecast(Shape):
    days: int
    checking: float
    card_debt: float
    pace_now: Series
    pace_stmt: Series
    card_autopay: list[DatedAmount]
    """the due-date walk's payments of cards paid by autopay, which an
    older client draws as "autopay" lines"""
    runs_out_label: str
    """the chart's mark beside the first day below $0 ("runs out
    MM/DD/YY"), empty when the walk stays above it"""
    one_offs: list[OneOffCharge]
    pace_note: str | None
    """the sentence naming the forecast's daily pace, the window it was
    read over and the one-offs it leaves out"""
    pace_basis: PaceBasis | None = None
    scenarios: list[CashScenario] | None = None
    """the three card scenarios, "usual" first; an older server sends
    none and the clients draw pace_stmt and pace_now"""
    card_timing_note: str | None = None
    """when card spending reaches checking in the usual walk, in words"""
    transfers_note: str | None = None
    """that money moved between the household's own accounts is not in
    the forecast, with how much moved in or out of checking over the last
    60 days (todayview.transfers_note); null below the threshold, when the
    forecast walks a savings plan, and from an older server"""
    stopped_paychecks_note: str | None = None
    """which paychecks the forecast leaves out because their money stopped
    arriving (todayview.stopped_paychecks_note); null while every paycheck
    is walked, and from an older server"""
    after_cards_label: str | None = None
    """the heading of the scenario rows' after_cards column ("after card
    balances")"""
    score: ForecastScore | None = None
    """the forecast's track record; null before its first kept night"""


class ForecastHistoryDay(Shape):
    day: str
    """the day a 30-day forecast pointed at"""
    made: str
    """the night it was made"""
    predicted: float
    actual: float
    error: float
    """actual − predicted"""
    rebuilt: bool
    """the real balance was rebuilt from the next night's"""


class ForecastScorePanel(Shape):
    """"How has the forecast done?" (forecast_score.panel)."""
    history: list[ForecastHistoryDay]
    coverage: float | None
    coverage_text: str | None
    card_paying_text: str | None
    skips_text: list[str]
    model_changes: list[str]
    """the days the forecast's model changed"""
    empty_text: str | None


class ForecastMissPart(Shape):
    kind: Literal["paychecks", "bills", "card_payments", "spending",
                  "transfers", "other", "pending"]
    label: str
    predicted: float
    actual: float
    diff: float


class ForecastMissItem(Shape):
    text: str
    amount: float
    date: str | None


class ForecastMiss(Shape):
    """"Why the forecast missed" (forecast_score.miss): the parts add up
    to `error` to the cent."""
    day: str
    made: str
    horizon: int
    predicted: float
    actual: float
    error: float
    rebuilt: bool
    parts: list[ForecastMissPart]
    items: list[ForecastMissItem]


class ForecastRow(Shape):
    date: str
    amount: float
    label: str
    bal_now: float | None
    bal_stmt: float | None
    kind: Literal["event", "month_end"]
    """month_end: the month's last day, a balance with no amount"""
    card: str | None = None
    """the card a bill is charged to (it moves no checking balance that
    day), else null"""
    estimated: bool = False
    """the amount is the forecast's estimate; the label says so"""


class SavingsGoalProgress(Shape):
    name: str
    target: float
    target_date: str | None
    monthly_plan: float
    saved: float | None
    rate_90d: float | None
    saved_90d: float | None
    pct: float | None
    eta: str | None
    on_pace: bool | None
    contributed_90d: bool | None
    plan_only: bool
    needs_account: bool
    mode: Literal["monthly", "sweep"]
    swept_this_month: float | None
    sweep_satisfied: bool | None
    saved_this_month: float | None


class TodayFull(Shape):
    """GET /api/today/full: the Today page (and the back-navigated day
    with ?date=)."""
    date: str
    is_today: bool
    real_today: str
    min_date: str | None
    yesterday: str
    """start of the "recent" window"""
    day_of_month: int
    days_in_month: int
    verdict: Verdictish
    today_view: Literal["summary", "detail"]
    simple: SimpleFace
    variance: float
    tolerance: float
    variable_actual: float
    variable_budget: float
    month_share: float
    variable_budget_paced: float
    month_remaining_paced: float
    paced_from: str | None
    income: float | None
    income_received: float | None
    income_expected: float | None
    income_source: Literal["paychecks", "scenario", "typed"] | None
    plan_surplus: float | None
    savings_suggested: int | None = None
    """The monthly savings the planner suggests on the saved plan: what an
    ordinary month leaves (the income series' average, not this month's
    own deposits) with the plan's own Savings goal put back, in whole
    dollars and never below 0 (forecast.plan_savings_suggestion). Null
    with no income to plan on, and on a past day."""
    avg_bills: float
    plan_variable: float
    scheduled_month: float
    savings_plan: float
    plan_goals: list[PlanGoal]
    plan_line: PlanLine | None
    plan_legs: PlanLegs
    """the Budget page's summary line, an income-less plan included
    (budget.plan_legs); the clients print these legs alone"""
    plan_excess_words: PlanExcessWords | None
    """the words and tone after the summary line's excess figure
    (todayview.plan_excess_words); null when it prints no excess"""
    bills_weight: str | None
    """beside the plan line's bills (the evened-out average): that they
    are evened out, and this month's own bills when they sit well away
    from it (todayview.bills_weight)"""
    avg_bills_count: int
    plan_rows: list[ServerPlanRow]
    plan_total: PlanTotal
    sweep_plan: float
    sweep_available: float | None
    sweep_line: str | None
    variable_scale: VariableScale | None
    irregular_bills: list[IrregularBill]
    overdue_unpaid: list[OverdueEntry]
    mtd: list[tuple[str, float]]
    amazon_subs: list[tuple[str, float]]
    amazon_total: float
    allow: dict[str, Allowance]
    """keyed by bucket (food, other)"""
    allow_kids: list[KidAllowance]
    bill_cards: list[BillCard]
    days_left: int
    pace_line: str
    month_words: str | None = None
    """the words after the hero's month figure: "over the month's $1,234 ·
    9 days left" (todayview.hero_face)"""
    left_label: str | None = None
    """the label over the left-to-spend number"""
    why_chips: list[WhyChip]
    headroom: float | None
    buckets: TodayBuckets
    fixed_unpaid_due: float
    reasons: list[Reason]
    why: Why | None
    alerts: list[Alert]
    runway: Runway | None
    bills_remaining_month: float | None
    month_end_parts: MonthEndParts | None = None
    """bills_remaining_month split for printing (report.month_end_parts);
    null on a past day"""
    plan_bills_label: str | None = None
    """the plan line's bills leg in words: "bills & envelopes" when the
    plan has a pool, else "bills" (absent: an older server)"""
    income_due_dates: list[str] | None
    income_due_total: float | None
    income_due_note: Literal["none", "budgeted", "off"] | None
    after_cards_bills: float | None
    after_cards_bills_paychecks: float | None
    month_end_left: float | None
    """checking at the month's end under the usual pattern
    (todayview.month_end_left)"""
    month_end_note: str | None
    month_end_label: str | None = None
    """the tile's label ("checking at month end")"""
    month_end_cards_note: str | None = None
    """what the same walk still holds on the cards that day and its next
    card payment after the month, in words; null with nothing on them"""
    cash_notice: CashNotice
    cash_band: CashBand | None = None
    """the heavier-month line under the notice; null when quiet or when
    the cash notice speaks"""
    day_meter: DayMeter
    recent_variable_spending: float
    next_paycheck: str | None
    recent: list[Txn]
    forecast: Forecast | None
    forecast_rows: list[ForecastRow]
    savings_goals: list[SavingsGoalProgress]


class TodayGlance(Shape):
    """GET /api/today/glance: the widget's few figures. With no budget set
    only the date, budgets_set and as_of are sent."""
    date: str
    budgets_set: bool
    as_of: str
    verdict: str = ""
    left_today: int = 0
    day_spent: int = 0
    day_allow: int = 0
    days_left: int = 0
    pace_line: str = ""
    chips: list[Chip] = []


# ---- the lenses ------------------------------------------------------------

class Projection(Shape):
    pace_total: float
    variance: float
    tolerance: float
    verdict: str
    tail: str | None = None
    """the Month hero's words after the projected figure: "$120 over",
    "$40 under", "on plan" (lenses.hero_words)"""
    tail_tone: str | None = None
    """neg, pos or warn"""


class HeroWords(Shape):
    """A line of the Month hero in the server's words, and its colour."""
    text: str
    tone: str
    """neg, pos or warn"""


class LensBucketChild(Shape):
    name: str
    actual: float
    expected: float
    month_budget: float


class LensVariableBucket(Shape):
    actual: float
    expected: float
    month_budget: float
    children: list[LensBucketChild]
    display_actual: float
    display_expected: float
    remaining_budget: float


class LensBuckets(Shape):
    fixed: FixedBucket
    food: LensVariableBucket
    other: LensVariableBucket


class LensPaidBill(Shape):
    date: str
    amount: float
    payee: str
    category: str | None
    detailed: str | None


class LensEnvelope(Shape):
    payee: str
    uid: str | None = None
    """the pool's stable id"""
    drawn: float = 0.0
    """what the pool paid out this month: its part of the Envelopes
    row's gauge"""
    cap: float = 0.0
    """drawn + what the pool has left: its part of the gauge's right
    figure"""
    left: float = 0.0
    unspent: float = 0.0
    """a settled month: what the pool did not spend"""
    used: float
    monthly: float
    overflow: float
    pool: float
    period_months: int
    period_used: float
    period_left: float
    show_today: bool
    shown_used: float
    """what the row prints as spent: a yearly pool's spend this year, a
    monthly envelope's this month"""
    shown_of: float
    """what the row prints it against: a yearly pool, a monthly cap"""
    shown_period: str | None = None
    """the period of the pool's pair, said after it: "this year", "this
    month" (absent: an older server)"""
    pair_spent: float | None = None
    """the spent-of-cap pair's used (budget.envelope_shown); with `pair_of` the pair
    a current client
    prints (envelope_breakdown.pool_line; absent: an older server, which
    prints `shown_used` / `shown_of`)"""
    pair_of: float | None = None
    """the pool as it stands"""
    pair_past: float | None = None
    """what the matcher sent to variable spending for this pool this
    month — never pair_spent less pair_of"""
    pair_text: str | None = None
    """the pool's line, printed as sent (envelope_breakdown.pool_line;
    absent: an older server)"""


class LensBills(Shape):
    posted: float
    posted_counts: str
    """What `posted` holds, in the words beside it: the bill payments
    ("3"), or those and the envelope charges it counts too ("3 bills + 12
    envelope charges") (lenses.posted_counts)."""
    planned: float
    awaiting: float
    paid: list[LensPaidBill]
    overdue: list[OverdueEntry]
    envelopes: list[LensEnvelope]
    notes: list[str] = []
    """The month's lines about its bills, the parts of its breakdown: a
    closed month's "$28 under plan", "not paid: Gym Pass $50" (such a
    month sends `awaiting` 0 and an empty `overdue`); a running month's
    "$275 still due", "$15 under plan"."""
    breakdown: BillsBreakdown | None = None
    """the month's bills in parts (the Bills row's own); `posted`,
    `planned` and `awaiting` above are its whole dollars"""
    bills_posted: float | None = None
    """the due-dated bills' posted, the Bills row's own (`posted` holds
    the pools too, as an installed client reads it); absent: an older
    server"""
    bills_posted_counts: str | None = None
    """what `bills_posted` holds, in the words beside it"""
    bills_planned: float | None = None
    """the due-dated bills' plan"""
    bills_awaiting: float | None = None
    """the part of the bills still due that is due already"""
    envelopes_set_aside: float = 0.0
    """the plan's monthly share of the pools, apart from `planned` (the
    Envelopes row's "set aside")"""
    set_aside: float | None = None
    """`planned` + `envelopes_set_aside`: the month's whole plan as
    printed (absent: an older server, whose `planned` is the whole)"""


class LensTxn(Shape):
    date: str
    amount: float
    payee: str
    category: str
    account: str | None
    pending: bool
    fixed: bool


class LensVariableTxn(LensTxn):
    items: list[str]
    """what was bought, when a receipt or order says"""


class LensIncome(Shape):
    actual: float | None
    budgeted: float | None
    source: Literal["paychecks", "scenario", "typed"] | None
    plan_text: str | None
    """The words beside the income figure, the plan's income named by its
    source ("expected from paychecks $4,000"); null with no plan figure or
    when it is the same whole dollars as what came in."""


class LensGoal(Shape):
    monthly_plan: float
    rate_90d: float | None


CategoryEntry = tuple[str, float, str | None]
"""[display label, amount, the stored key the ledger filter matches]"""


class MonthLensData(Shape):
    """GET /api/lens/month: the month report card."""
    remaining_word: str | None = None
    """between the hero figure and the budget: "over", "left of", "left
    unspent of"; null on an unjudged month (lenses.hero_words)"""
    pace_expected: float | None = None
    """where the hero bar's pace tick stands: the spend the pace expects"""
    variance_text: HeroWords | None = None
    """the verdict line: "$300 over pace so far", "within tolerance
    ($50)" """
    y: int
    m: int
    status: str
    as_of: str
    days_in_month: int
    verdict: str | None
    variance: float
    tolerance: float
    budget_set: bool
    pace_label: str | None = None
    """The hero bar tick's label: "today's pace" on the month in progress,
    null on a closed or future month (no label printed)."""
    standing: str = "judged"
    """Whether a budget of the household's own stands behind the month
    (lenses.month_standing): "judged", "no_budget" (none saved yet),
    "before_budget" (a closed month from before there was one) or
    "no_data" (a closed month with no rows at all)."""
    standing_label: str | None = None
    """For a month that is not judged, the pill in place of a verdict."""
    standing_text: str | None = None
    """...and the sentence under it."""
    income_text: str | None = None
    """Set when the bank feed does not measure the month's income
    ("income incomplete"): printed in place of the income figure and the
    net."""
    projection: Projection | None
    variable_actual: float
    variable_budget: float
    buckets: LensBuckets
    plan_rows: list[ServerPlanRow]
    """The month's money map rows (todayview.map_rows), as Today sends
    them. Empty for a month from before the budget."""
    plan_total: PlanTotal
    """The Variable spending row over the judged rows."""
    bills: LensBills
    by_category: list[CategoryEntry]
    amazon_subs: list[CategoryEntry]
    amazon_total: float
    by_category_variable: list[CategoryEntry]
    biggest: list[LensTxn]
    biggest_variable: list[LensVariableTxn]
    income: LensIncome
    dating: Literal["bill_month"]
    spend_total: float
    remaining: float
    """The spendable left (`variable_budget` less `variable_actual`);
    negative is over."""
    net: float | None
    """Income less `spend_total`; null while income is unknown."""
    variable_scale: VariableScale | None
    plan_surplus: float | None
    saved_month: float | None
    savings_goal: LensGoal | None
    budget_source: Literal["snapshot", "live"]
    bills_source: Literal["snapshot", "live"]
    first_year: int | None


class YearCell(Shape):
    """One month of the year: a future month carries no figures, a month
    before the ledger only its money in and out."""
    m: int
    status: str
    verdict: str | None
    variance: float | None
    variable_actual: float | None
    variable_budget: float | None
    income: float | None = None
    spend: float = 0.0
    net: float | None = None
    label: str | None = None
    """The pill of a cell that is not a verdict (lenses.cell_words):
    "net", "spent" when the month's income is not measured, "no data"."""
    figure: str | None = None
    """...and the figure under it, as printed ("+$1,200", "−$300",
    "$450"); null for a month with no data. Under a verdict, the margin
    in words ("over by $300", "under by $120", "on budget")."""
    tone: str | None = None
    """pos, neg or mut"""
    note: str | None = None
    """"income incomplete" on a month the bank feed does not measure: the
    cell is drawn greyed."""


class YearTotalsDollars(Shape):
    """The totals in the whole dollars the page prints
    (lenses.totals_dollars): when every month's income is known, left over
    is printed income less printed spend."""
    income: int | None
    spend: int
    saved: int | None


class YearTotals(Shape):
    income: float | None
    spend: float
    saved: float | None
    rate: float | None
    dollars: YearTotalsDollars
    saved_months_known: int
    saved_months_total: int
    unknown_text: str | None = None
    incomplete_text: str | None = None
    """Set when some elapsed month has incomplete income ("income
    incomplete — left over counts 8 of 12 months"): income, saved and the
    rate count the other months. Printed under the totals."""


class AnnualBucket(Shape):
    actual: float
    expected: float
    month_budget: float


class AnnualBuckets(Shape):
    food: AnnualBucket
    other: AnnualBucket
    fixed: AnnualBucket


class YearChanges(Shape):
    """This year against last (reporting.change)."""
    categories: list[Change | None]
    """In the order of `categories`."""
    spend: Change | None
    """The total spend's."""


class YearLensData(Shape):
    """GET /api/lens/year."""
    y: int
    cells: list[YearCell]
    detail: dict[str, Any] | None
    """the Cash Flow page's detail; this door sends null"""
    totals: YearTotals
    prev_totals: YearTotals
    dating: Literal["bill_month"]
    prev_through: str | None
    categories: list[tuple[str, float, float, str | None]]
    """[label, this year, last year, stored key]"""
    changes: YearChanges
    saved_ytd: float | None
    savings_goal: LensGoal | None
    buckets_annual: AnnualBuckets
    plan_rows: list[ServerPlanRow] | None
    """The year's money map rows over its elapsed budgeted months; null
    when none has elapsed (no map)."""
    plan_total: PlanTotal | None
    plan_caption: str | None = None
    """Which months the Yearly budget card covers ("Aug–Nov, the months
    with a budget"); null when there is no map."""
    plan_surplus: float | None
    excess_text: str | None = None
    """"An ordinary month leaves about $X" (the standing surplus per
    month), printed under the Yearly budget card; null with nothing
    budgeted, no surplus, or the year to date over budget."""
    first_year: int | None


# ---- budget snapshots and suggestions ---------------------------------------

class BudgetSnapshot(Shape):
    y: int
    m: int
    food_monthly: float
    other_monthly: float
    variable_budget: float
    source: Literal["close", "backfill"]
    captured_at: str
    bills_monthly: float = 0.0
    """absent from snapshots that predate bill freezing"""
    income_monthly: float = 0.0
    status_text: str | None = None
    """"so far" for the month still running (re-frozen nightly until it
    closes); null for a closed month"""


class BudgetSnapshots(Shape):
    months: list[BudgetSnapshot]
    missing: int
    budget_set: bool


class SnapshotsBackfilled(Shape):
    backfilled: int


class SuggestedBudgets(Shape):
    buckets: dict[str, float]
    """by carve-out name"""
    food_monthly: float = 0.0
    """absent when the ledger is too short to suggest one"""
    other_monthly: float = 0.0
    income_monthly: float = 0.0
    savings_monthly: int | None = None
    """What a planner seeded from history offers to save a month:
    `income_monthly` less the bills, the suggested food and everything-else
    budgets and the other goals' plans, in whole dollars and never below 0
    (budget.suggested_savings). Absent when history names no income."""
    unallocated_monthly: float = 0.0
    """history's Everything else beside the saved categories: other_monthly
    less each saved everything-else category's own suggestion; the hint
    the planner prints beside that row on a saved plan"""


class BucketCandidate(Shape):
    name: str
    category: str
    monthly: float


class BudgetSuggestions(Shape):
    """GET /api/budgets/suggest: trailing medians, current month left out."""
    months: list[str]
    suggestions: SuggestedBudgets
    samples: dict[str, list[tuple[str, float]]]
    history_days: int
    not_enough_history: bool
    history_scaled: bool
    min_rate_days: int
    bucket_candidates: list[BucketCandidate]
    avg_bills_monthly: float
    bills_count: int


# ---- activity, alerts, calendar ---------------------------------------------

class ActivityRow(Shape):
    tenant_id: str
    id: str
    at: str
    actor_user_id: str | None
    actor: str
    kind: str
    action: str
    target: str | None
    label: str
    summary: str
    detail: dict[str, Any] | None
    """what changed, in the kind's own words (before/after, counts)"""


class ActivityPage(Shape):
    """GET /api/activity, newest first; `next` is the cursor."""
    rows: list[ActivityRow]
    more: bool
    next: str | None
    actors: list[str]
    kinds: list[str]


class AlertKey(Shape):
    kind: str
    message: str


class AlertsDismissed(Shape):
    ok: Literal[True]
    dismissed: int


class AlertsRestored(Shape):
    ok: Literal[True]
    restored: int


class AlertRow(Shape):
    kind: str
    severity: str
    message: str
    first_seen: str
    last_seen: str
    active: int
    dismissed: int
    link: str | None = None
    kind_label: str | None = None
    """what the history calls the kind ("bill proposals", "charge")"""
    group: str | None = None
    """the history chip that lists it: connection, bills or cash"""
    text: str
    """the words the history prints: `message` in the current build's
    wording for its subject (an older build may have logged it another
    way). `message` stays the words as logged, the key dismiss and
    restore match on."""


class AlertHistory(Shape):
    """GET /api/alerts/history: one line per subject — the latest wording,
    first and last seen across every wording, live lines first."""
    alerts: list[AlertRow]


class CalendarEvent(Shape):
    label: str
    """The event's name with its note joined on ("… · note")."""
    amount: float
    kind: str
    """"bill", "card" (a card payment), "income", or "on_card": a bill a
    card pays, listed on its charge day and left out of the day's
    `came_in`, `went_out` and `total` (the card payment that pays it is
    the money leaving checking)"""
    name: str
    """The event's name alone: the payee, or the card payment."""
    note: str | None
    """What the figure is, printed apart from the name: "full balance" on
    a card payment (the walk pays a card in full on its due date); null
    otherwise."""


class CalendarDay(Shape):
    date: str
    total: float
    # what lands that day, in and out, each as a positive amount; `total`
    # is their difference
    came_in: float
    went_out: float
    events: list[CalendarEvent]


class CalendarData(Shape):
    """GET /api/calendar: the days ahead with what lands on each."""
    days: int
    start: str
    calendar: list[CalendarDay]
