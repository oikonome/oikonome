# Today: the daily verdict

The Today page (and the daily email — same content, same math) answers
one question: **are you on budget today, and what's the one number to
hit?**

## Before you have a budget

The verdict card does not appear until you have set a monthly budget, and
the daily email does not go out either. There is nothing honest to say
yet: with no plan the arithmetic is "on budget, $0 of $0 left", which is
a judgement on a plan you have not made. Finish setup — or just set Food
and Everything-else in **Budget** — and both start the next day. In your
first month the Budget page may show suggested figures drawn from the
spending seen so far; those are a starting point, not a budget, until
you save them.

("Send today's email now" on the Daily email card in Settings still
sends at any time, if you want to see what the message looks like
before then.)

## The setup checklist

Until setup is complete, Today opens with a five-step checklist (owners
only; the phone app shows the same one): connect a bank or add an
account, bring in history, find your recurring bills, set your two
budgets, pick your primary checking. Each step is ticked by what is in
the household's data, not by having walked past it in the wizard — at
least one account, one transaction, one approved bill, a saved budget,
and a primary checking account pinned (a household with no checking
account at all has nothing to pin, so that step counts as done). The
checking step lists your checking accounts right on the card — tap the
one your bills and paychecks run through; closed accounts kept only for
their history are not offered, and when at least two of them have had
a transaction in the last 90 days, only those are offered. With two or fewer steps left the card
folds to one slim line ("2 setup steps left") naming them, with **show all**
to open it again. Live alerts sit in one line beside it — "2 things need
you" and the most serious of them — which opens the Alerts page (see
[Alerts](alerts.md)). The card goes away once all five are done; the
wizard's last step shows where it stands.

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

