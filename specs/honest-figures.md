# Figures that say what they are

A number must not claim more than the data behind it, and two numbers
for one thing must agree. These are the rules that keep them so. Each
rule names the figure, the misreading it prevents, and what it says.
Web, phone and email follow the same rule in every case.

## No paycheck scheduled: ask, do not forecast

A cash forecast that counts no income always runs short. "Short $X
today — slow spending to $0/day" on a household that has not set its
paycheck up is a verdict on the setup, and it sat beside "Under budget".

- While no paycheck is scheduled (`income_due_note` is `none`,
  `budgeted` or `off`), the cash notice — critical or tight — is replaced
  by the missing step: "Add your paycheck under Bills & Income to see
  what's safe to spend", or that income is switched off there.
- The notice links to Bills & Income on the page and the phone; mail
  carries the sentence.
- Once a paycheck is scheduled the shortfall and tight lines return
  unchanged.

Code: `todayview.PAYCHECK_ASK` / `paycheck_ask`, mirrored in
`Today.tsx` and the phone's Today tab.

## A debt with no rate is walked at a typical one

A debt nobody had given an APR for was walked at 0%: the earliest
possible debt-free date and no interest, which reads as good news.

- A debt with no rate from the bank or the household takes the typical
  rate for its kind (`debt.TYPICAL_APR`: card, car loan, student loan,
  mortgage, other loan). Its `apr_source` is `typical`.
- A typical rate is a guess and a known payment is a fact. Where a
  reported or typed payment would not retire the debt at the typical
  rate within thirty years, the rate is lowered to the highest one at
  which it does
  (`_rate_within_payment`), so a loan that is being paid down on schedule
  is never called one that never ends. One rule for every kind,
  cards included, so the rate never jumps at some payment size. A
  payment that would not clear the debt in thirty years even at 0% says
  nothing about the rate: the typical one stands and the walk reports a
  debt those payments never clear.
- The row shows the rate as the input's placeholder, marked "est. — add
  yours". The hero says how many debts are estimated and the interest
  tile is labelled estimated, until every debt has a rate.
- Typing the APR on the row and saving is the whole ask; there is no
  switch for estimates.

## Pace is said in plain words

"$X ahead of pace" under "Over budget" read as praise. The direction is
now spelled out: "$X more than planned by today" and "$X less than
planned by today", on the verdict line, the per-bucket sentences, the
money-map rows ("$X more than planned") and the summary line of the
mail.

## One spelling for a category

A bank key (`FOOD_AND_DRINK`, or the spaced `FOOD AND DRINK` reports
carry) reads "Food and Drink" everywhere; a name somebody typed is left
exactly as typed; "no category" reads "Uncategorized". The rule lives in
three places that must agree: `categories.display_label` (mail), and
`catLabel` in the web and phone clients. Stored values, rules and API
payloads are unchanged.

## Net worth adds up on the page

- The headline's "this month" is the change in the total. The Financial
  accounts tile carries the financial part and the Property tile the
  rest, so the two tiles' changes sum to the headline's.
- A ring draws only what is owned. What is owed (card debt, an
  institution netting below zero) is listed under it as "not drawn",
  followed by the net, which is the Financial accounts tile's figure.
  The ring's centre reads "assets" when anything is left out.

## The plan's income is one figure

The Budget page's header counted this month's paychecks while the
planner counted the figure typed in its box, so the two showed different
incomes and different excess figures.

