# Cash Flow page rework

Status: SHIPPED. Kept as the design record.

## Goal

One continuous cash graph: **all historical actuals** + **90-day forward
forecast** on the same axis.

## UX

- Current calendar year **expanded by default**; older years collapsible
- Actuals vs forecast visually distinct (e.g. solid vs dashed)
- No second money model: forward series from `engine/forecast.py` events
  (bills, income, pace / card scenarios as currently exposed)

## Touch

- `webapp/src/pages/CashFlow.tsx`
- Cash-flow / history API routes in `web/api.py`
- `engine/forecast.py` only if a small export helper is needed

## Done when

- Graph shows full history + 90d forward
- Overlapping horizon reconciles with Today forecast
- API tests for shape; suite green

## Out of scope

- Scenario consolidation (coordinate if series keys change).
- Investment performance charts.
