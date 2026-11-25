# What the debt payoff plan walks

The Debt page answers "when is the last payment, and what does the order
cost". It is only honest about debt the household actually carries from
month to month, and it has to see every debt the household told the
product about. Two rules decide that, and both live in ONE server
function: `engine/debt.load_plan_debts` (called by `debt.build`, the
`/api/debt-plan` reply). No client decides either.

## A card paid in full every month is not in the plan

A card whose statement is cleared each month never charges interest.
Walking its balance as revolving debt quotes a debt-free date years away
and interest nobody pays.

`debt.paid_in_full(raw, ledger, balance)` decides, per card:

- **Interest charged in the last 90 days** (an interest-charge row on the
  card in the ledger) means it is not paid in full, whatever else says so.
- **The issuer's statement**, when the aggregator sent one
  (`last_statement_balance`): paid in full when the last payment
  (`last_payment_amount`, made on or after the statement's issue date when
  both dates are sent) covers the statement balance, or the statement
  balance is zero. A statement the last payment did not cover means the
  card revolves. When the last payment predates the latest statement's
  issue date, that statement is not due yet and the previous cycle
  decides: the ledger's evidence below when it has it, else paid in full
  while the due date (`next_payment_due_date`, or 25 days after issue
  when none is sent) is still ahead. A $0 minimum on such a paid statement is the issuer
  saying nothing is due — evidence for this rule, not a missing figure.
- **The ledger alone**, when the issuer sent no statement: at least 90 days
  of the card's history, payments in at least two of the last three
  months, and payments over those 90 days at least the card's current
  balance.

Such a card leaves `debts` and is listed in `paid_in_full` with the
evidence (`basis`: `issuer` or `ledger`, the statement and the last
payment). The pages show the list under the plan as "paid in full
monthly — no interest". It is still money owed this month, so the
retirement projection keeps netting its balance out of cash.

## A hand-entered loan joins the plan

A mortgage or loan typed in on Net Worth (`manual_assets`, kind
`mortgage` or any negative value) is a debt. It joins the plan as an
`origin: "hand_entered"` row with the id `manual:<name>` (the id its
overrides are saved under), unless a linked mortgage account replaces it —
the same rule net worth applies to it, over the same accounts net worth
counts (`reporting._live_accounts`: a current balance, not a linked twin,
not a business's unless the combined view is on).

- **Payment**: the ledger's matching recurring payment
  (`debt.ledger_payment`): outgoing rows of the last 100 days whose name
  or merchant carries a distinctive word of the loan's name ("Northwind" in
  "Northwind Mortgage"; generic words like mortgage, loan, home are not
  distinctive), in at least two different months, at amounts within 15%
  of the latest. The latest amount is the minimum, `min_basis: "ledger"`.
  It can include escrow; the household can type the principal and
  interest in its place. No match: the usual estimate.
- **Rate**: asked once. Until the household types it (saved in the plan's
  per-debt overrides, like any other APR), the row walks at the typical
  rate, lowered to what the payment retires, and carries
  `rate_needed: true` so the page asks for it.

## Fields

Added only; older clients keep reading what they read.

- `paid_in_full[]` — the cards left out, with `basis`, `statement_balance`,
  `last_payment`.
- `debts[].origin` — `account` or `hand_entered`.
- `debts[].min_basis` — where the minimum came from, including `ledger`.
  `min_source` keeps its three values: a ledger payment reads as
  `estimate` there, so an older client still says the figure is not the
  bank's.
- `debts[].rate_needed` — a hand-entered loan with no rate typed yet.
- `alt_same_path` — the other method walks the same balances month by
  month (one line on the chart, not two).

## Older client, stale tab, restore

- An older client never sees the paid-in-full cards (they are gone from
  `debts`) and shows hand-entered loans like any loan, labelled estimate.
- A stale tab saving the plan keeps every stored override: the save
  re-reads the stored plan and `overrides` carries ids that are not
  listed, so a paid-in-full card's old override survives.
- A restore brings back `manual_assets` and the plan document; both are
  read at request time, so nothing is stored that could disagree.