- `budget.month_status` decides the month's income once and names its
  source in `income_source`: `paychecks` (landed plus still due),
  `scenario` (the active income scenario's take-home), `typed`
  (`budgeted_income_monthly`) or null. `/api/today` sends it.
- The planner on both clients shows the server's `income` and counts it
  in its excess. Only when the source is `typed` (or null) is the box
  typeable; a tracked income is changed where it comes from.
- A planner save sends `budgeted_income_monthly` only while the box is
  typeable, so a tab opened before a paycheck was tracked cannot change
  what the plan counts.
- There is no household switch to plan on a typed figure over tracked
  paychecks: a setting that changes which number every reader counts
  is a new decision point, and two of them is how the figures drifted.

### A plan with no income still prints its line

`plan_line` is null for a plan with no income (it has no excess to add up
to), so the Budget page's summary line fell back on both clients to the raw
figures (`avg_bills`, `plan_variable ?? variable_budget`, `savings_plan`)
and rounded them itself.

- **The one function**: `budget.plan_legs(income, bills, to_spend, saved,
  excess)`, beside `plan_line` in `month_status`, sent by `/api/today` as
  `plan_legs` for every plan. With an income and an excess they are
  `plan_line`'s terms, which add up to the printed excess. Without an
  income `income` and `excess` are null and each other leg is its own
  figure in whole dollars; `saved` is null with no monthly savings leg.
- **Clients read**: the Budget page's line on both clients prints
  `plan_legs` alone: "in" only when `income` is sent, "excess" only when
  `excess` is, its colour from the sign of the printed excess. Today and
  the email keep printing `plan_line`, which they show only with an income.
- **Older server** (the phone only): no `plan_legs`, and the line is built
  from `plan_line` or the raw figures as before (`olderPlanLegs`, one
  marked stand-in).
- **Stale tab / restore**: worked out on each read from the month's plan;
  nothing stored.

### The planner's suggested savings is an ordinary month's

The planner filled "Save / month" with what was left of the plan's income
after bills and budgets, worked out in the client from `plan_surplus` —
and the plan's income, for a household with tracked paychecks, is THIS
month's: what landed plus what is still due, a refund or any other
one-off deposit included. Left untouched and saved, that became the
household's standing monthly savings plan. With an invented household
paid $4,000 a month, $1,200 of bills and $1,400 of budgets: in a month a
$1,700 refund landed the box read $3,100, and saving the plan committed
$3,100 a month from then on, where an ordinary month leaves $1,400.

The suggestion is the server's, measured on the
income an ordinary month brings, and a typed figure always wins.

- **Function**: `budget.suggested_savings(left)` decides the figure —
  whole dollars, never below $0, none when there is no income to measure.
  It has two callers, one per state of the plan, each handing it what an
  ordinary month leaves before the planner's own "Savings" goal:
  - a **saved plan**: `forecast.plan_savings_suggestion(conn, st, today)`
    is `forecast.standing_surplus` (the month's income swapped for
    `budget.income_monthly_figure`: the income series' own deposits over
    their last twelve complete months, the figure the Bills headline and
    the planner's suggested income quote; a typed or scenario income is
    standing as it is) with the plan's own goal put back.
    `report.gather` writes it once as `savings_suggested`, and
    `GET /api/today/full` sends it; null on a past day and with no income.
  - a **first run** (nothing saved; the planner is seeded from history):
    `budget.suggest_budgets` sends `suggestions.savings_monthly` — the
    same income figure (its `income_monthly`) less its `avg_bills_monthly`,
    its suggested food and everything-else budgets and the other goals'
    monthly plans. Absent when history names no income.
  - a **first run with no income in history** (the reply above sent no
    `income_monthly`): the same door, `GET /api/budgets/suggest?income=`,
    takes the take-home the planner's box holds, as typed, and
    `budget.suggest_budgets(..., typed_income=)` measures
    `savings_monthly` on it by the same subtraction. The typed figure is
    never sent back as `income_monthly`, and with income in history it
    is not read. Blank, unreadable, non-finite or not above $0 is no
    suggestion, never an error. A ledger with no finished day still gets
    no suggestion at all.
- **Clients** (web `BudgetPlanner`, the phone's planner): the box shows the
  server's figure for the state the planner opened in — `savings_monthly`
  when it was seeded from history, `savings_suggested` when it opened on a
  saved plan — until the person types. Neither client works the figure
  out, and typing in another box no longer moves it; the excess line
  (the planner's one allowed preview) shows what the typed plan leaves.
  A typed figure, or a stored "Savings" goal (a stored $0 included), is
  never replaced: the suggestion is then offered beside the box with
  "use". With no suggestion the box is empty and takes what is typed.
  On a first run with no income in history each client asks again once
  typing in the income box pauses, prints only the reply to what the box
  holds now, and holds Save while an untouched savings box waits for it.
- **Older client**: works its own figure out from `plan_surplus`, as
  before; an older client never sends the typed income, so a household
  with no income in history gets no suggestion there. **Older server** (a
  current phone): no field, so no suggestion; the box takes what is typed.
  It ignores `?income=` and sends no `savings_monthly`, so the phone shows
  nothing in its place (no stand-in is printed). **Stale tab**: the suggestion arrives with
  the Today read the planner already makes. **Restore**: derived from the
  restored series and plan on the next read; nothing stored.

### Bills & Income prints the same income

Bills & Income printed an income of its own — a nine-month average of
what landed, `recurring_load`'s `income_monthly` — and "Left for spending
& savings" as that average minus the bills, worked out on the page. With
an invented household: Budget and Today said "$6,000 in → $2,500 bills →
$2,000 to spend → $1,500 excess" while Bills said "Income $7,400" and
"Left $4,900".

The page shows the plan's income, and "left" is the
plan's to-spend plus excess, sent by the server.

- **Function**: `api_bills.bills_hero(month_status, bills_monthly=…)`
  writes `totals.hero` on `GET /api/bills`: rows `bills` (the plan line's
  bills, "a month, evened out"), `income` (the plan line's income, named
  by `income_source`: "this month's money in", "your income scenario",
  "the monthly income set on Budget") and `left` ("Left
  after bills", or "… and savings" when the plan saves; the amount is
  `to_spend + excess` and the note says "to spend $2,000 + excess
  $1,500"). The live month's `month_status` is the one Today reads.
- **Clients**: the web tiles and the phone's rows print `hero` in its
  order and add nothing up; the page-computed "Left for spending &
  savings" is gone from both.
- **The income caption is "money in"**: a month counted
  from its deposits holds every deposit that landed, not paychecks alone,
  so the caption never calls the figure "paychecks". It names paychecks
  only beside the portion they are — `api_bills._income_note` adds "incl.
  $X of paychecks still due" from `month_status.income_expected` on a live
  month. Budget's planner says the same in its own words: "What landed this
  month, plus $X of paychecks still due" from the budget reply's
  `income_expected`. An older phone prints the server's note as sent.
- `income_monthly` / `income_basis` stay in the reply with their meaning
  (the nine-month average) for an older phone; no current surface prints
  them.
- With no income in the plan, the hero is the Bills row alone.
- **Older phone**: prints its old average and difference. **Older
  server**: no `hero`, so the clients print no headline. **Stale tab**:
  the hero arrives in the one Bills reply. **Restore**: derived on the
  next read.

## Bills says what is paid and what is still due

- A bill paid this month beside its next due date read "paid ✓" next to
  a date in the next month; a bill due twice in the month with one
  payment in said nothing of it. `api_bills.paid_words` writes each
  row's `paid_text` from the month's occurrence stats and the latest
  payment's day (`paid_on`): "paid 05/03", "paid 05/03 · next 06/03"
  when the date beside it is already next month's, "1 of 2 paid",
  "2 of 2 paid · last 05/15", "received 05/15" for income; none while
  nothing is paid. The web's status cell and the phone's row print it
  in place of "paid ✓" / "received ✓", and a half-paid bill prints it
  before its countdown.
- Today's one still-due figure (the running month's breakdown's
  `still_due`, `bills_breakdown.running`, read with the live month's
  `month_status` as `report.gather` reads it) is the hero's last row,
  "Still due — bills not yet paid, the figure Today counts", under the
  label the Bills row gives the figure; with a bill late from an earlier
  month its note gives the parts (specs/today-figures.md, "The Bills row
  is one breakdown"). The hero's order is the plan line's: Income,
  Bills, Left after bills, Still due. The web's "this month so
  far" label over the monthly figures is gone.
- **Older server**: no `paid_text`, so a paid row prints no paid words;
  no `due` row.

## A bill row says when in the server's words

Both clients counted the days to a bill's due date on the device and
worded the countdown themselves ("due today", "in 3 days", "2 days late"),
coloured it from the same count, filled the cycle bar from it and built
the attention queue's sentences from the row's raw evidence. Two copies of
one rule, and a device clock that is not the household's day.

- **The one function**: `api_bills.due_words(row, today, live=…)`, counted
  from the household's day (`localtime.now_local`), sends on each row of
  `GET /api/bills`: `due_label` (the Due column: "today", "tomorrow", else
  MM/DD/YY), `due_text` (the countdown: "due today", "due tomorrow",
  "in 12 days", "1 day late", "3 days late"; income says "today" /
  "tomorrow"), `due_tone` (`late`, `withdrawn` — late with the charge taken
  back by the bank feed — `soon` within three days, else `ok`) and
  `cycle_frac` (the bar's fill, 0..1, from the cadence value). All four are
  null on a closed month, an envelope and a row with no due date;
  `due_text` and `due_tone` are null once the month's occurrences are paid.
- **The queue's sentence**: `api_bills.attention_words(row)` sends
  `attention_text` for every row whose health wants a decision (stale,
  mismatch, misdated, drifting) — "missed its ~03/14/26 cycle — nothing
  since 02/14/26", "charges now run $64.00, bill says $58.00" (cents,
  as every recurring row prints). Which rows the
  queue lists stays `api_bills.queued` (the headline's `attention` count),
  so a dismissed or disabled row keeps its sentence and a flag restored on
  the page reads right before the list is read again.
- **Clients read**: the web's Due column, cycle words, their colour, the
  bar and each queue line print these fields and nothing else; the phone's
  row, its "· due …" line and its queue lines print the same.
- **Older server** (the phone only): a reply without `due_text` is
  counted on the device as before (`olderDueWords`), and one without
  `attention_text` builds the sentence as before (`olderAttentionWords`);
  each is one marked stand-in.
- **Stale tab**: the words are the reply's day. A page left open past
  midnight keeps yesterday's countdown until it reads the list again (on
  focus or the next refetch) — the same day the list's paid marks and the
  headline are judged on, rather than moving ahead of them as words
  counted on the device would.
- **Restore**: nothing is stored; the words are worked out on each read.
- Visible differences: none on the web; the phone's countdown colour
  follows the server's tone.

## A closed month shows its own figures

Stepping back to a past month on Bills & Income kept today's headline
(today's bill load and income), "next 7 days", today's offers and the
next five weeks' calendar above that month's paid list; Budget history,
the past-month page and the monthly report printed three different
"bills" figures for one month with nothing saying which was which.

A closed month shows its frozen figures everywhere,
and each bills total says its basis where two could be confused.

- **Function**: `GET /api/bills?month=YYYY-MM` for a closed month builds
  `totals.hero` with `api_bills.bills_hero(st, closed=True)`, where `st`
  is `lenses.month_state` for that month (its snapshot budget and frozen
  schedule — what the Month lens and the monthly report judge) and Bills
  is the load frozen with the month (`budget.snapshot_bills_monthly`, the
  figure Budget history prints), noted "a month, evened out, as frozen
  when the month closed". Income is what landed, Left is that month's to
  spend + excess. A month with no snapshot has no Bills row.
- The same reply carries no offers and no week ahead (`week_total` 0) —
  those are today's — and `month_calendar`: that month's dated money from
  the occurrences the paid list counts (a bill on the day it was paid,
  with its fees; a paycheck on the day it landed — a paycheck is the
  month's it pays, so one due on the 1st that landed on the 30th before
  sits on the 1st, "<payee> — landed 04/30", and one that landed after
  the month on its last day, and the calendar's money in is the
  paychecks Income counts; a bill the month left
  unpaid on its due day, "<bill> — not paid"), in `/api/calendar`'s day
  shape.
- **Clients**: on a past month the web and the phone print the hero as
  sent and draw the month's own calendar from `month_calendar` (titled
  with the month; no day dimmed), not the next five weeks.
- **Basis words**: Budget history's column reads "Bills, evened out"
  (web) / "Bills $X evened out" (phone); the monthly report's Bills bar
  adds "planned from the month's due dates" — the month's own schedule,
  a different figure from the evened-out load.
- **Older phone**: a closed month shows no offers and a $0 week; it keeps
  its own headline and the five-week calendar. **Older server**: no
  `hero` and no `month_calendar`, so a past month shows neither.
  **Restore**: the snapshot comes back with the month and is read as is.

## Bill offers a person can act on

The finder's offers on Bills & Income piled up: offers dated months ago,
one merchant offered as several weekly bills at different amounts, a
card payment offered as a bill (approving it counts the card's spending
twice), and offers to remove or join bills that no longer exist.

- **Function**: `bills.pending_proposals(conn, today)` is the one list
  the Bills page, its history page, approve-all, the email's "needs you"
  section and the Today count read. It leaves an offer out — never
  deletes it, never answers it — when:
  - it would add a credit-card payment as a bill (`card_payment_offer`:
    the bill rule's card-payment reading, or the charges' card-payment
    category). Such an offer is not filed at all any more
    (`_insert_proposal`).
  - it removes or changes a bill that is not active, or joins a fee or a
    merchant to a bill that is not active;
  - an active bill already covers it (approving it would be withdrawn as
    already tracked);
  - it is an unanswered offer to add a bill whose
    expected date is a full cycle past (`offer_lapsed`). Detection gives
    a standing offer the series' new date each night it still charges,
    so only a series that stopped (or moved to another price, which has
    its own offer) lapses. An offer the person answered is never
    rewritten.
- Several offers to add one merchant (same direction, same merchant key)
  are shown as the one with the latest expected date. Turning it down
  turns down its twins; approving it makes a bill that covers them.
- Approve-all approves exactly what the list shows. An offer the list
  hides (covered by an active bill, a twin of the one shown) is not
  tried, so it is never reported as one that "stayed pending"; the
  nightly retires a covered offer.
- **Words**: `bills.offer_words` writes each offer's `change_text` (what
  approving does: "new bill", "new income", "new envelope", "stop
  tracking", "fee", "add merchant", "amount change", …), `tag_text` (the
  advisory tag as a sentence: "looks like everyday spending, not a bill")
  and `cadence_text` ("every 2 weeks"; "varies month to month" for an
  envelope; none for an offer to stop tracking, a fee or a merchant). The
  web and the phone print those three and the evidence `summary`, which
  counts charges without a "?" when no cycle was measured. The web no
  longer composes "weekly ×2". `bills.offer_upcoming_due` sends
  `upcoming_due`: the finder's next date rolled forward by the cadence to
  today or later, none for a fee, a merchant, a removal, an envelope or a
  one-time offer gone by — a row never says "next" of a day already past.
  The setup wizard's bills pane draws the same row as the Bills page, on
  both clients: the `change_text` pill, "a fee of …" / "add to bill “…”"
  from `bill_payee`, then cadence, amount, next date, tag and evidence. It
  composes no cadence of its own and adds no day of the month to a weekly
  offer.
- **Clients** print the list they are sent and compute no words.
  **Older phone**: the same list, fewer rows, its own pills.
  **Older server**: no words, so the row shows no pill and no cadence. **Restore**: offers come
  back as stored and are filtered on the next read.

## The Bills calendar says what moves on a day

One net figure for a day with a paycheck and a bill ("$2,385" for a
$2,400 paycheck and a $15 bill) would hide the bill behind the
paycheck, and a card's payment labelled by the card's nickname and
"payment" could read as money in.

- **Function**: `/api/calendar` (the cash walk's own events) sends each
  day's `came_in` and `went_out` beside `total`. The web cell and the
  phone's grid cell print them apart, each signed: "+$2,400" and
  "−$15" (`calendarCellFigures` on the phone); the phone's opened day and
  its large-text list say "+$2,400 in · $15 out". Neither adds anything up.
- A card's payment stays on the calendar, labelled
  "Card payment — <card>" (`forecast.build` → `card_events`).
- **Older phone**: keeps its net figure; the new label reaches it as
  text. **Older server**: no `came_in`/`went_out`, so the phone's cell
  shows no figure.

## Bills & Income and Budget say one thing one way

- **The planner at rest prints the plan line.** The planner showed "in
  $6,001 · 100%" (the cent income) under a header saying "$6,000 in", and
  worked its own shares out, so its excess could read 24 % where the plan
  line's `shares.excess` said 25. While its figures are the saved plan's
  (`plannerAtRest`, web and phone twins: income, out, saved and excess
  each within rounding of `plan_line`), both clients print the plan line's
  whole dollars and shares; once something is typed away from it they
  print the live figures. The "Everything else" line no longer says the
  rest "flows to savings" beside "Excess cash … stays in checking".
- **Budget history**: the month still running carries `status_text` "so
  far" (it is re-frozen nightly until it closes). The backfill reaches
  `budget.BACKFILL_MONTHS` (24) back at most (`backfill_start`), so an
  imported statement archive does not turn the offer into decades of
  months judged against today's budget; `missing` counts the same window.
- **Bills, both clients**: the same title ("Bills & Income"), the same
  section order (Income, Proposed changes, the bill groups, Envelopes,
  calendar), the hero's rows as sent, and the week ahead as the server's
  `week_text` after "Next 7 days" ("$165 across 2 bills"). A used-up
  envelope pool reads "used up" (", this year" for an annual one), not
  "full".
- **Bill history**: "edit with these values" is offered only when the
  ledger's values differ from the bill's (`health.fit_diverges`); the
  envelope fit reads "3 charges a month", or "2–4 charges a month".
- **Envelope editor**: each envelope row carries `charges_category`, the
  category this month's charges carry most; when it is not the envelope's
  own category the "count all <category> spending" box says "— its
  charges this month are <category>" on both clients. Nothing is changed
  for the person.

## Smaller rules

- The monthly and yearly report's Envelopes bar is what the pools not
  pinned to Today absorbed (`drawn`) of what they held (`cap`), from the
  Envelopes row (specs/today-figures.md, "Envelopes are their own
  row"). A pool pinned to Today has a bar of its own, printed by its
  name, so it is the pool's own pair: a yearly pool's year so far of the
  pool (on the yearly email too, never its monthly shares summed), a
  monthly one's month of its size. A spill past the pool is everyday
  spending (the Food and Everything else bars count it), so the bar
  turns red and says "past the pool, counted in spending"; the Bills bar
  is the due-dated bills (`lens_email._budget_rows`).
- A bill due today, or late and unpaid, a paycheck due today and a card
  payment its issuer dates today sit in today's calendar cell. The cash
  walk still dates them tomorrow; a calendar is not a walk. A card with
  no due date stays on tomorrow, the walk's assumption.
- The "Driving it" chips name a merchant as the ledger pages do (the
  resolved merchant), not by the bank's shortened descriptor.
- A helper paragraph is one sentence and a "How this works" link to the
  matching Help topic. Safety, consent and cannot-be-undone sentences
  stay on the page.
- Help opens on the everyday guide (Today), not the reference material.
- Every state of the System health page has its title.
- The activity log names a changed setting in words.
- A cash position reads "Cash (USD)", not the aggregator's `CUR:USD`.
- The retirement table has one verdict colour per row: the "at target"
  cell. The history share is the figure behind it, uncoloured.
- The primary-checking question offers only accounts the Accounts page
  counts as live (a current balance).
- Every over-time toggle opens on 1y (`DEFAULT_RANGE` on the web,
  `DEFAULT_TREND_RANGE` on the phone): Net Worth opened on All, unlike
  Cash Flow. At phone width the toggle's track scrolls sideways, and the
  chosen option is scrolled into view on both clients.
- At phone width: a checklist step's link drops under its text, account
  pills truncate inside the card, and the top bar shows the signed-in
  name as an initial instead of seven characters of it.

## The month so far says so

"This month" on Cash Flow read like a finished month: the range label
("Nov 2026"), a per-month pace beside a partial month, and "vs the month
before" for what was really the same days of last month.

- **Function**: `reporting.window_words` decides the words of every
  windowed tab (Overview, Spending, Income): `so_far` (the window runs to
  today — "This month", or a ledger still in its first month), `heading`
  ("Nov 1–17 so far"; the range label otherwise) and, on Spending and
  Income, `prev_text` ("vs Oct 1–17" — the days `_prev_window_end` cuts
  last month to; "vs the year before" for a range of whole months).
  `reporting.income_still_due` adds `income_due` to the Income tab and
  the Overview while the window is the month so far: the paychecks still
  due this month from `budget.expected_income_remaining`, the figure
  Today and Budget call "still due" ("$2,400 in paychecks still due this
  month"), null when nothing is due or no paycheck is tracked.
- **Clients**: the web and the phone print `heading`, `prev_text` and
  `income_due.text` as sent, and print no "/mo" figure while `so_far`.
  The web's and the phone's own comparison names are gone.
- **Older phone**: keeps the range label, its own "vs the month before"
  and the per-month pace. **Older server** (a current phone): no
  `heading`, `so_far` or `prev_text`, so the phone prints the label and
  its own comparison name as before. **Stale tab**: the words come in the
  reply their figures do. **Restore**: nothing stored.

## Months before any bill are unsplit

Fixed is what the household's bills matched, so a month before its first
bill read as all variable: on a long window "Fixed" shrank and "Variable"
swelled for no reason but the window's length.

Chosen: a third band, not a split over the scheduled months only. A
split over part of the window would leave Fixed + Variable short of what
went out, so the picture would no longer add up to the total beside it;
a third band keeps every dollar drawn and says why it is apart.

- **Function**: `reporting._flow_split` (from `flow_breakdown`): a month
  is reached by the schedule as `budget.month_reached` decides (below);
  the spend of the months none reached is `out.unsplit`,
  `out.variable_scheduled` is the variable spend of the rest, and
  `out.unsplit_text` says "$12,000 went out in 24 months before your first
  bill — not split into fixed and variable. In the other 12
  months the charges your bills matched are 75% of what went out." A window with no
  scheduled month at all (a household with no bills) or no unscheduled
  one has nothing unsplit. `out.split_shown` is the nodes as printed
  (fixed, variable, unsplit, saved).
- **What reaches a month** is decided once, by `budget.month_reached`
  inside `month_status`, which writes `buckets.fixed.reached`;
  `reporting._schedule_reached` only reads it. A month is reached when
  some bill or envelope had **started** by the month's end. So the
  months before the household's first bill began are unsplit and every
  month from it on is split, whether or not a bill was paid in it: one
  boundary, never a hole after it, and the caption ("before your first
  bill") is true of every month it counts. **"Your first bill"** is the
  earliest start among the household's live bills and envelopes (today's
  list, disabled bills left out): a bill's start is its
  `scheduled_since` (below), its earliest payment ever; an envelope has
  one only once it was used (a cap alone plans no month). A bill entered
  by hand that is first due next month starts then and reaches no
  earlier month. The boundary is read from the live list for every
  month, closed and frozen ones included. A closed month whose frozen
  schedule (`snapshot_bill_rows`) holds any bill or envelope is also
  reached, whatever their start: it was budgeted under them. An EMPTY
  frozen schedule (months closed before the household set up its bills)
  adds nothing, and the month is reached by the live boundary like any
  other; its bill payments are then variable in it, as the Month lens
  reads that month.
  - *Rejected*: measuring a month against today's plan (the bills started
    by then must carry half of today's monthly figure). A household whose
    current big bills began recently (a new mortgage, a large envelope)
    failed it for nearly every past month in which it paid real bills,
    and the caption then called those months ones "with no bill planned
    or paid". Weighing what was paid against the month's spend was
    rejected for the same reason: it asks a threshold to say whether a
    schedule existed, and a month with real bill payments came out
    unsplit.
- **A bill's start** is `bills.raw.scheduled_since`, an ISO date: the
  bill's earliest payment in the ledger, so rent paid since long before
  the bill was entered counts from then, and a new household's one-year
  view stays split. It is read by `budget.schedule_first_payments` with
  the same matchers `month_status` pays bills by; an envelope's is the
  first day it was used. The earliest payment EVER, not the start of the
  current run: a bill paid for a year, skipped for two months and paid
  again was the household's from its first payment, and the months
  before the break are not "before your first bill". `bills.save_bill` keeps
  the stored start through every edit and fills it for a bill that has
  none; a batch of saves (the wizard's approve-all, the demo seed) runs
  inside `bills.starts_filled_once`, which fills every new bill's start
  in one ledger read when the batch ends. The nightly
  `bills.fill_scheduled_since` reads every active spending bill's
  payments again, in one ledger read for all of them, and a start only ever moves
  EARLIER: history that arrives after the bill was saved (a bank's
  historical backfill, a statement import, another institution linked)
  pulls it back, and nothing moves it later. An edit keeps the stored
  start. A
  bill with no stored start (never paid yet, or a value that does not
  read as a date) reads as starting at its first due date when it has
  no payment history (`lastDueOn` unset), else as started since the
  ledger began.
- **The sum holds.** In a month no schedule reached, the charges a bill
  matched before its start are not counted as fixed: the month's whole
  spend is unsplit, so fixed + `variable_scheduled` + `unsplit` is still
  what went out. The start changes only the Cash Flow picture: the Month
  lens, the Budget page and the emails still call a matched charge fixed.
- **The fixed share of the covered months** is the second sentence of
  `unsplit_text`: fixed over fixed + `variable_scheduled`, a whole
  percent (`shown_shares`), with the count of the window's other months.
  The picture's own shares still add to 100 over the whole window; the
  sentence is where a reader gets "fixed is this much of the months it
  can be measured in". It is left off when nothing fixed was paid in
  those months.
- **Clients**: the web and the phone draw `variable_scheduled` and
  `unsplit` ("Before any bill", grey), print `split_shown`, and print
  `unsplit_text` under the picture. No email draws the flow picture.
- `out.variable` and `out.shown` keep their meaning (every dollar not
  fixed). **Older phone**: draws them as before; one that already draws
  the split prints the longer `unsplit_text` as sent. **Older server**:
  no split, and its `variable` is the same figure the phone draws.
  **Stale tab**: the picture is refetched per timeframe. **Restore**:
  derived from the rows and the bills; a bill's raw arrives verbatim, `scheduled_since`
  included; an older archive has none and the nightly fill supplies it,
  and a value that does not read as a date is dropped on the way in
  (`restore.clean_bill_raw`). An older server ignores the key. The
  closed-month cache keys on every bill's raw, so a start that moves
  recomputes the months it touches.
- **Fixed is today's bills, and a long window says so.** Fixed is what
  the bills the household tracks TODAY matched, so on a window that
  reaches back before most of them began, rent paid to a past landlord
  or a loan since paid off is variable spend, and Fixed reads small with
  no word (the first-bill boundary above is deliberately not moved for
  it). `out.fixed_basis_text` says it, decided by
  `reporting._fixed_basis_words` from `budget.month_mostly_unstarted`:
  a month is "before most of them began" when fewer than half of today's
  schedule had started by its last day — every live spending bill
  counts (one with no start reads as started since the ledger began),
  and an envelope counts once it has a start (an unused cap is in no
  month's count). Only the months the picture splits are counted (the
  unsplit band says its own months). With N ≥ 1 of those M months the
  sentence is "Fixed counts the bills you track today; 18 of these 36
  months are from before most of them began." ("of the other 36" when
  an unsplit band is drawn; "this month is" when the window is one
  month); with none it is null. The starts are the same stored
  `scheduled_since` the boundary reads, so the sentence needs no extra
  ledger read and moves when a start moves.
  - **Clients**: the web and the phone print it under the picture, after
    `unsplit_text`, as sent. **Older phone**: does not know the key and
    prints nothing. **Older server**: sends no key; the phone prints
    nothing in its place (no client-side guess). **Stale tab** and
    **restore**: as the picture above — derived from the bills' raw on
    every read.
  - *Rejected*: splitting the early months by category (rent and loan
    categories read as fixed). It would count as "fixed" charges no bill
    claims, so Cash Flow would disagree with the Month lens and the
    Budget page about the same month.
- **Known limit.** "Your first bill" is the first of today's bills: a
  bill since deleted does not move the boundary earlier. A frozen month
  before the boundary whose schedule held a bill is split by it, so the
  unsplit months around it are still before the first of TODAY's bills.
  A month after the boundary in which nothing was paid is split, with
  nothing fixed in it. An envelope that pools a category starts at the
  first charge of that category it matched, which may be years before
  the envelope was set up.

## A month's categories read one way

The everyday-spending lists (the Month lens's variable list behind the
monthly email's ring, the Year lens's and the yearly email's) listed an
envelope's spill past its cap as a category called "envelope overflow",
while the Spending tab, Today and the Month lens's whole list count that
purchase under its own category.

- **Function**: `budget.listed_category(row)` is the label and key a
  category list counts an engine row under. The spill row keeps its
  composed label inside the buckets (the Why's and the money map's
  readers need it) and carries the purchase's own category
  (`purchase_category`, `purchase_stored_category`), which every list
  reads (`lenses._by_category`, `lenses.month_category_ledger`). So $20
  spilled past a $400 physio envelope lists under Medical, and the
  category's link opens the purchases it came from.
- **Groupings that remain** are named on the page: the Spending tab dates
  by the day paid and lists every purchase under its category, bills
  included; the Month and Year lenses date a bill by its month; the
  emails' ring puts the bills under their categories and an envelope
  under its name ("… · the bills under their categories, an envelope
  under its name").
- **Older phone / older server**: the lists are server rows; an older
  server still sends "envelope overflow", which the clients print as
  sent. **Restore**: derived from the rows and the bills.

## Accounts say what a due date and a backup are

- **A card's due date once it has passed.** "due 11/15" two days after the
  15th read as a bill still ahead. `data.card_due_words` writes each
  card's `card_due_text` from the liabilities pull and the ledger: "due
  11/25" while the date is ahead; once it has passed, "paid 11/14" when a
  payment came in after the statement was issued (the bank's last payment
  date, or a credit on the card — from the statement issue date, else 25
  days before the due date), otherwise "past due since 11/15". None with
  nothing owed. Both clients print it beside the balance. **Older
  phone**: prints its own "due" date. **Older server**: no
  `card_due_text`, so the phone prints the date as before.
- **A login of linked backups.** Its accounts count through the serving
  copy, so the login has no subtotal; the web printed "$0" over full
  balances. `reporting._backup_logins` sends `totals.backups` = {login:
  "backup of Plaid — not counted"} (the serving account's connection, by
  its aggregator, else its institution). The web prints the words where
  the subtotal goes and dims the rows; a category the server sends no
  section for prints no figure. The phone prints the words under the
  login's name and dims the rows.
- **One account, no subtotal.** The phone printed a login's subtotal
  above its only account; like the web it now prints the balance on the
  row alone.

## An investment's value now is its balance

The investment tables summed holdings, which a feed refreshes on its own
clock: a crypto account whose holdings were last written months earlier
read "value now" far from the balance Net Worth counts, and the Accounts
page's Top holdings could add up past an account's balance.

Value now is the account balance the totals use,
and the holdings detail is dated and hidden once it is old.

- **Functions**: `reporting._brokerage_pl` (and through it
  `_retirement_combined`) and `reporting._coinbase_pl` take each
  account's `balance_current` as `value`. Coinbase's profit is that
  value against the bank-side flows. A brokerage's profit is taken at ONE
  moment — the holdings' value against their cost basis — because the
  basis comes from the holdings and an old basis against today's balance
  read every deposit since the snapshot (and cash the feed does not list
  as a position) as gain: the part of the balance beyond the holdings is
  added to the money in, at no gain, like a holding with no basis. A
  manual basis override is today's figure and is used as given. When the
  snapshot is older than `HOLDINGS_FRESH_DAYS`, the row carries
  `pl_as_of` and `retirement_combined.pl_note` names the accounts ("…:
  gain counted to its holdings of 08/01/26; what the balance moved since
  counts as money in."); both clients print the note under the table. `data.fresh_holdings` builds `GET /api/holdings`,
  and it alone decides which positions are listed and what is said about
  the rest. A position is listed with the day its price was reported
  (`priced_text`, "priced 11/16"), so prices a day behind the balance read
  as what they are. An account's positions are left out for one of two
  reasons, and `hidden_text` names each such account with its own detail,
  each reason said once for all the accounts it holds back: its snapshot
  is more than `HOLDINGS_FRESH_DAYS` (2) old ("Invented IRA (last priced
  03/02) — priced more than 2 days ago"), or its positions add up to more
  than `HOLDINGS_BALANCE_TOLERANCE` (3%) above the balance every total
  counts ("Invented Taxable (6% over) — they add up to more than the
  account's balance"), the line opening "Holdings not shown: ". Positions a little
  above the balance are yesterday's prices on a day the market fell, and
  are shown; hiding them emptied the card on every down day. Positions
  below the balance are never a reason: the difference is cash the feed
  does not list. `as_of_text` ("holdings as of 11/16/26") is the oldest
  snapshot listed. Each row's `qty_text` is its quantity as printed (three
  decimals), null when the feed sent none.
- **Clients**: the web and the phone print the tables as sent, each
  position's `priced_text` and `qty_text`, and the two holdings texts
  beside "Top holdings". With nothing listed the card is the title and
  `hidden_text` alone: no table and no header row.
- **Older phone**: the same fields with the new values; no dates and no
  note. It prints the quantity it rounds itself and no "priced" day.
  **Stale tab**: the list and its texts come in one reply.
  **Older server**: the holdings sums, as before. **Restore**: balances
  and holdings come back as stored and are judged on the next read.

## Crypto cash in and out count bank money once

A crypto account's table read "cash in (bank)", "cash out" and a return
over every buy, sell, deposit and withdrawal row. A sale settled through
the account's cash wallet is three rows — the coin leaving, the dollars
arriving in the wallet, the withdrawal that carries them to the bank —
so one sale was cash in once and cash out twice, and the return (profit
over cash in) fell with every sale. The profit was right only because
the sale's two legs cancel in it.

Cash in is the money that came from the bank, cash
out the money that went back to it, one leg each.

- **Function**: `reporting._bank_cash_flows` (from `_coinbase_pl`, the
  only reader). A deposit or withdrawal of dollars counts whole. A buy
  or sell written as ONE row was settled with the bank directly (the
  older shape, and a bank-funded buy's still) and counts whole. A buy or
  sell written as two opposite rows sharing the trade's id (`raw.buy.id`
  / `raw.sell.id`) is one movement inside the account and counts nothing,
  whatever the two legs leave over: the difference is the trade's fee,
  money that never reached the bank. So cash in and cash out are bank
  money only, and a fee lowers `pl` (value + cash out − cash in) through
  the value it took, never raises it as cash out.
- **Clients**: the web and the phone print `cash_in`, `cash_out`, `pl`
  and `pct` as sent; neither adds or divides. **Older phone / stale
  tab**: the same fields with the new values. **Older server**: every
  leg counted, as before. **Restore**: derived from the rows on each
  read; nothing is stored.

## Property enters the trend on its recorded date

The Net Worth trend held today's property value flat back through every
month the ledger reaches, so a house recorded this year read as owned
since the ledger's first year and the all-time change was mostly a
house appearing.

Each property item enters on its recorded date;
earlier months show the financial accounts alone.

- **Function**: `reporting._property_entered` (from `compute_networth`,
  over the layer `_property_trend` and `_held_with_cash` build): a month
  before an item's date carries none of it. A hand-entered item's date is
  its `acquired_on` month — the day it was first recorded, or the
  owner's "Owned since". It is kept apart from `as_of`, the day the value
  was last stated, because every value edit restamps `as_of`: reading
  that dropped a revalued house out of every earlier month. A row with
  no `acquired_on` falls back to its `as_of`, and one with neither enters
  on the month in progress. A
  linked loan is a debt the ledger walks back through its own rows; it
  leaves the earlier months only beside property that entered later (a
  mortgage with its house), from the earlier of its first ledger row and
  the first item's month, and never on its own. What leaves a month is
  what the layer held for the item in that month: for a loan, its walked
  balance held exactly where `_held_with_cash` holds the layer (with no
  recorded month-end, every month holds today's balance). A month the
  nightly snapshot recorded a property net for keeps the recording. The
  headline, the tiles, the Over time chart and `trend_windows` all read
  the one series.
- **The date is written** by the settings door: a row sent with an
  `acquired_on` keeps it; one sent without (a value edit, or a client
  that predates the field) keeps the stored row's of the same name, and
  a new row is acquired today. Migration 228 gave every stored row its
  `as_of` as `acquired_on`, the best date there was.
- **Clients** print the series and its windows as sent. The web and
  phone Net Worth editors show an editable "Owned since" per item and
  send it back with the list. **Older phone**: never sends the field, so
  the door keeps the stored date. **Older server**: the flat back-fill.
  **Restore**: the items' dates come back with the settings; an archived
  row without `acquired_on` takes its `as_of`
  (`restore.clean_manual_assets`, the migration's rule).

## Cash Flow and Net Worth polish

- A change that rounds to $0 reads "unchanged" (`changeWords`, web
  `moneymath.ts` and its phone twin): the Net Worth headline and tiles and
  Cash Flow's best month, never "+$0".
- "Saved, by month" draws the month in progress once — its saved-so-far
  bar (solid), not a forecast bar beside it; the forecast bars (hollow)
  start the month after.
- Best and worst months carry their year ("best Mar '25"), and a wide
  chart's January label reads "Jan '06" (`monthWithYear`).
- No figure comes from a client fallback: the web prints no "after $0
  mortgage payoff" when the server sends no payoff (the phone's rule);
  the interest-and-fees tile prints the server's `paid_ytd` (this year's
  cost less refunds, $0 with nothing charged) and a year whose refunds
  passed its charges says "refunded $12", not "paid −$12"; figures the
  server always sends carry no `?? 0`; the phone's Accounts section total
  is the shown figure alone.
- The Net Worth footer says "Figures as of 11/17/26 09:14": the report is
  built on each request, and "Reports cached" said otherwise.

## Bills residuals

- A closed month's received dollars beside a paycheck count the deposit
  the month owns wherever it landed — one due on the 1st that landed on
  the 30th before is that month's (`api_bills._month_bill_stats`, the
  rule of `budget._month_income_parts`).
- The budget wizard's seeds (the bill load, the bill matchers and the
  income) count only the offers the Bills page lists
  (`budget._listed_add_offers` over `bills.pending_proposals`): no lapsed
  offer, no twin, no card payment, nothing an active bill covers.
- Approve-all's words (`approveAllWords`, both clients) leave an offer
  withdrawn as already tracked (`failed[].superseded`) out of "stayed
  pending" and say it was withdrawn; the phone keeps only the offers that
  stayed. An older server sends no flag, and every failure reads as
  before.
