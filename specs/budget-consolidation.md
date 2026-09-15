# Budget lives on the Budget page, not in Settings

Status: built.

## Why

The Budget page gave the product one place to plan: the planner (income → bills →
categories → savings) plus this month tracked in the plan's own language. But
Settings kept its own "Budgets & income" section, so the product
had **two** places to edit a budget, with two different money models — the
planner's carve-out model and Settings' flat food/other/income fields. Same
config underneath, two contradicting stories on top. The Budget page is the
budget, so the budget leaves Settings.

## Decisions

1. **Custom buckets → an advanced card on the Budget page**, not deleted.
   The planner edits "Everything else" carve-outs as plain rows, but it can't
   express two things the old editor could: buckets that carve out of **Food**,
   and per-bucket **category/merchant** matching. Those are real capabilities,
   so they get an escape hatch (`<details>`, collapsed) rather than a grave.
2. **Income scenarios and recurring-bill tweaks → the Budget page too.**
   Neither is really settings: scenarios are the income that drives the plan,
   tweaks are the bills the plan spends against. Settings loses the section
   outright — no renamed remnant.

## What moved

| Was (Settings § Budgets & income) | Now |
|-----------------------------------|-----|
| Food / Everything else / Budgeted income fields | **deleted** — the planner owns these numbers |
| Dynamic variable budget toggle | Budget → Fine-tune |
| Custom buckets editor | Budget → Fine-tune (collapsed) |
| Income scenarios card | Budget page (`components/IncomeScenarioCard.tsx`) |
| Recurring-bill tweaks card | Budget page (`components/RecurringTweaksCard.tsx`) |

`Balance.tsx` went with it: pointed `/balance` at `/budget` but left
the page file orphaned. The redirect stays (bookmarks, older docs); the dead
page is gone. Today's "balance the budget →" link now goes straight to
`/budget` instead of through the redirect.

## Mechanics worth knowing

- **No API change.** `settingsSave` bodies were always partial; each card
  sends only its own keys. Fine-tune deliberately does **not** send
  `food_monthly` / `other_monthly` / `budgeted_income_monthly` / savings —
  those are the planner's to write, and sending them would fight it.
- **Two editors, one `custom_buckets`.** The planner and Fine-tune both write
  that key, and each seeds its form once on mount — so after one saves, the
  other's state is stale and its next save would clobber what just landed.
  The Budget page remounts the sibling (via a bumped `key`) *after* the
  settings refetch lands, and only on a real save — never on a background
  refetch, so nobody loses what they're typing.
- **Viewer chrome:** the moved cards are owner-only, as they were in Settings
  (`me.role !== "viewer"`); writes are 403'd server-side regardless.
- Settings' `active` section now defaults to `SECS[0]` rather than the
  hard-coded `"budgets"`, and its once-shared budgets/email form is email-only.
