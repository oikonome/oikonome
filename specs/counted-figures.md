# Counted figures (spec)

Every money figure a person reads is decided once, on the server, by one
named function, and every client — web, phone, the emails — shows that
value under the same words. This note lists the figures most at risk of
being decided twice or worked out by a client, and the rule each follows.
`specs/honest-figures.md` and `specs/w2-wages.md` are the earlier
examples of the same contract.

## The contract

- **One deciding function per figure.** A second function that needs the
  same number calls the first; it never re-derives it.
- **Clients display, they do not compute.** No sum, difference, percent,
  pace or state threshold over server figures in a client. A figure a page
  shows is a field in the payload. Sorting and picking which row to
  highlight from server rows is presentation and stays on the client.
- **No fallback chains between different definitions.** `a ?? b` is
  allowed only where `a` and `b` are the same figure from an older payload,
  and the older payload's value is then shown with the same label.
- **Same words for the same figure** on web, phone and email; different
  figures never share a label.
- **Older clients**: a new field is additive; an older app keeps reading
  the fields it read, which keep their old meaning unless named below.
  **Stale tab**: figures are refetched with the page; no client caches a
  derived figure. **Restore** carries the inputs (goals, answers,
  settings), never a figure.

## Budget and Today

- **The month's excess** is `budget.month_status` → `plan_surplus`:
  income − bills − planned spending − the monthly plan of EVERY savings
  goal. The planner on both clients shows it and lists each goal's plan
  above it, not only the goal it owns. The one client
  sum allowed is the planner's preview of values typed but not yet saved:
  `plan_surplus` adjusted by the difference between the typed values and
  the saved ones (`plan_variable`, the owned goal's plan, and the income
  while the income is typed), so with nothing typed it is exactly the
  server's figure. With no `plan_surplus` yet (no income saved, the first
  wizard pass) and an income box to type in, the preview is the typed
  income − `avg_bills` − the typed planned spending − every goal's plan.
- **The planner's bills** are `month_status.avg_bills`, the same figure as
  the header. Pending proposals are not counted until approved.
- **Paychecks still due** are `budget._month_income_parts` →
  `income_expected` everywhere: Budget's "still due", Today's "paychecks
  still due" and the email. A paycheck pending in primary checking — the
  account the cash figures start from — counts as landed when the bank
  reports an available balance there (it holds the pending credit); when
  it reports none, the cash figures start from the current balance,
  which does not, so that paycheck stays still due — at its pending
  amount, on every surface — until it posts. A paycheck pending in any
  other account counts as landed, as a posted deposit there does: it is
  in no balance the cash figures read either way. One judge
  (`budget._pending_landed`) decides for the payday re-date and the
  month's income alike. With an income series but no check landed
  and none due, Budget shows the budgeted income and Today's still due
  is the same function's $0.
- **Planned spending on Today and Budget**: the plan line's "to spend" is
  `plan_variable` (the budgets as set), so income → bills → to spend →
  saved → excess adds up on both pages. The variable-spending progress
  ("$A of $X") reads `variable_budget_paced`, and when that differs from
  `plan_variable` it says why beside it — "scaled to $X this month" when
  `variable_scale` is set, "paced from <date>" when the ledger began
  inside the month (`paced_from`) — never under the words "to spend".
  The page shows the auto-scale note when `variable_scale` is set, as
  Today does. The total bar's budget is the sum of its rows.
- **The plan line adds up as printed.** "$X in → $A bills → $B to spend
  → $C saved → $Y excess" prints whole dollars, and as printed X − A − B
  − C is Y. `budget.plan_line` decides them once, in `month_status` →
  `plan_line` = `{income, bills, to_spend, saved, excess}`: the excess is
  `plan_surplus` rounded once (halves to even, as `money()` rounds) and
  the terms are rounded by largest remainder to it with
  `reporting.shown_dollars`, so a term may be a dollar off its own
  rounding and the excess never is. `saved` is null when the line has no
  savings leg; the field is null when there is no excess. `/api/today`
  sends it; Today and Budget on web and phone, and the daily email's
  plain and HTML renders, print it and round no term of their own.
  **Older phone**: reads the unrounded fields and rounds each, as
  before. **Older server** (a current phone): no `plan_line`, so the
  phone prints the unrounded fields rounded one by one — the same figures
  under the same words. **Stale tab**: refetched with the page.
  **Restore**: carries the plan's inputs, never the line.
- **Cash notices** (`todayview`): the server sends `cash_notice` =
  `{state: critical|tight|null, amount, slow_to_per_day, until}`. The
  tight notice reads "left until your next paycheck … if you paid off
  all $Y on your cards and the bills due before then" ($Y the cash card's
  card debt, `cash_notice.card_debt`); the KPI tile reads
  "left at month end after cards & bills" and is the server field
  `after_cards_bills` (and `after_cards_bills_paychecks`). The thresholds
  live only in `todayview`.
