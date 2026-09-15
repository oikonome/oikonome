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
- The right-side delta says it in words: `over by $X`; `$X ahead of
  pace` (past the pace tick, still inside budget); `$X left`; on Bills,
  `$X still due`, prefixed `$X awaiting ·` when a due bill hasn't
  posted; and an amber `no budget set — set one` on a category with
  spending but no budget — the state is never color-alone.

## The rows

- **Bills** — the whole month's schedule, segmented paid / awaiting /
  still-due. The runway story reads from the unpaid segments.
- **Variable spending** — the total the verdict judges, with the budget
  categories (Food, Everything else, and your custom categories)
  indented beneath it. The header row only appears when more than one
  category is judged; a single category stands on its own.
- **Savings / Investment** — on Today and on the Month and Year lenses,
  when a savings plan or posted transfers exist. A plan with no
  destination account has nothing to measure and folds into the footer
  instead of drawing an empty bar.
- Excess cash is never a bar: it folds into the footer line "Also this
  month: $X excess stays in checking", beside any plan-only savings
  row.
- **Every label opens its rows** on the monthly map (Today, the Budget
  page, the month lens): a category name opens the transactions the
  number counts for that month; a bill's name opens its history. The
  year lens's map keeps its labels as plain text — a year's bucket has
  no single month to open. The label reads as plain text and becomes a button on hover
  (a press on the phone) — the number beside it is the point of the row,
  so nothing is underlined. The same labels link in the daily email.

## Where the cash numbers went

Checking-now and card-debt-now are **point-in-time facts**, not plan
rows — they live as plain numbers at the top of Today's Cash timeline
card (**checking now**, **card debt**, **after cards & bills**), the
last shown red when checking doesn't cover the cards and the bills
still due. The one-line plan flow at the bottom of the verdict card
carries no cash numbers. Cash *warnings* have exactly one home: the
verdict card.

## Gotchas

- The Year lens map uses real elapsed-month totals (not this month ×
  12), so its bars are honest annual actuals against annual budgets.
- Rows are **not** on a shared dollar scale — a $6,000 Bills bar and a
  $2,000 category bar are the same width. Compare dollars by the
  numbers on the right; compare budget health by the bars.
