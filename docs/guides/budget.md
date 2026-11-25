# Budget: the plan and how it's tracked

The Budget page owns the whole plan: income → bills → budget categories
→ savings → whatever's left. The page opens with that plan in one line,
then **This month** — the plan tracked in its own categories — and
below it **Your plan**, the planner, so the plan and reality speak one
language.

## The planner

- **Income** — the planner and the line above it always plan on the
  same income. Until a paycheck is tracked it is one field, "Expected
  income / month" (take-home), suggested from the paychecks in your
  ledger as the average of what landed over the last twelve months (the
  same figure the Bills page shows). With an active income scenario the
  planner shows the scenario's take-home instead, and you change it under
  Income scenarios. Once an income series is tracked on the Bills page,
  the planner shows this month's actual income (not typeable; you change
  it on Bills): the deposits that
  have landed plus the paychecks still due before month end, each at
  your latest deposit amount (the line under the plan shows the two
  halves; a paycheck already posted as pending counts as landed), so a three-paycheck month shows three checks and a check that
  changed carries its new size forward. A check counts for the month
  it pays, not the day it lands: a paycheck due Sunday the 1st that the
  bank deposits on Friday the 30th is the next month's income. The
  budgeted field stands in
  for households with no income series. Named scenarios
  ("full salary", "sabbatical") live in the collapsed **Income
  scenarios** card below the planner. The one marked Active drives the
  cash forecast and the daily email: the paycheck it describes is walked
  in at no more than its take-home, so a lower take-home applies at
  once. It sets the plan's income (and with it excess cash) only while
  no income series is tracked on the Bills page; once one is, the plan
  counts this month's own checks.
- **Bills** — read-only here, averaged over the year (a $600 annual
  premium shows as $50/mo). Bills are edited on the Bills page —
  one owner for bill data.
- **Budget categories** — Food and Everything else are the two fixed
  buckets the verdict judges. You can add custom categories (Gas,
  Pets, …) that carve spending out of one of them. Setting a category
  to $0 keeps it and its matching rules — $0 is a plan; the row's
  remove button is what removes a category.
- **Savings** — a planned $/month. It reduces excess cash and appears
  in the forecast as an outflow, but never affects the daily verdict.
  The planner lists every fixed-plan goal's monthly plan above the excess
  line, and the excess subtracts all of them, not only the goal edited here.
  Until you type one, the box shows a suggestion: what an ordinary month
  leaves after bills, budgets and your other goals, measured on what your
  paychecks usually bring in — not on this month alone, so a refund or a
  third paycheck this month does not become a standing plan. When no
  paychecks have been found yet, the suggestion is measured on the
  take-home you type in the income box, and follows it as you type.
  Typing in the other boxes does not move it; the excess line shows what your plan
  leaves. Your own number always wins, and an explicit $0 is remembered —
  the field won't silently refill; a "suggested $X" chip appears when the
  suggestion differs.
- **Excess cash** — the named residual: income − average bills −
  budgets − fixed savings plans. It stays in checking (the forecast
  shows it accumulating); it is not spendable headroom in the verdict.
  Sweep-mode savings plans (below) draw *from* this surplus when a
  month provides it, so they don't subtract here. The line is absent
  until income is set — without a top line there is no residual to name.

While anything in the planner is unsaved, an **Unsaved changes** bar
stays on screen with **Discard** (back to what is saved) and **Save
budgets**.

The planner prefills each category from your history (its suggestion
chips say "3-month median"). The **Suggest from history** button in
the Bucket rules card proposes a budget per bucket from roughly the
last six finished months: the median month, rounded to $10, with
outlier months more than twice the median dropped (the month in
progress counts only for a bucket no finished month has spending in)
— and for a category that shows up in only a few of those months, the
median of the months it did. A category is offered as a budget of its
own only when it had spending in at least half of those finished months:
a one-off purchase (a set of tyres, a laptop) is never proposed as a
monthly budget at its full price — it stays inside Everything else,
which already counts it. You approve each suggestion; nothing applies
itself.

A ledger shorter than a month (a new account, a first file import) has
no complete month to take a median of, so its suggestions are the
spending of the finished days seen so far, scaled to a whole month, and
the planner says so: "Based on 9 days of history — scaled to a month".
Under a week it adds that the figure will move and to revisit it after
your first full month; with not one finished day yet it offers nothing
and says to come back tomorrow. Until you save a budget, those figures
are only suggestions: no page, lens or email judges a month against
them.

