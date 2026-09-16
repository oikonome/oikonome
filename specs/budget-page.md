# Budget page + one budget language everywhere (spec)

The setup wizard teaches a plan (bills · budget categories ·
unallocated · savings). Every tracking surface speaks that same language
rather than a separate one (Variable spending / Food / Everything else /
Fixed bills), and the plan has a home.

Decisions:

1. **Budget page = plan + actuals in one**, in the nav right after
   Recurring. Top: the balance planner (the existing shared
   `BudgetPlanner`). Below: this month's actuals, one bar per budget
   category, same names/colors/order as the plan. `/balance` redirects
   here (the standing "balance your budget" entry points keep working).
2. **Today shows the plan's categories.** The bullet bars become one bar
   per budget category (Food, Gas, Pets…) + Unallocated + Bills — same
   language as the planner. **The verdict math is untouched**: it stays
   variable-spend-only; only the display groups by the plan.
3. **The language reaches** Month lens, Week lens (weekly = monthly ÷
   4.3, the existing lens convention), the daily email, and the Spending
   report (aligns to plan categories where they exist).
4. **Bills and Savings render in tracking, marked not-judged**: bills
   (posted vs planned) and savings (transferred vs plan) appear with the
   existing "not counted in the verdict" caption doctrine.
5. **Actuals stay matcher-based** — the engine already computes
   per-bucket actuals from each category's categories/merchants;
   Unallocated = variable spend claimed by no category. No engine risk.
6. **Zero-budget categories render amber "— no budget set"** with a fix
   link, not a red 100%-over bar (a category with spend and no budget
   would otherwise render as maximally over). Red means over budget.
7. **Bills are read-only** on the Budget page with a link to Recurring —
   one owner for bill data.

## Mechanics

- `/api/today/full` already carries `buckets.food.children` (custom
  buckets with actual/expected/month_budget). The Budget page and the
  aligned bars read that same payload — no new engine math.
- Shared renderer: one `<PlanBars>` component (Today, Budget, Month,
  Week) so the surfaces cannot drift again.
- Unallocated actual = `other.actual − Σ children.actual` (the existing
  `display_actual`); its budget = `other.month_budget − Σ children
  .month_budget`.
- Savings bar actual = the goal's 90-day-rate month-to-date
  contributions (`savings.progress`), plan = `monthly_plan`.
- Email: the bucket lines follow the same order/names; no HTML rework
  beyond the loop source.
