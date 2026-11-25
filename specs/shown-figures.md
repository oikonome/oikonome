# Shown figures: the shares, changes and splits the server decides

`specs/counted-figures.md` says a client displays and does not compute: no
sum, difference, percent, pace or state threshold over server figures. The
Net Worth rings and the Accounts sections follow it (one function decides
the whole dollars, `reporting.shown_dollars`, and one the shares,
`reporting.shown_shares`; both clients print the `shown` fields). This note
lists the places a client worked a figure out, names the server function
that decides each one, and says what web and phone read from it.

## The rules every unit follows

- **One deciding function.** Each figure below names it. A second caller
  calls it; nothing re-derives it.
- **The reply carries what is printed.** A share is a whole percent, a
  change carries its amount and its percent, a split carries its parts in
  the whole dollars they print. The client formats and draws (bar widths,
  ring arcs and which rows fit on screen stay presentation).
- **Additive only.** Every new figure is a new field beside the old ones,
  which keep their meaning (`make api-types-compat`: a field may be added,
  never removed or narrowed). Several of these figures live in **tuples**
  today (`categories: [category, spent, previous]`, the ranked payee rows);
  the compat guard fails a tuple whose length changes, so new figures go in
  a sibling list of objects in the same order (`category_shares`,
  `category_changes`), never in a longer tuple.
- **Older phone** (an installed build against a new server): reads the old
  fields and works its figures out as it does today. Nothing it reads
  changes.
