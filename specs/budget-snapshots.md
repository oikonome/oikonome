# Month budget snapshots

## Problem

The budget lives in one mutable settings document, so without snapshots
every closed month would be judged against *today's* numbers,
retroactively. Raise the budget in April and March's verdict would quietly
flip; months that predate the install entirely would render as
fully-budgeted "UNDER BUDGET" report cards with today's budget and zero
spend.

## Design

**A month is judged against the budget it actually ran under.**

- `budget_snapshots` (tenant, year, month → config JSONB, RLS) holds a
  frozen copy of the settings document per month.
- The nightly job upserts the **current month only** (local date). The
  last write before the month turns is therefore the config in effect as
  the month closed — the finalized snapshot — and earlier months are
  never rewritten. Capture is skipped while both variable budgets are $0:
  an unset budget must stay "no budget", not become a verdict against
  zero.
- The lenses (`_month_state`) pass a closed month's snapshot into
  `budget.month_status(cfg=…)`. The current month always uses the live
  config (it is the working plan); future months plan from live config.
- The snapshot freezes the **budget** (variable budgets, custom buckets,
  income, excluded accounts, caps/disabled lists). The bill/envelope
  *schedule* still comes from the live recurring tables — the documented
  back-projection that gives past report cards their fixed/variable
  split.

## A net metric is not a budget verdict

A closed month with **no snapshot** (predates the product, or predates
this table) gets no verdict. The year grid shows its **net cash flow**
— income − spending, the Cash Flow report's frozen definitions — as a
muted "net" cell (`status: "net"`, with `income`/`spend`/`net`; income is
null in years without bank coverage, same rule as the annual totals).
The distinction matters and every surface states it: a verdict says how
spending compared to *the plan that existed at the time*; net says only
what came in versus what went out, and implies no plan at all. The two
are styled differently and never mixed.

The month lens keeps rendering a full report card for closed months with
no snapshot (the decomposition is still the best available view), but
labels it: judged against the current budget, applied retroactively
(`budget_source: "live"` vs `"snapshot"`).

Surfaces: Year/Month pages (web + mobile), the yearly report-card email
(net rows + footnote). Deliberately not backfilled by default:
fabricating snapshots from today's config for old months would recreate
the exact lie this removes.

## Budget history card + opt-in backfill

The Budget page (web + mobile) carries a **Budget history** card —
read-only rows of each frozen month (`GET /api/budget/snapshots`:
budgets, `source`, captured date) — plus the one deliberate action:
**backfill** (`POST /api/budget/snapshots/backfill`), which applies the
current budget to every closed no-snapshot month since the ledger began.
That is the owner explicitly *choosing* the retroactive judgment, so it
is allowed where automatic fabrication is not; the rows are marked
`source='backfill'` and labeled in the card forever. Frozen months are
never editable — rewriting the record is what snapshots exist to
prevent. Viewers read the card; the write gate stops them backfilling.
