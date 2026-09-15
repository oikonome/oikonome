# The money map: cash + plan on one dollar scale

Status: spec · build with it

> **Amended:** the cash header (decision 3)
> was removed after living with it — the checking/card-debt bars read
> as noise above the plan. Checking now, card debt, and their delta
> (red when the cards owe more than checking holds) moved to Today's
> "This month's plan" pane as plain numbers. The map is plan rows only;
> everything else below stands.
>
> **Amended again:** the "no Variable spending total bar"
> bullet is reversed — flat Food / Everything else rows read as peers
> of Bills. The judged rows now sum into the same "Variable spending"
> header the Budget page tracks and sit indented beneath it (indent
> shifts a row's origin, never its length, so the dollar axis holds).

## Why

Today carried two adjacent graphics that could not be compared by eye:
the checking-allocation bar (dollar-scaled) and the plan bars (each row
scaled to its own budget — a $500 food bar rendered as long as a $1,800
bills bar). One visual instead: what you have on top, what it is spoken
for below, all to scale.

## Decisions

1. **One shared dollar axis.** Every bar's length is its dollars on one
   common scale — checking vs bills vs budgets directly comparable.
2. **Bills = the whole month, segmented** paid / awaiting / still-due.
   The runway story reads from the unpaid segment vs the checking bar.
   (The old bills-before-next-paycheck strip is retired with the card.)
3. **The cash header (checking now + card debt now) is identical on all
   four lenses** — it is a point-in-time fact; only the plan rows below
   scale to the timeframe.
4. **Full plan amounts with remaining highlighted** — spent portion
   filled, remainder visually distinct ("filled = gone · light = still
   coming out of checking").

## Shape

`components/MoneyMap.tsx` — one renderer for Today / Week / Month /
Year (the PlanBars doctrine: shared renderer so surfaces can't drift).
Row DATA still comes from `planRows()`; only the rendering differs.

- Header: Checking now (green bar) · Card debt (blue bar), both on the
  shared scale. Purely factual — cash WARNINGS have one home (the
  verdict pane) and it isn't here.
- Rows: label + `actual / budget`; bar = max(budget, actual)/scale wide;
  segments: spent (green, red when over the pace tick), pending (amber,
  bills), remaining (same green at low opacity), pace tick kept.
  Overage extends the bar past the budget length — the dollar scale
  makes over-budget literally longer.
- No "Variable spending" total bar — on a dollar axis a parent bar
  double-counts its children; the total lives in the verdict card.

## Surfaces

- **Today**: replaces BOTH the checking-allocation card and the plan
  bars card with one MoneyMap card (overdue-unpaid footnote kept).
- **Week / Month**: PlanBars → MoneyMap, same rows they already build;
  cash header read from the cached `today` payload.
- **Year**: gains the map. New `buckets_annual` on /api/lens/year
  (food/other/fixed summed over elapsed months — actual, expected,
  month_budget in PlanBucket shape) so the same `planRows()` builds the
  rows honestly from real annual numbers.
- **Budget page keeps PlanBars** (per-row scale is right when editing
  the plan itself).

## Out of scope

Verdict math, savings/excess rows on the lenses (Today keeps them; the
lenses keep their current row sets), Budget-page rendering.

## Rework: per-budget gauges + hero bar

The one-dollar-scale rendering above is superseded. What changed and why:

- **Grammar bug**: an over-budget bar filled to the spend and stopped —
  the budget boundary vanished exactly when it mattered. Now every bar
  is a gauge of ITS OWN budget; overspend is a striped segment past a
  solid 2px budget divider (email: solid darker red — no gradients in
  mail clients).
- **Pace** moved out of the bar to a ▲ caret underneath (the in-bar
  ink tick was the subtlest mark carrying the most meaning).
- **Delta text** per row (`over by $X` / `$X left` / `$X still due`) —
  state is never color-alone.
- **Hero bar**: "Money to spend this month/week/year" = bills +
  variable (savings/excess stay in checking, excluded), with
  left-of-budget headline, days-to-go (Today), and ahead/under-pace.
- Rows are no longer on a shared dollar scale — compare dollars by the
  numbers, budget health by the bars. `docs/guides/money-map.md` is the
  user-facing description.
