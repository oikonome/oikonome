# Forecast card scenarios 3→2

Status: shipped — the Today page and the daily email each show exactly the
two card scenarios described below.

## Goal

Only two card-payoff scenarios in product UI and email:

1. **Pay all cards now**
2. **Autopay statement balance on due date** (`pace_stmt`)

Drop **pay full balance at due dates** (full current balance on due date)
as a first-class series — it is ≈ autopay except mid-cycle, and autopay is
the realistic case.

## Engine notes

`server/oikonome/engine/forecast.py` documents plan/pace vs `pace_stmt`.
Keep generating what tests need, but **SPA + email expose only two**
user-facing card scenarios; default “with cards” path should prefer
statement autopay where a primary is required.

## Touch

- `engine/forecast.py` (public series / keys)
- Today forecast tiles, chart, legend, event table
- Email / todayview mirror
- `runs out` marker source if tied to removed series
- Tests that assert series keys

## Done when

- Two scenarios only in SPA + email
- Legend and docs updated
- Suite green; spot-checked against a seeded instance

## Coordinate

Match the cash-block pane's wording. Do not change bill/income generation
beyond card-payoff presentation.
