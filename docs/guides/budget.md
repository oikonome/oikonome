# Budget: the plan and how it's tracked

The Budget page owns the whole plan: income → bills → budget categories
→ savings → whatever's left. The page opens with that plan in one line,
then **This month** — the plan tracked in its own categories — and
below it **Your plan**, the planner, so the plan and reality speak one
language.

## The planner

- **Income** — one field, "Expected income / month" (take-home),
  prefilled from the paychecks detected in your ledger. Named scenarios
  ("full salary", "sabbatical") live in the collapsed **Income
  scenarios** card below the planner; the one marked Active powers
  budgeted income, the cash forecast, and excess cash.
- **Bills** — read-only here, averaged over the year (a $600 annual
  premium shows as $50/mo). Bills are edited on the Bills page —
  one owner for bill data.
- **Budget categories** — Food and Everything else are the two fixed
  buckets the verdict judges. You can add custom categories (Gas,
  Pets, …) that carve spending out of one of them.
- **Savings** — a planned $/month. It reduces excess cash and appears
  in the forecast as an outflow, but never affects the daily verdict.
  An explicit $0 is remembered — the field won't silently refill; a
  "suggested $X" chip appears when the computed leftover differs.
- **Excess cash** — the named residual: income − average bills −
  budgets − fixed savings plans. It stays in checking (the forecast
  shows it accumulating); it is not spendable headroom in the verdict.
  Sweep-mode savings plans (below) draw *from* this surplus when a
  month provides it, so they don't subtract here. The line is absent
  until income is set — without a top line there is no residual to name.

The planner prefills each category from your history (its suggestion
chips say "3-month median"). The **Suggest from history** button in
the Bucket rules card proposes a budget per bucket from roughly the
last six months, the month in progress included: the median month,
rounded to $10, with outlier months more than twice the median dropped
— and for a category that shows up in only a few of those months, the
median of the months it did. You approve each suggestion; nothing
applies itself.

## Fixed vs variable

Every transaction lands in exactly one of:

- **Fixed** — matched to a recurring bill (merchant tokens + amount
  tolerance, one transaction per scheduled occurrence). Tracked against
  the schedule; never drives the verdict. Matching reaches into next
  month's schedule (rent autopaid on the 28th for the 1st is this
  month's fixed spend), and a fee that rides with a matched charge — a
  processing fee a few days after the payment — belongs to the same
  occurrence rather than reading as a stray purchase. A bill you track
  by hand as a card payment is left out while a linked card carries a
  balance; the card's own payoff covers it.
- **Food** — the **FOOD AND DRINK** category, plus rows recategorized to
  **Amazon - Food & Drink** or **Costco - Food & Drink** by receipt
  matching (your recategorizations move rows in or out).
- **Everything else** — all other variable spend.

Custom categories are display carve-outs of Food/Everything else: the
verdict still compares total variable spend to total variable budget;
the categories are the *why* (bars, email lines, over-pace callouts).

## Envelope bills

An envelope is a **cap, not a schedule** — for spending that's bill-like
in size but irregular in timing (groceries at one store, vet visits).

- **Monthly envelope** — a pool of $X per month. Matched spending
  counts as fixed up to the cap; anything past the cap overflows into
  variable spending (and the verdict). The overflow keeps the purchase's
  date, and the swipe counts once — the part inside the cap as fixed,
  the rest as variable. An envelope matches by merchant; give it a
  category on the Bills page and every row in that category counts
  toward the pool too, even when the merchant doesn't match (a vet, a
  groomer, a pet store).
- **Annual envelope** — one pool for the whole **calendar year**
  (resets Jan 1), for lumpy annual costs: insurance, domain renewals,
  car repairs. The plan carries pool ÷ 12 every month, but a $300
  renewal in March is recognized in full as fixed spend that month —
  it never reads as variable overspending. What's left of the year's
  pool shows on the bill's detail. A charge that a scheduled bill
  occurrence claims is the bill's, and never draws from the pool.

Either way the cap is the promise: spend past it and the overflow is
judged like any other variable spending.

## Bucket rules

The collapsed **Bucket rules** card holds the advanced pieces:
per-category matching (a category = a set of transaction categories
plus optional merchant rules — a merchant rule wins over the category),
carving out of Food instead of Everything else, the **Suggest from
history** button, and the **dynamic budget** toggle ("adapts to the
month so far"). When dynamic is on (the default) and a month's bill schedule
is heavier than income covers, your variable budgets are compressed
proportionally so the month still breaks even — shrink-only; light
months stay capped, and the surplus is your savings.

## Savings goals

Named funds (trip fund, emergency fund) live here too. A goal matches
transfer transactions into its destination account (optional
description tokens let several goals share one account), plus an
optional starting balance — progress is measured from the ledger, not
self-reported. A goal can carry a target amount and date; pace projects
from your trailing three months of actual contributions.

Each goal has a **contribution mode**:

- **every month (fixed plan)** — the default. The plan is a commitment:
  it reduces excess cash and the forecast walks it out like a bill,
  whether or not the month went well.
- **only when the month worked out (sweep)** — for the habit of moving
  money to savings only when the rest of the budget held. At month end
  the goal contributes min(plan, the month's realized surplus) — a month
  with no surplus contributes nothing. A sweep plan does **not** reduce
  excess cash, and the forecast never shows sweep money leaving that the
  month's own numbers say cannot leave — so a sweep goal never makes the
  runway read worse than having no goal at all. Today (and the daily
  email) show the state as "sweep up to $X — $Y available so far this
  month" — or "sweep up to $X at month end" while the month's
  availability isn't known yet — flipping to "swept $X to savings this
  month" once the real transfer posts; the posted transfer is what
  marks the month done.

## Gotchas

- A category with spending but a $0 budget shows amber "no budget set",
  not red — red is reserved for over-budget.
- The planner and Bucket rules edit the same underlying config; save
  in one and the other refreshes.
- Bills shown here are the evened yearly average; the actual heavy and
  light months show up in Today's irregular-bills list and the
  forecast, not as a broken plan.