- **Day meter** (`spent X of Y today`) is `todayview.simple_day_meter`,
  sent as `day_meter`. **Recent variable spending** is a server sum, the
  same one the email prints.
- **Plan rows** (Food, carve-outs, Everything else, Bills, Savings,
  Excess) are built once by `todayview._plan_rows` and sent; the web
  `PlanBars`, the phone money map and the email draw those rows.

## Cash Flow, lenses, Net Worth, Accounts

- **One window for every Cash Flow tab.** Overview, Spending and Income
  cover the same months. A month the bank feed is missing is marked
  `incomplete` (`incomplete_months`) and still counted in what came in
  and went out; nothing is silently dropped. **Saved and the savings
  rate** of a range (the Overview) are computed only over the months
  that have bank data, so spend with no feed beside it never reads as a
  loss; the payload says over how many (`saved_months_known` of
  `saved_months_total`), and both clients show "left over over N of M
  months" when N < M.
- **Spending → All starts at the first counted charge.** The
  one exception to the shared window: on "All", the Spending tab's
  heading began at the income anchor (the paychecks' first month) while
  its `/mo` divided from the first charge it counts, so the heading
  named months the average left out.
  - **Function**: `reporting._spending_span(conn, today, range_key)` is
    Cash Flow's window (`_cashflow_window`) with, on "All", its start
    moved to `MeasuredWindow.pace_start(start, SPEND)` — the month of the
    first row a spend window totals, the same rule `_pace_months` divides
    by. `spending_window` (`from`, `label`, `heading`, the total, `/mo`,
    `by_month`, `incomplete_months`) and `spending_window_ledger` (the
    drill's rows, `from`/`to` and chip label) both read it.
  - **Unchanged**: every other range, the Overview and Income tabs, and
    Net Worth keep their windows.
  - **A range longer than the spending says what `/mo`
    divides by.** A 3y or 5y window keeps its heading, total
    and `/mo`, but where `/mo` divides by fewer months than the heading
    names (`_pace_months` starts at the first counted charge),
    `reporting.per_month_basis(months, span)` words it:
    `per_month_basis` = "over the 20 months with spending", null when
    the divisor is the heading's own months (always on All, This month
    and a first-month ledger). The Income tab follows the same rule
    through the same function: its divisor starts at the bank feed's
    first row (`MeasuredWindow.pace_start(start, INCOME)`), so it reads
    "over the 18 months since the bank feed began" (its All can say it
    too, since Income keeps the shared window). The Overview prints no
    "/mo". Both clients print it after "/mo" as sent. **Older client**: today's line. **Older server**: no field,
    today's line. **Restore**: derived on read; nothing stored.
  - **Clients** print the server's `heading` (the phone's older-server
    path prints `label`, which moves with it); neither works a date out.
    **Older client**: prints what the server sends, so it shows the new
    span. **Older server**: the old heading. **Restore**: derived on the
    next read; nothing stored.
