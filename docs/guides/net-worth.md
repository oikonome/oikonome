# Net Worth

## Net worth now

The headline number is exact: the sum of live account balances (debts
negative), plus any property, vehicles, and other manual assets you've
added — those are editable right on this page. Accounts linked as
multiple sources of one real account count once (see the Accounts
guide). Beside the total, **+$X this month** is the change in that
total since the end of last month — property included, so a mortgage
payment counts only the interest it cost, not the whole payment that
left your checking account. Two tiles split it into **Financial
accounts** (cash + investments − card debt), whose own **this month**
line is the change in the financial accounts alone, and **Property &
vehicles, net** (after the mortgage payoff). The property layer's past
values come from its nightly record, or for a connected loan from the
principal paid on it: rows the lender marks as principal, or each
payment split by the principal-paid-this-year figure the lender
reports. A loan whose history shows only whole payments, with neither,
leaves the months the nightly record does not cover unknown, so no
headline change shows until the record covers last month. Manual assets hold
their value until you edit them.

A connected loan account typed as a **mortgage** moves into the
property layer alongside your real estate and, once linked, takes the
place of any manual mortgage entry you had typed in — you do not need
to delete the manual one. Other loans (auto, student) stay where they
were and never displace it.

## Net worth over time

The chart draws **financial accounts only** (cash + investments − card
debt); property and its loan are left out, so its **This month** pill
matches the Financial accounts figure above, not the headline's change,
which includes property. The trend line is a **reconstruction, not a
recorded history**. Every
account is anchored to its exact current value and walked backward:
investment accounts through their share-level transaction history
valued at historical prices (so market growth and crashes are
captured, not just contributions), clamped to the day each account
first existed. Cash and card balances are **not** walked through their
flows — each is held constant at today's balance across the whole
history, and only transfers into or out of your investment accounts
offset it (so money that moved from checking into a brokerage is not
counted twice). The part of an investment balance its holdings do not
explain — a cash sweep, an unpriced fund, a plan that reports only a
total — is carried as a constant from the account's inception, the same
way. Transfers between your own accounts are netted out, so a
round-trip does not read as a bump. Where recorded month-end snapshots
exist, they pin the line.

Everything before the first recorded month is drawn dashed with a faint
tint, and the solid line takes over at that boundary; there is no
graduated shading, and an old stretch is not drawn any softer than a
recent one. Expect the recorded tail to be exact and the reconstructed
years to be approximate. **This month / 1m / 3m / 6m / 1y / 3y / 5y /
All** pills pick the window, and the window's gain shows in both dollars
and percent. The series is one point per month, so the two short windows
are two points each: **This month** is the change since the last
month-end, and **1m** is the last complete month's change on its own.

## Retirement & investments, and Coinbase

When long-term investment accounts carry a cost basis or contribution
history, a **Retirement & investments** table lists each account with
**Invested**, **Value now**, **P/L** and **Return**, grouped by
provider with a blended **Total**, and the headline gain rides the
title row. A Coinbase connection gets its own table — **Cash in
(bank)**, **Cash out**, **Value now**, **P/L**, **Return** — anchored to
the money that actually left and returned to your bank, so transfers
between wallets do not count as new money. Either section stays hidden
when there is nothing to show.

## The retirement projection

The Retirement page projects whether the portfolio funds your
retirement and replays the plan against every historical start year
since 1928 — the full model, its assumptions, and the success-rate
math are in the retirement guide.

## The debt payoff plan

The **payoff plan** link on the Financial accounts tile opens the Debt
page: every card and loan on one schedule, avalanche or snowball, with
an extra-payment what-if and the debt-free month — the debt guide has
the model.

## Investment fees

The fees section answers one question: **how much have your investment
platforms charged you, where, and when?** It totals every charge it can
identify as a fee — advisory, admin, participant, account — on your
**investment accounts only** (brokerage, retirement, crypto), across
your whole transaction history. Bank, card and ATM fees are ordinary
spending and are not counted here; they show up on Spending like any
other charge. The section is hidden entirely when nothing matched.

### What counts as a fee

A transaction on an investment account is surfaced when the standalone
word **fee** or **fees** appears in either:

- the provider's **detailed category** for the transaction, or
- the transaction's **name/description**.

