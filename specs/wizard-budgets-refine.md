# Wizard budgets step: buckets + savings (spec)

Step 5 of 7 of the setup wizard. Decisions:

1. **Suggested bucket chips + light add.** The step offers up to 4
   non-food categories from history as one-click carve-out chips
   (3-month median each, floor ~$75/mo, bill-matched rows excluded,
   categories already covered by existing buckets skipped), plus a
   minimal name+monthly add row. Full category/merchant editing stays in
   Settings (machinery unchanged — chips create ordinary custom
   buckets with `categories=[that category]`).
2. **The default savings bucket IS a savings goal** named
   "Savings" — saving the step creates/updates its `monthly_plan`. One
   concept: reduces plan surplus, rides the cash forecast, shows on
   Today; the user can later add a target/account for ledger-verified
   progress. Open-ended goals (no target) are valid: plan-only, no
   pct/ETA until a target exists.
3. **Savings prefills from the surplus math**: income − detected monthly
   bills − food − everything-else, floored at $0, recomputed live as the
   fields are edited. (Buckets are carve-outs of everything-else, so
   they don't change the surplus.)
4. `/api/budgets/suggest` grows `bucket_candidates` and
   `avg_bills_monthly` to power the above.

## The budget planner

5. **Dynamic money-out.** The everything-else field displays the
   UNALLOCATED REMAINDER: adding a carve-out visibly moves budget out of
   it (stored `other_monthly` stays the total — engine semantics
   unchanged). Bucket totals can grow the total but the remainder never
   goes negative.
6. **Bills in the picture.** Money out opens with a read-only
   "Bills — $X/mo (N tracked, evened out)" row; in the wizard it points
   Back, standalone it links to /recurring. The step blurb describes
   the whole plan, not just two fields.
7. **The balance graphic.** "Where each dollar goes": an income bar over
   a live allocation bar (bills / food / each bucket / unallocated /
   savings, hatched 'unplanned' remainder, red over-plan warning) with a
   legend of live amounts — every keystroke moves it.
8. **Reusable feature.** The whole planner is a shared
   component (`components/BudgetPlanner.tsx`) used by the wizard step AND
   a standing `/balance` page ("Balance your budget") — entry links on
   Today's spend-at-most card and the Settings Budgets card. Standalone
   mode treats saved config as authoritative; the wizard keeps
   suggestion-first until the step is saved.
