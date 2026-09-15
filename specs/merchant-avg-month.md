# Merchant history average-per-month

Status: SHIPPED. Kept as the design record.

## Goal

On merchant drill-down (`/recurring/history?payee=…`), show average spend
per month next to the lifetime total.

## Decisions

- **Formula:** mean over **months that have activity** only (not empty
  months in the span). Label: **Avg / mo (active)**.
- Optional: show count of active months.
- Display-only; may compute client-side from existing `monthly[]` or add
  `avg_monthly_active` on the API for easier tests.

## Touch

- `webapp/src/pages/BillsHistory.tsx`
- Optional: `web/api.py` recurring history

## Done when

- Sparse merchants are not understated by empty months
- Test covers the formula
- Suite green

## Out of scope

Median mode, future spend prediction.