For the name/description, "fee" or "fees" must be surrounded by spaces
(or sit at the very start or end of the name). "Coffee" in a name does
not qualify — but neither does "fee" joined to punctuation, like
"MONTHLY FEE." or "FEE-REVERSAL". Category matching is
underscore-delimited, so a COFFEE category does not qualify either.

Two consequences of how the match works:

- Detection reads the transaction's **original** detailed category and
  its name — not your category override. Recategorizing a transaction
  neither adds it to nor removes it from this section.
- **Interest is not detected.** An interest charge appears here only if
  its description happens to carry the word "fee"; otherwise it counts
  as ordinary spending like any other charge.

Deleted/removed transactions are excluded.

### The math

- The headline total sums the **signed** amount of every matched
  transaction, all time: a charge counts as money out, and a refunded
  or rebated fee arrives with the opposite sign and nets off the total
  rather than adding to it.
- The **Investment fees** table (by **Platform**) groups by the institution of the account each
  charge posted to, with the count of matched transactions. On an
  estimated advisory line that count is the number of **years**
  estimated, not transactions.
- **Fees by year** is the same sum bucketed by calendar year.

It is computed fresh on every page load from your current ledger —
there is nothing to refresh.

### The estimated advisory line

A robo-advisor's exported data usually carries no fee rows at all — its
advisory cut simply comes out of your balance, invisibly. If you tell the
app which of your institutions charge one, the report adds an
**estimated** line per institution, labeled like "Evergreen Robo advisory
(estimated 0.25%)" in the platform table:

- The rate you set, applied to the average of the institution's managed
  value at the start and end of each year (reconstructed from your
  holdings and transaction history).
- The current year is prorated by how far into the year it is.
- Years where the estimate comes to less than $1 are dropped.

The estimate is folded into the yearly bars and the total. It exists
only for the institutions you name; nothing is assumed about any other
platform, even ones that also don't itemize their fees.

Set it up under **Settings → Connections → Investment fees & basis** (the
same card is in the mobile app's Settings, under Connections):

- **Advisory fees** — one row per institution: a piece of its name (any
  case; it matches every institution whose name contains it) and the
  yearly fee as a percentage, `0.25` for 0.25%.
- **Fee-free accounts** — the last characters of any account at those
  institutions that is self-directed and pays no fee, comma-separated
  (2 to 8 letters or digits, as the bank shows them).
- **Money-in overrides** — an account's last characters and the dollars
  actually put into it. This replaces the feed's cost basis for that
  account where it is not trustworthy — lots transferred in-kind arrive
  with a near-zero basis — while the value stays live.

Clearing every row and saving switches the estimate (or the override) off.
Over the API the same three keys ride `POST /api/settings`, from an
owner's session or token only (anyone else gets 403):
`advisory_fees` (`{"name fragment": 0.0025}`, a fraction from 0 to 1),
`advisory_fee_exempt_masks` (a list of masks, each 2 to 8 letters or
digits, stored upper-case) and `investment_basis_override`
(`{"1234": 400}`, dollars of 0 or more). A key you send replaces that
section whole, so a script that reads, edits and writes back should also
send the value it started from as `<key>_base` (for example
`advisory_fees_base`): when the stored section has changed since, the save
answers 409 and nothing is written, and the script reads again.

## Gotchas

- Accounts are classified by subtype and name (401k/IRA/Roth/…); if a
  balance lands in the wrong tax bucket, fix the account's type on the
  Accounts page. That same type decides whether a fee is counted as
  investment drag: a fee on an account typed as checking is spending,
  not a fee here.
- Fee detection is text-based. A fee your platform describes without
  the word "fee" ("SERVICE CHARGE", "ADMINISTRATIVE EXPENSE") is
  invisible here — it still counts as normal spending, just not as a
  fee.
- Conversely, a non-fee transaction on an investment account whose
  description contains the standalone word "fee" will be counted. You
  cannot recategorize it out, since the match ignores category
  overrides.
- Fund expense ratios (the fee inside an ETF or mutual fund's price)
  never appear in any transaction feed and are not estimated — the
  advisory estimate covers platform advisory fees only.
- The advisory estimate is exactly that — an estimate from reconstructed
  balances, not a statement figure.
