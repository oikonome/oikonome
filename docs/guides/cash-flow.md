# Cash Flow

The Cash Flow page is the money report, in three tabs:

- **Overview** — where the money came from and where it went, what you
  saved each month, and what you saved each year.
- **Spending** — where the money went, by month, year, category and
  merchant.
- **Income** — where it came from, including imported tax documents.

Cash flow *is* income minus spend, so both halves live behind one
heading; `/spending` redirects to the Spending tab.

## Overview

The page opens with a **flow picture** for the timeframe you pick —
the same This month / 1m / 3m / 6m / 1y / 3y / 5y / All toggle every over-time chart on
the site uses (a year is the default; wide windows take a moment to
compute the fixed split, then stay cached). On the left, where
the money came from: paychecks, interest and dividends, and other
income. On the right, where it went: **fixed bills** (the charges a
recurring bill matched), **variable spend** (everything else), and what
stayed — **Invested & saved**. Band widths are the dollars, so the
biggest destination is the widest band. When a period spent more than
it earned, an "overspent by" source appears on the left so the two sides
still balance, and the sentence under the picture says so in plain
words: came in, went out, stayed (or overspent), and the share of income
you kept.

Below it, **saved by month** — one bar per month with the amount
written on it, green when the month kept money and red when it did not.
The hollow bars at the end are the **60-day forecast**, from the same
forecast engine Today uses (bills on due dates, paychecks on cadence,
variable spend at pace) — there is no second money model, so the tail
always reconciles with the Today forecast card. The same timeframe
toggle picks the window; wide windows thin the labels out so they stay
readable.

Then **savings by year** — in, out, saved and the saving rate per year;
open a year for the same labelled bars over its months.

## Spending

This tab answers one question: **where has the money actually gone** —
over the timeframe you pick with the same toggle, and **what changed**
against the previous equal window. **This month** is the month so far;
**1m** is the last complete month, the one window that closes before
today, so mid-month you can read last month whole.

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
  reimbursement counts at charge − reimbursed, never below $0. Fully
  reimbursed pairs drop out entirely (see the Reimbursements guide).

A transaction's category is always **your override if you set one,
otherwise the imported category** — recategorizing a row to a transfer
removes it from spend everywhere on this tab.

### The breakdowns

- **The story line** — total spent over the window, the monthly pace,
  the biggest category, and the change against the previous equal
  window (a 1y view compares to the year before it; All has no
  previous window, so no comparison). A small pace line shows the
  window's months once it holds at least four of them.
- **What changed** — up to four categories that moved most against the
  previous window, biggest absolute change first, rises in red and
  falls in green (spend going up is the bad direction). A move counts
  once it reaches $25, or 0.5% of the window's spend if that is larger.
- **Where it went** — the window's top 15 categories ranked, with their
  share of the window's spend, per-month pace, and change.
- **Top merchants** — the window's top merchants with visit counts and
  the same change column; the top 10 show, the rest (up to 25) expand.
- **Records** (collapsed) — the archival tables: spend by year, Amazon
  by category (see the receipts guide), and the category × year matrix
  (top-15 all-time categories crossed with every year; a **·** cell
  means no spend that year).

This tab computes fresh from the ledger on every load — nothing here
is cached. (The Overview's fixed-bills split for closed months is the
one cached figure on the page, kept for ten minutes and keyed to your
bills and budget settings, so an edit refreshes it.)

### Canonical merchant names

The **Top merchants** table groups by the canonical merchant map, so one
real payee's descriptor variants consolidate into one row (what the map
is and why it exists: see the concepts guide). Before ranking, it also
folds names that differ only in punctuation or case ("H-E-B" and
"H E B") into one row, the bigger variant keeping the name. Page-specific facts: the
automatic cleanup pass runs nightly with the maintenance job; reviewed
merges always win over the automatic pass and are never recomputed; the
map affects display and grouping only — rows are never rewritten.

## Income

The same shape as Spending: a timeframe toggle, a story line — total
in, monthly pace, the share that was paychecks, and the change vs the
previous equal window — the **kind split** (paychecks / interest &
dividends / other, the flow picture's classifier and colors) with the
same pace line, and **income by source**: who actually paid — the top
15 payers, ranked, each with its share, how many deposits, and its
change vs the previous window (payer names consolidate like the
Spending tab's merchants, punctuation-only variants folded too). The archival
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
- The Spending tab's comparison needs a previous window of data — a
  1y view early in your history compares against a mostly-empty year
  and reads as a huge change. The All view simply has no comparison.
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
