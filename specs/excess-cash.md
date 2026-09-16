# Excess cash: a remembered $0 savings plan and a named residual

An explicit "I'm not planning savings" ($0) must be remembered, and the
in-minus-out leftover has one name everywhere: **Excess cash**.

## Rules

1. **The zero persists.** An explicit $0 (any explicit value) is remembered;
   the in-minus-out difference shows as a named **Excess cash** amount
   instead of re-filling the field.
2. **One name everywhere.** Excess cash appears on Today's plan pane, the
   Budget page footnote, and the daily email — the same number (income −
   typical bills − budgets − savings plan). The plan bars carry an Excess
   cash row so the bars account for every planned dollar.
3. **No forecast event.** Excess cash stays in checking; the forecast's
   balance line already shows it accumulating. Only a real savings plan
   schedules an outflow.
4. **Auto-fill never overwrites a saved value.** Once any value is saved
   (including $0), the field shows it on return; a "suggested $X — use"
   chip appears when the computed leftover differs (same approve-each
   pattern as the budget suggestion chips).

## Mechanics

- **Persistence**: the planner always writes the `Savings` goal row on
  save, `monthly_plan: 0` included — presence of the row = "the user has
  spoken". No config key or migration of its own. Engine paths tolerate
  a 0-plan goal (forecast skips `plan <= 0`, `monthly_plan_total` adds 0,
  and the plan-only rules give it null progress).
- **Planner init**: a found `Savings` goal (any value) marks savings
  touched and shows its number; only a truly absent row lets the auto
  value fill first.
- **Server validation**: a goal needs a target or a monthly plan, where an
  **explicit** `0` counts as an answer (accepted) and a **blank/omitted**
  plan does not (400, because the Settings goals form sends `""` for an
  untouched field and that 400 is its only guard).
- **Displays**: the graphic's hatched leftover is labeled Excess cash in
  the legend with $/% like every other segment; plan bars row renders
  "— / $X" (nothing to track — it's a residual, not a budget); Today's
  savings-goals card hides the pure marker row ($0 plan, $0 target,
  plan-only) instead of listing "Savings · plan $0/mo".

## Out of scope

- Treating excess as spendable headroom in the verdict (it is not — the
  verdict stays variable-budget-only).
- Auto-sweeping excess into the savings goal.
