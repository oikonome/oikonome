# Today, Month, Year

The home page is one page with three timeframe lenses — **Today**,
**Month**, **Year** — that answer the same question at three scopes:
**was this period on budget?** Same engine, same verdict rules,
different window.

It doesn't have to be where you land, though: **Settings → Preferences → Home page**
lets the household pick where each client opens — a web route (Today,
Transactions, Bills, Accounts, Net worth, Budget, Cash flow,
Reimburse, Business) and a mobile tab — applied once per launch.
Tapping the logo still goes to Today.

## One verdict formula, three scopes

Every lens applies the same judgment (see the Today guide for the full
derivation):

- variance = actual variable spending − expected-so-far
- tolerance = the larger of $50 or 5% of expected
- within tolerance → **ON PLAN** (the emails write it "On budget");
  past it → **OVER BUDGET** or **UNDER BUDGET**

Expected-so-far is prorated to the days that have elapsed: for the
month in progress that is (day − 1) ÷ days in month, since today has
not elapsed yet; a closed month expects the full variable budget.

**Only variable spending is judged** — Food and Everything else (custom
budget categories are carved out of those two for display, so their
spending counts inside them). Fixed bills never make a period "over";
they appear in each lens's Bills row instead. A bill you track by hand
as a card payment drops out of the month verdict and the week summary
while a linked card carries a balance — the card's own payoff covers
it. As everywhere, spending shows as positive numbers (see the concepts
guide).

Category and bill names in a lens's tables open their rows — every
transaction in that category for the month (the whole year on the year
lens), bill charges included, or the bill's history — on the web and on
the phone alike. The clustered Amazon row stays plain text.

## Browsing the past

Every lens has **‹ ›** stepper buttons next to the title:

- **Today** steps a day at a time. Stepping back shows that day's page
  as of that date — verdict, daily numbers, map — under a "Viewing a
  past day" banner; the cash forecast, alerts and cash notice are
  hidden, since they describe now, not that day. The banner's
  "‹ back to today" link returns to the live view, as does stepping
  forward onto the real today. The **‹** button stops at your ledger's
  first date.
- **Month** steps a month, **Year** a year. Both also have dropdowns,
  whose range runs back to the first year with data.
- The **›** button disables at the current period — there is no future
  browsing (a future month or year shows as planned/blank, not judged).

Every view is deep-linkable: the URL carries the lens and date, so a
bookmark or a shared link lands on the same view.

## Month: the report card

The Month lens is the report card. When a month closes, its budget
and its bill schedule are **frozen into the month's budget snapshot**.
A past month is judged against that frozen budget — the one it actually
ran under, not today's numbers applied retroactively — and its card
expands its bills from those frozen rows, which is what lets it split
the month into bills paid vs planned, and why editing a bill or a
budget today leaves closed months alone. A closed month with no
snapshot is judged against the current budget, and the card says so:
"No budget snapshot exists for this month — the verdict compares it
against the current budget, applied retroactively." Months that closed
before the freeze existed likewise fall back to today's schedule
projected backward. (The engine never fabricates: a bill with no
history before its start date adds no expectations to months before it
existed.) The **current month** is in progress ("day 12 of 31") and
adds a pace projection: at the current rate, spending lands at
actual ÷ day of month × days in month (today's date, today included),
judged against the full variable budget with the month-end tolerance.

The card shows: the money map for the month, **bills paid vs planned**
(each posted payment, anything due but not posted, envelope usage and
overflow), **spending by category** (Amazon subcategories clustered
under one parent), the ten **biggest transactions**, and **income vs
spend** with the net. In a heavy-bill month the header notes that
variable budgets were auto-scaled (see the Budget guide).

## Year: the twelve-cell grid

The Year lens is twelve month cells: each finished month's **final
verdict and its margin** in dollars, the current month marked **proj.**
with its projection, future months blank. A finished month with no
budget snapshot gets no verdict: its cell carries a muted **net** pill
with the month's income − spending (or "spent $X (income unknown)"
without bank income data), a footnote under the grid explains it, and
the month is left out of the year's money map, since nothing was
budgeted. Clicking a cell opens that month's report card.

Below the grid: a money map summing the **elapsed months only** (a July
view shows seven months of plan, not a phantom full year), annual totals
— income, spend, saved, savings rate — and the top twelve spending
categories next to the prior year's figures, with a Total spend row. Annual income and spend use the Cash Flow
report's definitions (see the cash-flow guide). A year with no bank
income data shows income and savings as **unknown, not $0**.

## Which summary mirrors which lens

- The **daily email** mirrors the Today page — same content, same math
  (see the Today guide).
- The **weekly email** summarizes the **last completed Mon–Sun week**
  (per-day budget built from each day's own month allowance), sent on
  your chosen weekday. An envelope purchase sits on the day it
  happened — the part inside the cap as a bill, any overflow as
  variable spending — so the week counts the swipe once.
- The **monthly email** renders the **previous month's** report card,
  sent on the 1st.
- The **year in review** renders the previous year's Year lens, sent on
  January 1st.

Every summary is built from the exact same computation the lens fetches,
so the email and the page cannot disagree. Each cadence is switched on
and scheduled independently — and each can also go out as a **short SMS**
or a **push notification** to any browser you enable and any phone
registered through the mobile app, from Settings → Scheduled summaries.
Texts need a verified phone number and an instance with its own Twilio
account. The SMS and push texts are short forms of the same numbers;
they can't disagree with the email either.

## Gotchas

- A bill due one week but paid in an adjacent one is week-boundary
  timing noise in the weekly email — the same way an early payment
  reads on Today.
- The current month's projection is pace, not fate — one early splurge
  reads hot and usually cools.
- Past report cards are judged against the budget, and decompose bills
  using the schedule, **frozen when the month closed**, so editing a
  bill or a budget today doesn't shift an old month's fixed-vs-variable
  split or its verdict. The exception is
  months that closed before the freeze existed: those still project
  today's schedule backward, so a bill edit can move their split — and,
  since the verdict judges only the variable side, their verdict too.
