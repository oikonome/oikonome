# Today's figures: one function each, printed by every surface

Today is read on four surfaces: the web page, the phone's Today tab, the
daily email's HTML and its plain text. Each figure and each sentence below
is decided by ONE server function; the four surfaces print what it sends
and compute nothing (`specs/counted-figures.md`, `specs/shown-figures.md`).
Each section names the function, what each surface reads, and how an older
phone, a stale tab and a restore behave. Amounts in examples are invented.

The rules every section shares:

- **Additive.** New fields sit beside the old ones, which keep their
  meaning unless the section says otherwise (`make api-types-compat`).
- **Older phone** (an installed build against a newer server): reads the
  old fields and shows what it showed before.
- **Older server** (a current phone against a server that has not
  upgraded): a new field is absent, so the phone leaves that line out.
  It never composes the words or picks another figure in its place.
- **Stale tab**: every figure arrives in the one Today reply and is never
  cached apart from it.
- **Restore** brings back transactions, bills and settings; every figure
  here is derived from them on the next read.

## Words are written once

The money map's status words and the cash notice's sentence used to be
composed in four places (two clients, the template, the plain text),
which matched only by care.

- `todayview.row_status` returns, beside `kind` / `amount` / `pending`,
  the `text` every surface prints ("$70 more than planned", "$450 still
  due") and its `tone` (`neg`, `pos`, `mut`) — `todayview.status_words`
  writes them. The web map, the phone's map, the email HTML and the plain
  text print `status.text` in the tone's colour.
- `todayview.cash_sentence` writes the cash notice; `cash_notice.text`
  carries it. The page and the phone print it after the notice's dot, the
  email HTML prints it in the notice's colour, the plain text prints it
  with "!! CASH: " in front when the notice is critical.
- The plan line on both clients prints `plan_line`'s terms only. With no
  plan line (no income yet) there is no line; the clients no longer pick
  `avg_bills`, `scheduled_month` or `income` in its place.
- Older phone: still composes its own words from `kind` and `amount`,
  which keep their meaning. Older server: no `text`, so the phone prints
  no status words and no cash sentence.

## The money map reads against the pace

Every judged row of Today's money map compares with
where it should be by today, the basis of the headline's "$X more than
planned by today". A row used to say "over by $X" once it passed its whole
month's budget and "$X more than planned" against the pace otherwise, so
one card mixed two bases and no row figure added up to either headline.

- **Function**: `todayview.today_map(st)` builds the rows
  (`_plan_rows`) and the Variable spending row (`plan_total(rows,
  "pace")`) and gives every judged row its status from
  `todayview._pace_status`: `vs_pace` = spent − what should have been
  spent by today, to the cent, and the words "$X over pace", "$X under
  pace" or "on pace". Bills and Savings keep their budget-basis words
  ("still due", "over by"). The Month and Year lenses keep the budget
  basis: a closed month's pace is its whole budget, where the two bases
  are the same figure.
- **How the rows and the headline reconcile**, exactly:
  1. The judged rows' `vs_pace` add up to the Variable spending row's
     `vs_pace` to the cent: the rows' spent and expected add up to the
     buckets' own (a parent's display row is the parent less its
     carve-outs, `budget.month_status` → `display_expected` = the
     parent's expected less its children's, floored at $0 when carve-outs
     claim more than the parent's budget — the one case where it does not
     hold).
  2. The Variable spending row's `vs_pace` is the verdict's `variance`
     (food + everything else, spent − expected), the figure the headline
     prints.
  3. The whole dollars printed beside the rows are apportioned by largest
     remainder (`reporting.shown_dollars`) to the whole dollars printed
     beside Variable spending, so the printed row figures add up to it, a
     row at most a dollar off its own rounding. The headline's
     `pace_line` rounds the same variance the same way
     (`reporting.whole_dollars`), so "Over budget — $412 more than planned
     by today" sits over "Variable spending … $412 over pace".
  4. The headline says "on pace" whenever the variance is inside the
     verdict's tolerance; the Variable spending row still prints the
     variance itself ("$30 over pace").
  The "by category" lines in the Why round each bucket's variance on its
  own, so one can differ by a dollar from its apportioned row.
