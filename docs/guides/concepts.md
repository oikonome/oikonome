# Concepts: the rules the numbers follow

A handful of conventions explain almost every "why does it say that?"
moment.

## Sign convention

Inside the engine, **positive = money out** (the aggregator
convention). Display layers flip signs where a human expects it, but if
you ever export data or read raw amounts, spending is positive and
income is negative. If an import shows spending as income, the file's
sign option was wrong — roll it back and re-import (see the import
history guide).

## What counts as spending

Spending deliberately **excludes**:

- credit-card payments (the purchases were already counted when they
  happened — counting the payment too would double-count),
- transfers between your own accounts (moving money isn't spending),
- rows on loan accounts (the payment from checking is the spend),
- shadow copies of multi-source accounts (see the Accounts guide),
- reimbursed amounts (fully reimbursed pairs drop out; partial
  reimbursements net the received money off the charge).

Mortgage and loan **payments do count** — money left, permanently.

## One merchant, many descriptors

Banks describe the same payee many ways — a processor prefix here, a
reference number there — so one real-world business can arrive as a
dozen different strings. The app maintains a **canonical merchant**
map: a deterministic cleanup strips payment-processor wrappers,
reference clauses, and trailing id junk, keeping the real name tokens.
The cleanup is conservative on purpose — it **under-merges** rather
than risk fusing two different businesses.

Everything resolves through the canonical form: reports group by it,
top-merchant lists label with it, and **category rules key on it** — so
a correction made on one descriptor variant reaches every other variant
of the same payee, past and future. The raw strings are never modified;
the map sits alongside them, so it's reversible.

## Learned categories vs your corrections

The aggregator categorizes every transaction and is trusted by default.
On top sit merchant-level **category rules** in three tiers — the same
three groups the Rules page shows: **built-in** seed rules (always on,
no LLM needed), **model-learned** rules (optional, needs an LLM — see
the FAQ), and **your rules** (created by your corrections). Two
invariants hold everywhere: built-in and model rules only ever sharpen
vague buckets and never override a specific aggregator category, while
your rules apply merchant-wide, past and future, over anything; and no
rule of any tier ever touches transfers, loan payments, or income (the
spend exclusions above stay intact). Full mechanics — provenance,
promotion, disabling, conflicts — are in the rules guide; how a single
correction becomes a rule is in the transactions guide.

One category decision is made before any rule tier runs, from the bank's
own words rather than the aggregator's guess: when a statement line
names a chain and then the pump ("NORTHWIND GAS"),
the charge is filed as fuel and shown under the chain's fuel arm — a
tank of petrol sitting inside a grocery budget makes both numbers
wrong. Your own overrides still outrank it, as they outrank everything.
The merchants guide has the detail, and the page to undo it on.

## The verdict is variable-only

Fixed bills never make you "over": they're obligations with their own
schedule, recognized at payment, tracked against the plan. The daily
verdict compares **variable spending** (Food + Everything else) to its
prorated budget with a tolerance of max($50, 5% of expected-so-far).
The proration counts the days that have elapsed — today is not one of
them — and a closed month expects the full budget. Early in the month
(the first tenth) "under" reads as on budget, since barely any month
has passed to spend in; "over" is never suppressed. The full math is in
the Today guide.

## Envelope pools

An envelope caps bill-like-but-irregular spending: a monthly pool, or
an annual calendar-year pool for lumpy costs. Up to the cap it's fixed
spend; past the cap it overflows into the variable verdict, keeping
the purchase's own date, so the swipe is counted once, on the day it
happened. The plan always carries pool ÷ months, so an annual pool budgets evenly while
recognizing each lump in full when it lands. Details in the Budget
guide.

## Excess cash

Excess cash = income − average monthly bills − variable budgets −
fixed savings plans (sweep-mode goals draw *from* the excess, so they
don't subtract). "Average monthly bills" evens yearly bills out (an
annual premium counts as one-twelfth each month), which is why a
balanced plan shows roughly $0 excess everywhere while individual
months still swing — the swing shows up in the irregular-bills list
and the forecast instead. Excess cash stays in checking and is **not**
extra verdict headroom; if you want it working, give it a name as a
savings plan.

## Balance reliability

File imports carry transactions, not balances — a file-only account
sits at $0 until you set its real balance on the Accounts page.
Because headroom and the forecast are computed from balances, the app
**suppresses** the forecast and cash warnings while any balance is
still the unset default, rather than presenting numbers seeded from
$0. Connected accounts refresh balances every sync; manual accounts
trust what you typed.

## Reaped connections

A **Plaid** connection stuck in a state only you can fix (login
expired, consent revoked) is released after 30 straight days and
archived locally; accounts and history are untouched and reconnecting
picks up where it left off. Other connection types are never
auto-reaped. Full details in the accounts guide.

## Owners

Accounts can be tagged with an owner, and reports offer a per-owner
lens — a view filter, never a change to the money. Individual
transactions can also carry their own owner override on top of the
account's default.

## Gotchas

- Canonical merchant cleanup runs nightly; a brand-new descriptor may
  display raw until the next nightly pass groups it.
- A reaped connection stops syncing but keeps every transaction it
  ever brought in — history never disappears with the connection.
- Recategorizing a single transaction doesn't just pin that row — it
  also teaches a merchant-wide rule (flow categories excepted).
