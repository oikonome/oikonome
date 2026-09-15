# Page relocations & renames

Status: SHIPPED. Kept as the design record.

## Ship (all four)

1. Today: hide Savings goals card when there is nothing useful to show
   (no trackable goals; respect $0 Excess cash marker rules).
2. Move **Savings goals** management from Settings › Planning → **Budget**
   page (owner-gated).
3. Move **Property, vehicles & mortgage** (`manual_assets`) from Settings
   → **Net Worth** with add/edit/delete.
4. Settings: rename **Household → Users** (prefer label/title; keep route
   id only if needed for deep links).

## Touch

- `webapp/src/pages/Settings.tsx`, `Budget.tsx`, `NetWorth.tsx`, `Today.tsx`
- Docs that still say “Household” if user-facing

## Done when

- Settings no longer edits goals or manual assets
- Budget / Net Worth own those editors
- No empty goals card on Today
- tsc + suite green

## Out of scope

Auto-valuation; Net Worth chart redesign.
