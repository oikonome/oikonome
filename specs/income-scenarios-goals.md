# Income scenarios generalized + savings goals (spec)

## Income scenarios

1. **Named scenario list.** Any number of named scenarios ("full salary",
   "60% sabbatical", "no 401k"), each with its own take-home per paycheck
   and cadence; exactly ONE is active and powers budgeted income, the
   cash forecast, and plan surplus. Legacy saving/not-saving values are
   read as two list entries, the toggle as the active marker — existing
   config behaves the same.

## Savings goals (trip fund, emergency fund, …)

2. **Ledger-driven progress, matched transfers.** A goal matches transfer
   transactions by account + optional merchant/description tokens, with
   an optional starting balance; progress = net of matched in/out.
   Several goals can carve one savings account; brokerage transfers work
   the same way.
3. **Fixed-bill budget semantics.** Each goal carries a user-set planned
   $/month: the plan reduces plan surplus and appears in the cash
   forecast as a scheduled outflow, but NEVER makes the daily verdict go
   "over" (verdict stays variable-spend-only). The ledger measures
   actual progress; surfaces show plan-vs-actual.
4. **Goal = target amount + optional target date.** With a date it is
   judged against its schedule (`specs/savings-pace.md`); without one the
   arrival month is estimated from the trailing 3 months of actual
   contributions.
5. **Surfaces**: a Today tile (progress + pace per goal), the
   Settings → Planning & assets card (configure + read), and the daily
   email on **edge-triggered transitions only** — crossing 25/50/75/100%
   and falling off pace for a dated goal (existing alert doctrine; no
   daily noise). Explicitly NOT the Net Worth page.

## Mechanics

- Config-based like custom_buckets/manual_assets — no schema migration:
  `income_scenarios: [{name, take_home, cadence}]` + `active_scenario`,
  `savings_goals: [{name, target, target_date?, monthly_plan,
  account_id?, tokens[], start_balance?}]`.
- Legacy `income_biweekly_saving` / `income_biweekly_not_saving` +
  `retirement_saving` toggle are read-migrated into the list form once,
  then written back in the new shape.
- Goal progress: net sum of TRANSFER-category rows on the configured
  account matching every token (recurring-bill token rules), plus
  start_balance. Goals sharing an account split its rows ONCE, by row
  (`savings._partition`): each row goes to the first goal, in the
  household's own order, whose tokens all appear in it; a row no goal's
  tokens claim goes to the first goal with no tokens (later token-less
  goals read 0). So the goals on one account add up to what arrived and a
  transfer carrying two goals' words counts for the first only. Saved,
  the 3-month figure, the pace's days, the sweep line and the forecast's
  plan evidence all read that one partition; the checking-leg inference
  keeps the same order.
- Email milestones: edge-triggered in the nightly sweep with an
  alerts_log guard per (goal, milestone).

## The scenario's month is the server's

The card prints "Active now: $X/mo" beside the scenarios. That month is
the plan's income whenever no paycheck series is tracked, so it is one
figure with one definition.

- **The one function**: `budget.scenario_monthly(s)` — the take-home times
  the cadence factor in `CADENCE_MONTHLY` (weekly ×4, biweekly and
  semimonthly ×2, monthly ×1: the extra paychecks of a 52- or 26-paycheck
  year read as bonus, not as plan). `active_monthly_income` calls it; a
  stored take-home that is not a finite number reads as no figure, so one
  bad row an older door let in cannot fail the whole settings read.
- **What each client reads**: every entry of `income_scenarios` on the
  settings read carries `monthly`. The web card (`IncomeScenarioCard`) and
  the phone's Budget scenarios card print the SAVED active scenario's
  `monthly`; an edit not yet saved does not move the line until the save
  returns its own view. The save door ignores `monthly` (it rebuilds each
  entry from name, take-home and cadence), so the figure is never stored.
- **Older client**: an installed phone build keeps its own cadence table,
  the same factors as today, so it prints the same number.
- **Older server** (a current phone against a self-host that has not
  upgraded): no `monthly`, no "Active now" line — never a figure worked
  out on the phone.
- **Stale tab**: the line speaks for the list the card last saved or was
  seeded with; another device's save reaches it with the next settings
  read, like every other row of the card.
- The factors themselves are a product rule: changing them to 52/12 and
  26/12 moves the plan's income for every household planning on a
  scenario, and lives on its own, in `CADENCE_MONTHLY`, in one line.