- **A month's income is complete or it is not — one rule, in every
  figure.** A year judged whole (known only when every one of its months
  is) beside month bars that count its measured months would read "no
  data" above eleven bars of it, and the all-time total would count as
  measured what the list calls unknown. One rule, by the month — an incomplete
  month is flagged "income incomplete", and left out of saved and the
  savings rate, in the month bars, the year list, every range's total,
  the Year lens and the yearly email alike. There is no whole-year rule.
  - **Function**: `MeasuredWindow.month_known` is the predicate, read
    through `reporting._income_known`; `reporting._incomplete_months`
    is its list form. The flow picture, the Spending and Income tabs,
    the saved-by-month series (`_cash_graph`), `compute_cashflow`'s year
    rows and month bars, the cumulative-savings line
    (`_cumulative_savings`), the lens month cells and the year strip all
    read it. The words are decided once too:
    `reporting.INCOME_INCOMPLETE` ("income incomplete") and
    `reporting.income_incomplete_note(known, total)` ("income incomplete
    — left over counts 8 of 12 months").
  - **The year list** (`savings_by_year`): Out is every month of the
    year, and In every deposit counted in it, as each range's total
    counts them. Saved and the rate are over the year's complete months
    only; a year with none keeps null In, Saved and rate. A year with an
    incomplete month carries the note in `savings_by_year_notes[year]`
    ("income incomplete — no month has bank data" when it has none).
  - **The month bars**: `months_by_year[year]` is every month of the
    year from the ledger's first through this one, in order, each
    `[label, saved]` with a null saved for an incomplete month;
    `monthly_by_year` keeps its meaning (the complete months only). The
    bars still add up to the year's Saved.
  - **The flags**: `incomplete_months` is the whole list ('YYYY-MM',
    ledger start through this month) and `incomplete_label` the words
    for one.
  - **The Year lens totals** (`lenses.cell_totals`, this year's and last
    year's): spend is every elapsed month; income, saved and the rate
    are the sum of the month cells whose income is complete, and
    `incomplete_text` carries the note when some month is not. Only a
    year with no complete month has null income, saved and rate, with
    `unknown_text` ("No month of 2024 so far has bank data — income and
    what was left over are unknown, not $0."; a year that is over says
    "No month of 2019 had bank data — …"). `saved_months_known` of
    `saved_months_total` count the cells.
  - **The yearly email**: the headline and the net tile sum the month
    cells that have a net; when some month has none the tile's sentence
    ends with the note, and only a year with no such month reads "income
    unknown"; the subject's net is followed by "(income incomplete)".
    The monthly email's year-to-date tile follows the same rule: the
    net of the months so far that have one, the note after its months
    line, and "—" only when no month has. The year strip (`detail.years`) gives each year the net of
    its complete months, `incomplete: true` when a month is left out
    (drawn grey, the legend naming it with the label), and no net for a
    year with none.
  - **Clients**: the web and the phone grey a Cash Flow year row that
    has a note and print the note under its Left over figure; they draw
    `months_by_year` as sent, an incomplete month as a grey slot named
    with `incomplete_label`; the Year lens on both prints
    `incomplete_text` under the totals, never a sentence of their own
    built from the counts. Neither decides which month is
    incomplete. **Older phone**: on Cash Flow reads `savings_by_year`
    and `monthly_by_year`, and on the Year lens the totals and the
    counts — a partly measured year now shows its figures with the "over
    8 of 12 months" line it already builds. **Older server**: no notes,
    no `months_by_year`, no `incomplete_text`; a partly measured year
    reads unknown, as before. **Stale tab**: refetched with the page.
    **Restore**: derived from the rows.
- **What was not spent is called "Left over", and money moved to
  investments is its own line.** The Overview named one figure three ways
  — the picture's band "Invested & saved", the sentence's "stayed", the
  chart's "Saved" — and the first of them said something the figure does
  not measure: it is income less spending, not what went into
  investments, which the same page reports as a different amount.
  The band, the sentence and the chart (with the
  year rows' charts and the chart's caption) say **Left over**, on web
  and phone. The reply keeps its field names (`saved`, the `saved` node):
  only the words printed move.
  The name holds wherever the figure is printed, not only above the
  chart: Cash Flow's year table is **Left over by year** with a **Left
  over** column; the Year view's totals say **left over** on web and
  phone; and the words the server writes about it say so too —
  `reporting.income_incomplete_note` ("income incomplete — left over
  counts 8 of 12 months", printed under a year row, under the Year
  view's totals and in the monthly and yearly emails, HTML and plain
  text alike, which read that one function), `lenses.year_unknown_text`
  ("income and what was left over are unknown, not $0.") and the short
  yearly message (`notify.render.short_yearly`: "Spent $30,000 on $36,000
  income — $6,000 left over (17%)."). Each string has ONE writer on the
  server; the clients print the notes as sent and hold only the two
  headings and the totals label. What keeps its name, because it is a
  different thing: the **savings rate** (the percentage — "Rate" in the
  year table, "savings rate" in the Year view), a savings **goal** and
  what it has saved, the plan line's "saved" (money the plan sets aside
  for goals), a savings **account**, and every field name. **Older
  phone**: prints its own "Savings by year" / "Saved" headings over the
  server's new notes. **Older server**: the old notes under the new
  headings. **Stale tab / restore**: words only; no stored value moves.
  - **Function**: `reporting.investment_funding_between(conn, start,
    end)` is the window's net money into investments — the Records'
    "Investment funding by year" figure (`_investment_funding`, the same
    rows: bank-side transfers to and from an investment provider, plus
    workplace-plan contributions) over the window's days.
    `_flow_breakdown` sends it as `invested` with `invested_text`, the
    line as printed: "moved to investments $2,400", "took out of
    investments $900" for a net withdrawal, null when it rounds to $0.
  - **Clients**: print `invested_text` as a line of its own under the
    sentence. **Older phone**: the old words, no line. **Older server**:
    no `invested_text` and no line. **Stale tab / restore**: derived with
    the flow on each read.
- **Left over** (the `saved` fields) is income − spending everywhere on
  Cash Flow (monthly bars, the year rows, the lenses). The month in progress is
  `cash_graph.current` (saved so far); `cash_graph.points` keeps its
  history-then-forecast meaning for older apps. The forecast is drawn as
  hollow bars beside it and is never inside a "saved" figure. The
  horizon in the caption is the server's (`forecast_days`).
- **Best and worst month are the window's.** Under "Saved, by month"
  a client that picked the best and worst month itself, out of every bar
  drawn, could name the month in progress (half over, a paycheck still
  due) the worst month of a window that ended before it, and on "This
  month" best and worst at once.
  - **Function**: `reporting._saved_extremes` (from `compute_cashflow`)
    sends `cash_graph.extremes`, one entry per timeframe: `best` and
    `worst` as `[YYYY-MM, saved]`, picked among `cash_graph.points`
    before `forecast_from` (months that are over and whose income is
    known) that fall inside that timeframe's Cash Flow window
    (`_cashflow_window`, the months the picture above it covers). Both
    are null when the window holds fewer than two such months — "This
    month", "1m", a ledger with no complete month — because one month is
    neither. The month in progress and the forecast are never candidates.
  - **Clients**: print the two entries for the chosen timeframe, the
    month named with its year and the amount signed the same way for
    both; with nulls, no best/worst words. Neither client ranks months.
    **Older phone**: ranks the bars it draws, as before. **Older
    server**: no `extremes`, and a current phone prints no best/worst.
    **Stale tab / restore**: derived with the report on each read.
- **The month bars end where the window ends.** "Left over, by month"
  follows the page's one timeframe, decided on the server, not cut by a
  client's own month table. A window of complete months ("3m" in
  November is August through October) is not followed by a half-finished
  November and a December forecast the picture above it does not cover.
  - **Function**: `reporting._drawn_windows` (from `compute_cashflow`,
    through `_graph_timeframes`) sends `cash_graph.windows`, one entry
    per timeframe: `first` and `last`, the month keys (inclusive) of the
    slots that timeframe draws, and `note`, the line under the chart. A
    timeframe of complete months runs from its Cash Flow window's first
    month (`_cashflow_window`) through its last complete month: no month
    in progress, no forecast. A timeframe that includes this month runs
    through the last forecast month: "This month", "All" (the whole
    graph, as the name says), and every key of a household with no
    complete month yet, whose windows are the month so far. `note` says
    "this month so far included" only for those.
  - **Clients**: draw every slot of the graph — a finished month of
    `points`, `current`, a forecast month of `points` — whose month is
    from `first` through `last`, the month in progress once, and print
    `note`. Neither client works out a window. **Older phone**: cuts and
    adds the tail itself, as before. **Older server**: no `windows`; a
    current phone cuts as that server's phones did (`cashflowCutoffMonth`,
    kept for this alone) and prints no note. **Stale tab / restore**:
    derived with the report on each read.
