# Household ownership

Owner labels answer one question about any account or transaction:
**whose money is this?** Yours, mine, ours, the kids' — any labels you
like — as a way to *view* household money, never a way to change it.

## The model

- Every account can carry a default **owner** label — free text, set in
  the account's edit row on the Accounts page. "Yours / mine / ours" is
  the idea, but names work just as well.
- Every transaction **inherits its account's label**. A per-transaction
  override wins when set — though there's no in-app control to attach,
  change, or clear one yet. Effective owner = the override if there is
  one, else the account's label.
- **No label = unattributed.** Unattributed money appears in the
  all-owners (household) view and in no individual owner's view.

Because the effective owner is computed at read time, changing an
account's label reattributes all of its transactions immediately —
except the ones with their own override, which keep it.

## A lens, never a ledger change

Ownership is a **reporting and filtering dimension only**. The verdict,
the budgets, the plan, the forecast, and the spend math never read
owner labels — attaching, changing, or clearing labels changes
**nothing** about what the app says you spent or whether you're on
budget. The household stays one budget; this is guaranteed by a test,
not just a convention. If you want spending actually treated
differently, that's a category or reimbursement question, not an
ownership one (see the concepts guide).

## Filtering transactions

Once at least one owner label exists, the Transactions page grows an
**owner** dropdown listing every label in use — account defaults and
per-transaction overrides both, alphabetically. Pick one and the month
view shows only that owner's transactions.

The lens applies to the **month view only** — a search runs across all
owners regardless of the dropdown.

## Net worth by owner

The Net Worth page always shows the **household** view — every account,
labeled or not. Per-owner net worth isn't in the app yet — the page has
no owner selector today. When an owner view is totaled, it counts only
that owner's live accounts (debts negative, same as always). The
individual views add back up to the household only once every account
carries a
label — an unattributed account counts in the household total but in
no individual owner's total.

Property, vehicles, and the historical trend are **household-level** —
they carry no owner label, so an API owner view shows only that
owner's account total and omits the property layer and the trend
chart. For what the full report includes, see the net worth guide.

## Not the same as member roles

Owner *labels* are unrelated to the **household roles** (owner, member,
view-only). Roles are access control — who may sign in and what they may
change. Labels are attribution — whose money a balance or charge
represents. Anyone can use the owner filter, but setting or clearing an
account's label — in its edit row on the Accounts page — is an edit, so
it takes the owner or a member.

## Gotchas

- Labels are exact-match text: "Alex" and "alex" are two different
  owners. Pick a spelling and stick to it.
- The owner dropdown is invisible until the first label exists — label
  an account on the Accounts page and it appears.
- Unattributed transactions vanish from every individual owner's view.
  If someone's total looks light, look for unlabeled accounts in the
  all-owners view.
- The search box ignores the owner lens — search results always span
  the whole household.
- An owner's net worth view (API only) intentionally has no property
  or trend; only the household view carries them.
