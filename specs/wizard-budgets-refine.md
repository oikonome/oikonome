# Wizard budgets step: buckets + savings (spec)

Step 5 of 7 of the setup wizard.

1. **Suggested bucket chips + light add.** The step offers up to 6
   non-food categories from history as one-click carve-out chips
   (median of the finished months each, floor ~$75/mo, bill-matched rows
   excluded, categories already covered by existing buckets skipped), plus a
   minimal name+monthly add row. **A category is offered only when it had
   spending in at least half of the finished months of the window**
   (`budget.steady_category`, the one function that decides it): a single
   tyre purchase or one laptop is a month with spend among five without,
   and offered as "$600 a month" it plans money that will not be spent
   again. Such a category stays inside Everything else, which already
   counts it. Only finished months vote; the month in progress and the
   month the ledger starts in are partial. With no finished month at all
   nothing tells a habit from a one-off, and the category is kept (its
   figure is a partial month's rate, which the planner's history line
   says). Both clients print `bucket_candidates` as sent; an older client
   reading a newer server simply sees fewer chips. Full category/merchant editing stays in
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
   mode treats saved config as authoritative; the wizard opens on the
   household's plan whenever it has one (below).

## Running setup again

9. **The step starts from the household's plan.** Settings → Wizards can
   clear every step mark, so the step's own "done" mark cannot say whether
   a plan exists. The one answer is the server's `budgets_saved_by_user`
   (below). Web and phone read it from `GET /api/onboarding` (`planSaved`
   in `src/checklist.ts` / `components/wizard-logic.ts`) and seed the
   planner from the saved categories and amounts when it is true; only a
   household without a plan is seeded from history. Continue is enabled
   when it is true AND `budgets_set` holds (a budget above zero is stored —
   `budgetsStepKept`), so it never leaves past a plan the step did not
   show, and Done reports what Continue kept.
## Who saved the plan

`budgets_set` cannot answer "did the household save a plan": once a
finished month is on the ledger `_load_config` STORES the first-run seed
(food and everything else), and `GET /api/onboarding` itself loads the
config first — so a first-run household would read as having a plan: the
wizard would open on the machine's mid-import guess, skip history's
category rows and enable Continue with nothing saved. The answer is its own fact,
decided in ONE function, `budget.budgets_saved_by_user`:

- **The mark.** `budgets_saved_by_user: true` in the settings document,
  stamped (`budget.mark_saved_by_user`) by the settings door whenever a
  request carries `food_monthly`, `other_monthly`, `custom_buckets` or
  `budgeted_income_monthly` — the planner, the Budget page, the wizard's
  manual step — and by the legacy form save. The seed never stamps it;
  a save of anything else does not either. It is never cleared, and the
  activity log does not list it as a change of its own.
- **Plans saved before the mark existed** carry none. Without the mark a
  household still counts as having saved a plan when `wizard_steps.budgets`
  is `done` or `skipped`, or when the stored row holds a budget category or
  a planned income — the seed writes neither.
- **Older client.** It ignores the field and keeps reading `budgets_set`:
  the behaviour this replaces, nothing worse. **Older server.** A client
  that finds no field falls back to `budgets_set`.
- **Restore** brings the mark back with the settings row (held to a
  boolean, like `wizard_done`); an archive from before the mark falls to
  the rule above. The demo household is seeded with the mark.
- `budgets_set` is unchanged and still gates the verdict, the daily email,
  the month snapshot and the Today checklist: a stored seed is a budget to
  judge by, just not one the household chose.

10. **History as hints.** On a plan opened from what is saved, each row
    shows history's figure beside it, as `/api/budgets/suggest` sends it:
    Food → `suggestions.food_monthly`, a saved category →
    `suggestions.buckets[<its saved name>]`, Everything else →
    `suggestions.unallocated_monthly` (worked out in `suggest_budgets`,
    never on a client: per month, the non-food pool less what the saved
    everything-else categories took that month, then suggested by the same
    rule as every row — a difference of two suggestions would read ~$0 when a
    sparse category's figure came from its one active month). History's other categories stay
    as the "+ name ~$X/mo" chips. A plan seeded from history shows no hint:
    its figures are the hints.
11. **Unsaved edits hold Continue.** While the planner holds typed figures,
    Continue waits ("save or discard the changes first"); otherwise it
    would keep the stored plan while the screen showed another, and Done
    would report a figure the step did not show last.
12. **Older client, stale tab, restore.** An older client ignores
    `unallocated_monthly` and `budgets_saved_by_user` and still seeds from
    what it read before — the
    behaviour this replaces, nothing worse. A tab open across a save
    elsewhere re-reads `/api/onboarding` every 5 s on the web, but the
    planner is seeded once; its save merges with the server's current
    lists (mergePlannerBuckets), so a stale seed cannot erase a sibling's
    rows. A restore brings back the stored row, and `budgets_set` and
    `budgets_saved_by_user` follow it ("Who saved the plan").
