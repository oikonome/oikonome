# The money map: per-budget gauges + hero bar

`docs/guides/money-map.md` is the user-facing description.

## Rules

- **Every bar is a gauge of ITS OWN budget.** Rows are not on a shared
  dollar scale — compare dollars by the numbers, budget health by the
  bars. Overspend is a striped segment past a solid 2px budget divider,
  so the budget boundary stays visible exactly when it matters (email:
  solid darker red — no gradients in mail clients).
- **Pace** is a ▲ caret under the bar, not a tick inside it.
- **Delta text** per row (`over by $X` / `$X left` / `$X still due`) —
  state is never color-alone.
- **Bills = the whole month, segmented** paid / awaiting / still-due.
- **Variable spending** is a header row; the judged rows (Food,
  Everything else, custom buckets) sit indented beneath it.
- **Hero bar**: "Money to spend this month/week/year" = bills +
  variable (savings/excess stay in checking, excluded), with
  left-of-budget headline, days-to-go (Today), and ahead/under-pace.
- Checking now, card debt, and their delta (red when the cards owe more
  than checking holds) are plain numbers on Today's "This month's plan"
  pane, not bars on the map.

## Shape

`components/MoneyMap.tsx` — one renderer for Today / Month / Year, so
surfaces can't drift. Row DATA comes from `planRows()`; only the rendering
differs. Cash WARNINGS have one home (the verdict pane), not the map.

## Surfaces

- **Today**, **Month**: one MoneyMap card (overdue-unpaid footnote kept on
  Today).
- **Year**: `buckets_annual` on /api/lens/year (food/other/fixed summed
  over elapsed months — actual, expected, month_budget in PlanBucket
  shape) so the same `planRows()` builds the rows from real annual numbers.
- **Budget page keeps PlanBars** (per-row bars suit editing the plan
  itself).

## Out of scope

Verdict math, savings/excess rows on the lenses (Today shows them; the
lenses keep their own row sets).