A month whose ledger begins partway through is paced from its first
day, not from the 1st: the days before your first transaction were not
days of no spending, they are days the app never saw. That month may
spend the budget's share for the days from the first transaction to
month end, and the verdict, the Today tiles, the hero's "$X of $Y left",
and the forecast all read that same window, so spending exactly what
the tiles hand out never reads as over. (The Month lens's per-category
rows still show each category's whole-month budget.) From the next month
on, the whole budget applies again. Wherever the variable-spending
progress uses a different budget from the plan's, it says why beside the
figure: "scaled to $X this month" when the dynamic budget compressed it,
"paced from mm/dd" when the ledger began inside the month.

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
- **Food** — the **Food and Drink** category, plus rows recategorized to
  **Amazon - Food & Drink** or **Costco - Food & Drink** by receipt
  matching (your recategorizations move rows in or out). A matched
  receipt with items of several kinds is split by those items, so only
  the groceries half of a mixed warehouse run lands here — see
  [splits from a receipt](transactions.md#splits-from-a-receipt).
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

On Today, Budget and the lenses the pools are their own **Envelopes**
row under Bills — what they paid out of what they held — and the Bills
row is the bills with due dates. The plan itself does not change: its
bills leg holds both ("bills & envelopes"), and the month's plan, the
verdict and the excess read the same as before. A yearly pool set up
during the year counts spending from the month it was set up; one set
up before this was so counts from January 1.

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
self-reported. Goals sharing one account split its transfers so each
counts once: a transfer goes to the first goal, in your list's order,
whose match words all appear in it, else to the first goal with no
words. So a transfer whose text carries two goals' words counts for the
first of them only, and a second goal with no words reads $0 until you
give it words of its own. A goal can carry a target amount and date. A goal with
both is **on pace** when what you have saved is at least what a straight
line, from the day you set that target and date to the date itself, asked
for 38 days ago, however your deposits arrive (weekly, monthly, a
windfall). It reads **off pace** when you are further behind than that,
and on pace again once you are back within a week of the line; changing
the target or the date starts a new line from that day. Four days after
its target date, a goal that has not reached its amount reads off pace.
A goal with no target shows what your last 3 months saved ("$1,000 in
the last 3 months" — from the same day three months back, so each
month's deposit counts once). A goal with a target but no date shows
roughly when you would arrive at the rate of your last 3 months of
contributions.

The account picker lists the accounts a goal can measure: open checking,
savings and other cash accounts, and investment accounts whose ledger
has rows (a holdings-only account never shows a transfer) — each with
its last four. Hidden accounts and the backup copy of a linked pair are
not offered; an account a goal already uses stays listed. The plan's
own **Savings** line, while it is plan-only — no account, target, date
or match words — is shown as the plan's savings line: its amount is set
on the planner, and an account picker beside it chooses the account the
savings land in. Once it has an account (or a target, date or words),
it is measured and edited like any other goal.

A goal with **no account (plan only)** is not measured: its monthly plan
still lowers excess cash and rides the forecast (the forecast always
treats it as still owed), but it shows no progress. Match words need an
account. On their own they would match both sides of every transfer, the
money arriving in savings and the money leaving checking, and nothing
says which side is the fund. So a goal with match words and no account
cannot be saved (the account picker labels the empty choice *no
account (plan only)*); pick the account the money lands in, or clear the
words. A goal already stored with match words and no account keeps its
plan, can be saved unchanged, and shows "match words need an account" on
Today and here until you pick one.

A goal whose destination is a **business** account belongs to that
business: its progress counts the business's own money in that account,
and its monthly plan does not come out of the household plan surplus,
the household forecast or the sweep line (unless the combined
household-and-business view is on, where the household figures include
the businesses anyway).

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
- Savings goals, custom categories and income scenarios are each saved
  as a whole list, and an edit made on one device is merged onto what
  another device saved in the meantime, so two people editing different
  goals both keep their change. Only if the list moves again while that
  merge is being saved does the save stop with *"Changed on another
  device. Reloaded the latest; make your change again."* — the screen
  then holds the stored list, and your change did not land.
- Bills shown here are the evened yearly average; the actual heavy and
  light months show up in Today's irregular-bills list and the
  forecast, not as a broken plan. Income is the month's own, so
  the **excess** line moves with it — a third paycheck is excess that
  month.
- A charge split by hand on the Transactions page lands in the buckets
  part by part — the grocery half of a big-box run is Food, the rest is
  Everything else (or a custom bucket that names its category).