- **Older server** (a current phone against a self-hosted server that has
  not upgraded): the new field is absent, so the phone prints the figure it
  does have and leaves the derived one out (no share, no change, no "this
  month" profit). It never falls back to working it out; a fallback is a
  second definition. The web is always served by its own server and has no
  such branch.
- **Stale tab**: every figure here is part of the reply its rows come in,
  so a share can never label a row from a different fetch. Nothing derived
  is cached on the client.
- **Restore** carries the inputs (transactions, settings, goals, the
  assistant's stored answers), never a derived figure. Where a stored thing
  predates a field (an assistant answer saved before rings carried shares),
  the server derives it on read.
- **Rounding** is the server's: whole dollars by `reporting.whole_dollars`
  (halves to even, what the clients' `money()` prints), shares by
  `reporting.shown_shares` (largest remainder to 100), dollar splits by
  `reporting.shown_dollars` (largest remainder to the shown total). The
  server's rounding is symmetric; a client's `Math.round` would send
  halves up (+2.5% reads +3%, −2.5% reads −2%).

## The inventory

Each entry: what was computed on the client, where, the formula it used
("Before"), whether web and phone agreed, and what a person sees
differently now the server decides it. Places are named by file and function; all amounts in examples are
invented.

### Assistant answer ring

- **Where**: an assistant answer drawn as a ring ("pie"), on the web
  (`AssistantBlocks.tsx`, `PieChart`: its `total` and the legend's share)
  and the phone (`answer-blocks.tsx`, `Ring`, with `ringLayout` in
  `lib/answer-pure.ts`); also the saved/shared answer page
  (`assistant_blocks.render_block` → `charts.donut`).
- **Before**: the centre is the sum of the positive values; each legend row
  is `Math.round(100 × value / that sum)`%, and its dollars are rounded on
  their own. The report page rounds with Python's `:.0f`.
- **Web and phone** agree: the same formula. The report page rounds
  halves differently, so it can disagree with both by a point.
- **Server**: `assistant_blocks.ring_figures(labels, values)` returns
  `{total, rows: [{label, dollars, share}]}` with `shown_dollars` and
  `shown_shares` over the drawn (positive) values. `validate` attaches it
  to every pie block as `shown`, recomputing it even when a client sent one
  back (the report door re-validates what it is sent). The history read in
  `assistant.py` passes each stored pie block through the same function, so
  an answer saved before this change carries `shown` too. `charts.donut`
  takes the figures instead of computing its own.
- **Clients read**: `shown.total` in the centre, `shown.rows[i].dollars`
  and `.share` in the legend. With no `shown` (older server) the legend
  prints the rows' own dollars and no share.
- **Visible difference**: shares now add to 100, and the legend's dollars
  add to the centre. Three equal $500 parts read 34% / 33% / 33%, not
  33% / 33% / 33% (Choices made, 1).

### Spending tab: shares, per month, changes, what changed

- **Where**: Cash Flow → Spending, web `Spending.tsx` and phone
  `app/spending.tsx`.
- **Share of the window** (web `share()`, used by "biggest" and each
  "Where it went" row; the phone's header and `BarRow` `right` text):
  `Math.round(100 × category / window total)`. Agree: yes.
- **Per month** (header "/mo"): `total / max(months, 1)` on both clients;
  the web's per-category "/mo" column is the same division (hidden at phone
  width on the web, absent on the phone, which agree). The Income tab
  already gets its `per_month` from the server.
- **Change vs the previous window** (`Delta` on both clients): amount
  `current − previous`, percent `Math.round(100 × change / previous)`,
  hidden past ±1000%, "unchanged" under half a cent. Used on the header
  and every category and merchant row. Agree: yes.
- **What changed** (the movers, `moved()` on both clients): every category
  plus the merchants paid only before, change `current − previous`, kept
  when `|change| ≥ max($25, 0.5% of the window total)`, the four largest
  categories and three largest dropped merchants by size of change, each
  with its percent. Agree: yes. The threshold is a state rule over server
  figures and moves with the rest.
- **Server**: `reporting.spending_window` adds `per_month` (as
  `income_window` does), `per_month_by_category`, `category_shares`
  (`shown_shares` over every category spent on in the window, in the order
  of `categories`; a category spent on only before has share null),
  `change` for the header, `category_changes` and `merchant_changes` in
  row order, and `movers` (the rows "What changed" prints, each `{kind,
  label, current, previous, change}`). Every change comes from one new
  function, `reporting.change(current, previous)` →
  `{amount, pct, unchanged}`, which the Income tab and the Year lens use
  too.
- **Visible difference**: shares add to 100 across every category (only
  the top fifteen are listed, so the listed ones can still add to less).
  Percent halves round symmetrically (Choices made, 1 and 3).

### Income tab: source shares and changes

- **Where**: Cash Flow → Income, web `CashFlow.tsx` `IncomeSection` (the
  "Income by source" share column) and phone `app/cashflow.tsx` `IncomeTab`
  (`IncDelta`).
- **Source share**: web `Math.round(100 × source / window total)`. The
  phone did not show it: a difference between the clients.
- **Change**: web uses the Spending tab's `Delta` (shows "unchanged"); the
  phone's `IncDelta` is the same formula but shows nothing for an
  unchanged figure: a second difference.
- **Server**: `reporting.income_window` adds `source_shares` (in the order
  of `sources`), `change`, `kind_changes`, `source_changes`, all through
  `reporting.change` and `shown_shares`. `per_month` and `paycheck_share`
  already come from here.
- **Clients read**: both print the source share and every change from the
  reply; the phone gains the share column and the "unchanged" word, as the
  web has them (mobile matches web).
- **Visible difference**: the phone gains two things it lacks; shares add
  to 100.

### Income tab: a payer who stopped

`income_window` sends `dropped_sources` — the payers of the previous window
who paid nothing in this one. Without a sentence naming them, a fall in
the change beside the total has no stated reason.

- **The one function**: `reporting.dropped_words(dropped, prev_text)`,
  sent as `dropped_text`: "Nothing this time from Acme Payroll ($900),
  paid the year before." — three payers named, largest first, the rest
  counted ("… or 2 others"); null when none dropped out or the range has
  no previous window. `dropped_sources` stays in the reply (fields are
  only added), and its rows are what the sentence names.
- **Clients read**: both print `dropped_text` under the headline, beside
  the change it explains, also when the window itself has nothing in.
- **Older server**: no sentence; nothing was printed before either, so the
  phone keeps no stand-in.

### Cash Flow picture: node shares and dollars

- **Where**: the flow picture on Cash Flow → Overview, web
  `CashFlowCharts.tsx` `FlowChart` (`pct`) and phone
  `components/flow-chart.tsx` (`pct`).
- **Before**: a source's share is `Math.round(100 × node / income total)`;
  a sink's is of `max(income, spending)`; each node's dollars are rounded
  on their own. Agree: yes, the same formula (the phone hides the labels of
  thinner slivers at a smaller height than the web; that is layout).
- **Server**: `reporting.flow_breakdown` adds `in.shown` and `out.shown`:
  each side's nodes (paychecks, interest, other; fixed, variable, saved) as
  `{key, dollars, share}`, `shown_dollars` to the side's shown total and
  `shown_shares` over the side. "Overspent by" carries dollars and no
  share, as now. The picture's geometry keeps using the unrounded values.
- **Visible difference**: fixed $700, variable $700 and saved $2,600 of
  $4,000 read 18% / 17% / 65%, not 18% / 18% / 65% (101%). Node
  dollars add to the total printed beside them (Choices made, 2).

### Monthly and yearly report emails: the spending ring

- **Where**: the server's own mail, `lens_email._pie`: each slice's
  `pct` is `round(100 × share)` on its own.
- **Server**: the slices take `shown_shares` (and `shown_dollars` for the
  amount text) after the fold into "Other". No client is involved; it is
  listed so the mail cannot disagree with the screens.

### Budget planner: where each dollar goes

- **Where**: the planner on Budget, web `BudgetPlanner.tsx` (`pct`, and the
  per-part segment labels beside it) and phone `app/budget.tsx` (`pct` in
  the plan card).
- **Before**: "in $X · 100%", "out $Y · Y/X%", "save $Z · Z/X%", "excess
  $E · E/X%", each `Math.round` on its own, over figures that include
  values the person is typing and has not saved. Agree: yes (the web also
  labels each money-out segment with its share; the phone draws no
  segments).
- **This one stays on the client.** It is the input echo the counted
  figures standard already allows: the preview of typed, unsaved values.
  A server figure would lag every keystroke and could not see the box.
  What changes is the rounding: one client function,
  `plannerShares(parts, income)` in `moneymath.ts` with its phone twin in
  `lib/pure.ts`, implements the largest remainder exactly as
  `reporting.shown_shares` does, and the shared case table runs the same
  inputs through both twins and through the server function. The server
  adds `plan_line.shares` (`{bills, to_spend, saved, excess}`), so with
  nothing typed the planner's shares are checked against the server's in
  the suite.
- **Visible difference**: in $4,000, out $2,990, save $510, excess $500
  read 75% / 13% / 12%, not 75% / 13% / 13% (101%).

### Business: this month's profit, cash, the tax set-aside

- **Where**: Business overview tiles (`OverviewSection`) and the "Set aside
  for tax" card (`TaxSetAside`), web `Business.tsx` and phone
  `app/business.tsx`; the arithmetic in web `bizmath.ts`
  (`bizPeriodProfit`, `bizTaxStillDue`) and its word-for-word phone twin in
  `lib/pure.ts`.
- **This month's profit**: the year's worksheet rows replayed through the
  P&L rules on the client (revenue less operating cost, net of
  reimbursement links, transfers, card payments and owner tax payments out,
  capitalized start-up and organizational costs out). Agree: yes.
- **Cash in the business**: the sum of the entity's depository balances
  after the shadow and hidden-account rule (`countsInTotals`). Agree: yes.
- **Still to pay**: `max(0, estimated − paid)` rounded to the cent. Agree:
  yes.
- **Rate**: `Math.round(100 × estimated / profit)` "% of profit". Agree:
  yes.
- **Held for tax**: the nominated reserve account's balance, else all
  business cash, times the household's ownership share when the estimate
  covers the household; **covered by / short by** is `held − still to
  pay`. Agree: yes. Both clients also choose between `covered_tax_paid`
  and the P&L's `estimated_tax_paid`, and between `covered_profit` and
  `net_profit`, on the client.
- **Server**: `books.period_profit(conn, entity_id, period)` runs
  `_pnl_core` over the period's rows with the entity's lifetime index (so
  a refund nets against the cost it reverses exactly as the year does);
  the P&L reply adds `this_month = {period, net_operating}` for the
  household's current month. `books.entity_cash(conn, entity_id)` returns
  `{total, accounts}` under the server's shadow and hidden rule, on the
  same reply. `selfemploy.tax_set_aside(conn, entity_id, year)` returns,
  on the estimated-tax reply, `set_aside = {still_due, rate_of_profit,
  held, account, stale, covered}` with the covered/household choice made
  there. `bizPeriodProfit` and `bizTaxStillDue` and their phone twins are
  deleted.
- **Visible difference**: the month figure, the cash tile and the rate are
  the same numbers (the client replayed the server's rules; where the two
  differ, the server's figure stands). The set-aside card's three figures
  add up as printed (Choices made, 2).

### Accounts on the phone: institution subtotals

- **Where**: phone `app/(tabs)/accounts.tsx`. The web prints each
  institution card's subtotal from `totals.sections[pane][cat].shown.by_item`
  (keyed by the bank login); the phone prints none.
- **Also**: the phone grouped accounts by institution **name**, the web by
  **login**. Two logins at one bank were two cards on the web and one card
  on the phone, so the phone could not read the per-login subtotal without
  adding two together, which it must not do.
- **Server**: nothing new; `reporting.account_totals` already sends
  `shown.by_item`.
- **Clients read**: the phone groups by login as the web does and prints
  `shown.by_item[login]` on each card header (Choices made, 4). With no
  `shown` (older server) the card has no subtotal, as before.

### Smaller leftovers found by searching both clients

Each is the same number moved to the server unless it says otherwise.

- **Money map row words** (web `MoneyMap.tsx` `rowDelta`, phone
  `components/money-map.tsx`): "over by $X" (`actual − budget`, past
  $0.50), "$X more than planned" (`actual − expected`), "$X left"
  (`budget − actual − pending`), "$X awaiting · $Y still due" for Bills,
  and the "Variable spending" total row that sums the judged rows' actual,
  expected and budget. Agree: yes. Server: `todayview._plan_rows` adds
  each row's `status = {kind, amount, pending}` and the total row itself;
  the thresholds move into `todayview`.
- **Month lens** (web `MonthLens.tsx`, phone `app/month.tsx`): "left of $X
  to spend" is `variable_budget − variable_actual`, and the flow line's net
  is `income − spend_total`. Agree: yes. Server: `lenses.month_state`
  sends `remaining` and `net`.
- **Year lens vs last year** (web `YearLens.tsx`, phone `app/year.tsx`):
  each category's and the total's change is `this year − last year`.
  Agree: yes. Server: the year reply adds `changes` through
  `reporting.change` (Choices made, 3).
- **Reimbursements** (web `Reimburse.tsx`, phone `app/reimburse.tsx`): the
  headline "owed back" is `reimbOwed` summed on the client, and each row's
  "got $A of $B (P%)" is `received / expected` capped at 100. Agree: yes
  for charges (the web takes the absolute amount, the phone the signed
  one; they differ only on a flagged deposit). Server: the list reply adds
  `owed`, from `alerts.reimb_pending`, the function the Today strip and the
  email already state, and each row's `received_pct`. `reimbOwed` is
  deleted from both clients. The matcher's running sum of picked deposits
  and its "exact ✓ / $X stays income" hint (`pairGapHint`) are an input
  echo of a selection not yet saved, and stay — summed only from the
  figures the server sent ("The matcher counts against the server's
  room", below).
- **Transactions day totals** (web `TxnTable.tsx` `daySpend`, phone
  `lib/pure.ts`): each day divider sums `rowSpend` over that day's rows.
  Agree: yes. Server: the ledger reply adds `day_spend` for the days on
  the page, by the same spend rule the verdict uses.
- **The phone's "listed" line** (phone `app/(tabs)/transactions.tsx`,
  `listedSum`): a month narrowed by a filter shows "N listed · $X total
  for this filter", the sum of the raw amount column (card payments and
  transfers included). The web had no such line, and the figure is one
  the transactions table itself says must not be a column sum (Choices
  made, 5).
- **Fallback chains to remove**: Today's hero `month_remaining_paced ??
  variable_budget − variable_actual` (web `Today.tsx`, phone
  `app/(tabs)/index.tsx`) falls back from the paced figure to the whole
  month, a different figure; Bills' `period_left ?? amount − used` (web
  `Bills.tsx`, phone `app/(tabs)/bills.tsx`); the phone's "Needs
  attention" `unclassified ?? suggestions.length`, which falls back from
  the real count to the length of a capped page (the web uses the count
  alone). Each becomes the server field alone, and nothing for an older
  server.
- **A bill row's amount with its fees** (web `Bills.tsx`, phone
  `app/(tabs)/bills.tsx`): each row printed `amount + fee_total`, added on
  the client. Server: `bills.amount_with_fees(amount, raw)` — the bill's
  amount plus the fees that ride with each payment, rounded to the cent —
  sent as the row's `amount_with_fees`; both clients print it, and
  "incl. $X fee" beside it stays `fee_total`. An older server sends none
  and the row prints no amount. An older client keeps its own sum (the
  same figure).
- **An income row prints the last real deposit** (same two screens). An
  income series printed its planned per-paycheck amount while
  Transactions, one tap away, printed the deposit the bank posted a few
  cents off it. Server: `bills.row_amount(amount, raw, verdict)` is the one
  decision behind the row's `amount_with_fees` — a bill row is
  `amount_with_fees` as above; an income row is the most recent deposit
  matched to the series, to the cent (the health verdict's
  `last_match_amount`, the payment `last_match` dates), else the planned
  amount. Printing only: the row's `amount` stays the plan, the edit form
  prefills it, and every plan figure (the budget, the forecast walk, the
  calendar's expected paychecks) reads the plan as before. The verdict is
  read as of today, like the row's health, also on a closed month. A client
  that prints `amount_with_fees` prints the deposit with no change; an older
  phone that adds `amount + fee_total` itself prints the plan.
- **Over-time gain and percent**: the server's `trend_windows`; the phone
  keeps no `trendWindow` of its own.
- **Stays on the client, by design**: bar and ring geometry (fill widths,
  the cycle bar's fraction), the split editor's "left to place" while
  typing, the planner preview above, and the optimistic patches that move
  a cached total for the moment before the refetch lands (the
  patch-then-invalidate standard; the refetched server figure replaces
  them).

## Where each figure is decided

Each move is the same on web and phone.

- **Phone institution subtotals.** The phone groups its cards by login
   (`groupByLogin` in `lib/pure.ts`) and prints `shown.by_item`, with no
   server change; the business card has the web's category tiers so each
   login's subtotal is one figure.
- **Assistant ring.** `assistant_blocks.ring_figures` /
   `with_ring_figures`, attached in `validate` and on the history read (old
   answers gain it on read), used by `charts.donut`; web and phone legends
   read `ChartBlock.shown` (`RingShown`). The phone's `ringLayout` is geometry only and
   `ringLegend` prints the server's rows.
- **Cash Flow shares.** `category_shares`, `source_shares`, the flow
   picture's `in.shown` / `out.shown`, the email ring; the phone has the
   income source share. All through `shown_shares` / `shown_dollars`.
   `shown_shares` takes a `base` (shares of a figure the parts
   are not the whole of add up to their own share of it, 100 when they are
   all of it), so every share keeps the base it is printed against: a
   category's and a payer's are of the window's total, a flow sink's of
   the larger of in and out (the height the picture draws). Where those
   bases are the parts' sum (every month measured, no category netted
   below zero) the shares add up to 100; otherwise to the parts' own
   rounded share, never re-based. `reporting._flow_shown`
   decides the picture's nodes; the phone reads them through
   `flowNodeFigure`.
- **Budget planner shares.** The client largest-remainder twin,
   `plannerShares` in `moneymath.ts` and `lib/pure.ts`, the case
   table `mobile/scripts/share-cases.json` (server suite and both twins,
   against `shown_shares`), and `plan_line.shares`;
   the planner's out / save / excess shares come from one call, and each
   money-out segment's from a second over the same income.
- **Changes vs before.** The phone shows "unchanged" on
   income as the web does. `reporting.change` (whole-dollar amount of the printed
   figures, percent of the exact ones with halves to even, `unchanged` at
   $0); `spending_window` sends `per_month`, `per_month_by_category`,
   `change`, `category_changes`, `merchant_changes` and `movers` (the
   threshold and ranking in `reporting._movers`), `income_window` sends
   `change`, `kind_changes`, `source_changes`, and the Year lens
   `changes = {categories, spend}`. Both clients print them through
   `changeLook` (`moneymath.ts`, `lib/pure.ts`), which formats and
   decides nothing but leaving a percent past ±1000% out.
- **Business figures.** The P&L door adds `this_month` (`books.period_profit` over
   the household's current month, the entity's whole history as its
   index) and `cash` (`books.entity_cash`: the bank balances and the count
   of the business's counted accounts, linked backups and hidden accounts
   out); the estimated-tax door adds `set_aside`
   (`selfemploy.tax_set_aside`: `still_due`, `paid`, `rate_of_profit`
   rounded halves up as the card always printed it, `held`, `account`,
   `stale`, and `covered` in whole dollars as the printed held less the
   printed still to pay). Neither client keeps business arithmetic of its
   own (no `bizPeriodProfit`, `bizTaxStillDue` or refund-category list),
   and neither chooses between the covered and the business-wide figures.
- **Leftovers.** `todayview.row_status` decides each money-map row's words
   (`over`, `ahead`, `left`, `due` with its amount and what awaits), sent
   on every Today plan row and on `plan_total`, and the email's words are
   built from it; both maps print `row.status` and the "Variable spending"
   row is `plan_total` on Today and Budget. `month_summary` sends
   `remaining` and `net` (the monthly email reads `net` too). The
   reimbursements reply sends `owed` (`alerts.reimb_owed`) and each row's
   `received_pct` (`alerts.reimb_received_pct`); neither client sums
   `reimbOwed`. A month's reply sends `listed_spend`, the listed rows'
   spend by the day totals' rule, which a month narrowed by a lens prints
   as "$X spent" on the phone and the web. The three fallback chains print
   the server field alone (nothing from an older server).
   **Day totals**: the
   transactions reply sends `day_spend` (ISO date → the day's spending
   over every row of that day in the result, by the rule each row's
   `spend_amount` follows: `data.day_spend` for a month, `data._whole_day_spend`
   over the whole result set for a search, a bucket or a counted
   category, the page's days only). A day divider prints it on both
   clients, so a day of 12 rows worth $340 cut after 8 by "show more" or
   a page boundary reads $340; the divider's count stays the rows listed,
   and neither client keeps a `rowSpend` / `daySpend`. **Percent
   received**: the phone prints the server's `received_pct`, the web's
   rule — $10 back on a −$40 flagged deposit reads 25% on both (the
   phone's "of $B" figure is unchanged). **A month narrowed to business
   rows**: `data.listed_spend` decides; when every listed row is a
   business's by the household test the aggregates use
   (`budget.PERSONAL_ONLY_SQL`: the row's entity or an account in
   `app.entity_account_ids`) `listed_spend` counts their spending by the
   verdict's spending rule with that test turned round and `listed_scope`
   is `"business"`, which both clients print as "$85 spent (business)"
   (`listedSpendWord`); a mixed list, an empty one, or any list under the
   combined toggle keeps the household rule. The day dividers stay
   household money either way.
- **Lens money maps.** The Month and Year lens replies send their
   own `plan_rows` and `plan_total`, built by the rows Today sends, and
   neither client builds map rows from the buckets (design in "The Month
   and Year lens money maps", below): `todayview.map_rows` / `sent_plan_rows` / `ledger_path`
   (Today's `_plan_rows` is `map_rows` over the month's state),
   `lenses.lens_savings_row`; both lens replies send `plan_rows` and
   `plan_total` (null on a year with no budgeted month); both clients'
   lens screens print them through `sentPlanRows` / `sentPlanTotal` and do
   not ask Today for the goal; the phone's map reads captions through
   `terseCaption` and links through `ledgerParams` (`lib/pure.ts`).

Every field above is in the `@documented` schemas and `api.generated.ts`
for both clients, and passes `make api-types-compat`; every new field is
optional on the phone through `OlderServer<…>`. Tests pin each server
function and the invariant it states (shares
add to 100, parts add to the printed total, a change is what its rule
says), plus a client test that the screen prints the field and computes
nothing.

## The Month and Year lens money maps

The Month lens's "Monthly budget" card and the Year lens's "Yearly
budget" card draw the same money map Today draws, but each client builds
the rows itself from the lens reply's `buckets` (`buckets_annual` on the
year), `bills`, `savings_goal`, `saved_month` / `saved_ytd` and
`plan_surplus`, and then restates the server's row words over them.

### What each map showed before the server built its rows

An invented March, in progress, with a Dining carve-out under Food:

| Row | Figures | Words on the right | Caption under the bar |
|---|---|---|---|
| Variable spending | $737 / $1,000 | $263 left | — |
| Food | $412 / $600 | $188 left | — |
| Dining | $95 / $100 | $5 left | — |
| Everything else | $230 / $300 | $70 left | unallocated — spending outside the categories |
| Bills | $1,250 / $1,800 | $300 awaiting · $250 still due | — |
| Savings / Investment | $400 / $500 | — | transferred vs plan |

and under the rows "Also this month: $220 excess stays in checking".

Both clients printed exactly this. What differed was what each client's
`planRows` built, not what reached the screen:

- **Bills caption.** The web builds "posted $1,250 · awaiting $300 · not
  counted in the verdict"; its map never prints a Bills caption
  (`terseCaption`). The phone builds none.
- **Savings caption.** The web builds "transferred vs plan · not counted
  in the verdict" and prints it without the tail; the phone builds
  "transferred vs plan". A month with no posted figure: the web builds "avg
  / mo, last 3 months · not counted in the verdict", the phone relabels
  its row to "avg / mo, last 3 months" (`labelSavingsRow`); the year's
  projection: "estimate from the last 3 months" on both screens, the same
  way. Both print the same words.
- **Plan-only Savings and Excess cash captions.** The web builds a
  caption for each, the phone none; both rows fold into the "Also this
  month" sentence, where no caption is printed.
- **The row's link.** The web builds `/transactions?bucket=food&y=2026&m=3`,
  the phone `bucket=food&y=2026&m=3`; both open the same rows.
- **Year Bills pace.** Both build the year's Bills row with no expected
  figure (`fixed` is not passed in); an unjudged row draws no pace tick
  and its words read the budget, so nothing shows it.

The Year lens shows the same anatomy over the elapsed budgeted months:
Food and Everything else (no carve-outs; the annual buckets have none),
Bills, Savings at the monthly plan times those months, and Excess cash at
the standing surplus times those months. A year with no budgeted month
has no card.

### The server design

`todayview.map_rows` builds the plan's rows from a bucket set, the Bills
figures, the Savings row and the excess, with a ledger path for each
label. Today's `_plan_rows` is that function over the month's state;
`todayview.sent_plan_rows` rounds each row and adds its `row_status`, and
`todayview.plan_total` sums the judged rows, as Today sends them. The
lenses call the same three:

- **Month** (`lenses.month_summary`): `map_rows` over the reply's own
  `buckets` (already at the month's paced share), Bills from `bills`
  (posted, planned, awaiting) and the fixed bucket's expected, the excess
  from `plan_surplus`, and the Savings row from `lenses.lens_savings_row`:
  the goal's monthly plan against `saved_month` ("transferred vs plan"),
  or with no figure the goal's 3-month average ("avg / mo, last 3 months"),
  or a plan-only goal's plan alone. Links open
  `/transactions?bucket=<b>&y=<y>&m=<m>`.
- **Year** (`lenses.year_summary`): the same over `buckets_annual` (Bills
  with the annual fixed expected), Savings at the monthly plan times the
  elapsed budgeted months against `saved_ytd` ("transferred vs plan"), or
  the 90-day average times those months ("estimate from the last 90
  days"), the excess `plan_surplus` times those months. Links open
  `/transactions?bucket=<b>&y=<y>`. With no budgeted month elapsed,
  `plan_rows` and `plan_total` are null.

The lens's Savings row stays the lens's own (the standing Savings goal),
not Today's every-goal row: that is a different figure. Captions follow Today's: the "not counted in the verdict"
tail only on Bills, none on Excess cash or a plan-only Savings row (both
fold into the footer sentence on both clients).

**What each client reads.** Both print `plan_rows` and `plan_total`
through `sentPlanRows` / `sentPlanTotal`, the Today map's path, and
compute nothing. The phone's map takes the web's `terseCaption` (no Bills
caption, the "not counted in the verdict" tail dropped), and its
`ledgerRoute` reads a link written as a path (`/transactions?…`), which the
server's rows are.

**An older client** (a store build, a stale web tab) never reads the new
fields and keeps building from `buckets` and the rest, which stay in both
replies. **A newer phone on an older server** has no `plan_rows`: the map
card is left off, never rebuilt from buckets. **A restore** stores
nothing new: the rows are built per request from the restored data.

**The monthly and yearly emails** do not draw the money map: their
"Monthly budget" / "Yearly budget" bars are their own anatomy (Bills —
the due-dated bills — then Envelopes without any pinned pool, Food,
Everything else, the carve-outs and each pinned envelope, each with a
note, no Savings or Excess row).

### What people see differently

On the **web**: nothing. The Month and Year maps print the same figures,
words and captions.

On the **phone**:

- The Month and Year maps: nothing.
- Today and the Budget screen, whose rows already come from the server:
  the Bills row no longer prints "posted $1,250 · awaiting $300 · not
  counted in the verdict" under its bar (the awaiting and still-due
  figures stay on the right). The Bills row is one breakdown
  (specs/today-figures.md): both clients print its short caption, "posted
  $P · plan $Z" — the phone only when the reply carries the row's
  `breakdown` — and its "How this adds up" table when the reply carries
  one; and tapping a row's label opens that bucket's rows for the month,
  as on the web.

The row figures are the server's rounded to the cent and the Variable
spending row is the sum of the rounded rows, as Today's are; the clients
summed the same cent figures, so the printed whole dollars are the same.

## A year prints its income and spend once

`reporting.year_dollars` decides a year's whole dollars for every page
that prints them: the Year lens's totals card, its Total spend row and
that row's change, Cash Flow's Left over by year and the yearly email's
net. Income and spend are each their own whole dollars
(`reporting.whole_dollars`), the figures the category table and every
other place that prints them alone show; with every month's income
known, left over is the printed income less the printed spend, so it
may sit a dollar off its own rounding. (Rejected: apportioning the three
to the left over's rounding, which moves income or spend instead: the
Year lens and Cash Flow date bills differently, so their spends differ
by cents, and one income would print a dollar apart on the two pages,
while the totals card's spend would disagree with the Total spend row
beneath it.) The rate is the server's to one decimal and both pages
print it with `signedPct`.

The plan's income, likewise, is printed in one set of dollars: the
Month lens's "expected from paychecks $X" prints `budget.plan_line`'s
income leg (`lenses.income_plan_text(…, shown=)`), the figure Budget and
the Bills page print for the month.

## Choices made

Only the choices that change what someone sees; everything else above is
the same number, now from the server.

1. **Percentages in a breakdown add up to 100%** (as the Net Worth rings
   do): a row may be one point off its own rounding. Three categories of
   $500 each read 34% / 33% / 33%; the planner's out, save and excess of
   one income read 75%, 13%, 12%. Rejected: rounding each percent on its
   own (a column can read 99% or 101%), and one decimal place (33.3%:
   mismatches only rarer, 99.9%, and the rows wider on a phone).
2. **The dollar parts of a split add up to the total printed with them**
   (as the Accounts and Net Worth lists and the plan line do): a part may
   be a dollar off its own rounding. "$3,002 came in" sits over
   Paychecks $2,501, Interest $11, Other $490, not parts that add to
   $3,000; on the "Set aside for tax" card, business cash $1,001 and
   still to pay $400 read covered by $601. Rejected: each figure rounded
   on its own.
3. **A change is the printed figure minus the printed figure**, and a $0
   change reads "unchanged": Dining $1,000 this year and $1,000 last year
   is "unchanged", never "+$1". The percent is of the exact figures,
   rounded to the nearest whole percent with halves rounded the same way
   up or down, so a rise and a fall of 2.5% both read 2%. Rejected: the
   change of the exact figures, which can disagree with the two numbers
   beside it.
4. **On the phone's Accounts tab, a bank with two logins is two cards**,
   each with its subtotal, as on the web: two "Northwind Bank" cards,
   $2,000 and $3,000. Rejected: one card per bank name with a new server
   subtotal for the name, and no subtotals at all.
5. **A filtered month's line counts spending only**, from the server, on
   the phone and the web alike (the rule the day totals and the verdict
   use): $120 of groceries and a $500 card payment read "$120 spent", not
   "$620 total for this filter". Rejected: dropping the line, and a plain
   sum of the amounts.

## Interest & fees paid: each year adds up as printed

The "Interest & fees paid" card (Cash Flow → Income, web `CashFlow.tsx`
`CashCosts`, phone `app/cashflow.tsx` `CashCosts`) rounded every cell of a
year on its own, so a year could print fees of $20 less $22 refunded and a
net of −$1, and the sentence above the table repeated it. The largest
charge was dated month/day in a table whose rows are years, beside a chart
whose axis reads month/year.

- **Function**: `reporting.compute_cash_costs` (the only builder of the
  card). Each year of `by_year` — and `ytd`, which is one of them — adds
  `shown` = `{interest, fees, refunds, net}`: `net` is the year's net in
  whole dollars (`whole_dollars`), and interest, fees and refunds are the
  whole dollars that give it (`shown_dollars` over interest, fees and the
  refunds taken off), so interest + fees − refunds = net as printed. A
  part may be a dollar off its own rounding, never more. Each year also
  adds `biggest_text`, the largest charge as the row prints it: its name,
  whole dollars and full date ("Service fee $35 · 05/14/24"), null when the
  year had no charge. The reply adds `shown` = `{paid_ytd, earned_ytd,
  vs_earned_ytd}` for the tiles: this year's cost and the interest earned
  in whole dollars, and "vs interest earned" as the one printed figure
  less the other (`change`'s rule: the difference of the two numbers
  beside it).
- **Clients**: both print `shown` and `biggest_text` as sent: no cell is
  rounded on the client, and neither formats the charge's date.
- **Older phone**: reads the cent fields and rounds each cell itself, as
  before. **Older server** (a current phone): with no `shown` the phone
  prints the cent fields it has, each rounded on its own (the same
  figures from the older payload), and no "biggest" line. **Stale tab**:
  the table and its shown figures come in one reply. **Restore**: derived
  from the ledger on each read.

## One income figure on Overview and Income

The Overview picture printed each income kind in the dollars that add up
to "came in" (`_flow_shown`), and the Income tab's legend printed the same
kind rounded on its own, so one page showed paychecks a dollar apart on
two of its tabs.

- **Function**: `reporting.income_kinds_shown(kinds, total)`: paychecks,
  interest and other in whole dollars adding up to the total as printed
  (`shown_dollars`, in that order). `_flow_shown` takes the picture's
  income dollars from it, and `income_window` sends its result as
  `kinds_shown`. The two tabs read one window (counted-figures.md, "One
  window for every Cash Flow tab"), so they print one figure.
- **Clients**: the Income tab's legend prints `kinds_shown[kind]` on web
  and phone; the bar's widths stay the exact amounts (geometry).
- **Older phone**: rounds `kinds` itself, as before. **Older server**: no
  `kinds_shown`; the phone prints `kinds` rounded one by one, the same
  figures from the older payload. **Stale tab / restore**: derived with
  the window on each read.

## A percent or a row that says nothing is left out

Two things on the Spending and Income tabs printed figures a reader can do
nothing with. "What changed" printed a change's percent as sent, so a
category that went from a few dollars to a few thousand read "+4100%",
while every other change on the page went through the clients'
`changeLook`, which leaves a percent past ±1000% out; and a change of a
few dollars on a large figure read "+$6 (+0%)". "Income by source" and
"Top merchants" listed payers whose window total prints as $0 (a few
cents of interest, a charge of a few cents), each a full row of
"$0 · 0% · 1× · unchanged".

- **Function**: `reporting.change` decides the percent once, for every
  reader: null when it would print as 0%, null at ±1000% and past it, and
  null when the earlier figure it is measured from is under $50
  (`CHANGE_PCT_FLOOR`): from $8 to $48 is "+$40",
  and "+500%" beside it describes a figure too small to carry a percent.
  The dollar change always prints.
  The clients' own cut stays for an older server and changes nothing
  against this one. So the movers, which print `change.pct` as sent,
  follow the rule every other row already did.
- **Function**: `reporting._listed` cuts the ranked payers both tabs
  list (`income_window` → `sources`, `spending_window` → `merchants`): a
  payer whose amount prints as $0 is not a row. The shares and the totals
  are still decided over every payer, so nothing counted changes; the
  sibling lists in the same order (`source_changes`, `source_shares`,
  `source_paychecks`, `merchant_changes`) follow the listed rows.
- **Clients**: print the rows and percents as sent. **Older phone / stale
  tab**: the same fields, fewer rows. **Older server**: the rows and
  percents as before. **Restore**: derived on each read.

## Investment fees: a count says what it counts

The Investment fees table's "Count" column printed one number per
platform, and the number meant two things: for a platform with fee rows
it is how many charges, for an advisory fee the app estimates (no rows
exist; the cut comes out of the balance) it is how many years the
estimate covers. The phone printed it bare after the amount.

- **Function**: `reporting.compute_fees` adds `platform_counts`, one
  string per row of `by_platform` in the same order: "40 charges" (or "1
  charge") for fee rows, "5 years, estimated" (or "1 year, estimated")
  for an estimate. `by_platform` keeps its tuple and its number.
- **Clients**: the web prints the string in the column (headed "Counted")
  and the phone after the amount. **Older phone**: the bare number, as
  before. **Older server**: no `platform_counts`; the phone prints the
  amount alone. **Stale tab / restore**: derived on each read.

## The matcher counts against the server's room

The matcher's "selected $X of $Y" reads `$Y` from the server, never from
`Math.abs(amount) − received` worked out on the client; each ticked
deposit counts its own `left_amount`.

- **The one function**: `data.reimb_left(conn, row)` (over `_charge_room`
  for a charge, `_deposit_left` for a deposit, never below 0), sent on each
  row of `GET /api/reimburse/pending` as `left_amount` — the same figure
  the candidates door states as `anchor.left_amount`.
- **The contract chosen**: the server sends each row's room and the
  clients sum only figures it sent (the candidates' `left_amount`). A
  server answer per selection would add a request per tick and race the
  next one, for a sum of numbers the client already holds as the server
  stated them; the selection is the reader's own unsaved input.
- **Clients read**: the "of $Y" is the candidates door's
  `anchor.left_amount`, else the pending row's `left_amount`.
- **Older server** (the phone only): no `left_amount` on the pending row,
  and the charge less what came back stands in until the door answers
  (one marked stand-in).
- **Stale tab**: a list read before another link landed states the room
  it had then; the candidates door, read when the matcher opens, states
  the current one and replaces it.
- **The line itself**: `POST /api/reimburse/selection`
  (`data.reimbursement_selection`) states the whole line by trying the
  link and rolling it back. Each ask costs what a Pair press costs under
  the household's pairing lock, so both clients ask once the ticking
  pauses (300 ms), the door is limited per member as well as per address,
  and past 50 ticked rows it answers every field null and both clients
  print no line.

