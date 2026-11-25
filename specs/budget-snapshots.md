# Month budget snapshots

## Problem

The budget lives in one mutable settings document, so without snapshots
every closed month would be judged against *today's* numbers,
retroactively. Raise the budget in April and March's verdict would quietly
flip; months that predate the install entirely would render as
fully-budgeted "UNDER BUDGET" report cards with today's budget and zero
spend.

## Design

**A month is judged against the budget it actually ran under.**

- `budget_snapshots` (tenant, year, month → config JSONB, RLS) holds a
  frozen copy of the settings document per month.
- The nightly job upserts the **current month only** (local date). The
  last write before the month turns is therefore the config in effect as
  the month closed — the finalized snapshot — and earlier months are
  never rewritten. Capture is skipped while both variable budgets are $0:
  an unset budget must stay "no budget", not become a verdict against
  zero.
- The lenses (`_month_state`) pass a closed month's snapshot into
  `budget.month_status(cfg=…)`. The current month always uses the live
  config (it is the working plan); future months plan from live config.
- The snapshot freezes the **budget** (variable budgets, custom buckets,
  income, excluded accounts, caps/disabled lists) and the bill/envelope
  *schedule* (below, `snapshot_bill_rows`); a month frozen before the
  schedule was kept reads the live recurring tables, marked as such.
- Beside the budget, the snapshot keeps the month's bill schedule
  (`budget._freeze_bills`): each active bill row with its `id` and its
  `uid`. The id is the payee's slug, which a rename rewrites; the uid
  (`bills.uid`, migration 234) is drawn once when the bill is made and
  kept by a rename, a re-save and a restore (an archived bill whose uid
  a bill of the household already holds is that bill, renamed since the
  archive was made, and is counted present; a restore draws a new uid
  only when the archive names one uid twice, or predates the column). Months frozen from that
  migration on record it; a closed month is still matched to its bills
  by payee and due date exactly as before — nothing reads the uid to
  decide a figure.

## A net metric is not a budget verdict

A closed month with **no snapshot** (predates the product, or predates
this table) gets no verdict. The year grid shows its **net cash flow**
— income − spending, the Cash Flow report's frozen definitions — as a
muted "net" cell (`status: "net"`, with `income`/`spend`/`net`; income is
null in years without bank coverage, same rule as the annual totals).
The distinction matters and every surface states it: a verdict says how
spending compared to *the plan that existed at the time*; net says only
what came in versus what went out, and implies no plan at all. The two
are styled differently and never mixed.

The month lens follows the same rule (see "A month's standing"
below).

Surfaces: Year/Month pages (web + mobile), the yearly report-card email
(net rows + footnote). Deliberately not backfilled by default:
fabricating snapshots from today's config for old months would recreate
the exact lie this removes.

## Budget history card + opt-in backfill

The Budget page (web + mobile) carries a **Budget history** card —
read-only rows of each frozen month (`GET /api/budget/snapshots`:
budgets, `source`, captured date) — plus the one deliberate action:
**backfill** (`POST /api/budget/snapshots/backfill`), which applies the
current budget to every closed no-snapshot month since the ledger began.
That is the owner explicitly *choosing* the retroactive judgment, so it
is allowed where automatic fabrication is not; the rows are marked
`source='backfill'` and labeled in the card forever. Frozen months are
never editable — rewriting the record is what snapshots exist to
prevent. Viewers read the card; the write gate stops them backfilling.

## A month's standing: one answer for the Month and Year views

The Month view and the Year grid agree: a month from before the
household had a budget gets no verdict and none of today's bills (it is
never judged against today's budget with today's bills written into it
as "due" and "awaiting"), and a month with no data says so rather than
getting a verdict and a row of zeros.

**One function decides: `lenses.month_standing(conn, y, m, today)`.** It
returns one of four words, and `month_summary` and `year_summary` both
read it (the monthly and yearly emails read those two):

- `judged` — a budget of the household's own stands behind the month: a
  closed month has its frozen snapshot (`budget.snapshot_config`, which a
  malformed row does not satisfy; an opted-in backfill row does), the
  month in progress or a future one has a saved budget
  (`budget.budgets_set`).
- `no_budget` — the month in progress (or a future one) of a household
  that has not saved a budget.
- `before_budget` — a closed month with rows and no snapshot.
- `no_data` — a closed month with no row dated in it that its figures
  could count: a live row (pending included, as spending includes it)
  on an account the budget math reads. A row on an excluded account, a
  linked shadow of another account or a business's account does not
  make a month have data.

Nothing new is stored. The fact "this month had a budget" is the
snapshot row the nightly job already writes; no migration.

### What an unjudged closed month shows

`before_budget`: what came in, what went out and the difference, the
spending by category and the biggest rows — what the Year grid's "net"
cell says, in full. `month_summary` reads the month with an EMPTY bill
schedule, so no bill is planned, due, awaiting or "scheduled but not
posted", there are no envelopes and no bill-driven scaling of the
budget; the money map is empty (`plan_rows: []`), the plan's income is
null (`income.budgeted`), and there is no excess-cash figure. `no_data`:
the same reply shape with nothing in it.

