# Today: the daily verdict

The Today page (and the daily email — same content, same math) answers
one question: **are you on budget today, and what's the one number to
hit?**

## Before you have a budget

The verdict card does not appear until you have set a monthly budget, and
the daily email does not go out either. There is nothing honest to say
yet: with no plan the arithmetic is "on budget, $0 of $0 left", which is
a judgement on a plan you have not made. Finish setup — or just set Food
and Everything-else in **Budget** — and both start the next day.

("Send today's email now" on the Daily email card in Settings still
sends at any time, if you want to see what the message looks like
before then.)

## The verdict card

The card opens with the verdict as a sentence — **On budget**, **Over
budget**, or **Under budget**. (The Month and Year lenses show the same
verdict as a chip: ON PLAN, OVER BUDGET, UNDER BUDGET.) It judges
**variable spending only** — your Food and Everything-else budgets,
prorated to how much of the month has gone by:

- expected-so-far = month budget × (days elapsed ÷ days in month) —
  today has not elapsed yet, so on the 12th eleven days count; a closed
  month counts every day
- variance = what you actually spent − expected-so-far
- tolerance = the larger of $50 or 5% of expected-so-far

Within tolerance you're on budget; past it you're over (or under). One
exception: while less than a tenth of the month has elapsed — the first
few days — "under" reads as on budget, because barely any month has
passed to spend in and the good news would be about the calendar, not
your spending. "Over" is never suppressed: blowing the budget on the 1st
is worth saying immediately.

**Fixed bills never make you "over."** A bill is an obligation with its
own schedule — rent posting on the 3rd instead of the 1st is timing, not
overspending. Bills are recognized at payment (paying early is never
"over" either), tracked in the plan and the cash timeline instead of
the verdict. Matching also reaches into next month's schedule: rent
autopaid on the 28th for the 1st is this month's fixed spend, not a
variable purchase. And a bill you track by hand as a card payment is
left out of the plan and the verdict while a linked card carries a
balance — the card's own payoff already covers it, and counting both
would reserve the same money twice.

Beside the verdict word, the same sentence says how today's pace
compares to plan ("$120 under pace on day 12"), with the month's
remaining variable budget as small context beside it — what to do, not
just what happened.

## The one number to hit

