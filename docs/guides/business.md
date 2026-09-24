# Business tracking: keep an entity's money separate

Run a business — an LLC or sole proprietorship — alongside your personal
finances, with the entity's money kept genuinely separate rather than
mixed into your personal ledger.

Find it on the **Business** page. The **Business** tab in the main
navigation, just above Accounts, appears only once a business exists — most households never
run one. The first entity is created through the **Business wizard**
(Settings → wizards, or the **Set up a business** card that the
`/business` page shows while it is empty); it walks you through naming
the entity, assigning its accounts and scanning for start-up costs, and
the tab appears as soon as it saves. Until accounts are assigned the
page keeps a **Continue guided setup** link to finish.

The page has its own row of tabs: **Overview** (the handful of numbers
an owner actually checks), **Review** (the Schedule C queue), **Books**
(P&L, balance sheet, owner equity), **Tax** (set-aside, year-end
package, estimated taxes, contractors, compliance), **Receipts**
(capture and the mileage log) and, pushed to the right, **Settings**
(details, accounts, tax reserve, members, archive).

## Declare your business

Add an entity with its name, structure (single/multi-member LLC, sole
prop, S-corp), state, EIN, formation date, and the date it began
operating. The **EIN is encrypted at rest** — only its last four digits
are ever shown back. You can run more than one entity. Under its
**Settings** tab you can also list **members** (name, ownership %,
manager), and under **Tax** set an **income-tax rate %** and
**home-office sq ft** that feed the estimates below; the API additionally
accepts a registered agent and a fiscal year end.

## Hard separation — the point

Assign a **whole account** to the entity (a business checking or card)
and every transaction on it becomes business money. For a one-off
business cost paid on a personal card, flag it on the Transactions page,
then **import your flagged transactions** into the entity from the
Business page — flagging alone is just a tag until it's imported.

Once money is assigned to a business, it **leaves everything personal** —
your daily verdict, Today page, the morning email, budgets and envelopes,
allowance, spending and cash-flow reports, net worth, and the cash
forecast. That's the whole "don't commingle" story, enforced in the
software. The one place you still *see* it beside personal money is the
Transactions page: a business account's rows are listed there, each with
a pill naming the business, so its history is one tap from the Accounts
page — but they never count toward the page's totals or day subtotals.
The **more ▾** filters can narrow the list to household only or business
only.

- **Combined view** — a checkbox that brings business + personal back
  together in every view when you want the full picture.
- **Archive** is how you remove a business you've closed (or stopped
  tracking). It's never destructive: the entity goes read-only and drops
  out of active use — no new assignments, no future filing deadlines —
  but **every record is kept**: its books, equity ledger, mileage log,
  1099 vendors, compliance entries, and transaction assignments all stay
  readable, from the collapsed *Archived* list on the Business page. You
  can **restore** an archived business any time. Tax records carry
  retention obligations, so this is the removal you almost always want.
- **Delete forever** exists only for a business created by mistake, and
  only the household **owner** sees the button (archive and restore are
  open to any editor). It asks you to type the entity's exact name (checked by the server, not
  just the screen), tells you exactly how many records will be destroyed,
  and then permanently deletes the entity and everything recorded under
  it. Its accounts and transactions are never deleted — they return to
  personal.

## Owner equity

Capital contributions, owner's draws and distributions, and
reimbursements are their own classes — they hit the entity's **capital
account**, never spend or income. The capital balance is contributions
plus retained earnings (cumulative net operating income) minus draws
and distributions — the page spells the sum out beside the figure. A
business cost you paid personally can be **capitalized** (recorded as a
contribution) or **reimbursed** (the business owes you back) — both keep
the books straight. Do it right from the **Transactions page**: open any
expense's category menu and pick **capitalize (owner contribution)** or
**reimburse from business** under your business; the transaction moves
to the entity and the equity movement is recorded in one step.

## Books, P&L, and Schedule C

Each business transaction sorts into one of three buckets:

- **Organizational (§248)** — the cost of *creating* the entity (filing
  fees, legal).
- **Start-up (§195)** — other costs before you opened. The app shows the
  first-year deduction — up to $5,000, shrinking dollar-for-dollar once
  total start-up costs pass $50,000 — and the remainder that amortizes
  over 180 months. Anything dated before your business start date lands
  here by default; move filing and legal costs to organizational
  yourself.
- **Operating** — ordinary post-open expenses, tagged to a Schedule C
  line.

The **Review** tab is the **Schedule C review** queue: every business
transaction without a line yet, with a suggested bucket and line you
accept or override one row at a time; it says "Nothing to review" when
the books are caught up.

The **P&L** shows revenue versus categorized expenses, the equity
summary, and a CSV export your CPA can consume. These figures are
informational — Oikonome categorizes and exports; your CPA decides the
tax treatment.

## Self-employed tax hygiene

For freelancers and single-member LLCs, the pieces QuickBooks Self-Employed
is known for — kept lean:

- **Estimated taxes** — from your net profit the app estimates self-employment
  tax (Social Security capped at the wage base; nothing at all when net
  self-employment earnings are under $400) and, if you set an assumed
  income-tax rate, income tax; gives you a **quarterly set-aside** number;
  and — only once that rate is set — adds the next 1040-ES deadline to
  your compliance calendar (and its `.ics`).
- **Set aside for tax** — the card at the top of the **Tax** tab puts the
  year's estimate beside the cash that covers it and says **Covered by**
  or **Short by**. Measured against all business cash it answers "is the
  money there"; nominate a **Tax reserve** account (a business checking
  or savings, under the entity's **Settings**) and it measures that
  account instead, answering "is it ring-fenced".
- **Year-end package** — one **Download package** button on the Tax tab
  zips the P&L, the ledger with Schedule C lines, the balance sheet, the
  1099 vendor totals and the mileage log for your accountant.
- **Mileage** — log trips; the IRS standard rate in force on each trip's
  date turns them into a Schedule C car-expense deduction (2026 has two
  rates, 72.5¢ through June 30 and 76¢ from July 1, so a year's summary
  lists each rate with its miles).
- **Home office** — the simplified method ($5/sq ft, up to 300).
- **Balance sheet** — a plain snapshot of business assets minus liabilities
  next to your capital account (not double-entry books).
- **1099 contractors** — mark who's a contractor; anyone you paid $600+ in a
  year gets flagged as needing a 1099-NEC. The panel lists the top payees by
  amount; `vendors-1099.csv` inside the year-end package has every vendor
  with its total.

All of these are **estimates to help you set money aside and file** — not tax
advice. Oikonome doesn't do payroll, invoicing, inventory, or sales-tax
filing; for those you want full accounting software.

## Compliance calendar

Upcoming filing deadlines with reminders and an **.ics** download for
your calendar. For the states in the built-in table (Idaho, Nevada
and Washington today) the LLC annual-report deadline is derived from the
entity's state and formation date — the last day of the anniversary
month; add another state's deadline, or any other obligation (a franchise
tax, a federal filing), by hand.
