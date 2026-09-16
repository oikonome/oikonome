# Bills

The Bills page manages the bills the plan spends against. Bills are
**ledger-driven**: detection reads your transaction history and proposes
them; you approve. Nothing invents a bill you didn't have.

The headline sums the schedule as evened-out monthly figures — what
bills cost per month, what income brings in, and the difference: what's
left for variable spending and savings. The two loads use the same math
as the Budget page's flow line, so the pages can't disagree about the
same schedule.

## Detection and proposals

Detection scans up to 48 months of history for merchants that charge on
a steady cycle — weekly, every 2 weeks, monthly, every 2/3/6 months, or
yearly. It wants at least 3 occurrences before proposing (2 for long
cycles like semi-annual and annual). It runs nightly, and on demand via
the **Find bills & income** button (which reads "scanning…" while it
works).

Everything detection wants to change arrives as a **proposal** — new
bill, cadence change, "this bill seems gone" — and sits pending until
you approve or dismiss it. The one exception is **amount drift**.

Bank connections that know their own recurring streams (Plaid does) are
cross-checked nightly: a stream Plaid sees that no bill covers arrives
as a proposal too, marked "Plaid sees this recurring". It goes through
the same approve/dismiss door. Switch the cross-check off in
Settings → Connections if you would rather rely on the ledger alone.

## Amount drift

When a bill's real charge settles at a new amount (at least $1
different), the bill's amount **updates automatically** and leaves an
audit entry — subscription price hikes shouldn't need paperwork. If you
hand-edit an amount, it's immune to drift for 60 days, so detection
won't fight you.

## How matching works

- A transaction matches a bill by **merchant identity**: a bill carries
  the merchants it pays (shown as chips when you edit it), and its
  charges are that merchant's rows within an amount tolerance. Renaming
  or merging the merchant keeps the bill paid. Only a bill with no
  merchants on it falls back to matching words in the transaction's
  text. The app offers additions to a bill's merchants (a renamed payee,
  a sibling spelling) as proposals you approve; it never widens a bill on
  its own. Such a proposal reads "add merchant · Northwind — add to bill
  “Northwind Headquarters”", followed by how many charges that merchant
  has and which merchants the bill counts today, so you can tell whether
  those charges really are this bill.
- **One transaction per scheduled occurrence** — a second charge from
  the same merchant at bill-like size shows up in the verdict's Why
  pane as "beyond bill schedule", not as a silently absorbed bill.
- An occurrence is recognized **at payment** — paying early is never
  "over budget"; an unpaid occurrence starts counting on its due date
  and shows in the overdue list until it posts.

## Envelopes

Envelope bills have no due dates — the amount is a spending **cap**
(monthly, or an annual calendar-year pool for lumpy costs — see the
Budget guide). Detection can propose monthly envelopes for
grocery-style refill spending; **annual envelopes are always created
manually** (pick "envelope (annual pool)" as the cadence).

An envelope can also pool a whole **category**: give it a category and
check "count all … spending", and every transaction of that category
draws the pool even when the merchants share nothing — a pet-care pool,
say, where the money goes to a vet, a groomer and a pet store that no
merchant matcher can unify. Merchant matches (this
envelope's or any other bill's) still win first, so a scheduled bill's
charge never leaks into a category pool.

## A bill that categorizes its own charges

Some merchants are several things at once: a gym's monthly membership
is fitness, while the café purchases there are dining — and a merchant
rule can only say one. When editing
an expense bill, set **matched charges are …** to a category, and every
charge the bill matches (its merchant, inside its amount tolerance — the
same rows that carry the ⟳ pill) is categorized that way, past and
future. A manual ↻ sync on the Accounts page runs that categorization
right away; the scheduled hourly pull applies it within the hour. Charges
at the merchant that the
bill does *not* match keep the merchant's own category. A category you
set on a row yourself always wins over the bill's; changing, clearing,
archiving or deleting the bill puts its rows back the way they were.

## Fees that ride with a payment

Some payments arrive in two pieces: the bill itself and a small fee the
processor posts beside it — a utility portal's $1 convenience fee, a
rent portal's small service charge. On their own those fees look
like tiny bills (detection would propose "$1 monthly, Portal Fee"),
and the bill they belong to reads cheaper than it is.

Attach the fee to the bill instead. In the bill's editor, **Fees &
add-ons** takes one row per fee: the words that appear on the charge
("portal fee"), its amount, and how many days either side of the
main charge it may land (3 by default). From then on:

- a matching charge landing within that window counts as part of the
  **same occurrence** — the bill shows as one line, "$91.00 incl. $1 fee",
  and its paid total, health and the Today/email figures include the fee;