- **Cash Flow dates spending by the day it was paid.** The Month and Year
  lenses date a bill by the month it belongs to (`month_status`), and the
  Year lens's totals — and last year's comparison totals, read as of the
  same day a year back — are the sum of their twelve cells. Each page
  says which it uses.
- **"Budgeted" is not the plan's income.** The Month lens labels the
  income by its `income_source` (paychecks / scenario / typed), as Budget
  does.
- **Net worth**: the server sends the headline's month change, the
  property tile's change, the ring's net and the "Over time" gain. The
  Over time chart charts the same total as the headline.
- **A property added inside the window is not a gain.** A hand-entered
  property item enters the trend on the month it was recorded
  (honest-figures.md, "Property enters the trend on its recorded date"),
  so the total steps up there, and an Over-time window with its first
  point before that month and its last on or after it measured the step
  as money gained — a rise printed beside accounts that had fallen.
  Such a window's gain and percent are measured on
  the financial accounts alone, like for like; the line still steps up.
  - **Function**: `reporting._like_for_like`, applied to every window of
    `trend_windows` in `_networth_figures`, over the steps
    `reporting._property_steps` reads off the layer the line itself is
    drawn from. `_property_layers` returns the property layer and, beside
    it, its hand-entered part month by month: an item's value from its
    date on in a rebuilt month, and what the nightly recording held
    beyond the loans in a recorded one. A step is a month that part
    moves from the month before. So the step is where the value first
    appears in the line, not where "owned since" says: a home typed in
    this month with an "owned since" date years earlier steps in the
    month it was typed in, because the months recorded before it hold no
    property. One source, so the
    line and the window cannot disagree.
  - **Every typed-in move counts, not only an arrival.** A value taken
    out, or re-valued, is not money gained or lost either: any window
    with a step inside it (after its first point, on or before its
    last) measures `gain` on `trend` (financial accounts) between its
    two points, `pct` over a positive first point. With a linked loan
    the hand-entered part is what is left after the loan's walked
    balance, so only a move past 2% of it counts; without one, any move
    past a dollar.
  - **`note`** says what and when: "Home added 09/26 — the change is
    measured on the financial accounts" for one arrival (the first
    property there is, or a rise the size of one item; the item's own
    name when it is the only one or the one that matches, a mortgage
    typed in beside it not being the thing named), "property added
    07/26, 09/26 — …" for several arrivals or a name too long to print,
    and "property value changed 03/26, 09/26 — …" when any of the moves
    is not an arrival. `start`, `end` and `full` are the same, so the
    same points are drawn. A window with no step — including one whose
    first point already holds the value — keeps its gain on the total
    and a null `note`.
  - **Clients**: the web and the phone print `gain`, `pct` and, under
    them, `note` as sent. Neither works out a gain or decides when the
    note applies. **Older phone / stale tab**: prints the new `gain` and
    `pct` without the note — the right figure, unexplained. **Older
    server**: no `note`, and the gain over the total as before.
    **Restore**: derived on each read from the items' dates and the
    recorded months, which come back with the archive.
