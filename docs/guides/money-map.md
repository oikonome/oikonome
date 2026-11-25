# The money map

The money map is the plan drawn as a row of **budget gauges**: every
bar is a gauge of its own budget, so "how am I doing against the plan"
reads identically on every line — a bar filled to the end has used its
whole budget, whatever the dollar amounts. (Dollar amounts and an
over/under delta sit at the right of each row.)

The hero bar lives in each page's **verdict card**, not on the money-map
card itself, and it tracks **variable spending** — the money the verdict
judges — against its budget. On the Month lens that is the whole month:
what's left of the variable budget, the pace tick, and how far over or
under you are. On Today the verdict is a sentence (On budget / Over
budget / Under budget, with today's pace), and the track beneath it is
scoped to the day — **Left to spend today**, today's spend against
today's allowance. Bills, savings transfers and excess cash aren't
judged, so they don't dilute it.

It appears on Today and on the Month and Year lenses, scaled to
each timeframe. (The Budget page keeps its per-row bars — when editing
a single category, its own scale is the right one.)

## Reading a row

Each row is `label · actual / budget · delta`:

- **Filled** portion — spent, gone.
- **Light** portion — the remainder, still coming out of checking.
- **Amber** segment (Bills) — awaiting: scheduled but not yet posted.
- A **vertical tick** through the bar marks where "on pace for today"
  sits; the filled bar turning red means actuals passed it.
- Spending past the budget renders as a **striped segment past a solid
  budget divider** — the budget boundary stays visible exactly when it
  matters, and everything past the line is overspend. (The daily email
  shows the striped segment as a solid darker red.)
- The right-side delta says it in words. On Today a spending row reads
  against today's pace: `$X over pace`, `$X under pace` or `on pace`
  (the Today guide says the same). On the Month and Year views it reads
  `over by $X`; `$X over pace so far` (past the pace tick, still inside
  budget); `$X left`. On Bills,
  `$X still due` — this month's bills not yet paid and any bill late
  from an earlier month; a month that is over says how it came out
  (`$X not paid`, `$Y under plan`, `over plan by $Y`), and a running
  month says `over plan by $Y` once it is; its caption names
  what posted (with its awaiting part, and for the running month the
  plan), and **How this
  adds up** under it opens a small table of the parts, each with its
  sign, that add up to the plan. An amber `no budget set — set one`
  marks a category with spending but no budget — the state is never
  color-alone.

## The rows

- **Bills** — the month's due-dated bills, segmented paid / awaiting /
  still-due. The runway story reads from the unpaid segments.
- **Envelopes** — directly under Bills when the plan has a pool: what
  the pools paid out this month (left) of what they held (right, what
  was paid out plus what is left). The bar never runs past its right
  figure, so it is never red; the caption says what the pools are
  still expected to pay out this month. It says "over" only as "$X past
  the pool, counted in spending" — a charge past a pool, which is
  counted once, in Food or Everything else. **Each pool**, under the
  row, lists the pools one by one, each as what its period charged of
  its amount, and what the plan sets aside for them each month.
- **Variable spending** — the total the verdict judges, with the budget
  categories (Food, Everything else, and your custom categories)
  indented beneath it. The header row only appears when more than one
  category is judged; a single category stands on its own. A bucket
  whose whole budget went to its custom categories, with nothing spent
  outside them, gets no row of its own (it would read $0 / $0).
- **Savings / Investment** — on Today and on the Month and Year lenses,
  when a savings plan or posted transfers exist. A plan with no
  destination account has nothing to measure and folds into the footer
  instead of drawing an empty bar.
- Excess cash is never a bar: it folds into the footer line "Also this
  month: $X planned excess (the plan's, with bills evened out)", beside
  any plan-only savings row — with "none this month" once the month's
  spending has gone past its budget by at least the excess (the Budget
  page's summary line says the same, muted rather than green). It is the plan line's figure — income less
  the budgets, the bills evened out to a month and savings — so it does
  not move with this month's spending or this month's bill schedule.
- **Every label opens its rows** (Today, the month lens, the year
  lens): a category name opens the transactions the number counts for
  that month — or, on the year lens's map, for the year: one listing of
  that bucket over the same elapsed, budgeted months the map summed,
  with a "Food · 2026" chip over it. A bill's name opens its history.
  The Budget page's plan, which is no period's tally, keeps plain text.
  The label reads as plain text and becomes a button on hover
  (a press on the phone) — the number beside it is the point of the row,
  so nothing is underlined. The same labels link in the daily email.

## Where the cash numbers went

Checking-now and card-debt-now are **point-in-time facts**, not plan
rows — they live as plain numbers at the top of Today's Cash timeline
card (**checking now**, **card debt**, **paychecks still due**, and
**left at month end** — the balance the month ends on after this month's
bills, paychecks, card payments and spending at your usual pace; with no
paycheck set up the note under it says so and counts none), the last
shown red when the month is forecast to end below $0. The one-line
plan flow at the bottom of the verdict card
carries no cash numbers. Cash *warnings* have exactly one home: the
verdict card.

## Gotchas

- The Year lens map uses real elapsed-month totals (not this month ×
  12), so its bars are honest annual actuals against annual budgets.
- Rows are **not** on a shared dollar scale — a $6,000 Bills bar and a
  $2,000 category bar are the same width. Compare dollars by the
  numbers on the right; compare budget health by the bars.
