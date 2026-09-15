# Category rules

The Rules page shows every categorization rule the system is applying
and why. Rules are **learned, not hand-authored** — you never write one;
you correct, disable, or delete the ones the system made.

## Where rules come from

Four provenance groups, each labeled on its rows:

- **Your rules** — created by your corrections. Recategorize a
  transaction, or set a category from a merchant's history page, and
  that choice is saved as a rule of yours.
- **Model-learned** — the local model's classifications. When an LLM is
  configured (Settings), merchants the data source left in vague
  buckets (general merchandise / services / other) are classified once
  by the model and cached. This group only ever holds **untouched**
  inferences — touch one and it becomes yours. It hides entirely when
  empty, which is the normal state of an instance with no LLM. The
  standard self-host install bundles a local model, so classification
  stays on your box. If the backend stops answering — a model removed
  from the server, an endpoint that has gone away — this group simply
  stops growing, so the nightly run's failure is announced instead: an
  alert on Today and in the daily email, and a **last run** row under
  Smart categorization on the Doctor page (see the Alerts guide).
- **Learned** — a small classifier trained on your own corrections. Once
  it has seen enough of them, it fills vague buckets for merchants that
  resemble ones you already sorted, and stays quiet when unsure. Like
  Model-learned, it holds only untouched inferences, hides when empty, and
  a correction makes the rule yours.
- **Built-in** — deterministic seed patterns for well-known chains and
  payment-processor prefixes. These match on your first import and need
  no LLM at all.

The machine sources take turns in a fixed order over the merchants still
uncategorized: built-in patterns first, then the learned classifier, and
only what neither could place goes to the LLM.

"Your rules" is expanded by default; the other groups collapse to a
count. The search box spans all groups (merchant and category), and
each group pages through its rules about 50 at a time — nothing renders
thousands of rows at once.

## One rule per merchant

Rules key on the **merchant itself** — the row on the Merchants page,
not the raw descriptor. A payee that arrives under several descriptor
variants is one merchant to the rule engine, so one correction covers
every variant — past and future — not just the descriptor the corrected
transaction happened to carry. Renaming or merging that merchant
afterwards keeps the rule attached to it, because the rule points at the
merchant rather than at a spelling. (A rule for a merchant the ledger
has never seen still matches by name, so nothing is lost either way.)
The **Matches** column shows how many transactions the merchant currently
has.

## What a rule can and cannot do

- **Your rules apply merchant-wide, past and future, even over a sharp
  source category** — you know your own merchant better than the data
  source does.
- **Model and built-in rules only fill vague buckets.** They never
  override a specific category the data source already assigned. Your
  rules always outrank them.
- **No rule ever touches** a transaction's own per-transaction
  override. Model and built-in rules also never touch the flow
  categories (transfers, income, loan payments) that drive spend
  exclusions — see the concepts guide. **Your** rules do reach them: a
  Venmo, Zelle or PayPal payment to a person arrives labelled as a
  transfer by the bank, and a rule you set for that payee ("Venmo —
  Casey → Child care") moves those rows into spending too, now and for
  every future payment. The confirmation tells you how many
  bank-labelled transfers the rule will move, because that changes the
  budget math. Correcting a transaction *to* transfers, income, or loan
  payments doesn't create a rule — it sets only the per-transaction
  override — and a merchant-wide set to any of these is rejected
  outright.

## How a correction becomes a rule

Recategorizing one transaction does two things: it sets a permanent
override on that transaction, and it teaches the merchant map — every
past row of that merchant updates immediately and every future row
lands pre-categorized. A merchant-wide set from a history page shows
you the affected count first and is undoable.

## Editing, disabling, deleting

Each row (the owner or a member — a view-only login sees the list
read-only) offers:

- **edit** — change the rule's category. Editing also re-enables a
  disabled rule.
- **disable** — the rule is kept but stops applying. Rows it already
  categorized keep their category; re-enabling re-applies the rule to
  past rows.
- **delete** — the rule is gone. No promotion, no undo.

**Editing or disabling a model or built-in rule promotes it to your
rules** — the system made a guess, you made a call, so the rule moves
to "Your rules" and gains your-rule semantics. Re-enabling a promoted
rule keeps it yours. There is no group-level kill switch; disable is
per-rule only.

## Gotchas

- Deleting a model rule for a merchant the source still reports vaguely
  just puts that merchant back in the classification queue — the model
  may learn it again. **Disable** (or edit) is how you say "stop";
  delete is how you say "forget".
- Disabling a rule doesn't revert anything — rows it already
  categorized keep their category until something recategorizes them.
- When several rules resolve to the same canonical merchant, they apply
  only while they **agree**; a merchant whose rules disagree is left
  split rather than picking an arbitrary winner. A merchant-wide set
  from a history page resolves the split.