A ledger that starts partway through the month (a new account, a first
import) counts its elapsed days from its first transaction rather than
the 1st, and the month's budget is the share for the days from there to
month end (see the Budget guide). Until it has at least a week of
finished days behind it, "under" likewise reads as on budget.

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
compares to plan ("$120 less than planned by today"), with the month's
remaining variable budget as small context beside it ("$1,200 of the
month's $2,000 left · 10 days left", or "$300 over the month's $2,000")
— what to do, not just what happened. The first figure is the pace by
today, the second the whole month's budget. That month figure opens the
Budget page, where the month's bars and the plan live.

## The one number to hit

On the **Summary** face (a new account opens on **Detail**; the **View** switch below changes it) the card shows it as literally one number: what's still
spendable **today** across all your variable budgets, over a day
meter (today's spend of today's allowance), with a small chip per
category ("Groceries $12
today", "Dining over by $4 today"). A category already over contributes $0 —
its allowance can't be borrowed by another bucket, and its chip says
so. Once the month's whole budget is spent, the label reads "Left to
spend today, in budgets not yet over": the number is what the categories
still under their budget hand out today.

The **View** switch (**Summary** / **Detail**) above the cards scopes
the whole page: Summary shows the verdict card alone; Detail brings back the plan line,
savings goals, the Monthly budget map and the Cash timeline. The choice
is remembered on your account and the mobile app follows it — except on
a demo instance, whose shared login makes every settings write refuse, so
there each browser and each phone remembers its own. The daily
email does not — its Summary/Detail shape is its own **email face**
setting on the daily row of your notification settings.

The email also ends differently from the page: the alerts sit at
the **bottom**, in a *Needs you* section that adds one-tap buttons for
proposed bills and uncategorized charges (see [Alerts](alerts.md)). Only
owners and members get that section; a viewer's or guest's copy stops at
the timeline.

In the detail view each category gets a full-width row — the same
bar style as the money map — whose number is **left today**: today's
allowance (fixed at the start of the day) minus what you've spent
today. It moves the moment you spend, so it answers "can I spend more
*today*?" Overspending a day shows red; tomorrow's allowance
recomputes and absorbs it. Under each figure one short line says how the
day stands ("of $40 today · nothing spent yet", "of $40 today · spent
$12", "over today by $8 · spent $48 of $40") — on the page at every width,
in the app and in the email. Every allowance is printed in whole dollars,
rounded down once on the server: a spend-at-most figure is a promise the
rest of the month must keep, so the tile, the day line, the recovery
sentence and the text message all name the same dollar. A category with nothing to hand out today
and nothing spent today folds to one line — its name, "$0 today" and
its daily note — instead of a big $0 over an empty bar. At phone width each row is one line — the name and what is left (or
"over by $X") — over its bar. The bar is day-scoped — fill = today's
spend against today's allowance, with overspend striped past the
budget divider; there is no pace tick because the day is the unit.
The month gauges (with their pace ticks) live in the Monthly budget
card below.

The daily rate — (month budget − spent before today) ÷ the days left,
today included — still drives every number (for a ledger that began
this month, the month budget is its paced share), but it only speaks up when it changes: a tile
whose allowance drifted from the plan says so in prose ("daily reduced
from $61 to $52"). A move too small to change the printed dollars says
nothing, so an untouched plan is never called reduced. A reduced daily also says how to get the old one
back — "skip 3 days of spending and the daily is back to $61" — or,
when the month can't get there, says so. Hold the stated daily and the
month lands on budget.

Hit the daily, end the month on plan — that's the whole game.

## Pinned

Bills flagged "pin to Today" (Bills page, edit a bill) get their own
rows under the categories, in the daily email too. An envelope pin shows
what its pool has used of itself this month (or this year, for annual
pools) — "used $X of $Y this year", the pair every place that names the
pool prints; a
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
the month inside its budget ("To get back on track over the 14 days left:
spend at most $18/day on Food"); a bucket
already through its budget is named as a $0-a-day one. On or under
budget there is no Why — the verdict speaks for itself. Each "by
category" line is headed by its bucket's name, and that name opens the
same rows its tile does.

Every category and bill name on Today — the Food / Everything else
tiles, the money map's rows, the Why's category lines, the pinned bills
— opens the rows behind its number: the category's transactions for
this month, or the bill's history. The money map's **Bills** row opens
the month's bill payments, and the **Envelopes** row the charges your
pools took. Plain text at rest, a button on hover
or a press on the phone.

## The plan, in one line

The bottom of the verdict card shows the month's plan as a flow —
**in → bills → to spend → saved → excess** — with a link to the
Budget page, which owns the detail (income, per-category budgets,
carve-outs, the savings plan). Every figure on the line is the plan's
own: **bills** is the bills evened over the year (a $600 annual premium
counts $50) and **to spend** is the budgets as set, so the line adds up
to its excess in every month. Beside the bills, a month whose schedule
is well away from that average says so and gives its own bills —
"evened out; this month lighter: $940" or "evened out; this month
heavier: $1,420" (nothing within $100).
What this month's bills actually come to is the Bills row of the Monthly
budget card. The "saved" step appears only when you
have a fixed savings plan; a sweep-mode goal sits beside the chain as
its own short sentence ("sweep up to $X — $Y available so far this
month").

In detail mode the **Savings goals** card — on the page, in the app and
in the daily email's detail face — shows each goal's progress and pace.
A goal with a target and a date reads *on pace for MM/DD* or *off pace
for MM/DD* against its schedule (the Budget guide has the rule); one with
a target and no date reads *~MM/DD at the current rate*; one with no
target reads what the last 3 months saved (*$1,000 in the last 3 months*).
A goal with no account is a plan only and shows just its plan; one with
match words but no account says "match words need an account — pick one
on the Budget page", because without an account it cannot be measured.

## Monthly budget

Every budget line as a gauge of its own budget: green is posted
spending, light green is what's still to come, amber is a bill that's
due but hasn't posted, and anything past the budget divider renders
striped (hover a segment for what it means; the email, which has no
hover, keeps a one-line key). A red segment means the line is past its
pace but still inside its budget. The words beside each spending line
read against where it should be by today — "$270 over pace", "$120 under
pace" or "on pace" — the same basis as the verdict's "more than planned
by today", and the dollars beside the lines add up to the **Variable
spending** line's, which is the verdict's own figure. The **Bills** line
reads posted / what the month's bills come to (posted plus still due),
with the one still-due figure the cash tiles subtract beside it — "$450
still due", and "over plan by $X" once the bills that posted came to
more than the plan — and "posted $X · $Z awaiting · plan $Y" in its
caption, the awaiting part (the amber of its bar) being bills due
already and not yet posted. **How this adds up**,
under the row, opens a small table: what was paid, what is still due,
and what bills that posted left under (or over) their plan — each with
its + or −, adding up to the plan. The Bills row is the bills with a
due date; envelopes, monthly and yearly, are the **Envelopes** row just
under it: what the pools paid out this month of what they held, with
"$Y still expected this month" in its caption while the month runs —
the envelopes half of what the month still owes. **Each pool**, under
the row, lists every pool by its own figures ("used $410 of $960 this
year") and ends with what the plan sets aside for them each month. A
pool paid in one go reads as a full bar, never as over; a charge past
a pool reads "$Z past the pool this month, counted in spending" and is
counted once, in Food or Everything else — $Z is exactly what was
counted there this month. The Year lens says the same for the whole
year ("$450 past the pool in 2026"), and a pool resized during the year
is a line of its own there.
The plan line's bills leg is the bills and the pools together, evened
out ("bills & envelopes" when you have a pool). A bill still unpaid from last month is in the still-due
figure but in none of this month's plan, so the table lists it below
the total, with its month and year ("Late from Aug 2026"). A
non-monthly bill already paid this month is listed at what it cost and
marked paid ("annual, paid"). Plan-only lines with nothing
measurable (a savings plan without a destination account, excess cash)
fold into a footnote instead of drawing empty bars.

## Cash: quiet until it matters

Cash warnings have one home — the verdict card — and three states:

- **OK** — no cash line at all. Silence is the signal.
- **Tight** — one calm line: "Cash is tight — $X left until your next
  paycheck (06/14/24) if you paid off all $Y on your cards and the bills
  due before then" ($Y is the card debt the cash card shows, pending
  charges included; with nothing on your cards it ends "after the bills
  due before then"). When the forecast is what runs low, it reads "Cash
  gets tight around 09/16/26 (low $150)", or, when it goes below $0,
  "Cash runs out around 09/10/26 (low −$640 on 09/16/26)". The chart
  marks the same day ("runs out 09/10/26").
- **Critical** — a red block that says what to do ("Short $2,100 by
  07/16/26 — cover it from savings, or slow spending to $X/day"; when
  checking is short today, "Short $X today if you paid off all $Y on your
  cards and the bills due before your next paycheck (06/14/26). …").
  The lines that stop at the next paycheck name its date.
- **No paycheck scheduled** — neither line appears. A forecast that
  counts no income always runs short, so the card says "Add your paycheck
  under Bills & Income to see what's safe to spend" (or that your income
  is switched off there) and links to that page.

## The cash timeline

Cash lives in one card: **checking now**, **card debt**, **paychecks
still due** (the paychecks your income series still have due this
month, each at its series' latest deposit, with up to three of the
dates; a paycheck already posted as pending counts as landed, not still
due), **checking at month end**, and the forecast's **low point**, over a
60-day chart. **Checking at month end** is the balance the cash timeline
ends the month on — the forecast's realistic walk on the month's last
day, "after this month's bills, paychecks, card payments and spending at
your usual pace" — and the timeline carries that day as its own
**checking at month end** row, so the tile and the row are one figure.
Under it a line says what is still on your cards that day and when the
next card payment after the month comes ("with $1,240 still on your
cards then · next card payment $1,180 on 08/03/26"). Without an
income series that is switched on under Bills & Income the note under it
says the figure counts no paycheck, and why: "no
paycheck set up — add your income under Bills & Income and this counts
it"; or, when your income is a budgeted amount rather than a schedule of
paychecks, "your income is budgeted, not scheduled — this counts no
paycheck"; or, when your income series are all switched off, "your
income is switched off under Bills & Income — this counts no paycheck".
Bills and paychecks stop at the same month end: on the month's last day a
bill or paycheck due that day and not yet posted still counts, while one
due on the 1st belongs to next month. A bill from last month that has
not posted yet stays reserved — in this figure, the forecast and the
headroom — for as long as a payment could still be that bill's (its
matching window), so the number does not jump by the bill on the 1st
and then drop again when it posts. Below it, one
chronological table: the month's paid bills and your recent
transactions, a **TODAY** divider, then every scheduled event —
bills on their due dates, envelopes spread evenly, paychecks on their
cadence, card payoffs — with a running balance (the balance column
appears only once checking has a balance to walk from; until then the
verdict card asks you to set it); an event due today or
tomorrow is dated **today** or **tomorrow**. Events show through
the forecast's low point; the rest sit behind an "all N events"
control (the email says how many more there are instead). Paychecks are
matched to the ledger the way bills are: a deposit that has already
posted is in today's balance and is not walked in again on payday, and
a paycheck that is late is simply not projected — unlike an overdue
bill, it is never re-dated to tomorrow. Payday itself is the one
exception: a paycheck due today that hasn't posted yet is walked in
tomorrow, since the walk starts tomorrow, while its line in the table
still names today.

A paycheck that has stopped arriving leaves the forecast. That takes
evidence the money stopped, not just a quiet feed: at least two of its
paydays have passed with the account it lands in still being read, and
no deposit since could be the same check under another name or amount.
A pay pattern with a break about a year earlier (a school-year salary in
its summer) is not counted as stopped. The page names it under the
forecast: "Harbor Payroll has not arrived since 04/10/26, so it is not
in this forecast — turn it off on the Bills page or it will be expected
again when it returns." On the phone this line stays in view, above
**About this forecast**.

Money moves on bank business days. A payday that falls on a weekend or a
US bank holiday is walked in on the business day before, the day a direct
deposit usually lands — unless your own deposits show that the last time
it happened the money came the business day after (or on the day), in
which case the forecast follows them. A bill due on a weekend or holiday
is walked out on the business day after, when an automatic debit posts.
Only the forecast moves: Budget still counts a paycheck in the month it
pays, so a check due Saturday the 1st is next month's income there while
"checking at month end" already holds it on Friday the 31st. The calendar is
the US one; outside the US, only weekends and US holidays move dates.

The forecast spends at your usual pace: your variable spending (Food,
Everything else and their carve-outs) over the last 60 days, per day, so
the 1st of a month is an ordinary day and one heavy week moves it only by
its share of the 60. A ledger younger than 60 days is read over the days
it has; under a week, the forecast spends your plan's daily figure. A
charge your bills now cover is walked as that bill, never as pace. It
leaves out a large one-off purchase — at least $200, at least five times
the window's median day — so one furniture order is not spent again every
day for two months. A pending charge is judged like a posted one. A
merchant's charges in the window count as one purchase (a small
diagnostic before the repair, a deposit before the delivery), as long as
one charge carries most of it; a shop you visit every few days stays in
the pace. The merchant must have no habit: a charge there before the
window of a third of the purchase or more in the last six months, or two
in the last year, keeps it in the pace. The sentence under the chart says
the rate and what it leaves out ("The forecast spends $125 a day on
everyday purchases — your last 60 days, less a one-off charge: … — plus
$33 a day of envelope money"): the envelopes' money is spent beside the
everyday pace. The verdict,
the tiles and "running $X/day" still read this month alone: they say how
the month is going, and still count a one-off, since it was spent.

Most of what you buy goes on cards, and a card's charges leave checking
when you pay the card, not on the day you buy. The forecast times them
three ways, and one switch above the chart picks which one the chart and
the timeline show; the low point of all three sits side by side above it:

- **Your usual** (the default, and the one the cash warning and "left at
  month end" read) — each card is paid the way your own payments show.
  If you usually pay your cards within a few days after payday (most of
  your card payments over the last six months), the forecast does the
  same: the business day after each payday it pays every card off when
  checking can cover it and keep what you usually keep after paying them,
  and otherwise waits for each statement's due date. Otherwise:
  A card you pay about two weeks after you buy is paid about two weeks
  after each charge, never later than the due date of the statement the
  charge is on; a card you pay at its due date (by autopay, say) is paid
  at its statements' due dates. A card with too little history (two
  months of activity, two payments, $300 of charges in the last 90 days)
  is read as paid at its due dates. A sentence under the chart says how
  long card spending takes to reach checking on each card.
- **Pay all cards today** — every card is paid off in full tomorrow at
  the latest (a payment already on its way is not paid again), then
  everything goes on as in "Your usual": can you afford to clear every
  card right now? Its low reads "If you paid every card off today, your
  low would be …".
- **Pay on the due date** — every card paid in full at each statement's
  due date: the latest you would pay.

Money you move between your own accounts by hand — into savings, back
from a brokerage — is not in the forecast. When at least $500 went that
way out of checking, or came back into it, over the last 60 days, a line
under the chart says so and how much ("Transfers between your own accounts
are not in this forecast unless you set one up as a bill — over the last
60 days they moved about $1,200 out of checking.", or "… about $4,000
left checking for your other accounts and about $3,800 came back."). A
bill you set up to pay one of your own accounts is walked like any bill.
A savings plan you set up is in the forecast too; while one is, the line
is not shown.

Under each low point, a line says what that view still has on your
cards that day ("with $2,950 still on your cards then"): paying at the
due date keeps more in checking only by leaving more on the cards.

On the phone the card keeps to the figures and the chart: it shows the
low of the view you have selected, and the sentences about how the
forecast is made — what the month-end figure counts, the daily pace, the
heavier month, how you pay each card — open under **About this
forecast**.

Every view marks each card's statement due dates on the chart ("Harbor
card due"), whenever that view pays the card; hover a due date to see
what that view still owes on the statement then, or "paid".

Beside each balance in the list, **after card balances** is checking
less everything owed on your cards that day — the most cautious measure.
When your usual pattern stays above $0 but this does not, a quiet line
says so: "On <date> your card balances would be about $X more than your
checking — you could not clear every card that day."

The forecast's daily spending splits by where your last 60 days of
spending were charged: the part you spend from checking leaves that day,
each card's part is charged to the card. A bill you pay with a card (the
card that paid most of its last three payments in the last 400 days) is
listed on its day as "… — on <card>" and leaves checking with the card's
payment. Card spending the chart's last day still owes is not dropped:
a line under the chart says how much is paid after it ends. Hover a day
on the chart to see every scenario's balance that day.

A bill whose amount moves with the seasons (a power or heating bill paid
monthly for well over a year, whose amounts vary) is walked at last
year's amount for that month, scaled by how this year's last three months
compare with the same months last year — "Acme Power (estimate)" on
the timeline. Set the amount by hand on Bills and your figure wins for 60
days. Only the forecast uses the estimate; Bills, Budget and the
still-due figures keep the bill's amount.

A paycheck that changed through last year — lower in January while
benefits and taxes reset, higher once a yearly maximum was paid — is
walked the same way this year: with more than a year of deposits, a
coming paycheck is today's amount scaled by how last year's check at that
point in the year compared with last year's check now (by no more than
15% either way), when last year's change held for at least two checks in a
row — or, for January, when a steady paycheck's first check of last year
came in 3% to 10% under the checks before it, even for that one check. A small deposit from
the same payer (a reimbursement) is not read as a paycheck. A raise since
last year is kept. It reads "(estimate, from last
year's pattern)" on the timeline; Budget, the plan and "paychecks still
due" keep today's amount.

Beside each scenario the chart draws a dashed **heavier month** line:
the same walk with everyday spending a set share above the forecast's
pace — the share by which one 30-day stretch in five of the last year ran
above the pace the forecast had going into it, surprise purchases (a
repair, a trip) included, held between 5% and 100%; with under six months
of history, 25%. The legend says the share ("spending 32% above the
forecast's pace, as one 30-day stretch in five ran in the last year") and
the line under the chart its dollars a day. When your usual line stays
above $0 but checking after card balances, or a heavier month, would not,
a quiet line under the verdict says which: "Your usual pattern stays
above $0 (low …). On … your card balances would be about $X more than
your checking — you could not clear every card that day.", or "… A
heavier month … would take it to …". When the usual line itself goes
below $0, the cash warning speaks alone.

**The forecast checks itself.** Every night the app keeps what the
forecast said, and when each day it pointed at arrives it compares the
prediction with your real checking balance. Once there are enough results,
a line under the chart says how close it has landed ("Over the last 3
months, the 14-day forecast landed within $… of your real balance 4 times
in 5. Over your last 13 weeks the 30-day forecast was typically within
$…"), and the shaded band around each scenario becomes the forecast's
own record: the range its past misses suggest. Until then the
heavier-month line stays, and for the first weeks a note says so ("The
forecast now checks itself against your real balance each night; the
first results show here in about 6 weeks."). Tap the line to see how it has done day by day and why
it missed on any day. See [The forecast's track record](forecast-record.md).

**Card debt** on the cash card counts each card's pending charges as well
as its posted balance, since they will leave checking too. A pending
charge is on no statement yet, so a card paid at its due dates pays it with the
card's next statement after the current one, not at the coming due date.
Once it posts it is in the card's balance instead — counted once either
way. The Debt page shows the posted balance, the one your issuer charges
interest on.

A statement you have already paid is not paid again: when your issuer's
last payment came after the statement was issued and covers it, nothing
leaves at the coming due date and the card's balance waits for the next
statement; a partial payment leaves the rest of the statement at the due
date.

If you pay a card by hand, the card usually shows the payment a few
business days before the money leaves checking. Until it does, the
timeline lists it as **Card payment on its way** and takes it out of
checking three business days after the card showed it (never before
tomorrow); once the payment appears in checking, the row goes.

These scenarios test the next few weeks' cash, so they pay each card off
in full. The longer question — when every card and loan is gone,
and in what order — is the Debt page's (see the debt guide).

Each card's **payment date** (a card paid at its due dates) is drawn on the chart as a dashed line
labelled with the card, so a step down in the curve says which payment
caused it. Only dates your card issuer actually reports are marked;
a card that reports none is still paid in the forecast — conservatively,
tomorrow — but gets no line, because a marked date nothing is known to
happen on would read as fact. Cards due the same day share one line.

If any account still sits at an unset $0 balance, the forecast hides
rather than present $0 as truth; the verdict card says so instead
("Cash forecast is waiting on your balance", with a link to Accounts).
Set real balances on the Accounts page and it appears.

The forecast starts from your checking balance, so with no checking
account linked (only cards, or only savings) there is nothing to start
from. The forecast and the cash warnings stay off, and the verdict card
and the daily email say "No checking account linked" with a pointer to
Accounts instead of reporting a shortfall against an account you do not
have. Link your checking account and the cash picture appears.

## On your phone's home screen

The mobile app ships a home-screen widget that shows the simple face of
this page: the one number left to spend today, the verdict, and the day
bar. The wider size adds the per-bucket chips. Tap it to open Today.

- **Same number as the app.** The widget is fed by the same door that
  composes the hero, in whole dollars, so it can never disagree with the
  page or the daily email.
- **Refreshes on the phone's clock** — about every half hour, and again
  whenever you open the app. When the last refresh is more than an hour
  old, or was fetched before today began, the widget turns grey and
  shows the time it was fetched, rather than a verdict that may no
  longer hold.
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
- Totals, budgets and the map show spending as positive amounts; a list
  of single transactions (the Month view's Biggest transactions) shows
  money out with a minus sign. See the concepts guide for the sign
  convention.
