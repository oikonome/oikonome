# Debt

The Debt page answers one question: **when is the last payment, and what
does the order cost?** Every credit card and loan carrying a balance goes
on one schedule, walked month by month until it clears. It is arithmetic,
not advice: the same balances, rates and payments always give the same
date.

Web: **Debt** in the left rail, right under Cash Flow (and the **payoff
plan** link on the Net Worth page). Mobile: **More → Debt**, under Cash
Flow.

## What is on the schedule

Every account of type **credit** or **loan** with a balance over 50
cents, except accounts the Accounts page marks **excl** (those stay out
of every report, and this one). A business entity's own accounts and a
manually-entered shadow of a linked account are not debts here either.

Two more kinds of debt are handled on their own:

- **A card you pay in full every month** is not on the schedule. It
  never charges interest, so planning its balance as debt would quote a
  debt-free date and interest that never happen. A card counts as paid in
  full when the bank's last statement was paid off (a $0 minimum on that
  statement means nothing is due) and no interest was charged in the
  last 90 days — or, when the bank sends no statement, when your own
  payments over the last three months cover its balance with no interest
  charged. A new statement that is not yet due does not put the card
  back on the plan: until its due date, the card is judged by the cycle
  before it. Such cards are listed under the plan as **Paid in full
  monthly — no interest, so not in the plan**, each saying why (*last
  statement $X paid in full*, or *your payments clear it each month*).
- **A mortgage or loan you typed in on Net Worth** (kind *mortgage*, or
  any item with a negative value) joins the schedule, marked **entered on
  Net Worth**. Its payment is read from your ledger — the payment that
  carries the loan's name (its lender's word, like "Northwind" in
  "Northwind Mortgage") in at least two of the last three months — and
  shows **from your payments**. Its rate is asked once: type it on the
  row and save. A linked mortgage account replaces a typed-in mortgage,
  as on Net Worth — only one Net Worth counts, so a business account,
  the backup copy of a linked pair or a mortgage with no balance
  replaces nothing.

Each debt needs two numbers beyond its balance, and the page says where
each came from:

- **APR %** — the bank's, where the connection reports liabilities (a
  card's purchase APR, a mortgage's rate, a student loan's rate): marked
  **issuer**. What you typed: **yours**. Neither: **est. — add yours**
  (the rate shows greyed in the box) — a typical rate for that kind of debt (22% for a card, 7.5% for a car loan, 6.5%
  for a student loan or a mortgage, 10% for any other loan), lowered
  where a known payment would not retire the debt at it. The page marks
  the debt-free date and the interest as **estimated** ("Interest to pay
  (estimated)", and how many debts are walked at a typical rate or an
  estimated minimum) until every debt has a rate and a minimum from the
  bank or from you; type them on the row and save.
- **Min $/mo** — the bank's minimum payment (**issuer**), what you typed
  (**yours**), or an **estimate**: a month's interest plus 1% of a card's balance, never under 2% of it (at least $25),
  a five-year payment for another loan, a thirty-year payment for a
  mortgage. The estimate exists so the page is never empty for a bank
  that sends nothing; correct it.
- A **mortgage** is planned on its principal and interest only. The
  monthly payment a servicer reports usually includes escrow (property
  tax and insurance), which never pays down the loan, so the planner
  works the principal and interest out from the rate and the remaining
  term the servicer sent (**issuer**), or, without a term, from the
  original loan amount on a thirty-year schedule. With neither, it keeps
  the servicer's whole payment, which overstates the principal and
  interest by the escrow. If your statement shows the principal and
  interest, type that figure in.
- A loan whose servicer sends a **maturity or payoff date** is planned to
  end that month: that date is the last payment. Whether this month's
  payment is still owed is read from the servicer's own dates — a next
  due date later this month (or today) means it is still owed, whatever
  was paid earlier in the month; a next due date in a later month means
  it is paid; with no usable due date, a payment reported this month
  means it is paid. Without either date, the payment counts as owed through its payment
  day (the maturity date's day of the month), that day included.

Type over either number in the table and **Save plan**. Clearing the box
goes back to the bank's number (or the estimate). **Skip** leaves a debt
out of the plan — a 0% promotional balance you are paying down on its
own schedule, a loan someone else pays — without excluding the account
anywhere else.

## How the walk works

Each month, every open debt accrues one month of interest (APR ÷ 12) and
receives its minimum. On top of that there is one **pool**: the **extra
$/mo** you chose, plus the minimum of every debt already paid off. The
pool goes to one target at a time, and the month that target clears, the
pool — now larger by its minimum — moves to the next. That rolling
minimum is what makes a plan accelerate, and why the last debt goes
faster than the first.

Two orders are offered, and the page always computes both so you can
see the difference:

- **Avalanche** — highest rate first. Pays the least interest in total.
- **Snowball** — smallest balance first. Clears a debt soonest, which
  some people find easier to keep up.

The order is fixed from today's balances. The **… instead** tile shows
the other method's debt-free month and interest under the same money,
and the chart draws both balance curves.

## Reading the page

- **Debt-free** is the month of the last payment under the chosen
  method and extra. **Not within 50 years** means the walk gave up; if
  it adds *the minimums don't cover the interest*, at least one balance
  is growing at the payments entered.
- **Paying monthly** is every minimum plus the extra — the cash the plan
  assumes leaves each month.
- **Interest to pay** is the total over the whole plan, with the saving
  against paying minimums only with nothing rolled forward (the
  baseline holds each minimum flat, so it is the cost of *keep paying
  what you pay now*, not of the issuer's shrinking floor).
- **In payoff order** lists each debt with its position, its payoff
  month, and the interest it will cost under this plan.

## What-if versus saved

Switching the method or typing an extra payment recalculates at once
without saving, and the hero says **unsaved what-if** while the numbers
on screen differ from the plan on file. **Save plan** stores the method,
the extra and every rate or minimum you typed, for the household — an
owner or a member can save; a viewer can run what-ifs but not keep them.

## Gotchas

- Minimums are held flat for the whole walk. A card's real minimum
  shrinks with its balance, so the minimums-only baseline finishes
  sooner here than an issuer's statement would predict — and the plan's
  own schedule does not depend on it.
- Interest is simple monthly accrual on the balance; no daily
  compounding, no promotional periods that expire, no fees.
- A balance the bank reports as negative (a credit) is not a debt and
  is left out.
- The cash forecast on Today pays cards off in full at their due date to
  test the next few weeks' cash; this page assumes the minimum plus the
  plan. The two answer different questions and will not agree on a
  card's payment amount.
