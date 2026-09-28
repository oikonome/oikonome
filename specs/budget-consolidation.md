# Budget lives on the Budget page, not in Settings

The Budget page is the one place to plan: the planner (income → bills →
categories → savings) plus this month tracked in the plan's own language.
Settings has no budget section, so there is one editor and one money model.

## What lives where

| Capability | Home |
|------------|------|
| Food / Everything else / income numbers | the planner |
| Dynamic variable budget toggle | Budget → Fine-tune |
| Custom buckets that carve out of **Food**, or match by category/merchant | Budget → Fine-tune (collapsed `<details>`) |
| Income scenarios | Budget page (`components/IncomeScenarioCard.tsx`) |
| Recurring-bill tweaks | Budget page (`components/RecurringTweaksCard.tsx`) |

`/balance` redirects to `/budget` (bookmarks, older links); Today's
"balance the budget →" link goes straight to `/budget`.

## Mechanics worth knowing

- **No API change.** `settingsSave` bodies are partial; each card sends
  only its own keys. Fine-tune deliberately does **not** send
  `food_monthly` / `other_monthly` / `budgeted_income_monthly` / savings —
  those are the planner's to write, and sending them would fight it.
- **Two editors, one `custom_buckets`.** The planner and Fine-tune both write
  that key, and each seeds its form once on mount — so after one saves, the
  other's state is stale and its next save would clobber what just landed.
  The Budget page remounts the sibling (via a bumped `key`) *after* the
  settings refetch lands, and only on a real save — never on a background
  refetch, so nobody loses what they're typing.
- **Viewer chrome:** these cards are owner-only (`me.role !== "viewer"`);
  writes are 403'd server-side regardless.