By default (the **summary** face) the card shows it as literally one number: what's still
spendable **today** across all your variable budgets, over a day
meter (today's spend of today's allowance), with a small chip per
category ("Groceries $12
today", "Dining over by $4"). A category already over contributes $0 —
its allowance can't be borrowed by another bucket, and its chip says
so.

The **summary / detail** toggle above the cards scopes the whole page:
summary shows the verdict card alone; detail brings back the plan line,
savings goals, the Monthly budget map and the Cash timeline. The choice
is remembered on your account and the mobile app follows it — except on
a demo instance, whose shared login makes every settings write refuse, so
there each browser and each phone remembers its own. The daily
email does not — its summary/detail shape is its own **email face**
setting on the daily row of your notification settings.

The email also ends differently from the page: the alert strip sits at
the **bottom**, in a *Needs you* section that adds one-tap buttons for
proposed bills and uncategorized charges (see [Alerts](alerts.md)). Only
owners and members get that section; a viewer's or guest's copy stops at
the timeline.

In the detail view each category gets a full-width row — the same
bar style as the money map — whose number is **left today**: today's
allowance (fixed at the start of the day) minus what you've spent
today. It moves the moment you spend, so it answers "can I spend more
*today*?" Overspending a day shows red; tomorrow's allowance
recomputes and absorbs it. The bar is day-scoped — fill = today's
spend against today's allowance, with overspend striped past the
budget divider; there is no pace tick because the day is the unit.
The month gauges (with their pace ticks) live in the Monthly budget
card below.

The daily rate — (month budget − spent so far) ÷ days left — still
drives every number, but it only speaks up when it changes: a tile
whose allowance drifted from the plan says so in prose ("daily reduced
from $61 to $52"). A reduced daily also says how to get the old one
back — "skip 3 days of spending and the daily is back to $61" — or,
when the month can't get there, says so. Hold the stated daily and the
month lands on budget.

Hit the daily, end the month on plan — that's the whole game.

## Pinned bills

Bills flagged "pin to Today" (Bills page, edit a bill) get their own
rows under the categories, in the daily email too. An envelope pin shows
what's left in its pool this month (or year, for annual pools); a
fixed pin shows what's still to go out and its next due date, loud
when overdue. See the Bills guide.

## The Why block

Only when you're **over budget**, a Why section appears inside the
verdict card and names the drivers with dollars: top merchants in the
bucket that's running hot, envelope overflow, and charges that look
like a bill but land beyond its schedule. Custom budget categories
running over their own pace are named too. Open "by category ▸" and
each category gets its own line, plus — while days remain — a way back:
the most you can spend per remaining day in each bucket and still end
the month inside its budget ("spend at most $18/day on Food"); a bucket
already through its budget is named as a $0-a-day one. On or under
budget there is no Why — the verdict speaks for itself. Each "by
category" line is headed by its bucket's name, and that name opens the
same rows its tile does.

Every category and bill name on Today — the Food / Everything else
tiles, the money map's rows, the Why's category lines, the pinned bills
— opens the rows behind its number: the category's transactions for
this month, or the bill's history. Plain text at rest, a button on hover
or a press on the phone.

## The plan, in one line

The bottom of the verdict card shows the month's plan as a flow —
**income → bills → to spend → saved → excess** — with a link to the
Budget page, which owns the detail (income, per-category budgets,
carve-outs, the savings plan). The "saved" step appears only when you
have a fixed savings plan; a sweep-mode goal sits beside the chain as
its own short sentence ("sweep up to $X — $Y available so far this
month").

## Monthly budget

Every budget line as a gauge of its own budget: green is posted
spending, light green is what's still to come, amber is a bill that's
due but hasn't posted, and anything past the budget divider renders
striped. A red segment (and an "ahead of pace" note) means the line is
past its pace but still inside its budget. Plan-only lines with nothing
measurable (a savings plan without a destination account, excess cash)
fold into a footnote instead of drawing empty bars.

## Cash: quiet until it matters

Cash warnings have one home — the verdict card — and three states:

- **OK** — no cash line at all. Silence is the signal.
- **Tight** — one calm line ("Cash gets tight around 9/10").
- **Critical** — a red block that says what to do ("Short $2,100 by
  7/16 — cover from savings or slow spending to $X/day").

## The cash timeline

Cash lives in one card: **checking now**, **card debt**, **after cards
& bills** (checking − card debt − bills still due this month), and the
forecast's **low point**, over a 60-day chart. Below it, one
chronological table: the month's paid bills and your recent
transactions, a **TODAY** divider, then every scheduled event —
bills on their due dates, envelopes spread evenly, paychecks on their
cadence, card payoffs — with a running balance. Events show through
the forecast's low point; the rest sit behind an "all N events"
control (the email says how many more there are instead). Paychecks are
matched to the ledger the way bills are: a deposit that has already
posted is in today's balance and is not walked in again on payday, and
a paycheck that is late is simply not projected — unlike an overdue
bill, it is never re-dated to tomorrow.

Two card-payoff scenarios exist: **autopay statement balance** (the
realistic default) and **pay all cards now** (the comparison). When
both bottom out at the same low they collapse into a single balance
column.

These scenarios test the next few weeks' cash, so they pay each card off
at its due date. The longer question — when every card and loan is gone,
and in what order — is the Debt page's (see the debt guide).

Each card's **autopay date** is drawn on the chart as a dashed line
labelled with the card, so a step down in the curve says which payment
caused it. Only dates your card issuer actually reports are marked;
a card that reports none is still paid in the forecast — conservatively,
tomorrow — but gets no line, because a marked date nothing is known to
happen on would read as fact. Cards due the same day share one line.

If any account still sits at an unset $0 balance, the forecast hides
rather than present $0 as truth; the verdict card says so instead
("Cash forecast is waiting on your balance", with a link to Accounts).
Set real balances on the Accounts page and it appears.

## On your phone's home screen

The mobile app ships a home-screen widget that shows the simple face of
this page: the one number left to spend today, the verdict, and the day
bar. The wider size adds the per-bucket chips. Tap it to open Today.

- **Same number as the app.** The widget is fed by the same door that
  composes the hero, in whole dollars, so it can never disagree with the
  page or the daily email.
- **Refreshes on the phone's clock** — about every half hour, and again
  whenever you open the app. When the last refresh is more than an hour
  old the widget turns grey and shows the time it was fetched, rather
  than a verdict that may no longer hold.
- **Its own credential.** The widget never touches the token behind
  your biometric lock. It gets a lesser one that can read only this
  glance (no balances, no transactions, no account names) and that dies
  with the phone's sign-in: revoke the device in Settings, change your
  password, or sign out, and the widget goes back to "Sign in".
- **Before you have a budget** it says "Set a plan" — like the page,
  it does not render a verdict on a plan you have not made.

Add it the way you add any widget: long-press the home screen, find
Oikonome, pick "Left today" (Android) or the small or medium size (iOS).

## Gotchas

- A projected month-end number is pace, not fate — one big grocery run
  early in the month reads as a hot pace that usually cools.
- In a heavy-bill month the variable budgets may be automatically
  compressed so the month still breaks even (see the Budget guide);
  the card notes the scale, and the map's footnote lists the
  non-monthly bills that caused it.
- Everything money-related shows spending as positive numbers; see the
  concepts guide for the sign convention.
