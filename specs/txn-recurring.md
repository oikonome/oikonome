# Transactions: recurring affordances

Status: SHIPPED. Kept as the design record.

## Goal

From the Transactions ledger, see what already counts as recurring and
create bills/envelopes without a detour through Recurring.

## Ship

1. **Indicator** on rows already matched to a recurring bill/envelope
   (⟳-style + tooltip with bill name).
2. Action on merchant/row: **Add as bill** or **Add as envelope** —
   prefill payee + recent amount; reuse existing create/save paths.
3. **Suggest cadence** in that flow: run existing ledger-fit /
   `analyze_bills`-style logic for **that merchant only**; prefill amount
   + frequency; **user must confirm** (never auto-save).

## Touch

- `webapp/src/pages/Transactions.tsx`
- `web/api.py` transactions payload (+ recurring flag/id)
- `engine/recurring.py` (suggest helper if not reusable as-is)

## Done when

- Matched rows show indicator
- Create bill/envelope from a transaction works end-to-end
- Suggest is optional and confirm-gated
- Tests for payload flag + suggest prefill; suite green

## Out of scope

Changing match math; silent auto-create of bills.
