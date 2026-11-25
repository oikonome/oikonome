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
- **The pace label is the server's.** The Month reply's `pace_label`
  is "today's pace" only for the month in progress; a closed month (its
  pace is its whole budget, which the divider already marks) and a
  future one send null, and the hero bar prints no label. Web and phone
  print the field; they do not decide it (`lenses.month_summary`).
- Checking now, card debt, and their delta (red when the cards owe more
  than checking holds) are plain numbers on Today's "This month's plan"
  pane, not bars on the map.

## Shape

`components/MoneyMap.tsx` — one renderer for Today / Month / Year, so
surfaces can't drift. Row DATA is the server's: `todayview.map_rows`,
sent as `plan_rows` + `plan_total` by Today and by both lens replies, so
no client builds a row; only the rendering differs. Cash WARNINGS have one home (the verdict pane), not the map.

## Surfaces

- **Today**, **Month**: one MoneyMap card (overdue-unpaid footnote kept on
  Today).
- **Year**: `buckets_annual` on /api/lens/year (food/other/fixed summed
  over the elapsed budgeted months), from which the server builds the
  reply's `plan_rows` with the same `map_rows` (specs/shown-figures.md,
  "The Month and Year lens money maps").
- **Budget page keeps PlanBars** (per-row bars suit editing the plan
  itself).

## The Month hero's words

The Month view's hero card prints a figure, a word, a pace bar and a
verdict line; every word and the tick's position are decided once, by
`web/lenses.py:hero_words`, from the month's own fields, and both clients
print them:

- `remaining_word` — between the big figure (`remaining`, printed without
  its sign) and the budget: "over" when the spendable is spent past, "left
  unspent of" on a closed month, "left of" otherwise.
- `pace_expected` — where the pace tick stands: the spend the pace
  expects by now (`variable_actual` less `variance`).
- `variance_text` — the verdict line and its tone: "$X over budget",
  "$X under budget" or "within tolerance ($T)", with " so far" only while
  the month is under way (a planned month has nothing "so far"). Under
  way the variance is measured against the pace, not the budget, so the
  line says "$X over pace so far" / "$X under pace so far": the big
  figure beside "over" is the budget's, and two "over budget" amounts
  side by side read as one figure printed twice.
- `projection.tail` / `tail_tone` — the projection's last words: "$X
  over", "$X under" or "on plan".

An unjudged month (no budget of its own) has none of them; it prints the
standing sentence. A client reading an older server that sends none of
these prints the figure and the bar without words — never words of its
own. A stale tab shows the words of the reply it holds; a restore changes
nothing here, the words are recomputed on every read.

## Out of scope

Verdict math, savings/excess rows on the lenses (Today shows them; the
lenses keep their own row sets).