- **The headline's change this month follows the same rule.** A
  hand-entered value typed in, taken out or re-valued since last month's
  end is not money gained or lost this month either. Measured on the
  total, taking out a $100,000 house would read "−$100,000 this month ·
  property −$100,000" beside a property tile of $0 and an empty Property
  section — a loss of a thing the page no longer shows.
  - **Function**: `reporting._networth_figures`. When a step of
    `_property_steps` falls on the month in progress, that step alone — this
    month's move in the hand-entered part of the layer, which
    `_property_layers` returns beside the layer — comes off
    `month_change` and `property_month_change`. A linked loan's paydown
    the same month stays in both: with no loan, the tile's change is $0
    and prints nothing, and `_month_change_text` has no property part,
    so the parts still add up to the headline. A month with no such step
    is measured on the total as before. (The "This month" window of the
    Over time card, `_like_for_like`, measures such a window on the
    financial accounts alone, loan included; the two differ by the
    paydown only.)
  - **Clients**: print `month_change`, and the property tile's change
    when it is a dollar or more, as they already do. **Older phone / stale tab**: the
    same fields, so the same figures. **Older server**: the change on the
    total. **Restore**: derived on each read.
- **A gain with no percent says what it is measured from.** Over a
  window whose first point is at or below zero no percent is printed
  (there is no base to take it of), and the gain alone could exceed the
  whole net worth with nothing to say why: a line that starts at −$4,200
  and ends at $31,000 gained $35,200.
  - **Function**: `reporting._trend_window` adds `from_text` to each
    window of `trend_windows`: "from −$4,200 in 03/08", the first
    point's value and month, set exactly when that value is not above
    zero — the windows `pct` is null for — and null otherwise, where the
    percent already gives the base. `_like_for_like` restates it on the
    financial accounts when it moves the gain there, so the words and
    the gain are measured on one line.
  - **Clients**: print `from_text` after the gain's caption. **Older
    phone / stale tab**: the gain alone. **Older server**: no field.
    **Restore**: derived on each read.
- **The month's change says what moved.** A large "this month" figure
  with nothing beside it reads as money lost, whatever made it: prices,
  a sale, or a withdrawal on its way to a bank the app cannot see.
  Under the figure the server says the change by
  part, and names money that left an account and has not arrived.
  - **Function**: `reporting._month_change_text` (from
    `compute_networth`), over the same two points the headline's
    `month_change` is measured between: last month's end and now. Each
    asset class's part is its balance now less what the last nightly
    recording of last month held for it (`networth_snapshot.by_class`);
    the property layer's part is `property_month_change`. The parts are
    printed in whole dollars that add up to the headline as printed
    (`shown_dollars`), in a fixed order — crypto, retirement,
    investments, health savings, education savings, cash, cards, other,
    property — and only the parts that moved: "crypto −$600 · retirement
    −$300 · cash +$50". It is null when there is no month change, or when the
    classes recorded for last month do not add up to the point the
    change is measured from (a month-end that came from an imported
    balance report has no parts): a line that did not add up to the
    figure above it would be a second figure.
  - **Function**: `reporting._unarrived_withdrawals` decides the second
    line, `month_withdrawn_text`: "$250 withdrawn 03/02–03/04 has not
    shown up in a linked account". It reads this month's withdrawals to
    the bank from a crypto account (the rows `_bank_cash_flows` counts as
    cash out that are a withdrawal of dollars) and looks each one up
    among the deposits of every bank account the household has linked: a
    deposit of the same amount to the cent, dated from the day before
    the withdrawal to seven days after, each deposit answering for one
    withdrawal. What is left unanswered is summed and dated by its first
    and last day. Null when everything arrived or nothing was withdrawn.
    Only crypto accounts: theirs are the only rows that say "this left
    for the bank" in the feed's own words; a brokerage's transfer out is
    not told apart from a transfer between its own accounts.
  - **Clients**: the web and the phone print `month_change_text` under
    the headline's change and `month_withdrawn_text` under that, as
    sent. **Older phone / stale tab**: the figure alone, as before.
    **Older server**: neither field, and neither line. **Restore**: the
    nightly recordings and the rows come back with the archive; both
    lines are derived on each read.