Both carry the words to print, written once on the server:
`standing_label` (the pill: "no budget then", "no data", and for
`no_budget` "no budget set") and `standing_text` (the sentence under it).
`verdict` is null and `budget_set` false, as for any unjudged month.

### Income that is not known

The rule of specs/counted-figures.md holds here too: a month the bank
feed does not measure has no income figure and no net, on the Month view
and in every Year cell (`MeasuredWindow.month_known`, the predicate
`reporting._income_known` reads). The Month reply says so in
`income_text` (`reporting.INCOME_INCOMPLETE`); a Year cell in `note`. A
cell never prints its spend under the word "net": each cell that is not
a verdict carries `label` ("net", or "spent" when income is not known,
or "no data"), `figure` (the signed whole-dollar net, the spend, or null)
and `tone`, decided by `lenses.cell_words`.

### The Year page's words

A sign means one thing on the Year page: money kept (+) or lost (−).
Only a cell that prints the month's net carries one. A judged cell (a
`final` or `projected` month) says its margin against the budget in
words, written by `lenses.cell_words` into the same `figure` field:
"over by $X" (tone `neg`) or "under by $X" (tone `pos`), whole dollars
from the cell's `variance`, and "on budget" when it rounds to $0. Web and
phone print `figure` under the verdict pill; they compute nothing.

The Year page's money map carries no "excess stays in checking" row:
the year has no excess of its own, and the standing surplus times the
budgeted months matched no month's figure. `year_summary` sends
`excess_text` instead, "An ordinary month leaves about $X" (the standing
surplus per month, `plan_surplus`, whole dollars), printed under the
Yearly budget card. It is null when nothing was budgeted (no data, no
map), when the standing surplus is not positive, and when the year to
date is over budget (the card's Variable spending: the food and
everything-else actuals above their pace over the months it covers, the
figure its "more than planned" reads) — a year
that overspent is not told what an ordinary month leaves.

The Yearly budget card covers only the elapsed budgeted months (the
`final` and `projected` cells), so it says which: `plan_caption`, written
by `year_summary`, "Aug–Nov, the months with a budget" for a run of
months, the months listed ("Mar, May, Jun, …") when there are gaps, "Aug,
the month with a budget" for one. Null when there is no map.

### A closed month's bills

After a month ends nothing in it is "still due" or "awaiting": a bill
was paid, paid for less, or not paid. For a closed judged month
`bills_breakdown.closed` (through `lenses.closed_bill_words`) writes,
from the month's own state:

- "not paid: NAME $X" for each scheduled occurrence with no payment
  matched — except a bill that has another payment matched in the same
  month (`paid_bills` on the month's state: the payees of the occurrences
  that were paid), which is never also listed as unpaid;
- in the Bills row's table, "Under plan" for what is left of the plan
  after the payments and the unpaid bills, or "Over plan" (with its
  minus) when the bills that were paid came to more — so posted + not
  paid + under plan is the plan, in the whole dollars printed
  (specs/today-figures.md, "The Bills row is one breakdown"). The
  Bills row is the due-dated bills only; a month's envelopes are the
  Envelopes row's (specs/today-figures.md, "Envelopes are their own
  row"), read from the same frozen rows: each pool's frozen amount,
  `envelope_months`, `uid` and `created_on`. No envelope figure is
  stored, so a month frozen by an older build reads in the same shape.

The "not paid" lines are `bills.notes`; the Bills row of the map has
nothing still due, so its words say how the month came out ("$N not
paid · $U under plan", "over plan by $X" — two phrases at most), with a caption ("posted $P") that has no
"awaiting" in it; its table lists each bill not paid. On a closed month `bills.awaiting` is 0 and
`bills.overdue` is empty: those two are the words "awaiting" and
"scheduled but not posted" on every client, and they belong to a month
still running.

### Clients, older clients, a stale tab, a restore

- **Web and phone** print `standing_label`, `standing_text`,
  `income_text`, `bills.notes`, and a Year cell's `label` / `figure` /
  `note`; for a month that is not `judged` they leave out the hero's
  budget figures, the money map and the bills card (a `no_data` month
  shows only its line). They decide none of it.
- **An older client** (a stale tab, a phone build from the stores) reads
  the fields it always did: `verdict` null and `budget_set` false, so it
  shows its "no budget set" pill with its own sentence ("No budget saved
  yet", wrong for a month from before the budget but not a verdict), an
  empty map, an empty bills card, and — where the reply still says
  `budget_source: "live"` on a closed month — its old line about a
  retroactive budget. In a Year cell with unknown income it still prints
  the spend under "net". A closed judged month shows no "due" figures
  and none of the new lines. Nothing it prints is a figure the server no
  longer stands behind.
- **An older server** sends none of the new fields: the clients fall
  back to what they printed before (the label "net", the client-built
  sentences).
- **Restore**: snapshots travel in the export, so a restored household
  keeps the standing of every month; an archive without them reads every
  closed month as `before_budget`, which is what the Year grid already
  did. The opt-in backfill on the Budget page is how an owner chooses to
  judge older months after the fact.
