# Cash Flow

The Cash Flow page is the money report, in three tabs:

- **Overview** — where the money came from and where it went, and what
  was left over each month and each year.
- **Spending** — where the money went, by month, year, category and
  merchant.
- **Income** — where it came from, including imported tax documents.

Cash flow *is* income minus spend, so both halves live behind one
heading; `/spending` redirects to the Spending tab. Every tab covers the
same months for a given timeframe — except that Spending's **All** starts
at the month of your first counted charge, where its per-month average
starts, when your paychecks go back further — "left over" is always
income − spending,
and spending is dated by the day it was paid (the Month and Year lenses
date a bill by the month it belongs to instead).

## Overview

The page opens with a **flow picture** for the timeframe you pick —
the same This month / 1m / 3m / 6m / 1y / 3y / 5y / All toggle every over-time chart on
the site uses (a year is the default; wide windows take a moment to
compute the fixed split, then stay cached). On this page **This month**
is the one window that runs to today; every other range holds complete
months only, ending with last month (see Spending below). On the left, where
the money came from: paychecks, interest and dividends, and other
income. On the right, where it went: **fixed bills** (the charges a
recurring bill matched), **variable spend** (everything else), and what
was not spent — **Left over**. Months before any bill was scheduled
cannot be split into fixed and variable (nothing was matching bills
yet), so their spending is drawn as a grey band of its own, **Before any
bill**, with a line under the picture saying so ("$12,000 went out in 24
months before your first bill — not split into fixed and
variable. In the other 12 months the charges your bills matched are 75% of what went
out."); every dollar stays drawn, so the picture still adds up. Fixed
counts the bills you track today, so a rent or a loan you no longer
track is in variable on a long window; when a window reaches back before
most of your bills began, a second line says so ("Fixed counts the bills
you track today; 18 of these 36 months are from before most of them
began."). A
spending envelope counts as a bill from the first month something was
charged to it. Band widths are the dollars, so the
biggest destination is the widest band. When a period spent more than
it earned, an "overspent by" source appears on the left so the two sides
still balance, and the sentence under the picture says so in plain
words: came in, went out, left over (or overspent), and the share of
income you kept. Left over is income less spending, whatever you then did
with it; when money moved between your bank and an investment account
over the window, a line under the sentence gives that as its own figure
("moved to investments $2,400", or "took out of investments $900").

A month no bank feed covers has missing or partial income, not $0, so
it is marked **incomplete**: it still counts in what came in and went
out, but left over and the savings rate cover only the months with bank data,
and the page says so ("left over over 7 of 9 months with bank data"). A month counts as covered when any of your bank
accounts has a transaction in it (a deposit, a transfer, a fee); a month
in the middle of your history where none of them has a single row is
labelled *income incomplete* and keeps an empty slot in the monthly
bars, and the note under the picture lists it ("2 months without a bank
feed (Mar 2025, Apr 2025) — counted, but income there is missing or
partial"), since a broken link or a missing statement is the usual cause. The same goes for the month a bank feed starts partway
through, when your card history reaches back further: its paychecks
before the feed began were never imported. When no bank feed covers the window at all, the picture is
not drawn and the sentence says *income unknown for this range* and
gives only what went out. A household whose ledger starts this month
sees the month so far, labelled as such, instead of an empty page.

On **This month** every tab says it is the month so far: the heading
reads "Nov 1–17 so far" instead of the month's name, no per-month pace
is printed beside a partial month, and the comparison names the days it
uses ("vs Oct 1–17"). The Overview and the Income tab add the paychecks
your income series still have due this month ("$2,400 in paychecks
still due this month") — the figure Today and Budget call still due.

Below it, **left over, by month** — one bar per month, green when the
month kept money and red when it did not, with the amount written on
each bar while the bars are wide enough (on wide windows hover a bar,
or open **Show numbers**).
The same timeframe toggle picks the window, and the bars end where it
ends: a range of complete months (**1m**, **3m** … **5y**) stops at its
last complete month. On **This month** and **All** the month in progress
follows, drawn once as its left-over-so-far bar, and the hollow
bars after it are the **60-day forecast**, from the same
forecast engine Today uses (bills on due dates, paychecks on cadence,
variable spend at your usual 60-day pace) — there is no second money model, so the tail
always reconciles with the Today forecast card. Wide windows thin the
labels out so they stay readable. Under the bars, **best** and **worst** name the window's
strongest and weakest finished months; the month in progress is never
one of them, and a window of a single month names neither.

Then **left over by year** — in, out, left over and the savings rate per year
(the last five years, with a control to show them all); open a year for the same labelled bars over its months.
A month with no bank data has incomplete income: it is counted in what
came in and went out, drawn as a grey slot in the bars, and left out of
left over and the savings rate. A year with such a month is greyed and says
so under its left-over figure ("income incomplete — left over counts 8 of 12
months"); a year with no bank data at all shows its spending only.

## Spending

This tab answers one question: **where has the money actually gone** —
over the timeframe you pick with the same toggle, and **what changed**
against the previous equal window. **This month** is the month so far;
every other range holds **complete months only** — **1m** is the last
complete month, **3m** the last three that have closed, and so on — so
the month in progress never sits beside whole ones, and a per-month pace
is measured over months that are actually finished. When the range is
longer than your spending history (a **3y** view of a ledger whose
charges began two years ago), the window and its total stay as they are
and the per-month figure is over the months with spending, and says so:
"$250/mo over the 24 months with spending".

### What counts as spend

Every number here comes from the same filter, applied identically
everywhere the product reports spend:

- **Positive amounts only** — positive = money out (see the concepts guide
  for the sign convention).
- **Credit-card payments are excluded.** Paying the card just moves money
  you already spent at the merchant; counting both would double-count.
- **Transfers are excluded** — anything whose effective category is a
  transfer (in or out) is you moving your own money, not spending it.
- **Mortgage and loan payments DO count.** They're real money leaving —
  only the mirror rows on the loan account itself are dropped, so the
  bank-side payment counts exactly once.
- Removed transactions and shadow copies from linked duplicate sources
  (see the Accounts guide) are excluded.
- **Business-entity transactions are excluded** — money tagged to a
  business entity stays out of every personal report (see the Business
  guide).
- **Partial reimbursements net off**: a charge with a paired partial
  reimbursement counts at charge − reimbursed, never below $0, and the
  deposit counts as income for whatever its partial links leave
  unclaimed. Fully reimbursed pairs drop out entirely (see the
  Reimbursements guide).

A transaction's category is always **your override if you set one,
otherwise the imported category** — recategorizing a row to a transfer
removes it from spend everywhere on this tab. A row you **split** by hand
counts under each of its parts' categories for that part's amount; the
totals are unchanged, since the parts add up to the charge.

### The breakdowns

- **The story line** — total spent over the window, the monthly pace,
  the biggest category, and the change against the previous equal
  window (a 1y view compares to the year before it; All has no
  previous window, so no comparison). **This month**, the one window
  that runs to today, is compared like for like: the previous window
  stops the same number of days into its own month, so on the 17th, this
  month's days are set against the first seventeen days of last month
  ("vs Oct 1–17"), not all of it. Every other range is whole months against whole months. A small pace line shows the
  window's months once it holds at least four of them.
- **What changed** — up to four categories that moved most against the
  previous window, then up to three merchants you paid only in the
  previous window, biggest absolute change first, rises in red and
  falls in green (spend going up is the bad direction). A move counts
  once it reaches $25, or 0.5% of the window's spend if that is larger.
  A change shows its percent beside the dollars except where the percent
  says nothing: one that would read 0%, one of 1000% or more, or one
  measured from an earlier figure under $50.
- **Where it went** — the window's top 15 categories ranked, with their
  share of the window's spend, per-month pace, and change.
- **Top merchants** — the window's top merchants with visit counts and
  the same change column; the top 10 show, the rest (up to 25) expand.
  A merchant whose total for the window rounds to $0 is not listed. The
  small **history** link beside a name opens the merchant's whole history
  on the same window — every charge filed under the name, refunds and
  holds included, not only what this figure counted.
- **Every figure opens the charges it counts.** Tap the total, a
  category, a merchant or a "What changed" row and Transactions lists
  exactly the rows that figure added up — refunds, transfers, card
  payments, business money and excluded accounts left out, a split
  charge listed once for its part, a partly paid-back charge for what you
  kept — under a chip naming the figure and its dates, with the figure
  as the total. A merchant you paid only before opens the window before.
- **Records** (collapsed) — the archival tables: spend by year, Amazon
  by category (see the receipts guide), and the category × year matrix
  (top-15 all-time categories crossed with every year; a **·** cell
  means no spend that year).

This tab computes fresh from the ledger on every load — nothing here
is cached. (The Overview's fixed-bills split for closed months is the
one cached figure on the page. It is keyed to everything the split is
computed from — your bills, your budget settings, the transactions and
how they are filed, split or reimbursed, which accounts count, and which
copy of a linked account is current — so any change to those refreshes
it on the next load. Otherwise a cached month is kept for up to a day.)

### Canonical merchant names

The **Top merchants** table groups by the canonical merchant map, so one
real payee's descriptor variants consolidate into one row (what the map
is and why it exists: see the concepts guide). Before ranking, it also
folds names that differ only in punctuation or case ("A-B-C" and
"A B C") into one row, the bigger variant keeping the name. Page-specific facts: the
automatic cleanup pass runs nightly with the maintenance job; reviewed
merges always win over the automatic pass and are never recomputed; the
map affects display and grouping only — rows are never rewritten.

## Income

The same shape as Spending: a timeframe toggle, a story line — total
in, monthly pace, the share that was paychecks, and the change vs the
previous equal window (when the range is longer than your bank feed, the
pace is over the months since the feed began and says so: "$3,100/mo
over the 20 months since the bank feed began"), with a line under it naming any payer of that
previous window who paid nothing in this one ("Nothing this time from
Acme Payroll ($900), paid the year before."; a payer whose amount rounds
to $0 is not named, nor a line that only renamed its month, such as a
bank's "March interest" paid again as "April interest") — the **kind split** (paychecks / interest &
dividends / other, the flow picture's classifier and colors) with the
same pace line, and **income by source**: who actually paid — the top
15 payers, ranked, each with its share, how many deposits, and its
change vs the previous window (payer names consolidate like the
Spending tab's merchants, punctuation-only variants folded too; a payer
whose total for the window rounds to $0 is not listed, though it still
counts in the total). A payer
counts as **paychecks** when its deposits are tagged as wages, match an
income you track under Bills & Income, or read as payroll — described as
pay, arriving weekly, every two weeks, twice a month or monthly, at about
the same size — so an employer you have left still counts for the years
it paid. Each source row says which it is, and anyone who can edit can
change it: **Paycheck**, **Not a paycheck**, or back to **Automatic**.
The choice changes the split, never the total. A returned card payment
and money moved back from a brokerage are your own money, not income,
and are left out. A bill payment the bank returned stays in, beside the
payment it undid, so the month's net is as if neither had happened. Under it,
**interest & fees paid**: what holding money and carrying a balance cost,
by year — interest charged, fees (ATM, overdraft, foreign-transaction,
wire, membership, late), and the refunds the bank gave back, netted — with
the year so far beside the interest the same banks paid you, and the
largest charge of each year named and dated. Each year is shown in whole
dollars that add up across the row (interest plus fees less refunds is
the net), so a cell can be a dollar off its own rounding. Bank and card
accounts only; an
investment custodian's fee is fee drag, reported on the Net Worth page.
The card hides when nothing was ever charged. A new fee or interest
charge is also a line in the next daily email ("first since Mar 2026" when
a card starts charging interest again), so it is heard the day it
posts, when a call can still reverse it. The archival
cards live behind **Records**: **career earnings** from an imported
SSA record, **reported income** from imported tax documents (total,
wages, investment and tax paid per year — joint returns are marked),
and investment funding by year.

## The lenses

The home page's Today · Month · Year picker steps one view through
three zoom levels over the same engine math — see the
Today/Month/Year guide for what each lens shows and how browsing
the past works.

## Gotchas

- A past month's report card uses the bill schedule frozen when the
  month closed, so editing a bill today doesn't change how an old
  month's spending divides into fixed vs variable. Only months that
  closed before the freeze existed still re-project today's schedule
  backward (see the Today/Month/Year guide). The Overview's flow
  picture is the exception: it splits every month in its window with
  today's live bill schedule, so a bill edit does move an old month's
  fixed-vs-variable split there.
- The current month's projection is pace (spend so far ÷ days elapsed
  × days in month) — early-month numbers swing.
- The Spending and Income tabs compare only against a previous window
  your history covers from its first day; one that starts before your
  accounts were imported shows no comparison rather than a huge change
  against months with nothing on file. A feed whose first row lands by
  the month's second weekday counts as covering it from the 1st, since
  nothing posts on a weekend or a bank holiday. The All view simply has
  no comparison.
- The Category × year matrix only lists the top-15 **all-time**
  categories. A category with real spend in one year can be absent if its
  lifetime total didn't make the cut.
- Early years imported from card statements alone can look artificially
  low — the report shows what's in the ledger, not what you don't have
  history for (see the import history guide).
- Seeing the same merchant twice in Top merchants means the canonical map
  hasn't merged those variants; the nightly cleanup only handles
  recognizable descriptor junk, not genuinely different names. Merge them
  yourself on the **Merchants** page (Settings → Merchants) — your answer
  outranks every automatic rule and can be undone (see the merchants
  guide).
