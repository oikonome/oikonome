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
not elapsed yet; a closed month expects the full variable budget. A
month whose ledger begins partway through counts its days from the
first transaction and expects only the budget's share from there to
month end (see the Budget guide), live or closed, so a first month is
judged on the days the app actually saw.

A lens judges only a budget you saved, or the frozen snapshot of a
closed month. Before you save one, a provisional figure seeded from your
history may fill the Budget page, but the Month lens shows a **no budget
set** pill and the month's variable spend instead of a verdict, and the
Year lens gives that month a **net** cell.

**Only variable spending is judged** — Food and Everything else (custom
budget categories are carved out of those two for display, so their
spending counts inside them). Fixed bills never make a period "over";
they appear in each lens's Bills row instead. A bill you track by hand
as a card payment drops out of the month verdict while a linked card
carries a balance — the card's own payoff covers
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
adds a pace projection: spending through yesterday ÷ the days elapsed,
carried to month end, judged against the variable budget (or a first
month's share of it) with the month-end tolerance. In the first week of
a new ledger the projection is the plan itself, unless even the plan's
pace is over.

The card shows: the money map for the month, **bills paid vs planned**
(each posted payment, anything due but not posted, envelope usage and
overflow), **spending by category** (Amazon subcategories clustered
under one parent), the ten **biggest transactions**, and **income vs
spend** with the net. In a heavy-bill month the header notes that
variable budgets were auto-scaled (see the Budget guide).

Every row of the Monthly budget map — Food, Everything else, a carve-out,
and Bills — opens the transactions it counted for that month; on the
Year lens the same rows open the year's. Each category in the spending
table opens that month's rows in the category.

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
view shows seven months of plan, not a phantom full year) — each of its
labels opens that bucket's rows for the year, the same months summed —
annual totals
— income, spend, saved, savings rate — and the top twelve spending
categories next to the prior year's figures, with a Total spend row. Annual income and spend use the Cash Flow
report's definitions (see the cash-flow guide), summed over the same
months as the grid: a year whose income is not known for every month
(no bank account feeds part of it) shows income and savings as
**unknown, not $0**, and only what went out. A hidden account, or the
backup copy of a linked pair, does not count as a feed: its deposits
are not in the income total, so its months are not known by it either.

## Which summary mirrors which lens

- The **daily email** mirrors the Today page — same content, same math
  (see the Today guide).
- The **monthly email** is the **previous month's** report card, sent on
  the 1st: the year it sits in month by month (each cell is the month's
  net, green when it made money, red when it lost) and the year-to-date
  net; the month's own net and verdict; the budget as bars — the bills in
  one bar, then the variable budget as you split it (Food, Everything
  else, any carve-outs) and any envelope bill you pinned to Today as a
  bar of its own, each green or red by whether it ran over;
  spending by category as a ring, the bills under their own categories
  (the mortgage, rent, power and water as one "Mortgage, rent and
  utilities" slice, and whatever you set as a bill's transaction
  category — tag the streaming and software bills "Subscriptions" and
  they show as one slice); a paid bill no category accounts for stays in
  a plain Bills slice. The ring holds up to eight slices; the rest fold
  into Other.
- The **year in review**, sent on January 1st, is the monthly report
  card at year scale: the year's net beside last year's, the twelve
  months coloured by what each kept or lost, how many finished on or
  under budget; the budget as annual bars over the budgeted months (the
  bills in one bar, then the variable budget as you split it, then any
  bill pinned to Today — a year with no budget on record has no bars);
  the year's spending by category as a ring, the bills under their own
  categories, with up to twelve slices before the rest fold into Other;
  and, under the twelve months, every year the ledger holds with its net,
  back to the first (a year whose income is not known for every month
  shows what went out instead).

Neither email lists individual transactions; the Month page still
shows the month's biggest. If the instance could not send the card on
the 1st (the server was down, or mail was failing), it is still due at
the send hour on each of the next three days until it goes out, and its
text or push goes out once.

Every summary is built from the exact same computation the lens fetches,
so the email and the page cannot disagree. Each cadence is switched on
and scheduled independently — and each can also go out as a **short SMS**
or a **push notification** to any browser you enable and any phone
registered through the mobile app, from Settings → Scheduled summaries.
Signing out of a browser stops its push notifications; enable them again
there after the next sign-in.
Texts need a verified phone number and an instance with its own Twilio
account. The SMS and push texts are short forms of the same numbers;
they can't disagree with the email either.

## Gotchas

- The current month's projection is pace, not fate — one early splurge
  reads hot and usually cools.
- Past report cards are judged against the budget, and decompose bills
  using the schedule, **frozen when the month closed**, so editing a
  bill or a budget today doesn't shift an old month's fixed-vs-variable
  split or its verdict. The exception is
  months that closed before the freeze existed: those still project
  today's schedule backward, so a bill edit can move their split — and,
  since the verdict judges only the variable side, their verdict too.