- the fee's ledger row keeps its own merchant, is marked "fee of <bill>",
  and — when the bill stamps a transaction category — takes that
  category too;
- detection never proposes an attached fee as a bill of its own.

Detection also **offers** the attachment: a small, fee-shaped charge
(fee, convenience, service, surcharge…) that lands within three days of
a bill's charge, on the same account, three cycles running, arrives as a
proposal — "fee · Portal Fee — a fee of City of Springfield" —
to approve or dismiss like any other.

## Pin a bill to Today

Any expense bill can be **pinned** ("pin to Today" when editing it):
it gets its own card on the Today page and in the daily email. An
envelope pin answers *how much is left in the pool this month* (or
year); a fixed pin answers *how much of this bill is still to go out,
and when* — paid so far, next due date, loud when overdue. A pinned
fixed bill only shows a card in months it's actually scheduled.

## Managing bills

The **Cycle** column is a bar for where you are in the bill's billing
cycle, with the answer written beside it: "in 12 days", "due today",
"3 days late". Green while there is time, amber inside three days of the
due date, red once it is late; the mobile app colours the same words under
each bill the same way.

Each bill row edits inline: rename, amount, cadence (the standard cycles,
or **Custom interval…** for every N months or years), next due date, an
occurrence cap (at most N per month), disable, archive, or delete.
Health pills flag problems — a bill that stopped appearing ("stale"), one
whose due-date anchor sits more than a couple of days off the real charge
phase ("misdated"), and "mismatch": the merchant is charging you, but
never near the amount the bill is set to. A mismatch says what it did see
("ledger $42.00 on 03/18/26"), which is usually enough to tell a price
rise from a wrong bill. A bill whose merchant has never appeared in the
ledger at all — a cash or hand-tracked bill, usually — carries
"off-ledger" instead.

Bills that need attention also collect in a short queue at the top of the
page (and of the Bills tab on the phone): one line per bill, a severity
dot, a plain-language story ("missed its ~08/22 cycle", "charges now run
$150.00, bill says $100.00") and one button. The button is chosen from
the evidence, never guessed — **Accept** the new amount only when the
recent charges agree on it, **Disable** a bill that has missed two full
cycles, **Review** when the ledger is ambiguous, and **Dismiss** to hide
that exact story until the evidence changes. Every action is reversible
with Undo on the same line; nothing in the queue deletes anything.

Health counts **pending** charges as well as posted ones: a charge that
is still settling, at an amount the bill would accept, is evidence the
bill is right rather than something to nag about for two days. Detection
itself stays posted-only — a pending row can still change amount or
vanish — so a new bill is never proposed off one.

Click a payee for its full history: every charge (with the same
recategorize / reimbursement / business-flag / receipt controls as the
Transactions page) over the usual 3m · 6m · 1y · 3y · 5y · All picker —
All by default, so the list agrees with the lifetime total — monthly
totals, the lifetime total, and
**Avg / mo (active)** — the average over months that actually had
activity, so sparse merchants aren't understated by empty months. That
lookup reads the whole ledger, so it is budgeted like the reports are —
60 payees a minute, far above reading and well under a loop. If a panel
ever says it's been asked too often, pause a moment and click again.

You can also create a bill or envelope straight from the Transactions
page: any row already matched to a bill shows an indicator, and
unmatched merchants offer "add as bill / envelope" with a suggested
cadence prefilled from that merchant's history (you confirm; nothing
auto-saves).

## Upcoming calendar

At the bottom of the page (web and mobile), **Next five weeks** shows a
Monday-anchored grid of every scheduled bill and income event over the
coming five weeks, with today highlighted and past days dimmed. Each day
with an event shows its net total and up to two event labels, plus a
"+N more" count when a day is busier; on the web, hovering a day lists
every event with its amount, and on the phone tapping a day opens that
day's events under the grid (tap again to close). The grid is the whole
agenda — nothing repeats below it, so the **Archived** table follows it
directly.

## Gotchas

- Deleting a bill doesn't touch transactions — they just go back to
  counting as variable spending.
- A bill's schedule projects forward from its cadence and next-due
  date; if occurrences look off, the cadence or anchor date is the
  usual culprit.
- Disabled bills stop matching and stop reserving cash in the plan and
  forecast.
- A bill you track by hand for a **credit-card payment** stays listed on
  the Bills page, but while any of your credit cards carries a balance it is left
  out of the month verdict, Today's headroom and the forecast — the card
  itself already reserves that payment, and counting the bill too would
  reserve it twice. Once no card debt remains, the bill counts again.