- **The month figure beside the verdict** ("$X over the $Y", "$X of $Y
  left") stays on the budget basis: it is how much of the month's budget
  is left, not a pace.
- **Clients**: the web map, the phone's map and the email HTML print
  `status.text`; nothing changes on them. The plain text prints the same
  rows (see "The plain text mirrors the page").
- **Older phone**: `kind` and `amount` keep their meanings — past the pace
  a row is `ahead` by `amount` (printed "$X more than planned"), otherwise
  `left` by the budget left — so an installed build shows the new basis in
  its old words. **Older server**: no `vs_pace`; the phone prints the
  words it is sent.

## A forecast below $0 says when cash runs out

A forecast that goes below $0 says "Cash runs out
around <date>", not that cash "gets tight", and the chart's label uses the
same date in the page's MM/DD/YY.

- **Function**: `todayview.build_context` decides the notice as before;
  `cash_notice.runs_out` is the walk's first day below $0
  (`pace_stmt.negative_date`) whenever the notice is about a date or a
  forecast, and `todayview.cash_sentence` writes "Cash runs out around
  05/14/27 (low -$615 on 05/21/27)." for the forecast notice. A forecast
  notice only exists when the walk goes below $0, so "Cash gets tight
  around …" no longer reaches a reader; the critical notice inside 14
  days keeps its "Short $X by <date>".
- **The chart's mark**: `build_context` writes `runs_out_label` ("runs
  out 05/14/27"), sent as `forecast.runs_out_label`. The web chart, the
  phone's sparkline and the email's PNG draw it; the clients no longer
  format the date themselves (they printed "5/14/27").
- **Older phone**: keeps its own sentence from `kind` / `until` / `amount`
  ("Cash gets tight around …") and its own label. **Older server**: no
  `text` and no `runs_out_label`, so the phone prints no sentence and no
  label.

## "Slow spending to $X/day" is decided against the walk

The advice was the walk's rate plus the shortfall spread over the days to
the dip, which assumes every dollar of spending reaches checking before
the dip. With card timing it printed "$0/day", or advice that still dipped.

- **Function**: `forecast.slow_to_rate` — the largest daily variable
  spending at which the usual walk (`pace_usual`, run again through the
  same `card_walk` at that rate: card timing, bills and paydays included)
  stays at or above $0 through the chart; a bisection over [0, the pace]
  in 14 steps, floored to whole dollars. `forecast.slow_to` = {rate,
  scheduled}, computed only when the usual walk dips.
- When even $0 a day dips — the dip comes from bills and card payments,
  not spending — or the rate is under $1, there is no spending advice:
  `cash_notice.slow_to_per_day` is null, `cash_notice.scheduled` is true,
  and `todayview.cash_sentence` says what is true: "Short $50 by 07/20/26
  — cover it from savings — bills and card payments due before 07/20/26
  exceed what is coming in." "$0/day" is never printed.
- **Older phone**: words its own notice from `slow_to_per_day`; null means
  no advice there either.

## A one-off is not a pace

The daily pace the 60-day forecast burns leaves out
single large one-off charges. A month with one $1,800 furniture order on
day 5 and $25 a day otherwise burned ($1,800 + 14 × $25) / 14 = $154 a day
for two months — the forecast ran out of money on that one order spent
sixty more times.

- **The rule** (`forecast.one_off_charges`): the charges judged are the
  rows inside the pace's numerator — every variable row (Food or
  Everything else, carve-outs included) on a finished day of the verdict's
  pace window, **posted or pending alike**, because `budget.paced_daily`
  counts both; a rule that judged only posted rows left a pending
  furniture order in the pace and could never take it out. Today's
  spending is never in the numerator, so never a one-off. Each merchant
  is judged by the one PURCHASE its largest charge makes in the window
  (`forecast.one_off_purchase`; a split purchase's parts are one charge
  first), never by all its rows: the largest charge, every charge at
  least half of it, and — for a merchant with no charge in the window
  farther than `ONE_OFF_PURCHASE_SPAN_DAYS` = 7 days from the largest
  (it appears for this purchase alone) — the smaller charges within those
  7 days (a diagnostic before the repair, a deposit before the furniture).
  A deli visited every week keeps its visits in the pace when one large
  catering order there leaves; judging the merchant's whole group took
  the everyday rows out with the outlier and under-burned the pace. The
  purchase is a one-off when all of these hold —
  1. it is at least `ONE_OFF_FACTOR` = 5 times the window's median day of
     variable spending, a day with nothing spent counting as $0;
  2. it is at least `ONE_OFF_MIN` = $200, the anomaly watch's floor for a
     large charge;
  3. its largest charge is at least `ONE_OFF_LEAD_SHARE` = half of it:
     one purchase and its preliminaries, not a run of large charges of
     similar size;
  4. the merchant has no habit of purchases that size. A charge before
     the pace window is evidence of a habit only when it is at least
     `ONE_OFF_PRIOR_SHARE` = one third of the purchase being judged; one
     such charge in the `ONE_OFF_LOOKBACK_DAYS` = 180 days before the
     largest row, or two in the `ONE_OFF_HABIT_DAYS` = 365 days before
     it, make it a habit (the same merchant as the anomaly watch asks it:
     the row's merchant, else its descriptor). Small regular visits are
     no habit for a purchase ten times their size. A row inside the
     window is never evidence of a habit.
  When they pass, the purchase's charges are left out together and named
  once: the largest charge's payee and date, the purchase's total. The
  merchant's other charges stay in the pace (and in `pace_shares`). Every test is
  decided in whole cents (the floor, "half of the total", "a third of the
  total"), so a tie — two equal rows at one merchant, a row exactly half
  or a third of a total — never turns on floating-point noise in a sum;
  the heavier month's fast and reference forms do the same.
- **Why these**: the median day is what the household spends on an
  ordinary day and is not moved by the one charge it is judging (a mean
  would be); five ordinary days in one charge is a purchase, not a habit.
  The $200 floor keeps a quiet month (median near $0) from calling every
  coffee a one-off. The habit test is what makes it one-off rather than
  big: a large grocery run at the store whose runs are that size stays in
  the pace, as does a second visit to the same dealer, and so does a
  merchant visited twice in the year for that kind of money (a twice-yearly
  service). A third is the share at which an earlier charge is the same
  kind of purchase: a $60 diagnostic, a $100 deposit or a coffee at the
  same shop is a small fraction of a $1,500 repair and says nothing about
  another one coming; a $600 visit says the household spends that kind of
  money there. The half share keeps a merchant visited every few days (a
  café, a new grocery store) in the pace even when its visits add up past
  the floor: no one visit carries the total.
- **Function**: the forecast's window is the last 60 finished days
  ("The forecast's pace is the last 60 days"): `forecast.window_one_offs`
  judges that window's rows, the median day, the habit look-back and
  "before the window" all read against it — a one-off 59 days old is still
  left out and named, at 61 days it is out of the window and out of the
  note. `forecast.one_off_charges` is the same rule over the month's pace
  window. `forecast.one_offs` lists them and `todayview.pace_sentence`
  writes the sentence under the chart ("The forecast spends $125 a day on
  everyday purchases — your last 60 days, less a one-off charge: Northwind
  Garage $1,800 (07/05/26)."), which the page, the phone and both emails
  print.
  A pending one-off and a purchase of several charges print in the same
  words (one name, the purchase's total, the largest charge's date);
  `one_offs[*]` keeps its shape (`txn_id` is the largest charge's). The
  heavier month's windows (`forecast._WindowOneOffs`) judge the same
  purchase, tested equal window by window.
  The verdict, the tiles and the lens projection keep
  `budget.paced_daily` as it was: a one-off was still spent this month,
  and the month's verdict never removes it.
- **Older phone**: shows the new walk (it is the same series) without the
  sentence. **Older server**: no `pace_note`, no sentence.

## Pending card charges leave checking at the next statement

A card's pending charges were in no figure the cash timeline walks: the
card debt and every card payment read the card's posted balance, so a
week of pending purchases never left checking in the forecast.

- **Function**: `forecast.card_balances` — per card in the runway's scope
  (`CARD_SCOPE_SQL`), its posted balance, its pending charges and its
  issuer's statement facts. A card's **pending charges** are its live
  pending rows with money out (a pending credit is not money until it
  posts), less any hold a posted row already names as its own
  (`pending_transaction_id`, the settlement's link): once a charge posts,
  the feed retires the hold and the posted balance carries it, so it is
  counted once on either side of the handover. A card's **debt** is its
  signed balance plus its pending charges, never below $0: a card in
  credit (a refund or an overpayment) absorbs its pending charges, and
  only what is left of them past the credit is owed and walked
  (`tests/test_a_card_in_credit_absorbs_its_pending_charges.py`).
- **What the walk does with them** (`forecast.build`): the cash card's
  `card_debt` is the sum of the debts, so the pay-now walk starts from
  checking less them. In the autopay walk a pending charge is on no
  statement yet: it is paid at the statement after the current one (the
  current due date plus a month), with the posted balance left over from
  the current statement. The full-balance walk pays the whole debt at the
  current due date, as before. A card with only pending charges is
  walked too.
- **A bill paid by a pending card charge** is matched to its occurrence
  already (`budget.upcoming_bill_occurrences` reads pending spend rows),
  so the timeline marks it paid and walks no bill event; the charge
  leaves checking once, through the card.
- **What each surface reads**: the cash card's "card debt" tile is
  `forecast.card_debt` on the page, the phone and both emails, and now
  includes pending charges; the timeline's card rows are the same events
  with the pending part in the next-statement row. No new field.
- **Not changed**: the runway's `card_debt` (headroom, the "after cards &
  bills" tiles an older phone shows) and the Debt page, Net Worth,
  Retirement and the export read the posted balance — the balance the
  issuer reports and charges interest on.
- **Older phone, older server**: the same fields; an older phone prints
  the new figures, an older server the old ones.

## A statement already paid is not scheduled again

The autopay walk paid each card's last statement balance at the issuer's
next due date, even when the household had already paid that statement:
the payment left checking twice in the walk.

- **Function**: `forecast.card_balances` reads the issuer's last payment
  (date and amount) and the last statement's issue date beside its
  balance; `forecast.statement_owed` — the one function — decides what of
  the statement is still owed. A payment on or after the statement's issue date counts
  against it: paid in full, nothing is owed at the due date; paid in part,
  the rest of the statement is. A payment before the issue date paid an
  earlier statement and changes nothing. A missing or unreadable date
  counts as no payment.
- **The walk**: what is owed (never more than the posted balance) leaves
  at the due date; everything else on the card — the rest of the posted
  balance and the pending charges — at the statement after it. A settled
  statement draws no autopay line at the due date.
- The full-balance and pay-now walks are unchanged.
- **Surfaces**: the timeline's autopay rows, the chart's autopay lines
  (`forecast.card_autopay`) and "checking at month end" read the walk; the
  page, the phone and both emails print them as sent. No new field, so an
  older phone shows the corrected walk and an older server the old one.
- The issuer reports only its last payment: two payments against one
  statement count as the last one alone, which leaves more owed, never
  less.

## A card payment on its way leaves checking

A household that pays its cards by hand sees the payment's credit on the
card some business days before the debit leaves checking. In between the
card's balance has dropped by the payment and checking has not, so the
timeline started too high by the whole payment, and the statement's
autopay shrank to what was left of the balance.

- **Function**: `forecast.payments_on_their_way` — a credit on a walked
  card (`CARD_SCOPE_SQL`), posted in the last `PAYMENT_IN_FLIGHT_DAYS` =
  5 days, shaped like a payment (`reporting.card_credit_inflows`: a
  transfer or payment category, or none — never a merchant refund), with
  no checking-side leg yet. The leg is found by the Cash Flow month's own
  pairing, not a second matcher: `reporting.card_settlement_legs` and
  `reporting.claim_card_settlement` (same amount to the cent, within
  `CARD_SETTLE_DAYS` = 5 days, a row marked as a card payment claiming
  first, then a plain transfer out, each credit backing one leg), over
  the household's personal, not excluded accounts. A pending leg counts
  only when it is inside the balance the walk starts from
  (`budget._pending_landed`); a pending credit is not in the card's
  posted balance either, so it is no payment yet. **Funded from
  outside**: a credit no checking leg pairs with is then offered, by the
  same pairing, posted money-out rows on accounts the walk does not read
  (`forecast._claim_outside_legs`): on another card or loan, a row shaped
  like a balance transfer (an account transfer, a card payment, or named
  one) — never a purchase or a payment app's charge; on an excluded,
  shadow or business account, a card payment or a transfer out. A
  card payment or transfer out on a personal depository the walk reads is
  the first pass's, never this one's. Its money never leaves the walked
  checking, so it is not walked (`tests/test_a_card_credit_funded_from_outside_is_not_on_its_way.py`).
  **A weak shape needs the card named**: a row with no category at all (a
  payment imported from a CSV or a statement) or a bare loan payment
  (filed LOAN_PAYMENTS with no detail naming a car, mortgage, student or
  personal loan), on any account but a card or loan — the walked
  checking included, where it has already taken the money — pairs only
  with a credit on a card its name names (`forecast._names_card`: a word
  of the card's own, official or display name less the words that name no
  particular card, its bank's whole name in order ("amex" for American
  Express), or the card's last four written
  as a card's: exactly four digits after x's, a star, a hash or "ending
  (in)", never after "check" and never the tail of a longer number). Amount alone never pairs it: a same-amount car-loan
  payment or a transfer to a person is a coincidence.
  The credited card's own charges are never its leg.
- **The walk** (`forecast.build`): each such payment is a dated outflow
  labelled "Card payment on its way — <card>", on the credit's date plus
  `PAYMENT_LANDING_BUSINESS_DAYS` = 3 business days and never before
  tomorrow (the walk's first day; a debit expected today or earlier is
  late and lands tomorrow). It is in every walk, so the pay-now walk's
  start (checking less card debt) is corrected by it too. Once the debit
  posts, the pairing finds it and the row goes; the balance already
  carries it.
- **With a paid statement**: the issuer's last payment is the same
  payment, so the statement is settled (`forecast.statement_owed`) and
  the autopay row at the due date goes; the payment leaves once, on its
  way.
- **The calendar** is the one paydays use (`engine/business_days.py`).
- **Surfaces**: a timeline row like any other event: the page and the
  phone print `forecast_rows[*].label` as sent, the email's timeline
  table the same. No new field; an older phone prints the row, an older
  server never sends it.
- **Not changed**: the runway's headroom (checking less posted card debt
  less the bills due before payday) does not hold the payment back.
- A payment made from an account the household has not linked, or has
  excluded, has no leg the pairing can find: it is walked out of the
  primary checking for its five days, then dropped.

## The forecast's pace is the last 60 days

The 60-day forecast burned the month-to-date pace: spend since the 1st over
the finished days. On the 1st it was the plan, on the 3rd two days of noise,
and a heavy week moved it by hundreds of dollars a day. The forecast now
burns the household's usual pace.

- **Function**: `forecast.forecast_pace` — the variable rows (Food,
  Everything else and the carve-outs, posted and pending, as
  `budget.month_status` buckets them under today's schedule:
  `forecast.trailing_variable_rows`) of the `TRAILING_PACE_DAYS` = 60
  finished days before today, cut at the ledger's first day; less the
  one-offs (`forecast.window_one_offs` over that window and those rows:
  "A one-off is not a pace"); divided by the finished days in the window;
  floored at a quarter of the plan's daily. Fewer than `MIN_RATE_DAYS` = 7
  finished days of ledger: the plan's daily. There is no "first tenth of
  the month" fallback: a trailing window is not early on the 1st. A charge
  today's schedule calls a bill is never pace, in any month of the window
  (a closed month is bucketed under today's schedule on purpose — the walk
  walks that charge as the bill, and a pace that also held its past
  charges would spend it twice). A household whose ledger began this month
  burns exactly the verdict's month-to-date pace less its one-offs.
- **What does not change**: the verdict, the Today tiles ("running
  $X/day"), the hero, the month lens' projection, `budget.paced_daily`, the
  sweep-savings cap (the month's variance) read the month to date — they
  say how this month is going. Only the forecast walks and the figures
  read from them (the cash notice, the low point, "checking at month end", the
  chart, the timeline) move.
- **Surfaces**: `forecast.pace_basis` (`basis` trailing | plan, `days`,
  `daily`); `todayview.pace_sentence` writes `forecast.pace_note`: "The
  forecast spends $125 a day on everyday purchases — your last 60 days,
  less a one-off charge: Northwind Garage $1,500 (07/05/26) — plus $33 a
  day of envelope money." (no one-offs: "… your last 60 days — plus …";
  a ledger under a week: "… your plan's daily spending, until the ledger
  holds a week — plus …"). Every walk also spends the envelopes' drip
  (`forecast.envelope_daily`, the first day's), so the sentence names
  both: the two together are the walk's own `rate`. Under $0.50 of
  envelope money the "plus" clause is left out. The page, the
  phone and both emails print it under the chart, so the page's two $/day
  figures each say which window they are.
- **Older phone**: prints the new sentence in the same field. **Older
  server**: the old one-off sentence, or none.

## Card spending leaves checking when the household pays the card

Most spending is charged to cards, and a card's charges leave checking when
the card is paid, not on the day of the charge. The walk the headline reads
now times them so, and the page shows three ways of paying side by side.

- **Which account pays a bill**: `forecast.bill_paying_accounts` — the
  account that paid the majority of the bill's last three matched payments
  (the bill's own matcher: its merchant identity, else its words; inside
  its amount bands for an occurrence bill) in the last
  `PAYING_ACCOUNT_LOOKBACK_DAYS` = 400 days (a yearly bill's one payment
  counts); a tie goes to the most recent. No payment, or an account the
  walk does not pay (checking or another depository account, an excluded,
  business, shadow or closed card): the bill is walked from checking, as
  before. An envelope is read the same way over the rows the month's own
  claimer gives it (`budget._claim_envelope`): its merchant or words
  first, then — for a category pool (`match_category`) — the rows of its
  category that no bill or envelope claims by merchant or words
  (`forecast._category_claims`). A category pool paid to many people
  from checking is then paid from checking; read by merchant alone it
  found no payment and its money was split like the pace, mostly onto the
  cards. An envelope with no payment at all is split like the pace.
- **When a card is paid**: `forecast.card_timing`, per walked card
  (`CARD_SCOPE_SQL`). The issuer's cycle: statement k closes on the last
  statement date plus k months and is due on the next due date plus k
  months (same day of the month, clamped: `forecast.statement_of`); a
  charge belongs to the first statement closing on or after it; with a due
  date and no statement date the statement closed `CARD_GRACE_DAYS` = 25
  days before it. **A statement is paired with its own due date once, in
  `forecast.card_cycle`**: once a due date passes, an issuer reports the
  next cycle's due date while its last statement date is the old one, so a
  reported due date more than `CARD_CYCLE_MAX_DAYS` = 35 days after the
  last statement date is the OPEN statement's, and the closed one's was a
  month earlier (a statement issued on the 4th and due on the 1st of the
  next month is its own). Card timing, the expected statement lag, the
  autopay signal and the debt's split read the pairing: what is still owed
  of the closed statement leaves tomorrow once its due date has passed,
  the rest of the balance at the open statement's reported due date. The card's **lag**: its mean posted balance over the
  last `CARD_LAG_DAYS` = 90 days divided by its mean daily charges — how
  long a dollar stays on the card — read only with 60 days of the card's
  rows, two payment-shaped credits (`reporting.card_credit_inflows`) and
  $300 of charges in the window. A due-date payer carries a dollar half a
  cycle plus the grace days (`stmt_lag`). **Mode**: `statement` with a
  cycle and no lag, or a lag within `CARD_STATEMENT_SLACK_DAYS` = 7 of
  `stmt_lag`; `lag` with a shorter lag; `charge` (paid the day of the
  charge) with neither.
- **The one rule**: `forecast.card_leaves_checking(timing, charge_day,
  today, scenario)`. *Your usual*: a statement-mode card at the due date of
  the statement holding the charge; a lag-mode card its lag after the
  charge, never after that due date; a charge-mode card on the day. *Pay on
  the due date*: every card with a cycle at that statement's due date, one
  without on the day. Every date is rolled to the next business day and is
  never before tomorrow.
- **A card's debt** (posted balance and pending charges, `card_balances`):
  a statement-mode or charge-mode card, and every card in *pay on the due
  date*, pays it as before — what is still owed of the last statement at
  the due date (`statement_owed`), the rest of the posted balance and the
  pending charges at the next statement's. A lag-mode card's debt is its
  newest charges (`forecast.card_debt_charges`: the card's live charges
  walked back from the newest until they add up to the debt), each leaving
  by the rule. A statement already paid is not in the debt; a payment on
  its way leaves checking once, as a checking event, as before.
- **The walk** (`forecast.build`'s `card_walk`): each day's pace splits by
  where the last 60 days' variable spending was charged
  (`forecast.pace_shares`: each row's account; checking and any account the
  walk does not pay are "checking"); checking's part leaves checking that
  day, each card's part is charged to the card. A bill a card pays is
  charged to it on its scheduled day (a card charge posts on a weekend) and
  is not a checking event; every other bill is unchanged. An envelope's
  card part is charged the same way.
- **The invariant** — one dollar, one exit: per card, its debt plus every
  dollar charged to it inside the horizon equals its payments out of
  checking inside the horizon plus `cards_after_horizon` for it, to the
  cent; a dollar of spending leaves checking directly or through exactly
  one card payment, never both. `tests/test_the_cash_walk_adds_up_for_generated_households.py`
  holds it for generated households with pending charges, paid and
  part-paid statements, a payment on its way, business days and bills on
  either account.
- **The three scenarios** (`todayview.CASH_SCENARIOS`), every one sent:
  - *Your usual* — walk `pace_usual`, the usual rule. It is the headline:
    the cash notice ("Cash runs out around …", the low, "slow to $X/day"),
    `runs_out_label`, "checking at month end" (`month_end_left`), the low
    points in words, the email's chart and timeline, and the Cash Flow
    graph's forecast tail all read it. In the reply it is only in
    `scenarios[]` (key "usual"), which installed clients do not read.
  - *Pay all cards today* — walk `pace_today` (key "now"), see "Pay all
    cards today, then your usual" below.
  - *Pay on the due date* — walk `pace_due`, the "due" rule: the latest the
    household pays. The legacy fields keep the meaning an installed phone
    gives them and are this walk: `forecast.pace_stmt` (labelled "autopay
    statement balance" there), `forecast.card_autopay` (its autopay lines),
    `forecast_rows` (`bal_stmt` its balance, `bal_now` pay-all-now's; the
    month-end row "left at month end — pay on the due date").
  The engine-internal walks `plan`, `pace` (full balance at each due date,
  `card_events`, also the calendar's card cells) and `pace_nocards` are
  kept at the new pace with no product reader. Not moved by card timing:
  the runway's headroom and the "after cards & bills" tiles (posted card
  debt), the card debt tile (`forecast.card_debt`), the calendar.
- **Surfaces**: `forecast.scenarios` — `[{key, label, series, min,
  min_date, negative_date, low_words, runs_out_label, marks, rows}]`,
  "usual" first. `low_words` ("$3,270 on 05/12/27", "· below $0 from
  05/06/27" when it goes negative) is printed for all three side by side
  on the page, the phone and both emails. One selector (the web's and the
  phone's segmented control) picks the scenario the chart and the
  timeline show; the chart's tooltip lists the other scenarios' balances
  for the hovered day. A card paid at its due dates keeps its timeline
  rows ("Northwind card — statement due") and chart lines (`marks`); a card paid sooner has no rows — its
  payments run with the daily pace — and `forecast.card_timing_note` says
  so ("Card spending reaches checking about 15 days after the charge on
  Northwind card, as your payments over the past 90 days show."). A bill a
  card pays is listed on its day as "Acme Power — on Northwind card"
  (`rows[*].card`, `forecast_rows[*].card`) and moves no checking balance.
  The Bills calendar (`/api/calendar`) lists such a bill on its day with
  the same words ("Acme Power — on Northwind card": `label`, `note`
  "on Northwind card"); its figure and day are unchanged. Its card cells
  are the usual walk's card rows — the same dates and amounts as the Today
  page's default view, `name` the card and `note` how it is paid
  ("Harbor card", "paid after payday") — not a full balance at the due
  date. On a card's due day only that statement's own row ("… —
  statement due") moves from tomorrow (the walk's first day) into today's
  cell; a round row stays on its day. `card_timing_note` is written in
  sentences, one clause per rhythm: a round card whose own gap is at least
  25 days is paid "about once a month, after a payday", and one with no
  statement cycle falls back to "about 30 days after the charge", not a
  due date. **Older phones**: the API cannot tell an older client (no
  client-version header or parameter is sent), so the calendar serves
  every client the usual walk's card rows; an older phone's Today shows
  the due-date walk, and its calendar the usual one.
  The emails show the usual walk, its chart and its timeline (one balance
  column) and the three lows. The "checking at month end" tile reads the usual
  walk; another scenario's month-end row names its scenario ("left at
  month end — pay all cards today"), so one label never carries two
  figures.
- **Older phone**: draws `pace_stmt` (the due-date walk, under its own
  label "autopay statement balance") beside `pace_now`, prints
  `forecast_rows` (that walk's rows, a card-paid bill's label naming its
  card) and the server's cash notice and "checking at month end" (the usual
  walk), and none of the new sentences
  (`tests/test_an_installed_phone_reads_pace_stmt_as_the_due_date_walk.py`
  reads the reply as such a phone does). **Older server**: no `scenarios`;
  the new clients draw `pace_stmt` and `pace_now` as before (the phone's
  marked stand-in).

## A seasonal bill is estimated from last year

A utility bill that runs higher in winter was walked at its current
amount, so a forecast made in autumn spent a summer bill in January.

- **Function**: `forecast.occurrence_amount(bill, history, due, today,
  hand_set)` — the amount the walk takes for one future occurrence of a
  spend bill. It applies only when all hold: the bill is monthly (one date
  a month); its matched payments (`forecast.bill_payments`: the bill's own
  matcher, inside its amount bands) cover `SEASONAL_MIN_COVERED` = 15 of
  the `SEASONAL_COVER_MONTHS` = 16 months before today's, including the
  occurrence's month a year ago and the three months before today's and
  their twins a year earlier; the last 12 payments vary (coefficient of
  variation at least `SEASONAL_MIN_CV` = 8 %); and the amount was not set
  by hand inside its grace (`bills.in_manual_grace`: the person's figure
  wins for 60 days). Then: last year's payment for the occurrence's month
  × r, r = the last three months' payments ÷ the same three months a year
  earlier, held in `SEASONAL_RATIO_CLAMP` = [0.80, 1.25]; the estimate
  held in `SEASONAL_AMOUNT_CLAMP` = [0.5, 2] × the bill's amount; to the
  cent. Example: last December $120, last August–October $90 + $95 + $100,
  this August–October $99 + $104 + $110 → r = 1.098, December's estimate
  $131.79.
- **Only the forecast's occurrence moves**, and only one due after today
  and not part-paid: the bill's amount, the plan's monthly figure, month
  status, Bills, the still-due tiles, the runway and the calendar's owed
  cells keep the bill's amount. A payment outside the bill's amount bands
  is not its payment, so a swing wider than the band reads as missing
  months and the rule does not apply.
- **Surfaces**: the row's label is "Acme Power (estimate)", composed by
  the server, and `forecast_rows[*].estimated` / `scenarios[*].rows[*].estimated`
  is true; every surface prints the label as sent. `forecast.estimated`
  lists the labels.
- **Older phone**: prints the label. **Older server**: the bill's amount.

## A paycheck that stopped is not walked

A pay series leaves the walk only when the ledger says its money stopped,
never because we stopped seeing it. Judged once a day, kept with the
day's learned facts (`learned(…, "stopped")`).

- **Function**: `forecast.stopped_paychecks(conn, cfg, today)` → {payee:
  last check}, as the ledger stood at the end of yesterday. A series
  stops only when all hold: it has a recurrence and a claimed check
  (`budget._claim_income_rows`); the accounts its last checks landed in
  are being read (a posted row at most `PAYCHECK_FEED_FRESH_DAYS` = 7
  days before yesterday — a feed gone quiet or a statement imported once
  a month says nothing); at least `PAYCHECK_STOPPED_PAYDAYS` = 2 of its
  paydays passed since its last check, each with a posted row there at
  least `PAYCHECK_STOPPED_GRACE_DAYS` = 5 days after it; no deposit there
  since could be the same check under another name or amount (an inflow
  no series claims as a fit — half to twice that series' check, within 3
  days of its payday — of income or transfer-in type, none, or its own
  checks' type,
  not one leg of an own transfer, at half to twice its usual check — or
  at least a quarter of it within 3 days of a payday, under the $200
  deposit floor too); and its own history shows no gap as long beginning
  about a year before its last check (a school-year salary in summer). A
  deposit another series claims (a new job's) is that series' own, so a
  job change still drops the old check about two cycles after it ended.
- **Surfaces**: the walk leaves the series out; `next_paycheck` never
  names it; the build sends `stopped_paychecks: [{payee, last}]` and
  `todayview.stopped_paychecks_note` words it once: "Northgate Payroll
  has not arrived since 03/06/26, so it is not in this forecast — turn it
  off on the Bills page or it will be expected again when it returns."
  (`Forecast.stopped_paychecks_note`, optional). The plan's income,
  "paychecks still due" and the Bills page keep the series until the
  household turns it off.
- **Older phone**: prints no note. **Older server**: walks the series.
- Test: `tests/test_a_paycheck_that_stopped_is_not_walked.py` (seeded
  live paychecks with a quiet feed, a monthly import, a renamed payer,
  short checks and a summer gap are never dropped).

## A paycheck follows last year's pattern

A paycheck that changes through the year — lower
early in the year while benefits and taxes reset, higher once a yearly
wage maximum has been paid — is walked by the series' own last year, not
by tax rules.

- **Function**: `forecast.paycheck_pattern_amount(current, deposits,
  payday, today)` — the amount the walk takes in for one future paycheck
  (a scheduled payday after today). It applies only when all hold: the
  series' deposits (the ones it claims) reach back
  `PAYCHECK_PATTERN_MIN_DAYS` = 365 days (a new job is not read); last
  year's cadence is today's (the median gap between deposits around a year
  ago within `PAYCHECK_CADENCE_TOLERANCE` = 25 % of the last 90 days' — a
  series that went from monthly to biweekly is not read); last year's
  deposit at the payday's point in the year (the deposit nearest the
  payday a year earlier, within half a gap) differs from last year's
  deposit at today's point by at least `PAYCHECK_STEP_MIN` = 1 %, and the
  deposit beside it is within 1 % of it — a step that held for two
  deposits in a row, not one bonus check. The deposit at today's point is
  the base only when a deposit beside it agrees within 1 %; a bonus check
  or a one-check dip there is no level, and the base is the median of it
  and its two neighbours
  (`tests/test_a_paycheck_pattern_reads_a_level_base.py`). **The year-start dip**
  (`forecast._year_start_ratio`): for a January payday, a step needs no
  second deposit when last year's deposit is the first of its year, also
  in January, the checks just before it (`PAYCHECK_YEAR_START_RUN` = 3,
  at least two) agree with one another within 1 % — a steady pay, not
  hours that vary — and it sits `PAYCHECK_YEAR_START_MIN` = 3 % to
  `PAYCHECK_YEAR_START_MAX` = 10 % below their mean; the ratio is that
  deposit over that mean. Deductions that reset with the year take the
  first check and may be back by the second; a cut deeper than 10 %
  (unpaid leave) is not a reset, a December payday whose year-ago
  neighbour is a January check does not take it, and on variable pay
  (weekly hours with a 15 % spread, biweekly with 6 %) the rule gives no
  more January estimates than the two-in-a-row rule alone
  (`tests/test_a_years_first_paycheck_follows_last_years_dip.py`). **Strays**: a deposit under `PAYCHECK_STRAY_SHARE` = 50 % of
  the series' median deposit (a reimbursement the payer sends beside the
  checks, however it was claimed) is left out of every step above — it
  neither breaks a step that held nor stands in as one. A series whose
  earlier deposits sit on an account since closed is read like any other
  (its deposits are claimed whatever account they landed in). The
  benchmark's `year_start_dip` persona carries the shape. Then: the current amount (the
  walk's own estimate, `_paycheck_amount`) × that ratio, held in
  `PAYCHECK_RATIO_CLAMP` = [0.85, 1.15] — an early-year withholding drop
  or a wage-maximum rise moves a check by a few percent, a bonus or a job
  change by more — to the cent. The ratio carries over, never last year's
  level, so a raise since then is kept. Example: last year $2,000 a check
  until August and $2,200 after, $2,300 a check now → September's checks
  walk at $2,530 in a forecast made in July.
- **Only the forecast's occurrence moves**: the plan's income, "paychecks
  still due" and its dates, the month's income, `next_paycheck` and the
  runway keep their figures.
- **Surfaces**: the row is labelled "Northstar Payroll (estimate, from
  last year's pattern)" by the server, with `estimated` true; every surface
  prints it as sent. No new field.
- **Older phone**: prints the label. **Older server**: the current amount.

## A heavier month is drawn beside the expected one

The walk spends at the usual pace, and the usual pace leaves out one-offs
— which still happen, just not on a day anyone can name. A heavier stretch
is drawn beside every scenario instead of being folded into it.

- **Function**: `forecast.heavy_month_factor` — for each day of the last
  `HEAVY_LOOKBACK_DAYS` = 365 with `HEAVY_WINDOW_DAYS` = 30 days of ledger
  after it and 60 before it: the variable spending of the 30 days from it
  per day (one-offs included: the heavier month is the month with the
  repair in it) over the forecast's pace as of that day — the same
  variable rows and the same one-off rule `forecast_pace` reads, floored
  at a quarter of the plan's daily. The factor is the `HEAVY_PERCENTILE` = 80th
  percentile of those ratios: 30-day spending ran above pace × factor in
  about one window in five. Held in `HEAVY_CLAMP` = [1.05, 2.00];
  `HEAVY_DEFAULT` = 1.25 under `HEAVY_MIN_HISTORY_DAYS` = 180 days of
  ledger. Rows are bucketed as the pace's are
  (`variable_rows_since`).
- **One ratio, surprises included**: the ratio's numerator is ALL the
  variable spending of the 30 days after each past day — one-offs
  included — and its denominator the pace the forecast would have walked
  that day (60 days less one-offs), so the line is, by construction, the
  pace the household exceeded in one 30-day stretch in five. Nothing is
  added beside it (an earlier "room for a one-off" on top of the factor
  counted the surprises twice and read far too gloomy). The legend and the
  quiet line say the same words (`todayview.heavy_label`): "heavier month
  — spending 32% above the forecast's pace, as one 30-day stretch in five
  ran in the last year" — what the factor is: a ratio to the pace the
  forecast had going into each stretch, not a dollar level the household
  reached in one month in five (an earlier "one stretch in five ran this
  high" read as dollars, and in dollars the line was crossed far more or
  less often than that); with the default factor, "heavier month — 25%
  above your usual pace". The pace sentence prints the heavier line's
  everyday dollars beside the pace: "… A heavier month spends $165 a day
  on everyday purchases." (`pace_basis.daily` × the factor, the rate the
  heavier walks apply on top of the same envelopes).
- **The walks**: each scenario's heavier line is the same walk at the pace
  times the factor (envelopes, bills and paychecks unchanged): `pace_low`
  is the usual one; `forecast.heavy[key]` carries all three. A heavier line
  is never above its scenario on any day.
- **The quiet line**: `todayview.cash_band` ("The quiet line reads cash
  after card balances too") — kind "heavier" when only the heavier line dips
  below $0. `cash_notice` stays quiet then; when the usual walk itself goes
  below $0 the notice is the one warning and `cash_band` is null. It is a
  field of its own so an older phone that words the notice from
  `cash_notice.kind` stays silent.
- **Card spending after the chart**: each scenario's
  `cards_after_horizon` and `cards_note` ("$3,240 of card spending is paid
  after 05/30/27, when the chart ends."), printed under the chart for the
  selected scenario (the usual one in the emails) when over $0.50.
- **Surfaces**: `forecast.scenarios[*].heavy` (and `heavy_label`, "heavier
  month") is drawn as the chart's dashed second line on the web, the phone
  and the email PNG, and listed in the web chart's tooltip; `cash_band.text`
  is printed where the cash notice is, on all four surfaces.
- **Older phone**: no line, no sentence. **Older server**: no `heavy`, no
  `cash_band`: the new clients draw one line and print nothing.

## Each card is paid its own way

Each card's payment timing is decided on its own, so an autopay card is
never pulled into a payday round and a carried balance is not read as a
paying lag. The way is decided by `forecast.card_timing` (`mode`), in
this order:

1. **Statement autopay** (`autopay`) — `forecast.autopaid_statements`: of
   the card's last `AUTOPAY_STATEMENTS` = 3 closed statements whose due
   date has passed (paired by `card_cycle`), a statement is autopaid when
   the card's payment credits within `AUTOPAY_DAYS` = 3 days of its due
   date (the due dates stepping back by month from the one `card_cycle`
   pairs; an issuer that dates the due a fixed grace after the close
   drifts by up to three days across February) add up to its balance at
   close (the posted balance less every
   posted row after the close) within max($1, `AUTOPAY_TOLERANCE` = 1 %).
   At least `AUTOPAY_MIN_OF` = 2 of 3 is autopay. The signal reads every
   statement of an autopay card and none of a hand payer's. With fewer than 2 statements judged a card is not autopay.
   Paid at each statement's due date.
2. **Paid in rounds after payday** (`rounds`) — the household has a
   paycheck series, and at least `PAYDAY_HABIT_SHARE` = 60 % of this
   card's own payment-credit dollars over `PAYDAY_HABIT_MONTHS` = 6 came
   within `PAYDAY_HABIT_DAYS` = 4 days after a household payday, over at
   least 4 payments. Only these cards are in a round, and the round's
   affordability test counts only what they owe. Such a card is paid only
   in rounds: when none is affordable, at its statement's due date, or —
   with no statement cycle — `ROUND_FALLBACK_DAYS` = 30 days after the
   charge (never the day of it).
3. **Lag** (`lag`) — a measured lag of at most `CARD_LAG_MAX_DAYS` = 45
   days, shorter than a due-date payer's by more than a week, with payments
   covering `CARD_PAYDOWN_SHARE` = 80 % of the card's charges over the last
   90 days.
4. **Carried balance** (`carried`) — a measured lag, but longer than 45
   days or payments short of 80 % of charges, and not a card whose last 3
   statements were each paid in full (within 1 %) by their due dates: the household carries the
   balance. The usual walk pays what it has been paying — the median of
   its last 6 calendar months' payments, at least the issuer's minimum
   payment when the liabilities report one — monthly on its usual day (the
   median day of month of those payments), while the balance lasts. The
   rest **stays on the card** (`stays`, `stays_on_card`): it is no exit
   from checking and not "after the chart", and the notes say so ("Summit
   card carries a balance: the forecast pays $120 a month, as you have
   been, and the rest stays on the card." / "$5,100 stays on your cards as
   a carried balance."). Pay on the due date stays the full-payment
   what-if.
5. **Statement** (a cycle, no other evidence) or **charge** (neither), as
   before.

One dollar, one exit becomes: per card, debt plus charges in the horizon =
payments out of checking in the horizon + `cards_after_horizon` + `stays`.
`tests/test_each_card_is_paid_its_own_way.py` simulates households day by
day (an autopay card beside a payday card, a revolver, rounds spaced a
month apart).

**Each card keeps its own spacing**: `card_timing` learns each round
card's median gap between its own payments over the last 6 months (`gap`,
from at least `ROUND_GAP_MIN_PAYMENTS` = 4 payments; payments within 4
days of each other are one) and its last payment (`last_paid`). A card
joins a round only once `ROUND_GAP_SHARE` = 0.8 of ITS gap has passed since
ITS last payment, and the round's affordability test counts only the
cards that join it. A card paid every two weeks and one paid monthly in
the same household are paid each on its own rhythm. Fewer than 4
payments: every affordable payday. The heavier line pays the same cards
on the same days (`pace_usual.round_cards`).

## Your usual: after payday when checking can cover it

A household that pays its cards within a few days after payday is
walked that way. The habit is learned per household; the measured-lag rule stays the
fallback.

- **Function**: `forecast.card_payday_habit` — over the last
  `PAYDAY_HABIT_MONTHS` = 6 months, the share of card-payment dollars that
  left checking (`forecast.checking_card_payments`: rows the feed marks as
  a card payment, and plain transfers out that pair with a card's payment
  credit by the Cash Flow month's pairing) within `PAYDAY_HABIT_DAYS` = 4
  calendar days after one of the household's own paydays (the deposits its
  income series claim). A **payday payer** has a share of at least
  `PAYDAY_HABIT_SHARE` = 60 % over at least `PAYDAY_HABIT_MIN_PAYMENTS` = 4
  payments and a paycheck series with `PAYDAY_HABIT_MIN_DEPOSITS` = 4
  deposits in the window. Its **floor** (`card_habit.cushion`): the LOWEST
  checking balance the household let itself reach at the end of the last
  day of a past round (the payments after one payday) over the last
  `PAYDAY_CUSHION_MONTHS` = 12 — the tolerance it has shown — never below
  $0 and never above `PAYDAY_FLOOR_PAYCHECK_SHARE` = half a paycheck (the
  median deposit of the largest series); $0 under
  `PAYDAY_CUSHION_MIN_ROUNDS` = 4 rounds. The floor is the lowest
  balance, not a typical one: money left over is not evidence of
  refusing to pay.
- **Which cards**: since "Each card is paid its own way", the habit is
  read per card (mode `rounds`); the household figures here (`share`,
  `cushion`, `gap`) describe its rounds.
- **The walk** (the usual scenario only): every charge on a round card
  waits on its card.
  Day by day, as checking would see it, each statement due that day leaves
  (the due-date rule); then, on the business day after each payday the walk
  lands (the rolled landing day: a Labor Day paycheck that lands Friday is
  followed by Tuesday), the round's cards are paid in full when both hold
  (`refused` in the walk): checking IMMEDIATELY after paying is at or above
  the floor — the same moment the floor is measured at — and the walk's
  checking stays at or above $0 through the day before the next paycheck
  lands (the bills, the statements due, the other cards' payments and the
  checking share of the spending until then counted: "would I run out").
  Nothing else gates a round: the floor is compared at the same moment
  it is measured, right after paying, since checking always falls further
  before the next paycheck. When the whole round is not affordable, the walk pays as many
  of its cards as keep both true, the nearest statement due first; the
  rest wait for their due dates and are offered again at the next payday
  ("when I can afford to I pay off all cards on payday; when cash is
  tight I wait"). The walk's checking counts every card payment it has
  placed (the other cards' statements and lags, the pay-today payoff).
  Each projected payday's decision is in `card_habit_debug` (`day`,
  `eligible`, `waiting_gap`, `paid`, `refused`: floor | trough), for
  tests and read-only probes; no client is sent it. Charges past the chart's end are counted in
  `cards_after_horizon`. Two earners' paydays are all paydays. A paycheck
  that already landed is a payday too (`forecast.landed_paydays`): a
  series' latest deposit counts when it landed on or after the household's
  last round, or, with no round in history, within the last 5 business
  days, and no card payment has left checking since it. Its round is the
  next business day from tomorrow, under the same affordability and gap
  tests (a check Friday, today Monday, the cards last paid two weeks ago:
  a round tomorrow) — but only while today is at most the household's own
  **delay** after payday plus one business day (`card_habit.delay`, the
  median business days from a payday to its round's last payment): a
  household that pays the next business day and let Friday's check pass
  by Wednesday has skipped that round, and the walk waits for the next
  payday that passes the gap and floor tests, instead of paying
  "tomorrow" on every day after.
- **What the household does now** (`forecast.recent_rounds`): which cards
  join, each card's gap between payments and the household's gap between
  rounds are read from the rounds of the last `ROUND_RECENT_DAYS` = 120 days when they
  hold `ROUND_RECENT_MIN` = 3, else of the last 180, else from all the
  rounds learned. A household that used to pay every payday and now pays
  about monthly is walked monthly, and the note says "about once a
  month, after a payday". The floor is not read from the recent window. A
  card paid in a round has its last payment (`card_timing[*].last_paid`).
  A household
  that is not a payday payer — or has no paycheck series — keeps the
  measured-lag rule. One dollar, one exit holds as before
  (`tests/test_a_payday_payer_pays_its_cards_after_payday.py`).
- **The heavier line keeps the rounds**: the rounds are decided once, on
  the usual walk (`pace_usual.rounds`); its heavier line (`pace_low`) pays
  every card on those same days, whatever it owes then, and otherwise at
  the due dates. It differs only by the extra spending, so it sits at or
  below the usual walk every day by construction — a heavier balance that
  failed the cushion test would otherwise skip the round and slide past
  the chart, drawing the heavier month above the usual one.
- **Surfaces**: each decision is a dated row ("Northwind card — paid after
  payday"; a statement paid at its due date stays "— statement due");
  `forecast.card_timing_note` says which rule was used: "You usually pay
  your cards right after payday when checking can cover it (and keep
  $X); otherwise at the due date." `forecast.card_habit` carries the
  learned figures. No new client field: an older phone prints the rows.

## Pay all cards today, then your usual

"Pay all cards now" used to read `pace_now`, which takes every future
card charge out of checking on the day of the charge: checking less what
is owed on cards, every day — paying every card to zero continuously. On
a payday payer it met "Your usual" on each round day and fell away
between rounds, and its sentence ("If you paid every card off today you
would dip to …") was false: paying everything off today and carrying on
as usual dips nowhere near there.

- **Pay all cards today, then your usual** (`forecast.pace_today`, the
  usual walk with `card_walk(..., payoff=True)`, `forecast.pay_off_cards`
  inside `build`): every card's whole current balance (`card_balances`'
  debt, posted and pending) leaves checking once, on the walk's first day
  ("Northwind card — paid in full"). A payment on its way is already out
  of the card's balance and is checking's own event, so it is not paid
  again; the issued statements are settled by the payoff (no due-date row
  for them). From that state the walk is exactly the usual one — each
  card under its own mode (autopay at its due dates, rounds after
  paydays with each card's own gap and the cushion, lag, carried), today
  counting as every card's last payment — so a round the usual walk takes
  within the card's own gap of today is skipped. The two walks are the
  same from the first round day on which both pay every card (with no
  other card's issued statement still ahead of the usual walk); before
  it they differ because the gap and the cushion decide from a different
  state. One dollar, one exit, as every card-aware walk. Its heavier line
  is the same walk at the heavier pace, paying on its own round days, so
  never above it.
- **Cash after card balances** (`forecast.pace_now`, unchanged): checking
  less everything owed on cards, day by day — every charge out of
  checking on its day. The cautious measure, no longer a scenario.
- **Who reads which**: the selector's "now" (`scenarios[]`, label "Pay
  all cards today", its chart, rows and heavier line) reads `pace_today`;
  its low says "If you paid every card off today, your low would be
  $2,880 on 05/19/27." (`todayview.pay_today_low_words`). Cash after card
  balances feeds the quiet warning (below) and the list's second column:
  every scenario row carries `after_cards`, headed
  `forecast.after_cards_label` ("after card balances"), on the web beside
  the scenario's balance — off at phone width, as every `hide-m` column,
  so the phone does not print it. It is not on the chart: the chart
  already draws the scenario, its heavier line and the marks, and a third
  line that is not a way of paying would read as one.
  `forecast.pace_now` and `forecast_rows[*].bal_now` keep their meaning
  for installed phones (which label it "pay all cards now" and draw it
  that way).
- **Older phones**: a phone with the scenario selector prints the
  server's label and words, so it shows "Pay all cards today" and the new
  walk; an older one (no `scenarios`) draws `pace_now` as before.

## Checking at month end

The month-end tile read "left at month end": the usual walk's checking on
the month's last day, with thousands still on the cards (paid at the next
round), and under it "still owed this month: bills $X + envelopes $Y",
some of which was already out of the figure (bills from checking before
month end, the checking share of the envelopes) while card-charged bills
and the cards' share were missing. The figure stays; the words now say
what it is and add only what it does not hold.

- **The figure** (`todayview.month_end_left`): the usual walk's checking
  on the month's last day, labelled `MONTH_END_LABEL` = "checking at month
  end" (`month_end_label`; the month-end row of every scenario's timeline
  says the same).
- **The line under it** (`todayview.month_end_cards_note`,
  `month_end_cards_note`): what the same walk still holds on the cards
  that day (`walk["cards_owed"]`: today's card debt, plus every dollar
  charged to the cards after today, less what the walk paid them — at the
  chart's end it is exactly `cards_after_horizon` plus `stays_on_card`)
  and its next card payment after the month: "with $1,240 still on your
  cards then · next card payment $1,180 on 08/03/26". Nothing on the
  cards: no line.
- `report.month_end_text` ("bills $X + envelopes $Y") stays beside the
  bills and envelopes still-due figures on the Bills page and the
  Envelopes row, where it is true; it is no longer printed under a
  checking balance (page, phone, both emails). An older phone that reads
  `month_end_parts` keeps it; a newer phone on an older server labels the
  tile "left at month end" (its marked stand-in).

## The timeline runs through the low

The timeline's rows stopped at the month's end or two weeks out,
whichever was later, while the low point the tiles quote could be weeks
after that — the events that make the low were listed nowhere, and the
email's "+N more after the low point" counted nothing.

- **The one place** (`todayview.timeline_horizon`): each scenario's rows
  run to the month's end or `TIMELINE_MIN_DAYS` (14) days out, whichever
  is later, and never stop before that scenario's own low point
  (`walk["min_date"]`). Every scenario has its own horizon, since each has
  its own low.
- **Shown**: the clients list the rows up to the selected scenario's low
  (and the month-end row) by default, the rest behind "all N events" —
  unchanged. The email keeps its short list (`todayview.email_timeline`):
  the usual scenario's rows up to its low but no further than the month's
  end or two weeks out, at most `EMAIL_TIMELINE_MAX` = 25 of them, plus
  the month-end row and the low point's own day; the rest is a count in
  the server's words (`tl_rest_words`) — "+ 4 more scheduled events after
  the low point" only when every hidden row comes after the low, else "+
  4 more scheduled events" — then "— open Oikonome for the full timeline".
- An older phone reads the same `scenarios[*].rows` (longer now) and the
  due-date walk's `forecast_rows`, which follow the due-date scenario's
  horizon. No stored figure moves.

## Every view marks the cards' due dates

A due date is a fact about the card, not about when the household pays,
so every scenario's chart marks it — "Your usual", "Pay all cards today"
and "Pay on the due date" alike.

- **Dates** (`forecast.card_due_dates`): each walked card that owes
  something or is charged in the walk, every statement due date after
  today through the chart's end, on the cycle `forecast.card_cycle`
  pairs (the closed statement's own due date, then a month on each) — the
  real date: a due date on a Saturday is marked on the Saturday, while the
  payment the due-date view makes lands the Monday after. The same dates
  in every walk (`walk["due_marks"]`).
- **Words** (`todayview.due_marks`, `scenarios[*].due_marks`: `{date,
  card, label, words}`): the chart's caption "Harbor card due"; the
  hovered day's line says what that view still pays the statement on its
  day — "Harbor card due — $412 owed", "… — paid" when the view paid it
  earlier (a payday round, the pay-today payoff, a lag card), "… —
  carried balance" for a card the view carries. Payment rows stay where
  each view pays them; the mark only shows the deadline.
- **Drawn**: the web chart and the phone sparkline draw one dashed line
  per mark (the web's tooltip lists the day's `words`; the phone has no
  tooltip), in place of the dated autopay lines, which only an older
  server's reply still draws (`marks`, the phone's marked stand-in). Payday
  rounds get no line: the list names them. The email's chart draws the
  usual scenario's due dates ("Harbor card due", "3 cards due").

## Each low says what the cards still hold

"Pay on the due date" showed the highest low of the three only because it
carried the most card debt to the chart's end, and nothing beside its low
said so; the chart's dated payment lines, which an older client labels
"autopay", were sent for every card whatever its mode.

- **The words** (`todayview.low_cards_note`, `scenarios[*].low_cards_note`,
  `fc_lows[*].cards_note`): what the same walk still holds on the cards
  on its low day (`walk["cards_owed"]` at `min_date`), "with $2,950 still
  on your cards then"; none at half a dollar or less. Printed under each
  low on the page, the phone and the HTML email, and after the low in the
  plain text ("…on 02/09/27, with $2,950 still on your cards then"; a low
  that ends its own sentence gives up its period to the clause). The
  chart's end keeps its own sentence (`cards_note`, "$X of card spending
  is paid after …").
- **Autopay is autopay** (`todayview.autopay_marks`): `scenarios[*].marks`
  and `forecast.card_autopay` carry only the payments of a card whose
  `card_timing` mode is "autopay", matched by card id (`walk["mark_cards"]`
  beside `marks`, `card_timing[*].id`), never by name: two cards may share
  one. The current clients draw `due_marks`
  ("Harbor card due") and never read them; an older client draws them
  labelled "<card> autopay", so it now draws only true ones. An older
  phone shows no low_cards_note.

## The notice says it pays every card

"Cash is tight — $X left until your next paycheck" and "Short $X today —
checking doesn't cover cards & bills before the next paycheck" are
checking less every card's whole balance less the bills due before the
paycheck, and never said that the cards are paid off in full now; and
their card figure was the posted balances alone while the cash card
beside them prints posted plus pending.

- **The one card-debt figure** (`forecast.card_debt_total`): every
  in-scope card's posted balance (never below $0) plus its pending
  charges — `card_balances`' `debt`, summed. The runway's `card_debt`
  (and so the headroom, `runway.card_debt` in the reply, the "after cards
  & bills" tiles and the integrations' card-debt figure) reads it; the
  forecast's `card_debt` (the cash card's "card debt") is the same sum,
  and `report.gather` takes the runway's from the forecast it already
  built (a second read only when there is no forecast).
- **The words** (`todayview.cash_sentence`): headroom — "Cash is tight —
  $640 left until your next paycheck (03/20/26) if you paid off all
  $2,310 on your cards and the bills due before then." ("… after the
  bills due before then." with no card debt); today — "Short $210 today
  if you paid off all $2,310 on your cards and the bills due before your
  next paycheck (03/20/26). Cover it from savings." (with no card debt:
  "Short $210 today — checking doesn't cover the bills due before your
  next paycheck (03/20/26). …").
- `cash_notice.card_debt` (new, optional) carries the figure for those
  two kinds, null for the others. `kind` values and thresholds are
  unchanged; every client prints `text`, so an older phone shows the new
  words; its amount moves by the pending card charges, now the same as
  the card tile.

## The Calendar counts checking money once

The Calendar (`api_today.calendar_api`) counted a bill a card pays in its
day's "out" on its own day and again inside the card payment that pays
it, and dated it by the checking rule (rolled to a business day) while
every timeline view dated it on the card's charge day.

- **The one rule** (`calendar_api`): checking's events are the usual
  walk's `checking_events` (every dated event but the bills a card pays)
  and its `card_rows`; a card-paid bill comes from the usual walk's
  `charged_bills` — the timeline's own rows, so the same date as every
  scenario's "… — on <card>" row (the charge day; each walk's
  `charged_bills` is the same list) — with `kind` "on_card" and note "on
  <card>", and is left out of the day's `came_in`, `went_out` and `total`.
- A card-paid bill owed today or late (the walk dates it tomorrow) sits
  in today's cell, as every owed bill does; on every other day it carries
  the timeline's date.
- Its `label` ("Acme Power — on Harbor card") and its `note` both say
  it is on the card, so a phone printing either field beside the day's
  in and out does not read as mis-added.
- Web and phone print the server's in/out figures and grey an "on_card"
  amount; an older phone prints the same figures and lists the row as any
  other. The closed month's calendar (`month_calendar`, what was paid) is
  unchanged.

## Own-account transfers are not in the forecast

The forecast walks bills, paychecks, card payments, spending and savings
plans; money a household moves between its own accounts by hand (into
savings, back from a brokerage) is not walked, so the page names it.

- **The figure** (`forecast_score.recent_own_transfers`): the walked
  checking account's own-account transfers — the pairs the self-grading
  reads as the household's own (`own_transfer_ids`: both legs
  transfer-typed, opposite amounts, on two of the household's accounts
  within 3 days) — over `OWN_TRANSFER_NOTE_DAYS` = 60 days to the end of
  yesterday, each direction apart (`{out, in}`). Every read stops at
  yesterday, the other account's legs included (`partner_until`), so a
  row dated today moves nothing. Kept with the day's learned facts
  (`kept_own_transfers`, `forecast.learned` "own_transfers:<checking
  id>"): read once per household per day. `report.gather` carries it as
  `own_transfers`.
- **The words** (`todayview.transfers_note`, `forecast.transfers_note` in
  the reply, `transfers_note` in the context): when the larger direction
  is at least `OWN_TRANSFER_NOTE_MIN` = $500 — one direction: "Transfers
  between your own accounts are not in this forecast unless you set one
  up as a bill — over the last 60 days they moved about $1,200 out of
  checking." ("… into checking.");
  both (each at least $10): "… over the last 60 days about $4,000 left
  checking for your other accounts and about $3,800 came back." Amounts
  to the nearest $10. None below it, for a payment to someone else (no
  leg on the household's side), and — silent, not netted — when the
  forecast walks a transfer of its own: a "(savings plan)" event or a
  month-end sweep. A bill set up to pay one of the household's own
  accounts is walked like any bill; a bill's name is no test of where its
  money goes, so the sentence carries that exception in its own words.
- Printed under the chart on the page, the phone and the HTML email, and
  in the plain text. An older phone shows nothing; no figure the forecast
  walks, records or grades moves.

## The quiet line reads cash after card balances too

The calmer usual walk can miss dips that "pay all cards now"
catches. The headline low point and the chart's default
stay "Your usual"; the quiet cash line (`todayview.cash_band`, the one
function) now also speaks when the usual walk stays at or above $0 and
cash after card balances (`pace_now`) dips below $0 inside the horizon:
`kind` "after_cards" (an older server sent "now"; no installed client
reads `kind`, they print `text`), "Your usual pattern stays above $0 (low
$3,270 on 05/12/27). On 04/21/27 your card balances would be about $385
more than your checking — you could not clear every card that day." — its
low and date are that walk's. With the heavier month's sentence after it
when that dips
too; `kind` "heavier" when only the heavier month dips ("… A heavier month
— like one 30-day stretch in five of the last year — would take it to …").
When the usual walk itself goes below $0 the cash notice is the one
warning. The page, the phone and both emails print `cash_band.text`.

## The forecast keeps what it learned for the day

The forecast learns from history: the heavier-month factor (a year of
30-day windows), each card's timing and the payday habit, the bills'
paying accounts and past payments, the usual pace, and the variable rows
of every closed month those read. On a large ledger that cost seconds per
build, and the Today page, the Bills calendar and the Cash Flow tail each
built it.

- **What is kept, and under what** (`forecast.learned`, `history_stamp`):
  the learned facts are stored per household per day (below, "What the
  forecast learned is stored for the day"), and the worker keeps its copy
  of the day's row for `LEARNED_KEPT_S` = 10 minutes under the household's
  day and a stamp of everything the facts read: the bills, the config and
  the undated ledger tables (`reporting._fixed_inputs_stamp`) and every
  ledger row dated before today. The built forecast is kept under
  `learned_stamp`, which adds every dated ledger write
  (`reporting._fixed_month_probe`, the trigger-kept write log), the
  issuers' facts (`liabilities`) and the account balances. A sync, an
  edit or a new day misses the build; the same day with no write is served
  from memory by every request.
- **What the forecast learned is stored for the day** (`forecast_learned`,
  one row per household per household-local day: the facts in the
  forecast's own encoding, `forecast.encode_learned` — every type a fact
  holds comes back as itself, dict order kept, stored as `json` so every
  number comes back as written — the `history_stamp` they were learned
  under, `LEARNED_RULE_VERSION`, the writing process). The stamp hashes only the settings
  the facts may read (`forecast.learned_config`: not the keys a send, a mute, a milestone mail
  or the outbound pause writes) and leaves the settings row's own version out, so a send stamp
  keeps the day's row. The plan's daily the pace is floored at is learned as of yesterday too
  (`forecast.plan_daily_as_of_yesterday`: yesterday's month as of yesterday — on the 1st, last
  month's plan): a dynamic budget scales the plan by what this month's income leaves, and a
  paycheck landing today would otherwise move it. The facts are a
  pure function of the ledger through yesterday and the day, so the row
  serves every worker and every restart, and a build that reads it equals
  one that learns again, byte for byte
  (`tests/test_what_the_forecast_learned_is_stored_for_the_day.py`: two
  fresh processes, one that learned and one that could only read).
  Written by the forecast's hourly step (`jobs/worker.forecast_all` →
  `forecast_tenant`: once per household-local day, the first tick after
  its clock passes 04:00 — the same moment of every household's day, in
  its own zone, with no day doubled or skipped across a daylight-saving
  change)
  and by `forecast.store_learned` after any build that had to learn
  something — the first build of a day with no row, or the first after a
  write dated before today, which moves the stamp. The stamp is re-read
  before a row is stored: a write that landed while the facts were being
  learned (from another connection) means they are not stored under the
  old stamp. A row carries the code that learned it
  (`forecast.code_identity`: the release the app reports when it names one
  exact release; otherwise — "dev", "unknown", a "-dirty" deploy, none —
  that word with a digest of the engine's source), so any deploy learns
  once again, lazily, on each household's first build after it; a row
  learned by other code, or under another `LEARNED_RULE_VERSION` (a manual
  escape hatch), is not read. A table not migrated yet is read as no row:
  the forecast is built all the same. A household keeps
  today's and yesterday's rows (`LEARNED_KEEP_DAYS`). The pace's window
  rows are its working, not a fact: they are not kept.
  **Derived data**: not exported (`web/pages.EXPORT_SKIP`, the operator
  archive's skip list), not restored (the first build after a restore
  learns again), gone with the household (an RLS table the
  erasure discovers), never a sign that an untouched account is in use
  (the reclaim machinery set).
- **Bounded** (`forecast._Kept`): every kept store (learned facts,
  closed-month rows, built forecasts) drops an entry unused for
  `KEPT_IDLE_S` = 24 hours and keeps at most `KEPT_HOUSEHOLDS` = 200
  households, the least recently used going first; a household keeps
  only the newest `CLOSED_KEPT_MONTHS` = 16 closed months (what a build
  can read).
- **A closed month's variable rows** (`forecast._closed_month_rows`) are
  kept across days under the inputs stamp and the dated-row stamps of
  every month the month's walk reads — reporting's own reach
  (`_fixed_month_reach` / `_fixed_month_stop`: the prepay window, and an
  annual envelope's whole calendar year): a new day buckets only the
  running month, and a write to any month it reads misses.
- **One build per page**: `forecast.build` keeps its result for the day
  (and at most `BUILT_TTL_S` = 10 minutes) under the same stamp; the Bills
  calendar asks for the Today page's horizon and reads its own days of it,
  and the Cash Flow tail asks for the same build. The result is read-only
  to every caller.
- **The heavier-month ratios** come from daily totals and prefix sums
  (`forecast.heavy_ratios`); `heavy_ratios_reference`, the window-by-window
  form, is the figure it is tested against.
- **No memory between builds**: a card's mode is a function of the ledger
  and the day alone — two workers, a restart and a rebuild after a write
  decide alike. The modes are stable because their signals are (the
  autopay signal reads every statement of an autopay card and none of a
  hand payer's; the cycle is paired by `card_cycle`), not because a worker
  remembers yesterday's.
- **What the forecast learned is yesterday's; what it sees today is
  today's.** Every learned fact — the 60-day pace and its one-offs, the
  heavier ratio, every bill's payments and paying account, each card's
  mode, gap, last payment, payday share and autopay evidence, the payday
  habit and its cushion — is computed from the ledger as it stood at the
  end of yesterday: its readers take the day and read no row dated today
  (the running month is bucketed as of yesterday; card credits through
  yesterday; balances as of the end of yesterday, `forecast.yesterday_balances`:
  the cards' posted balances and checking's balance as the walk starts
  from it — available, else current — less only the rows dated today, a
  post-dated row being in no bank balance yet).
  A build that reuses them after a write dated today therefore equals one
  that learns them again, by construction
  (`tests/test_what_the_forecast_learned_is_yesterdays.py`: a card payment
  today, a bill's claim moving today, any random write dated today on
  generated households). What happened today is applied live on top, in
  the part of the build that is never kept: a card payment that left today
  makes today that card's last payment (the round-gap test) and is a round
  since the payday (`landed_paydays`); a paycheck that landed today is a
  landed payday; today's charges and pending rows are in today's balances.
  Closed months too: a closed month's bills match a little past its end
  (into the next month), so the forecast reads a closed month whose
  matching reaches today's month as of yesterday as well
  (`budget.month_status(..., rows_until=today)` through
  `forecast._closed_month_rows`): a bill's late payment arriving today, or
  a row dated after today, re-files none of last month's rows until
  tomorrow. An annual envelope's earlier claims are decided over the same
  rows (`budget._envelope_year_claims(..., rows_until)`), for the running
  month as of yesterday and for a capped closed month alike: a yearly
  bill's payment dated today, taking back the occurrence that had held an
  early look-alike, hands that row to the pool tomorrow. Only a month whose matching reaches past today (the month
  after it plus the prepay window) is capped, and it is read again each
  day; older closed months stay kept. Every page's own month view keeps its live reading.
  The one consequence: a re-classification caused by a row dated today —
  a bill's real payment arriving and freeing an earlier look-alike row —
  reaches the pace tomorrow. `history_stamp` remains the invalidation for
  a write dated before today. Each named fact keeps one entry per
  household per day (the balances-keyed "timing:" and "habit:" replace
  their earlier entry).
- **History through yesterday** (`forecast.history_stamp`): the facts
  that read only the ledger before today — the heavier month, the usual
  pace, every bill's payments and the bills' paying accounts — are kept
  under a stamp of the bills, config and undated tables and of every row
  dated before today, so a sync or a charge today reuses them and only
  today's rows are read again. Card timing and the payday habit read
  balances: they are kept under the history stamp and the cards' and
  checking's balances as they stood at the end of yesterday (today's
  less today's rows) with the issuers' facts (`forecast.yesterday_balances`),
  so a sync that brings both a charge and its balance moves neither.
- **Each expensive reader once a build**: every bill's and envelope's
  payments are read in one pass (`bill_payments`, indexed by merchant and
  amount band); a card's pending charges in two indexed passes
  (`forecast.card_pending`: the holds, then the posted rows from a month
  before the oldest hold that name one — the same rule as the one
  correlated statement, `_CARD_PENDING_SQL`, which read every posted row
  per hold) that the paying accounts, the seasonal estimates and the
  heavier month cut from; the cards are read once (`card_balances`); a
  closed or idle card (nothing owed, no row in six months) gets no
  per-card reads; every month a build reads shares one measured window
  (the build's day, `forecast.as_of` in its memo).
- **The heavier month's ratios read the pace's own definition**: the
  rows are the months' buckets (the kept closed-month rows, the running
  month as of yesterday) and the one-offs `window_one_offs`' decision for
  each day's 60-day window, taken from day totals and per-merchant prefix
  sums (`forecast._WindowOneOffs`, tested equal to `window_one_offs`
  window by window, ties included) with the habit question answered from
  one read (`forecast.habit_lookup`). So each ratio is the 30 days'
  variable spending over `forecast_pace` as of that day, and "one stretch
  in five ran this high" is true against the real pace by construction.
  The cost is the closed months of the year, bucketed once per household
  per worker and kept across days.
- **Budget**: on the generated large household (tests/large_household.py:
  a twenty-year sparse tail, three busy years, 40 bills with years of
  payments, 16 card accounts most of them closed), a cold worker's build
  of a day whose row is stored takes about 0.1 s, as does the first build
  of a new day after the nightly step; with no row, about 1.5 s cold and
  0.7 s after a write dated before today; a page's second ask nothing
  (`tests/test_the_forecast_keeps_what_it_learned_for_the_day.py` and
  `tests/test_what_the_forecast_learned_is_stored_for_the_day.py` count
  the statements, the learned readers and the month buckets, and bound
  the time).

## Row order: the forecast is a function of the stored data

The forecast is a function of the stored data and the as-of day, not of
the order the database returns rows in. A query without ORDER BY may
return its rows in any order — a no-op UPDATE moves a row in the heap —
and a build that read rows in another order once gave other figures:
same-day events swapped, a float sum moved in its last digit, and two
bills that tie exactly for one charge took it by whichever was read
first.

- **Every multi-row read** on the forecast's path and in the learned
  facts' readers names a total order (the bill tables by `uid, id`; ledger
  rows by date and id; accounts by id).
- **The bill matcher's tie-break** (`budget.match_occurrences`): after the
  day gap and the amount gap, the bill set up first (`created_on`, a bill
  with no day first), then its name (case-folded), then its stable uid
  and the occurrence's day, then the row's own (its date, its id, its
  split line) — never the position either was read at. Every read of the
  bills walks them in that order (the forecast, detection's passes, the
  recurring pill on Today's recent pane, the Transactions page and its
  download), and an envelope's claim order is the same; the overdue bills
  are listed by name, then due day. On data where
  two bills tied exactly, which one takes the payment may differ from
  what a build happened to give before (that result was arbitrary); no
  total moves otherwise.
- **Every dated list** the build returns (events, bill and card events,
  card rows, charged bills, outflows) sorts by day, then label, then
  amount.
- `tests/test_the_forecast_is_not_a_function_of_row_order.py` builds every
  household twice, once with every unordered multi-row read reversed (in
  long mode also shuffled, several seeds), on generated households, on
  ones where two bills tie for one charge, two paychecks land on one day
  and two same-day bills share a merchant, and on bench households for a
  few nights, and the large household: the forecast, the Today context,
  the running month, the night's record, the Transactions page's
  recurring pills and the nightly detection's proposals are the same
  bytes. The primary checking account picked by balance breaks a tie by
  its id.

## Paydays and bills land on business days

A paycheck scheduled on a Saturday or a bank holiday was walked in that
day, though a direct deposit lands the business day before — which moves
a whole paycheck across a month end when the 1st is a weekend, and makes
"checking at month end" wrong by a paycheck. A bill's ACH debit on such a day
posts the business day after.

- **The calendar** (`engine/business_days.py`): the US banking calendar
  ACH settles on — weekends and the eleven federal holidays, written as
  data (`US_BANK_HOLIDAYS`). A holiday on a Sunday is observed on the
  Monday after; one on a Saturday is not moved, because the Federal
  Reserve Banks open the Friday before. There is no household country
  setting: every household is read on this calendar, so a household
  outside the US sees its paydays and bills moved off weekends and US
  holidays only, never off its own country's holidays.
- **Paydays** (`forecast.payday_landing`, deciding for every income
  occurrence the walk takes in): a payday on a business day stays; one on
  a weekend or holiday is walked in on the business day before, unless
  the series' own deposits say otherwise — `forecast.payday_habit` finds
  the last time in the past 400 days the series' payday fell on a
  non-business day and where the deposit nearest it (within three days)
  landed: after it, the paycheck is walked in on the business day after;
  on the day itself, on the day. A payday due today and not landed yet is
  still walked in tomorrow (the walk starts tomorrow), and a rolled payday
  is never walked in before tomorrow. The walk reads paydays up to four
  days past its horizon so one that rolls back into it is not lost.
- **Bills** (`forecast.bill_landing`): every bill occurrence the walk
  takes from checking that falls on a weekend or holiday is walked out on
  the business day after; one that then lands past the horizon leaves the
  walk. A bill a card pays is charged on its day and leaves checking with
  the card ("Card spending leaves checking when the household pays the
  card").
- **Which month a rolled paycheck belongs to**: the month plan counts a
  paycheck in the month it PAYS — its scheduled day — and that does not
  change: `budget._month_income_parts` (the month's income, Budget's
  "still due", Today's "paychecks still due" and its dates, the plan
  line) reads the scheduled days. The forecast walk, the cash chart, the
  timeline's rows and "checking at month end" (which reads the walk) put the
  paycheck on the day it lands. A check scheduled for Saturday the 1st is
  next month's income on the plan and is in this month's "left at month
  end", and the timeline lists it on Friday the 31st.
- **Never dropped on the day it is due**: a paycheck or a bill scheduled
  on the day the forecast is made — any weekday, a holiday, the day after
  one — is walked tomorrow at the earliest and no later than the next
  business day from tomorrow, even when its landing day rolled to before
  today (`tests/test_an_occurrence_on_the_as_of_day_is_never_dropped.py`).
- **Not changed**: `forecast.next_paycheck` (the hero's next-payday date,
  the runway's horizon) and the runway's bills before payday read the
  scheduled days.
- **Surfaces**: only dates inside the walk move; the page, the phone and
  both emails print the timeline as sent. No new field, so an older phone
  shows the new dates and an older server the old ones.

## "Left at month end" is the timeline's month end

"checking at month end" is the balance after the
month's expected spending — the same figure the cash timeline ends the
month on. The tile used to be checking − card debt − bills still due +
paychecks still due (`after_cards_bills_paychecks`), which leaves the rest
of the month's spending out: a green $10,000 beside a forecast heading
below zero.

- **Function**: `todayview.month_end_left(st, fc)` reads the forecast's
  usual walk (`pace_stmt`: card payments as the household makes them, the
  forecast's daily pace, the bills and paychecks still due, the envelope
  drip) on the
  month's last day. `build_context` puts it in `month_end` and adds the
  same day to the timeline as its own row (label "checking at month end", no
  amount, after that day's events), so the tile and the row are one value.
  The row is always among the rows shown before the expander.
- **The note under it** (`month_end_note`): "after this month's bills,
  paychecks, card payments and spending at your usual pace", or, without a
  paycheck series, the existing no-paycheck note.
- **Clients**: `/api/today/full` sends `month_end_left`,
  `month_end_note`, and `forecast_rows[*].kind` (`event` or
  `month_end`; the month-end row's `amount` is 0 and is printed blank).
  The web, the phone, the email HTML and the plain text print the tile
  as "checking at month end", with "paychecks still due" beside it whenever
  there is a paycheck figure.
- `after_cards_bills` and `after_cards_bills_paychecks` keep their
  meaning and are still sent for an older phone, which shows its old
  tiles. Older server: no `month_end_left`, so the phone leaves the tile
  out.
- With no forecast (a past day, no checking account, balances not set)
  there is no tile, as before.

## One bills-still-due figure; a light or heavy month says so

One still-due bills figure everywhere on Today, and beside the plan
line's bills the page, the app and the email say when this month is
lighter or heavier than the evened average (below, "The month's
weight").

- **The figure**: `report.bills_remaining_month` — the bill occurrences
  not yet paid through month end (a bill due already and not posted, the
  "awaiting" part, included) plus what is left of the envelopes. The cash
  tiles (`after_cards_bills*`) already subtracted it. It is
  `report.month_end_parts`'s total, which also splits it for printing:
  the bills part (the Bills row's still-due figure) and the envelopes
  part (the Envelopes row's "still expected this month"), apportioned once
  to the total's whole dollars, so "bills $X + envelopes $Y" under the
  "After cards & bills" tile adds up as printed (below, "Envelopes are
  their own row"). The money map's
  Bills row used to print its schedule less what posted less what awaits,
  so a bill that posted $10 under its plan still counted $10 as due and
  the map and the tile named two figures with the same words.
- **Function**: the Bills row's status is the month's breakdown's
  still-due figure (below, "The Bills row is one breakdown"): "$450 still
  due", `amount` that figure, `pending` the awaiting part (the bar's
  amber segment; no words). The bar still draws posted / awaiting / the rest. A
  past day has no still-due figure (it is a now-fact, like the cash
  tiles), so its Bills row prints its figures with no words beside them.
  The Month and Year lenses print the same breakdown for the running
  month.
- **The month's weight**: `todayview.bills_weight` compares this month's
  schedule (`scheduled_month`) with the evened average the plan line
  prints (`avg_bills`) and, when they are more than $100 apart
  (`BILLS_WEIGHT_MIN`), names the plan line's figure as the average and
  gives this month's own: "evened out; this month lighter: $940" (or
  "heavier"). It prints no difference — a third figure the reader would
  have to reconcile with the two. (Its old words, "light month, $180
  under your average", sat beside the average and read as if the average
  were this month's bills.) Within $100 it says nothing.
  `/api/today/full` sends it as `bills_weight`; the web and the phone
  print it after the plan line's bills, the email HTML and the plain text
  the same.
- **Older phone**: reads `kind: due` and prints `status.text` with the
  new `amount` — the same figure. **Older server**: no `bills_weight`, no
  clause.

## The plain text mirrors the page

`report.build` promises a plain-text mirror of the HTML email, which
mirrors the page. It printed its own table instead — "Fixed bills (info)
posted $X awaiting $0 month cash $Y" and expected-basis deltas — so it
stated a different bills figure and different row figures from the HTML
beside it.

- The plain text's "Monthly budget:" section prints
  `todayview.money_map`'s rows, the ones the HTML draws: each row's label,
  spent / budget, and its status words (Today's pace basis, the one
  still-due figure), then the row's caption, then the footer lines in the
  HTML's order — "Also this month", "Non-monthly bills this month",
  "Scheduled but not yet posted", "withdrawn by the bank feed".
- The sections come in the HTML's order: the hero (with the auto-scale
  sentence where the HTML has it), the plan line (only when the HTML has
  one), savings goals, the money map, the cash timeline, the recent rows.
- The recent rows are listed in the page's order — newest first, then
  the larger amount, then the id, the order `data.transactions_by_ids`
  returns them in. `build_context` sorts them that way for the HTML and
  the plain text alike.
- A count reads "×" everywhere; the HTML's "Also this month" and
  "Non-monthly bills this month" are two lines, as on the page.

## The verdict's reasons name merchants as the Why does

`/api/today` (`budget.month_status` → `reasons[*].detail`) grouped each
over-budget bucket's top spenders by the bank's raw descriptor, while
`/api/today/full`'s Why groups them by the merchant the ledger names
(`display_payee`). One merchant with two spellings was two smaller lines
for any integration reading the verdict. `reasons` now groups by the same
key as the Why's `_spenders`; its sentence shapes are unchanged. No screen
prints `reasons`, so no client changes.

## A day's allowance is one whole-dollar figure

The same allowance printed as "Food $37 today" in the tile and the SMS and
as "spend at most $36/day on Food" in the recovery sentence: the tile
rounded $36.70, the recovery floored it. A spend-at-most figure is a
promise the remainder must keep, so it is floored, once, on the server.

- **Function**: `budget.shown_daily` — whole dollars, floored, never
  below $0. `todayview.allowances` sends beside the cents fields
  `rate_dollars`, `today_allowance_dollars` and `left_dollars` (what is
  left of today, $0 once over). The day line ("of $36 today · …"), the
  plan-vs-now note ("daily reduced from $45 to $36"), the simple face's
  number and chips, the Why's recovery caps and the push/SMS
  ("at most $36/day Food") all print these; the tiles on the page, the
  phone, the email HTML and the plain text print `left_dollars`.
- An over-spent day still prints its overage ("over by $8") from the
  cents, as before: it is not an allowance.
- **Older phone**: prints `left_today` rounded, as before. **Older
  server**: no `left_dollars`; the phone prints `left_today` as before.

## The Bills row is one breakdown

A month's bills used to be printed from three slices of the data: the
bar's right figure was posted plus still due, the Month lens's "paid vs
planned" card and the caption named the plan, and the Year row added its
own sums. Each pair was balanced in turn and another came apart, so the
row is now built once from ONE breakdown of the month's plan. The row
says TWO figures, posted and still due, and how they
add up to the plan is a small table the row opens, where each part
carries its sign — a sentence of parts, some added and some taken off,
cannot show which way each one goes.

- **Due-dated bills only**: the Bills row counts
  the bills that have a due date — the month's occurrences — and nothing
  of an envelope. Its plan is `fixed.occ_budget` and its posted
  `fixed.occ_actual` (the engine's figures before the envelope block);
  monthly and yearly envelopes are their own row, directly under Bills
  (below, "Envelopes are their own row"). `fixed.month_budget` and
  `fixed.actual`, which still hold both, are what the plan, the verdict's
  scale and `total_actual` read, and do not move.
- **The one function**: `bills_breakdown` (server/oikonome/web/
  bills_breakdown.py). `running(conn, cfg, st, today)` reads the running
  month's walk ONCE, through `report.month_end_parts` (the reads
  `report.bills_remaining_month` sums), and prints the bills part of it
  as its still-due figure, so the Bills row, the Bills page tile and the
  cash tile's "bills $X" are one figure; `closed(st)` reads a month that is over;
  `future(st)` a month to come; `year(months)` folds the months of a
  Year lens. Nothing else computes a bills part; `gather`, the Month
  lens, the Year lens and the Bills page each take the breakdown from
  here, and a failure leaves the row with no parts rather than
  mismatched ones.
- **The parts** (whole dollars):
  - `plan`: the month's bill plan (`fixed.occ_budget`);
  - `posted`: what posted against it (`fixed.occ_actual`);
  - `due`: this month's own unpaid occurrences through month end, with
    `awaiting` (due already, not posted) inside it;
  - `late`: each unpaid occurrence of an EARLIER month the walk carries
    (it re-dates a late bill to tomorrow) — payee, amount, and the month
    AND year it was due. It is still due, and in no plan of this month;
  - `not_paid`: a closed month's occurrences no payment met (the items
    in `unpaid`); a running month has none;
  - `under`: what is left of the plan, signed — `plan − posted − due −
    not_paid`; the table's "Under plan", or below zero "Over
    plan" with its minus.
  - `refunds`: a month whose bill refunds came to more than its charges
    never prints a posted figure below nothing: posted is $0 and the
    difference is its own row, "Refunds beyond charges −$63", taken off
    the plan's sum, so the plan's remainder is still under plan and
    never over plan. The Year nets its months' refunds against the
    others' posted and lists them only when the year as a whole refunded
    more than it was charged.
  - `still_due` = `due` + the late amounts — on a running month exactly
    `month_end_parts`'s bills dollars (the late bills take their own
    pass, `due` the rest) — and `total`, the bar's right figure: posted
    + still due on a running month, otherwise the plan.
- **The sum holds as printed**: `posted − refunds + due + not_paid +
  under = plan` in the whole dollars every surface prints. Posted, due,
  each unpaid bill and the remainder are apportioned
  in ONE largest-remainder pass to the plan's whole dollars, where a
  part exact in cents prints exactly and nothing stays nothing — a
  $640.00 payment beside unpaid bills in cents prints $640, the sum of
  the paid list, and the bills not paid carry the rounding (the card's
  "not paid: NAME $X" lines print the pass's dollars). The late bills
  take one pass of their own. So posted $64.40 with $46.40 due and a
  $139 plan prints "$65 / $111", "$46 still due", "posted $65 · plan
  $139", and a table of +$65, +$46, +$28 under plan, $139 — the left
  figure, the caption, the words, the table, the Bills page's tile and
  the Year row all print those integers.
- **Two figures on the row** (`bills_breakdown.words`, `caption`,
  `row_status`): the words beside the row are its one still-due figure,
  "$1,610 still due" (`still_due`: this month's own bills and any bill
  late from an earlier month), sent as `status.text` and the breakdown's
  `text`. A row with nothing still due still says its outcome, since an
  older phone and the emails have no table. The phrases, in this order,
  two at most: "$D still due"; on a settled month or year (`settled`: a
  closed month, or a year that is finished) "$R refunded" when its bill
  refunds came to more than its charges, then "$N not paid"; "over plan
  by $X" whenever `under` is below zero, else, settled, "$U under
  plan". `status.kind` is `due` (amount the still-due figure, `pending`
  its awaiting part), else `over` (tone neg) only when `under` is below
  zero, else `left` (tone mut). No envelope reaches any of it: only
  envelope activity changing leaves every Bills figure and word as it
  was. The caption names posted: "posted $P · $A awaiting · plan $Z"
  on a running month, whose right figure is posted + still due (the
  awaiting part, due already and not posted, only when there is one —
  the figure stays visible outside the table, the word the Month card
  uses); "posted $P" on a closed or future month, whose right figure is
  the plan; "posted this year $P" on the Year, a different figure from
  Today's posted under a different label, and "posted in 2025 $P" on a
  past year, which never says "this year". No sentence of parts and no
  fold: the parts are the table's.
- **The table** (`bills_breakdown.table`, the ONE builder; sent as the
  breakdown's `table`): `rows` in order, each `label`, `amount` (whole
  dollars, never below nothing), `sign` ("+" added, "-" taken off) and
  `figure` as printed ("+$1,475", "−$15"), ending in the total row (sign
  "="), the plan as printed — the signed rows add up to it to the
  dollar. Then `outside_heading` and `outside`: each bill late from an
  earlier month, "Late from Dec 2025: <payee>", still due and in none of
  the plan, so in no sum. Labels, in plain words: "Paid so far" ("Paid"
  on a closed month, "Paid this year" on the Year), "Refunds beyond
  charges", "Still due" — or, once a late bill is part of the row's
  still-due figure, "Due this month, not paid yet", so "still due" names
  one figure on a page — "Not paid: <payee>" on a closed month ("Not
  paid in earlier months" on the Year, "Not paid" on a past one), "Under plan" / "Over plan", and the total "This month's plan" ("The
  month's plan"; on the Year "Plan for the year's budgeted months").
  A past year's posted row is "Paid in 2025". A row that rounds to $0 is left out (posted, the
  first, always shows). One label is one row: a bill missed or late on
  several dates of a month is one row, its dates' dollars added; a bill
  late on two dates of one month of which the Year planned one names
  each date ("Late from Jun 23, 2026: <payee>"), so no label is both
  above the total and below it, and two different bills of one payee
  late on one date carry their amounts. Late months always carry their
  year.
- **Two figures alike, two apart**: the still-due figure on the Year is
  the running month's, so it reads exactly as Today's; the Bills page's
  tile prints it under the row's label, "Still due", with a note naming
  its parts ("$1,475 of bills due this month + $135 late from Jun 2026")
  when a late bill is in it.
- **The Year's Bills row** is `year(months)`: the sum of its budgeted
  months' breakdowns, the bar posted / plan in those sums.
  - A late bill the running month carries is looked for only in the
    month it was due in (its home: an unpaid occurrence belongs to its
    due month); a month the year holds no plan for (no budget snapshot)
    has nothing to give, so that bill is outside the plan, never taken
    for the same bill's date in another month. It is found by its KEY —
    the bill's stable id, `bills.uid`, drawn once when the bill is made
    and kept by a rename, a re-save and a restore (migration 234), and
    its due date — among that month's occurrence rows, never by a
    nearest date (see "The Year is its months, occurrence by
    occurrence"). It moves from that month's "not paid" (or, for a bill
    paid on its other dates, from its plan's remainder) to "late from
    Mar 2026": counted once. The month's frozen schedule and its status
    carry each bill's uid (`overdue_bill_uids`). Only a month frozen
    before bills had one is keyed by the id it recorded
    (`budget.bill_identity`, the payee's slug, which a rename rewrites;
    on a schedule frozen before ids were kept, the id its payee was
    created under) — the one place that fallback is read is
    `bills_breakdown.same_bill` — or, a payee spelled another way since,
    by the date alone. Two entries that both carry a uid and differ are
    two bills, whatever their dates, so a bill archived unpaid and a new
    bill on its date are the first one not paid and the second late.
  - A bill re-dated INSIDE its closed month (a yearly bill's day edited
    after the month closed) carries its new date late; the closed
    month's row for the same bill whose date the bill's current schedule
    no longer has is that occurrence, re-dated, and the two are counted
    once — paired in date order, never by nearness.
  - Only a late bill NO budgeted month of the year held (December into
    January, or a month with no budget snapshot) is said apart: "$58
    late from Dec 2025" listed below the Year's plan under "Also still
    due, from outside this year's plan".
  - A closed month's unpaid bill whose due date has moved INTO the
    running month since (the same bill due again within its match
    window — a weekly schedule re-anchored by a day, a date edited
    forward) is planned there and owed here once: it leaves that
    month's not paid as the row "Planned in Apr 2026, now due this month
    +$140". It is said moved only between two entries carrying the
    bill's uid (a payee deleted and made again is another bill, and a
    month frozen before uids names bills by payee), and only when the
    bill's CURRENT schedule no longer has the closed month's date: a twice-monthly bill missed on the 27th and
    due again on the 2nd has both dates, so the miss stays not paid (or,
    paid on its other date, in that month's remainder).
  - So `posted + due + late inside + not_paid + moved + under = plan`
    on the Year too. The Year's plan is the sum of its months' due-dated
    plans; no pool share is in it, so no rounding of a share can be
    read as over or under plan.
- **A stored month this reader cannot parse** (a malformed date in a
  cached status) has a Bills row with its figures and no words, never
  words from another slice; the Year then has no parts.
- **What each surface prints**, all from the same breakdown:
  - Today's Bills row (summary and Detail), which the Budget page draws,
    the Month lens's months and the Year's row: posted / total, the
    still-due words beside it, the caption, and "How this adds up",
    which opens the table in place (web: `BillsTable.tsx`, folded like a
    chart's "Show numbers"; phone: `bills-table.tsx`, its twin).
  - The Month lens's "paid vs planned" card: posted and planned are the
    breakdown's dollars, "awaiting" its awaiting part, and its lines
    (`bills.notes`, `card_lines`) a closed month's bills not paid, by
    name; nothing else.
  - The Bills page's "Still due" tile: `still_due` (above).
  - The emails: the row's words, no table — the daily email (HTML and
    plain text) prints Today's rows; the monthly email's Bills bar is
    posted against the month's plan (`plan`, never posted + still due)
    with the row's words and, as the Month card lists them, each bill a
    closed month did not pay ("not paid: <payee> $X"); the yearly one
    the Year row's, against the year's plan.
  - The web and phone maps print the Bills row's caption and words and
    the table's labels and figures as sent; nothing on a device
    composes or adds a part.
- **A past day** on Today has no still-due figure (it is a now-fact like
  the cash tiles) and no breakdown: its Bills row prints posted / plan
  with no words and no caption — never one written from another reading
  of the figures. A lens whose breakdown could not be read prints the
  same.
- **The planned excess** (the Excess cash row's caption and the Budget
  summary's words, `todayview.excess_words` / `plan_excess_words`) says
  "none this month" only once the month's overrun of its variable budget
  is at least the excess (`todayview.excess_used_up`); a smaller
  overrun leaves the words as the plan's figure, which they never
  claimed was this month's, and a plan with no positive excess has
  nothing to use up, so nothing is said of it.
- **Older phone**: prints the row's figures and `status.text` as sent —
  "$1,610 still due" or a settled month's "$35 not paid · $12 under
  plan" stands alone (`kind` due / over / left as above), it keeps
  dropping the Bills
  caption, and it never asks for the table. **Older server**: no
  `breakdown` (so a current phone leaves the Bills caption out as
  before) and no `table`: the "How this adds up" button is not drawn —
  nothing stands in for it. **Stale tab**: the figures, the words and
  the table arrive in the one reply that prints them. **Restore**:
  derived on the next read. No reply key was removed.

## The Year is its months, occurrence by occurrence

A bill occurrence that straddles a month line — paid on the 2nd of the
month after, paid early in the month before, one date of a twice-monthly
bill paid late — is read by more than one month: the month it was due
in, the month its payment is dated in, and, while it is unpaid, the
running month's walk. The Year never decides it again.

- **One owner.** `budget.occurrence_home` is the ONE function that owns
  an occurrence's month: the month its payment is dated in when it was
  paid, its due month when it was not. That month alone plans it; every
  other month's pass reads it only as a decoy. Its state is decided by
  `budget.month_occurrence_pass` in that month — paid (by the payment
  the matcher bound to it), still due, not paid — and, for an unpaid one
  of a closed month, by the running month's walk, which carries it late.
- **Rows, not matching.** Each month's Bills breakdown carries the
  occurrences another month may read as rows (server-only, outside the
  reply): `not_paid`, `covered` (a bill paid on another of the month's
  dates) and `late`, each with its key — the bill's uid and its due
  date — and homed in its due month. `bills_breakdown.fold` places each
  late row on its home row by key; the later reading wins, so a late
  occurrence the year planned is still due and not "not paid", counted
  once. The one cross-key rules are a date changed after its month
  closed: inside the month (the re-dated row above) and into the running
  month (`moved`, unchanged). There is no nearest-date choice and no
  payee match.
- **What holds** (`tests/test_the_year_is_its_months_occurrence_by_occurrence.py`,
  over straddling and generated households read on several days of the
  running month): every occurrence a budgeted month plans has exactly
  one owning month, and no month hands one to a budgeted month that does
  not plan it; a payment pays at most one occurrence across the year;
  each Year cell is its month's own page, field by field; the Year's
  Bills plan and posted are the sums of its months', its parts add up to
  its plan, its still-due figure is the running month's, and it calls
  nothing late or not paid that a month counted paid; its saved figure
  is the sum of the months' saved. `tests/test_the_year_by_occurrence_moves_no_right_figure.py`
  compares every Year figure with the code before the fold, household by
  household: none moved.
- **A closed month's word stands for its month.** A closed month's
  status names every occurrence its payments paid (`paid_occurrences`);
  its breakdown carries them as `paid` rows. The running month's late
  bills come from the walk (`budget.upcoming_bill_occurrences`), which
  reads today's schedule: a bill whose merchant, amount or date changed
  after its month closed can miss the payment that month bound. The
  Year leaves out a late bill whose occurrence a closed month paid (by
  uid, else the bill's id or payee, and the date), so its still due is
  the running month's less those (`paid_late`). Today and the Month
  page still print the walk's figure; one year-wide occurrence pass
  with the months as its slices would retire the difference, and would
  change Today's running-month figure, so it is not done here.
- **On a shared date** (a month frozen before uids) a late bill takes
  its own bill's row first (`same_bill`), whatever that row's state; a
  not-paid row of another bill on the same date stays that bill's.

## Envelopes are their own row

An envelope — a pool with no due date, monthly
(a weekly cost kept as a monthly pool) or yearly (upkeep kept as a
yearly pool) — is not a bill. Folded into the Bills row, a yearly pool
was counted as pool/12 in a monthly bills figure, and every reading of
that fold (a month over plan because the pool was paid that month, a
Year over plan by a dollar of rounding, an overrun counted twice) was
wrong in a new way. So the map has a row **"Envelopes"**, directly under
Bills, for every pool, monthly and yearly; the Bills row counts the
due-dated bills only.

- **The one function**: `envelope_breakdown` (server/oikonome/web/
  envelope_breakdown.py), the twin of `bills_breakdown`. `month(envs,
  kind=…)` reads one month's pools (`fixed.envelopes` of its status);
  `year(months, latest, past=…)` reads a Year lens's months. Nothing
  else computes an envelope figure for a page.
- **One item per pool**, matched across months by the pool's stable id
  (`uid`, carried on each envelope of the status; a month frozen before
  bills had one by its payee's slug, `bills_breakdown.same_bill`). Each
  item, in cents, rounded once:
  - `drawn`: what the month booked to the pool (`used`, which the
    matcher caps at what the pool has left); on the Year the budgeted
    months' sum;
  - `left`: a yearly pool's `period_left` at the month's end (now, on a
    running month), a monthly pool's `pool − used`; on the Year a
    yearly pool's left in the newest budgeted month (nothing once the
    pool is gone), a monthly pool's left in the running month;
  - `cap`: `drawn + left`, the bar's right figure — a monthly pool's
    Year cap is its months' pools summed; printed as
    `whole_dollars(drawn + left)` with `left` printed as `cap − drawn`,
    so `drawn ≤ cap` as printed;
  - `overflow`: what the pool's own period spent past the pool — the
    matcher already counts it in Food or Everything else, and the row's
    figures never include it;
  - `unspent`: on a settled month a monthly pool's `pool − used`, and on
    a settled Year (a year that is over, or every budgeted month closed)
    or a closed December every pool's `cap − drawn`;
  - `outside` (the Year's table only, a yearly pool): `pool − cap`,
    signed — the pool used before the plan's months or before the pool
    was set up, or changed during the year.
- **The row**: `label` "Envelopes", unjudged; `actual` the items'
  printed `drawn` summed, `budget` their printed `cap` summed — the row
  is its list, as printed — so `actual ≤ budget` always and no client's
  bar draws it past its divider or red. Its `to` opens the ledger of
  the rows a pool claimed (bucket `envelopes`). Caption: the gauge's
  one other figure, what the pools are still expected to pay out to
  month end, "$R still expected this month · not counted in the
  verdict" (R the envelopes part of `month_end_parts`); with nothing
  still expected, no caption. The plan's set-aside, $S, is the pool list's
  total line, "Set aside in the plan: $S" — the pools' monthly shares
  in the plan: on a month the plan in the cents a reply sends it in,
  `whole_dollars(round(fixed.month_budget, 2)) −
  whole_dollars(fixed.occ_budget)`, so the Bills plan and the set-aside
  add up to the plan they came from as printed; on the Year the shares
  summed in cents and rounded once.
- **Words** (`words`, two phrases at most, in this order): "$X past the
  pool this month, counted in spending" (the row's period said) when a pool's own period overspent it
  (tone neg, `status.kind` over, amount X — the ONLY way the row is
  ever negative); on a settled month or Year "$U unspent" (tone mut,
  kind left) — on a closed month that is not December "$U of monthly
  pools unspent" when a yearly pool is on the row (a yearly pool's
  remainder is still the pool's until its year is over). On a row of
  several pools a phrase that is one pool's names it ("Trips: $X past
  the pool, counted in spending"); one that is several pools' counts
  them ("2 pools past their amounts, $X counted in spending"). "Set
  aside" names only the plan's share. Nothing else: a pool is never
  "under plan", "over plan" or "ahead".
- **A month that draws heavily from a pool**: a $960 yearly garden pool
  untouched until May, $410 spent on May 9: May reads "$410 / $960",
  green, no words, the list's total "Set aside in the plan: $80"; June
  reads
  "$0 / $550". A pool never touched reads "$0 / $960" in every month.
- **The Year reads the pool itself**: its drawn is the months' `used`
  summed in cents and rounded once, its left the newest month's, never a
  sum of rounded monthly shares — so a pool spent in full reads
  "$960 / $960" with no "over". A plan begun mid-year reads drawn over
  its own months; what the pool spent before them is the table's
  "outside" line, never words.
- **A pool set up mid-year** counts the year's spending at its merchant
  from the month it was set up (`bills.created_on`, migration 235): a
  yearly pool's prior use is the year's claims dated in or after its
  month of creation (`budget.pool_counts_from`). On a day before its
  creation day the pool does not exist yet (`budget.pool_exists_on`):
  it is in no month status read on that day, so its merchant's charges
  are ordinary spending and its share is in no plan; the budget
  suggestion's months before its creation month read it the same way.
  A pool with no `created_on` (every pool made before the migration)
  exists on every day and counts from January 1, as before. What moves
  for a pool made after the upgrade, by design: spending at its
  merchant before its creation month no longer counts against it, and
  before its creation day it is not in the plan. Nothing moves for a
  pool made before it.
- **Two pools that claim one charge**: the pools are tried in one
  order on every read (`budget._envelope_bills_load`): the pools with
  no `created_on` (made before the day was kept) first, among
  themselves by stable id; then by `created_on`; then by stable id. So
  which pool claims a shared merchant's charge no longer depends on the
  order the database happens to return the rows in. A charge is claimed
  by one pool only and what does not fit is spending, so for two
  UNEQUAL pools sharing a merchant the pre-upgrade split followed the
  physical row order and was arbitrary: after the upgrade the split
  between the pools, and with it the spill counted in variable spending,
  may differ by the difference between the pools. The plan,
  `scheduled_month`, `total_actual` and the excess never move.
- **Resized or deleted**: a closed month keeps its frozen pool; the
  running month reads the live one. A pool cut below what it drew reads
  `cap = drawn` (nothing left). A deleted pool has no newest month, so
  nothing is left; its months still list it on the Year, "(no longer
  tracked)".
- **What the Year's pool does not draw** (`outside`, a yearly pool, the
  pool less drawn less left) is said by cause, each only when it is not
  $0, each a signed line of the pool's list (`envelope_breakdown
  ._causes`): "spent before the plan's months" (the pool's draws before
  the Year's first budgeted month), "spent before the pool was set up"
  (draws before the first month the pool is in the plan, when that is
  later), "spent in months outside the plan" (draws in a month between
  two budgeted ones), "pool resized during the year" (a pool's size
  changed between two of its months, signed) and "left unspent in a
  pool that was removed" (what a deleted pool still held). The causes
  add up to `outside` as printed.
- **The table** (`table`, sent as the row's `breakdown.table`, opened
  like the Bills table): one line per pool, starting with the pool's
  own pair — "Garden (yearly pool $960): $410 of $960 this year · $0
  this month · $550 left" — then on the Year the pools' cause lines,
  then "Set aside in the plan: $S".
- **A pool named on a month surface prints only what that month's own
  status holds** (`envelope_breakdown.pool_line`; Today's pinned card,
  the daily email's pinned line, the Bills page, the Month card's pool
  list, the Envelopes row's pool list, the monthly email's pinned bar).
  No earlier month is read for it:
  - a yearly pool: the pair a yearly pool has always printed
    (`budget.envelope_shown`: the year's use of the pool, unchanged in
    meaning, printed as it always was when above the pool), its period
    in words — "used $961 of $1,000 this year";
  - a monthly pool: "$40 of $85 this month";
  - and, only when THIS month sent part of the pool's charges to
    variable spending, "· $15 past the pool this month, counted in
    spending" — the month's own overflow for that pool, exactly what the
    variable buckets counted;
  - a closed month (or one to come) names itself in both phrases: "used
    $824 of $1,000 by the end of June", "$40 of $85 in June", "$15 past
    the pool in June" — never "this year" or "this month".
  No year-to-date spill figure and no "the pool was …" words on a month
  surface. The fields: `pair_spent` / `pair_of` (the pair), `pair_past`
  (this month's overflow), `pair_text` (the line); a current client
  prints `pair_text`. `shown_used` / `shown_of` keep their earlier
  meaning for an installed phone.
- **The Year's pool line** is built from the Year's own months (which
  it reads once, shared): a yearly pool "$1,050 spent in 2026 · pool
  $300 · $450 past the pool in 2026, counted in spending" — S its months'
  charges at the pool's merchants, P the pool as it stands, O the
  months' own overflow summed (`envelope_breakdown.pool_figures`, never
  S − P); a monthly pool "$330 spent over 6 months · pools $300 through
  May + $60 this month · $60 of it past a month's pool, counted in
  spending". Pools are matched across the Year's months by stable id
  ONLY: a pool deleted and made again is a different pool with a line
  of its own. A month frozen before pools had ids matches by payee slug
  only when exactly one pool of that slug is in that month and in the
  newest month. A resize is the Year list's own signed line ("pool
  resized during the year"); no "was until" sentence.
- **Every "past the pool" names its period**: on a month surface that
  month ("this month", "in June"), on the Year that year ("in 2026"). A
  row's figure is the sum of its pools' figures for the row's period.
  Property (tests): every printed past figure is that pool's overflow
  rows in the variable buckets over the period the phrase names.
- **Cost**: a month surface reads no month state beyond its own — the
  Today reply, the Bills page and a Month lens make the same number of
  month-state reads with yearly pools as without (tests).
- **Lists print in one order**: pools and unpaid bills, wherever listed
  (the daily "Pinned:" line, the monthly email's "without …", the "not
  paid:" notes, the pool list), by display name, then stable id — two
  builds of the same data print the same text.
- **The emails tell "no Envelopes row" from "an older reply"** by the
  reply's own fields, never by the row's absence: a current server's
  Month card pools and the Year's pools carry `pair_text`, so a pinned
  pool with nothing to say this period still prints its own bar. The
  Bills bar is bills only and never names a pool.
- **Nothing moves but the printed split** (tested against the code
  before the Envelopes row, for generated households): the plan
  total, `plan_line` and `plan_legs`, `scheduled_month` and the scale,
  the variable buckets (overflow included), the verdict, `variance`,
  `excess`, `total_actual`, `bills_remaining_month`, every cash tile and
  the cash timeline (`forecast.build`) are the same to the cent for a
  household whose pools carry no `created_on`, or whose `created_on`
  is before any day the ledger is read on. `fixed.month_budget` = Bills
  plan + the shares; `fixed.actual` = `occ_actual` + the pools'
  `used`. A pool dated inside the
  ledger's span is absent from every day before its creation day — the
  months the cash timeline's history and the budget suggestion read
  included — by design (above). The one allowed difference: two pools that
  claim one merchant's charges (above) may split them differently.
- **The plan line keeps one leg** for both, worded "bills & envelopes"
  when the plan has a pool (`plan_bills_label`, sent beside
  `plan_line`/`plan_legs`; "bills" without one). The figure is the
  evened monthly load it always was.
- **The month-end line** "bills $X + envelopes $Y" comes from
  `month_end_parts` (the Today reply's `month_end_parts.text`, null
  unless both halves are above $0) and is printed beneath the "left at
  month end" figure on Today (web and phone) and in the daily email —
  what the month still owes through its end, of which the bills and the
  pools are the two parts. The Bills page's still-due note names the
  envelopes half beside the bills half ("envelopes $Y still expected
  this month").
- **A period with no dated bill has no Bills row**: when the month (or
  the Year) has no due-dated bill planned, posted, still due, late or
  not paid, the map sends no Bills row, and the monthly and yearly
  emails draw no Bills bar; an Envelopes row or bar with nothing in it
  ($0 drawn of $0, no words) is not sent either. An older phone prints
  the rows it gets.
- **A bill created after a frozen month closed** is still matched to
  that month's entries by its uid, or (a month frozen before uids) by
  its old id and due date, as before: `created_on` is not read by the
  Bills breakdown's matching, so the two known gaps of months frozen
  before uids (a later bill on the same due date, a payee deleted and
  made again) stand as described in the upgrade notes.
- **The Month card** ("paid vs planned"): `posted`, `posted_counts`,
  `planned` and `awaiting` keep their earlier meaning — the bills and the
  pools together — so an installed phone's card adds up for a household
  with only pools, only bills or both. A current client prints the
  due-dated figures (`bills_posted`, `bills_posted_counts`,
  `bills_planned`, `bills_awaiting`, the Bills row's own) and
  "Envelopes set aside $S" (`envelopes_set_aside`) with the plan they
  make together (`set_aside`, "… planned in all" on web and phone); its
  pool list prints each pool's own pair (above), and sends the row's
  gauge figures apart (`drawn`, `cap`).
- **The emails**: the daily email prints Today's rows (the Envelopes row
  included); the monthly and yearly emails draw a Bills bar (due-dated
  bills against their plan) and an "Envelopes" bar (drawn of cap, the
  row's words); a pool pinned to Today keeps its own bar and leaves the
  Envelopes bar, and that bar is the pool's own pair — the year so far
  of the pool for a yearly one (on the yearly email too, never the
  months' shares summed), the month of its size for a monthly one —
  red only past the pool ("past the pool, counted in spending", the
  one wording everywhere). A month's bills not paid are said once, by
  name ("not paid: NAME $X").
- **Older phone**: prints the new row as sent — a plain unjudged row,
  its caption (only the Bills caption is dropped) and its words, never
  red — and the Bills row's smaller figures with their own words. It
  has no table. **Older server**: sends the combined Bills row and no
  Envelopes row, which a current phone prints as sent; no `breakdown`
  means no table, no `set_aside` means the card prints its "Planned"
  alone (the one older-server stand-in), no `month_end_parts` means no
  line under the cash tile, no `plan_bills_label` means "bills".
  **Stale tab**: as the older phone. **Restore**: nothing stored is read
  in a new shape; `created_on` is restored as exported, and an archive
  from before it restores with none.

## The recovery sentence counts the days left

The Why's way back (`budget.month_status` → `why.recovery`) read "To get
back on track over the last 14 days: …", which reads as the fortnight just
past. It means the days still to come and now says so: "To get back on
track over the 14 days left: …". The page, the phone and both emails
print the sentence as sent.

## Smaller things Today said differently on one surface

- **A paid non-monthly bill** is listed at what it cost and marked paid
  ("Northwind Insurance $230 (annual, paid)"), as the cash timeline's "paid
  ✓" row has it, not at its plan as if still owed. `budget.month_status`
  → `irregular_bills[*]`: `amount` is the payment (with the companions
  that rode with it) once paid, `label` gains ", paid", and `paid` says
  so. An older phone prints the new amount and label as sent.
- **The line under each day's figure** ("of $40 today · nothing spent
  yet", "of $40 today · spent $12", "over today by $8 · spent $48 of $40")
  is written once (`todayview.allowances` → `day_line`). The web shows it
  at every width, the app shows it, the email HTML prints it; the daily note and the
  recovery line follow, as on desktop.
- **The alert line** wraps to two lines on a phone (web at ≤760 px and the
  app), so the amount and the ask are not cut off the end.
- **The cash chart's captions** make room for each other on a phone: an
  autopay caption prints only where it clears the one before it (the line
  stays; the timeline names every autopay), the "runs out" caption sits
  under the legend, and "$0 · out of money" sits at the left, where the
  walk starts from today's balance, instead of on the line where it
  crosses zero.

## The widget's glance reads only the hero

`/api/today/glance` (the home-screen widget's few figures) ran the whole
Today read — `report.gather` (the month status, the 60-day forecast walk
with a second month status inside it, the runway, goal progress, the
anomaly watch, the alert log write) and `build_context` — for a reply of
a verdict, one number, the chips, the day bar and the pace phrase.

- `todayview.hero_face(st)` builds those from a month status alone: the
  days left, each bucket's day (`allowances`), the simple face, the day
  bar, the pace phrase. `build_context` takes the page's and the mail's
  hero from it, and the glance calls `budget.month_status` and
  `hero_face` and nothing else, so the widget and the page still print
  the same figures to the dollar.
- Profiled in-process on an invented household (900 transactions over
  seven months, 13 bills): the glance's work fell from 30–38 ms to 7–9 ms.
  It also no longer writes the alert log; the Today read and the daily
  email still do.
- The reply's shape is unchanged.
