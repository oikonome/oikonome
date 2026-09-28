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
4. **Goal = target amount + optional target date.** Pace projects from
   the trailing 3 months of actual contributions; with a date it reports
   ahead/behind, without one it estimates the arrival month.
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
  start_balance.
- Email milestones: edge-triggered in the nightly sweep with an
  alerts_log guard per (goal, milestone).