- **A typed-in loan balance says how old it is.** A mortgage entered by
  hand is subtracted at the figure typed, on a page that says its figures
  are as of now; months of payments later nothing said the balance was
  old or asked for a new one. The balance carries
  its age, and asks for an update once a quarter.
  - **Function**: `reporting._property_items` (the one builder of the
    property rows, for the page and the nightly recording). A hand-entered
    row of kind `mortgage` adds `entered_text` and `update_due`, read
    from the row's own `as_of` — the day its value was last stated, which
    every value edit restamps and nothing else moves (it is already
    stored; no new key). `entered_text` is "entered 06/26"; once three
    calendar months have begun since that month `update_due` is true and
    the text reads "entered 06/26 — update". Saving a new balance
    restamps `as_of`, so the prompt goes for another quarter. A row with
    no readable date, a linked loan (its balance is the lender's) and
    every other kind carry neither. `compute_networth` also sends
    `mortgage_entered_text`: the oldest such row's text, for beside the
    "mortgage payoff" figure the rows add up to; null when no typed-in
    mortgage counts.
  - **Clients**: the web and the phone print `entered_text` beside the
    row's value and `mortgage_entered_text` beside the payoff, in the
    warning tone when `update_due`. Neither reads `as_of` or counts
    months. **Older phone / stale tab**: the balance alone, as before.
    **Older server**: neither field. **Restore**: `as_of` comes back
    with the settings.
- **The Accounts page's total is the Net Worth "Financial accounts"
  figure** (`reporting` net-worth builder), under the same label; loans
  are listed under it as owed, not subtracted from it; business accounts
  count exactly when the household combines them. Group and institution
  subtotals are server-sent.
- **The account editor's switches are offered by the rules the money
  math reads.** "Exclude from budget" takes an account's rows out of the
  verdict, so it is offered exactly where a row can be spending:
  `budget.account_counts_as_spending(type)`, which reads
  `budget.NON_SPEND_ACCOUNT_TYPES` — the one list `budget.SPEND_ONLY_SQL`
  and `reporting.SPEND_WHERE` are built from (today: every type but a
  loan, so an investment or "other" account is excludable). "Primary
  checking" is offered on the type the forecast picks among,
  `forecast.pin_offered(type)` (`PIN_OFFERED_TYPE`, which
  `eligible_checking_ids` reads). The accounts reply sends both per
  account as `can_exclude` and `can_pin`. **Clients**: draw a switch when
  its flag is true or the switch is already on, and decide nothing from
  the type. **Older server** (no flags): both switches are offered, never
  hidden. **Older phone**: offers both on every account, as before.
- **Lists add up in the dollars they show.** The Accounts sections, their
  institution subtotals and account rows, and Net Worth's by-asset-class
  and by-institution rows are shown in whole dollars, and as shown they
  add up to the total shown above them. One function decides the shown
  dollars, `reporting.shown_dollars` (largest remainder to the total in
  whole dollars, halves to even as the clients' `money()` rounds), and one
  the shares, `reporting.shown_shares` (whole percents of what the ring
  draws, largest remainder to 100). So a row may be a dollar off its own
  rounding: $1,234.40 owed can show $1,235, and never more than that.
  The new fields sit beside the old ones, which keep their meaning:
  - Net Worth: `rings.<list>.shown` = `{rows, owned, owed, net}`, each
    row `{label, dollars, share, owed}` in the order of `by_<list>`;
    `share` is null for a row the ring does not draw. `net` is the
    Financial accounts figure in whole dollars; `owned` and `owed` are
    the sums of the drawn and owed rows.
  - Accounts: each section's `shown` = `{total, counted_total,
    counted_text, by_item, by_account}` — the section total in whole
    dollars, its counted part (the sections' parts add up to the
    headline), `counted_text` saying that part where it is not the total
    ("−$250 in the total" for cards, "not in the total" for loans and an
    uncombined business section; null otherwise, and both clients print
    it beside the section's name), each institution's
    subtotal (adding up to the section) and each account's balance
    (adding up to its institution). An account a section does not count
    (a linked backup, no balance) is not in `by_account` and shows its
    own balance.
  - Accounts, a linked backup: `backup_shown` = `{account id: dollars}`
    beside `sections`. A backup is the serving account seen through a
    second feed, so when the two balances agree to the cent it prints the
    serving account's shown dollars — the largest remainder may have moved
    that row a dollar, and one balance must not read two ways on one page.
    One function decides it, `reporting._backup_dollars`, called by
    `account_totals` after the sections' `by_account`. A backup whose
    balance differs from its serving account's by a cent or more (a feed
    that is behind), one with no balance, or one whose serving account no
    section counts is not in the map and shows its own balance, as
    before. The map is in no sum: nothing adds up through a backup.
  The web donut legend and both clients' lists print these fields and
  compute no share or rounding of their own. **Older phone**: reads
  `by_<list>` and the section `total`, rounds them itself and works out
  its own shares, as before. **Older server** (a current phone): with no
  `shown` the phone prints the rows rounded one by one and no shares; it
  never computes them. A phone or tab from before `backup_shown` does not
  read it and prints a backup's own balance rounded, as before; a current
  client on an older server gets no map and does the same. **Stale tab**:
  refetched with the page. **Restore**: carries balances, never a shown
  figure.
- **A connection with no institution name is "Other", in a group of
  its own.** Net Worth's by-institution list (and its ring, and the
  integrations and webhook copies of it) groups accounts by institution
  in ONE place, `reporting.compute_networth` over `_live_accounts`: the
  group key is the name together with whether the connection gave one
  (`inst_unnamed`), and the label of the unnamed group is "Other". So
  the accounts of connections that never said their name form one
  "Other" row, and an institution that is literally named "Other" stays
  a row of its own beside it, two rows with one label; the Accounts
  page's institution count counts them as two. No balance or total
  moves. Clients print the server's rows in order and pair the ring's
  `shown` rows with them by position, never by label (the web donut, the
  phone's breakdown), so two rows with one label each keep their own
  figure. **Older phone**: keys its list rows by label, so two "Other"
  rows still print, each with its own figure. **Older server**: one
  merged "Other" row, as before. **Restore**: carries the connection's
  name as it was; an empty one reads as unnamed again.
- **Income per month and the paycheck share**, and **interest vs interest
  earned**, are server fields.

## A withdrawn deposit repays nothing

A charge a deposit paid back leaves spend: a full pair stamps it a
transfer, a partial link nets the deposit's share off it. When the bank
feed withdraws the deposit, that money never came back, so the charge is
spend again until something else repays it — closed months included.

- **Whether a link counts** is one rule, read the same way by every
  figure: a link repays only while both its rows are on the ledger
  (`budget.REIMB_RECEIVED` for what came back against a charge, the
  joins beside `reporting.NET_AMOUNT` and `INCOME_NET` for the nets).
  What keeps the stored links true to that rule is one writer,
  `web.data.settle_pairs`; every path that takes a row off the ledger or
  brings one back calls it.
- **On withdraw** (`sync.base.mark_removed`): after the re-anchor has
  looked for a re-numbered twin, a full pair still on the deposit becomes
  the partial link recording what it repaid, and the charge lets go of
  the transfer stamp the pair wrote. A partial link on a withdrawn row
  claims nothing, so the charge is spend again at once; the link itself
  stays, for a twin or the same id to pick up, until the nightly sweep
  releases it after the reconcile window. A partial link repays under
  the charge's own category, so nothing has to be stamped back later,
  and a category a person chose is never overwritten by a link that
  returns.
- **On re-report** (the ingest upsert sets `removed` back to 0, or a
  twin takes the links over in `sync.reanchor`): the returning row's own
  links take only the room the other side has left. What repaid the
  charge, or drew on the deposit, while it was gone keeps its claim; the
  returning link is cut, or dropped when nothing is left. A charge is
  never repaid past its cost, and the money that came in is counted once.
  The upsert brings the row back and makes that cut in one commit under
  the pairing lock, so no repair can run between them, and a cut that
  fails fails the page (fetched again, the cursor unmoved) rather than
  leave it to the repair below, which cannot tell which link came back.
- **Past months: an idempotent repair, not a migration.**
  `web.data.repair_pairs` finds the full pairs still on deposits the feed
  withdrew and the live rows repaid or claimed past their amount, and
  runs `settle_pairs` over them (where nothing says which link came back
  last, the newest gives way, as everywhere links overrun a side). It
  runs in the nightly sweep for every household, from
  `oikonome reanchor-stranded`, and after a restore commits. A migration is the
  wrong tool: the repair needs the link writer's own steps (what a pin
  replaced goes back, a deposit no longer used up is income again), which
  SQL would have to copy and then drift from; and the same state can
  re-enter from an archive or a feed path of an older version, which
  a one-time migration would never see again. Figures are computed from
  the rows, so a repaired closed month reads the corrected figure — an
  exception to "corrections apply from now on" for this case.
- **Dry run**: `oikonome repair-pairs --dry-run` runs the repair for
  every household (or `--tenant` one) inside a transaction that is always
  rolled back (`web.data.repair_pairs_preview`, which refuses to start
  inside a caller's transaction) and prints, per household numbered in
  order, the links settled, the pins restored and each affected month's
  In and Out change in dollars, then a total. It names no household, row,
  payee or description. Without `--dry-run` it applies the repair.
- **Restore of an older archive** lands its links, pins and withdrawal
  stamps exactly as written (the restore's own transaction stays a
  faithful copy, which the round-trip tests hold); once it has committed
  the restore job runs `repair_pairs`, so the household ends in the state
  a live ledger reaches. Should that step fail, the nightly sweep runs it.
- **Older clients** see no new field: the same figures, corrected. A
  pair that was full and now waits as partial is listed as a partial
  link of the charge's amount by both the web and the phone, which
  already show partial links.
- **Not changed**: a charge the feed withdraws stops claiming its
  deposit as before (the readers' rule); a stamp a full pair left on a
  deposit whose charge was withdrawn is released by the sweep after the
  reconcile window, as before.

## Retirement and Debt

- **Inputs**: the projection's spend and saving are the household's saved
  retirement answers. With no saved spend, the trailing year from the
  ledger; with too little history for that (under 90 days, or a year
  under the floor) the page asks for a spend and projects nothing — there
  is no assumed spend. With no saved saving answer the projection counts $0 of
  new saving (`saving_source` "none") and both clients say "saving not
  set — add it" with a box that saves the answer, as for a needed spend;
  the payload names the source of each (`spend_source`, `saving_source`).
- **An older phone and the spend-less reply**: only a client that sends
  `ask_spend=1` (both current clients, on every call) gets the reply that
  projects nothing. A phone built before it reads spend, rows and stress
  unguarded — an empty stress list read as "+$∞ vs target — survives the
  worst stress scenario" — so any other caller still gets the projection
  the older server sent, at its assumed $250,000 (`OLDER_CLIENT_SPEND`),
  with `spend_source` "needed"; every old field keeps its old meaning.
- **Social Security** at a claim age with no saved estimate is the
  straight line between the nearest saved ages; an age outside them is
  the nearest saved figure moved by SSA's adjustment (5/9 % a month for
  the first 36 months early, 5/12 % beyond, 2/3 % a month of delayed
  credit to 70, from the full retirement age the birth year sets —
  `retirement.ss_at_age`). The chosen claim age is saved from the walkthrough (a whole year,
  62–70); the adjust panel is a what-if and saves nothing.
- **Card debt in the projection** comes from the same rows as the assets
  — the Net Worth scope, so an account excluded on the Accounts page still
  counts, as it does on Net Worth — at the rates the Debt page resolves
  (`debt.card_aprs`); a card's credit balance nets.
- **Only answers a current client saved are inputs — or shown.** A save
  from a current client carries `ret_answers_v: 2` (and the claim age as
  `ret_wizard_ssage`) beside `ret_wizard_answers`, top-level fields an
  older server ignores; the server marks those keys (`answers_v2`). A
  walkthrough step left with Continue confirms every box on it. Answers an
  older client saved (every prefilled box) are neither inputs nor
  pre-filled: the wizard opens on marked answers, else the projection's
  figures, else blank. A restore keeps the marks.
- **Loans stay out of the projection's debts**: their payments from
  checking are already inside the ledger spend, so counting the balance
  too would count them twice.
- **Two worst-case spends, both server-decided**: `stress_floor_spend`
  ("the most you could spend and survive the worst stress scenario") and
  `hist.spend100` ("…survived every year since 1928"). The client never
  takes a minimum over the scenarios. Nor does it compare a figure with
  the target: each stress scenario's `under_target` (its spend below the
  target: red) and each age row's `meets_target` (its most a year covers
  the target: green) are sent; an older server sends neither and the
  figures print uncoloured.
- **Debt page**: "owed" is `plan.starting`; savings vs minimums and the
  other method's difference are server fields; "estimated" is the
  server's `estimated` per debt and per plan.
- **Money typed on the phone** that does not parse is refused, as on the
  web; it is never sent as 0.

## Not changed

- Cash Flow by payment date vs lenses by bill month is a deliberate
  difference (above), labelled on each page.
- The Year lens applies each closed month's frozen account exclusions;
  Cash Flow applies today's. Each says so under its total.
