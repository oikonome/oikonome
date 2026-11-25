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
it. Totals and the map show spending as positive amounts; the Biggest
transactions list shows each charge with a minus sign (see the concepts
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
- **Month** steps a month, **Year** a year. Both also have a picker
  (dropdowns on the web; on the phone, tap the month or year between the
  arrows), whose range runs back to the first year with data. The Year
  lens's **‹** stops at that first year.
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
budget today leaves closed months alone. A closed month from **before
you had a budget** (no snapshot exists for it) gets no verdict at all:
its card reads *no budget then* and shows what came in, what went out
and the difference, with the spending by category and the biggest
transactions. Today's budget is never applied to it and today's bills
are never written into it, so nothing there is "due" or "planned". (To
judge older months against today's budget on purpose, use **backfill**
on the Budget page's history card.) The **current month** is in progress ("day 12 of 31") and
adds a pace projection: spending through yesterday ÷ the days elapsed,
carried to month end, judged against the variable budget (or a first
month's share of it) with the month-end tolerance. In the first week of
a new ledger the projection is the plan itself, unless even the plan's
pace is over.

The card shows: the money map for the month, **bills paid vs planned**
(each posted payment, anything due but not posted, the plan's set-aside
for the envelopes apart from the bills plan, and each pool as what it
paid out this month of what it held, with any spill past it; once the month has ended nothing is "due" any more — the card
names a bill only when no payment for it was found that month, *not
paid: NAME $X*, and the Bills row's table says what the plan had left), **spending by category** (Amazon subcategories clustered
under one parent), the ten **biggest transactions** (a bill's payment
under the bill's name, as the Bills page lists it), and **income vs
spend** with the net. In a heavy-bill month the header notes that
variable budgets were auto-scaled (see the Budget guide).

Every row of the Monthly budget map — Food, Everything else, a carve-out,
and Bills — opens the transactions it counted for that month; on the
Year lens the same rows open the year's. Each category in the spending
table opens that month's rows in the category. In a ledger's first,
partial month the variable rows show the same share of each budget the
verdict judges; bills are shown in full. The Bills row says two
figures: what posted (its caption, with the plan for the month still
running) and what is *still due* — the bills not yet paid through month
end and any bill late from an earlier month, the figure Today, Budget
and Bills print. A month that is over says instead how it came out, in
a phrase or two — *$X not paid*, *$Y under plan* or *over plan by $Y* —
and a month still running says *over plan by $Y* too once the bills that
posted already came to more than its plan. The caption names the
*awaiting* part of still due (due already, not posted) when there is
one. **How this adds up**, under the row, opens a small table of the
month's bills that adds up to its plan, each row with its + or −: what
was paid, what is still due, each bill *not paid* in a month that is
over, and what bills that posted left *under plan* (or *over plan*).
Envelopes are not in it: they are the **Envelopes** row under Bills, a
gauge of what the pools paid out of what they held, never over unless a
charge ran past a pool (*$X past the pool this month, counted in
spending*; that part is counted in Food or Everything else, and the
figure is exactly what was counted there). A month that is over says
what a monthly pool left *unspent*. A month whose bill refunds came to more than its
charges shows $0 paid and a row of *refunds beyond charges*, and says
*$X refunded*. A bill still unpaid from an earlier month is
in none of this month's plan: the table lists it below the total, with
its month and year (*Late from Aug 2026*), and the month's own part
then reads *due this month, not paid yet*. A bill late on several dates
of one month is one row. On the Year lens the Bills row says what posted
this year (*in 2025* for a year that is over, which says how it came
out as a month does) and the same still-due figure as Today;
its table adds up its months the same way: a late bill is counted once,
in the month it was due, and one from a month the year has no plan for
(a December bill carried into January, or a month with no budget
snapshot) is listed below the total. The Year's Envelopes row reads
each pool itself — what it paid out over the plan's months, and for a
yearly pool what it has left — never a sum of monthly shares, so a
pool spent in full reads full, never over; a year that is over says
what the pools left *unspent*. A yearly pool set up during the year
counts the spending from the month it was set up; what a pool spent
before the plan's months is a line of its list, never words. A bill
renamed, or whose due date was changed, after its month closed is still
the same bill, counted once, and a bill a month that is over counted
paid is never shown late on the Year. A late bill sits on its own row
of the month it was due — matched by the bill and its due date, never
by the nearest date — so two bills that each carry their own identity
are never taken for each other because they fall due on the same date.
A month that closed before the upgrade that gave bills an identity
names its bills by name and date only, so on the Year a bill made later
with the same name and due date can be read as that month's bill: the
total still equals the plan, but its *not paid* and *under plan* rows
can be wrong until that month leaves the Year. A date a bill missed
is not read as moved just because the bill falls due again a few days
later.

## Year: the twelve-cell grid

The Year lens is twelve month cells: each finished month's **final
verdict and its margin** in dollars, the current month marked **proj.**
with its projection, future months blank. A finished month with no
budget snapshot gets no verdict: its cell carries a muted **net** pill
with the month's income − spending, a footnote under the grid explains it, and
the month is left out of the year's money map, since nothing was
budgeted. A month your bank feed does not cover has no income figure,
so its cell is greyed, reads **spent** with what went out, and is marked
*income incomplete* — never a net; a month with no transactions at all
(or only ones on accounts you excluded from the budget) reads **no data**. Clicking a cell opens that month's report card, which
says the same: *income incomplete* in place of the income and the net,
or *No data for this month.*

Below the grid: a money map summing the **elapsed months only** (a July
view shows seven months of plan, not a phantom full year; the month in
progress counts there to date, while its cell above is projected to
month end, and the card's caption says so) — each of its
labels opens that bucket's rows for the year, the same months summed —
annual totals
— income, spend, left over, savings rate — and the top twelve spending
categories next to the prior year's figures, with a Total spend row. Annual income and spend use the Cash Flow
report's definitions (see the cash-flow guide), and the totals are the
sum of the twelve cells, with a bill counted in the month it belongs to
rather than the day it was paid. A month with no bank data has
incomplete income — the same rule as Cash Flow and the yearly email. A
year with some such months counts income, left over and the rate over its
other months and says so ("income incomplete — left over counts 7 of 9
months"). A left over that rounds to $0 is even: neither green nor red,
and no savings rate beside it (Cash Flow's Left over by year reads the
same); a year with none shows what went out only and says "No month
of 2026 so far has bank data" ("No month of 2019 had bank data" for a
year that is over). A hidden account, or the
backup copy of a linked pair, does not count as a feed: its deposits
are not in the income total, so its months are not known by it either.

## Which summary mirrors which lens

- The **daily email** mirrors the Today page — same content, same math
  (see the Today guide).
- The **monthly email** is the **previous month's** report card, sent on
  the 1st: the year it sits in month by month (each cell is the month's
  net, green when it made money, red when it lost) and the year-to-date
  net; the month's own net and verdict; the budget as bars — the bills in
  one bar ("planned from the month's due dates", the month's own
  schedule rather than the evened-out figure), then the variable budget as you split it (Food, Everything
  else, any carve-outs), an Envelopes bar (what the pools paid out of
  what they held; no bar when that is $0 of $0) and any envelope you
  pinned to Today as a bar of its own — what that pool's period spent
  against the pool, the year so far of a yearly pool as Today's card
  prints it, the month of a monthly one — each green or red by whether
  it ran over (red only past the pool, "$X past the pool this month,
  counted in spending" — the dollars that went to your everyday
  spending that month, not the spend less the pool); a month with no
  dated bill has no Bills bar, and a bill not paid is named once;
  spending by category as a ring, the bills under their own categories
  (the mortgage, rent, power and water as one "Mortgage, rent and
  utilities" slice, and whatever you set as a bill's transaction
  category — tag the streaming and software bills "Subscriptions" and
  they show as one slice); a paid bill no category accounts for stays in
  a plain Bills slice. The ring holds up to eight slices; the rest fold
  into Other.
- The **year in review**, sent on January 1st, is the monthly report
  card at year scale: the year's net beside last year's (a net that
  rounds to $0 reads "broke even", with no savings rate), the twelve
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
registered through the mobile app, from Settings → Email & Push
(Scheduled summaries).
Signing out of a browser stops its push notifications; enable them again
there after the next sign-in. Each person can have push on in up to ten
browsers at once: enabling an eleventh drops the oldest one (a browser
already signed out goes first). Enabling push in new browsers is also
limited to 20 an hour per person.
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
