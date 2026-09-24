# FAQ

Short answers to the questions people actually hit. The other docs carry
the full stories.

**Spending shows positive, income negative — is that backwards?**
No — money out is positive everywhere inside the engine (your bank's
convention is flipped on display). If an *import* came in reversed, undo
it under Import → Recent imports and re-import with the other sign
option.

**Which version am I running, and what changed in it?**
The footer of every page (and the More tab on the phone) shows the
version. It links to the release notes on GitHub, one entry per release,
so the two questions have one answer.

**Today says OVER BUDGET but my bills are all paid — why?**
The verdict watches **variable spending only** — your Food and
Everything-else budgets, prorated to the day of the month. Bills never
push it OVER or UNDER; they live in the plan and the cash forecast
instead. OVER means variable spending is ahead of pace, nothing else.

**My checking balance reads $0 (or is just wrong).**
File imports don't carry balances, so a file-only account starts at $0.
On the Accounts page, edit the account and type the real balance (manual
accounts have an editable $ field), and tick **primary checking** so the
cash forecast anchors on it. Connected accounts get their balance from
the provider on every sync.

**Can I get the summaries as a text message or a notification instead
of email?** Yes — the **Scheduled summaries** matrix in Settings →
Email → Daily email card is per-cadence: each of daily / weekly /
monthly / yearly can go out by email,
SMS (a short form; verify your phone number first), push notification
(enable it per browser), or any combination. If you use the mobile app
and your server opts in to mobile push (`OIKONOME_PUSH_RELAY_URL`),
signed-in phones are tickled on the same push preference. The daily email itself has
two shapes, chosen on the daily row's **email face** picker: **summary**
sends just the verdict pane — the one number, its category chips, and
any pinned bills — while **detail** sends the full report with the
per-category rows. This is the email's own setting, independent of the
Today page's summary/detail toggle: what lands in your inbox and what
the page opens to are separate preferences. SMS needs the instance to
have Twilio credentials configured. **Push needs HTTPS**: browsers only
allow notifications on a secure origin, so a self-hosted instance reached
over plain `http://` on your LAN cannot enable them — the browser refuses
without ever asking you. Give the instance a certificate with
`./oikonome.sh https <domain>` (or reach it via `localhost`) and the
button works. Texts carry their **own opt-in box** next to the
number, covering the budget verdict (about one a day); msg & data rates
may apply, and you can reply **STOP** to cancel or **HELP** for help at
any time.

**What does the mobile push relay see about me?** Phones can only be
pushed through Apple/Google's push services, and those require the app
publisher's credentials — so a self-hosted server can't push a phone
directly, and pushes route through a relay run by the app's publisher
instead.
It's **off by default** on self-host (set `OIKONOME_PUSH_RELAY_URL` to
opt in, plus the `OIKONOME_PUSH_RELAY_KEY` the relay's operator gives you
— a relay whose operator has configured instance keys refuses pushes
without one; a relay run with no keys stays open). What your server
sends the relay per push is the device's opaque push routing token,
which platform it belongs to (Apple or Google), an event kind from a
fixed list (`daily`, `weekly`, …), an optional badge number and — for
the daily push only — the one line the lock screen shows: the verdict
("OVER budget by $120"), the top driver and today's allowance, exactly
what the SMS says. Never a merchant, a name, a balance or an email, and
nothing at all on the other pushes — the app wakes and fetches the
actual content from *your* server over its own authenticated
connection. If you would rather the relay and the phone platforms saw
no figure at all, set `OIKONOME_PUSH_CONTENT_FREE=1` on your server and
the daily push reads "Today's verdict is ready" instead. The relay
stores nothing and doesn't log tokens; what it could observe is which
tokens get which kind of tickle, from which server address.

**Why can't I take a screenshot in the mobile app?** By default every
signed-in screen refuses screenshots and screen recording (money and
recovery codes), and the app switcher shows a blank card. Turn it off
per device under **Settings → Security → Block screenshots and screen
recording** — to record a walkthrough, share your screen with support,
or because it's your phone. Off releases both.

**I installed the mobile app and I don't have an account — now what?**
Accounts are created on the server, never in the app. The sign-in
screen offers the door your server has open: *Create one* when
self-service signup is on, or a link to the product site; a self-hosted instance has no signup door at
all — the operator's setup link or an invite makes accounts there.

**SimpleFIN or CSV import — which should I use?**
SimpleFIN syncs transactions and balances automatically every hour
(US/CA, ~$1.50/mo paid to the bridge). File import (CSV / OFX / QFX /
QIF) is free and works everywhere, but it's manual and carries no
balances. They mix fine: connect for the ongoing feed, import files for
older history — the importer stitches them with no duplicates.

**Where did the Recurring page go?**
Renamed. It's the **Bills** tab now — same page, same features
(detection, proposals, envelopes, payee history). The Bills guide
covers it.

**What do "This month" and "1m" mean?**
Every timeframe toggle on the site offers the same eight windows (This
month, 1m, 3m, 6m, 1y, 3y, 5y, All). **This month** is the current month
so far, and **1m** is the last complete month, closed before today, so on
the 13th you can read August whole instead of thirteen days of September.
The longer ones count back from the current month to today. On Cash Flow
the window applies to every section of the page: the flow picture,
spending and income, and the comparison line reads against the month
before.

**Why am I asked whether two merchants are the same?**
Card terminals and banks spell a business several ways: cut at 13 or 20
characters, with the city glued on, without the apostrophe. Big chains
arrive already identified; a local shop does not, so each spelling starts
as its own merchant. One exception: where your bank's line and your data
provider have agreed on the same shop several times over, and the
provider then calls a single charge on that same line something the line
does not say, the charge stays with the shop the line means — the name
from that one charge is set aside, and a real business of that name still
gets a merchant of its own when you actually visit it.
When the app sees spellings that look like one
business it queues them under **Needs a look** at the top of the
**Merchants** page, one line per pair with the reason ("cut at
13 characters, a terminal's limit"); **details** shows both merchants'
rows, totals and latest bank lines beside what the merged one would hold. A quiet line on the Today page, the
daily email and the bell says how many a night's run found; it comes back
only when a later run finds more, not as you work through the list. You
choose:
**Merge** joins them, as if you had renamed one to the other, and shows
under Recent changes where you can undo it; **Not the same** keeps them
apart and the pair is never offered again. Nothing is merged unless you
say so.

**How do I rename a merchant, or merge two?**
Open the merchant on the **Merchants** page (web: it opens beside the
list; app: its own screen). **Rename** takes a new name. **Merge into…**
searches your merchants and moves every row under the one you pick, so
a typo can never fork a new merchant. Both are undoable under Recent
changes. The list's filters answer the usual questions: *one-off* shows
merchants with a single charge, *unnamed* the ones still wearing a bank
string as their name, *seen this month* what has been active lately.

**How does a bill know which charges are its payments?**
By merchant. Every bill lists the merchants it pays (the **Merchants** row
on its edit form, filled in automatically from your ledger when the bill
is created). A charge is the bill's when the ledger shows it under one of
those merchants and the amount is within the bill's band (a quarter of the
bill, with a small floor). So when your bank starts spelling a payee
differently, or you merge two spellings on the Merchants page, the bill
follows the merchant rather than the letters. When the nightly pass sees
your charges filed under a new merchant name, it offers to add it to the
bill as a proposal on the Bills page; nothing is added silently. A bill
with no merchants listed matches by the words of its name instead.

**What's the difference between a monthly and an annual envelope?**
A monthly envelope caps a bill at $X per month; an annual envelope gives
it one pool for the whole calendar year (for lumpy bills — insurance,
vet, car repairs). Either way, spending past the cap counts as regular
variable spending. Pick the cadence when editing the bill on the
Bills page.

**I recategorized one transaction and the whole merchant changed — why?**
That's the design: a correction teaches a **merchant-wide rule**. Every
past transaction from that merchant updates immediately, and future ones
land pre-categorized — even where the aggregator disagrees. Two things
are never swept along: flow categories (transfers, income, loan
payments) and rows that carry their own per-transaction override. The
Rules page lists every learned rule with its provenance; correct,
disable, or delete any of them there.

**How are merchants categorized automatically?**
In order of authority: your own corrections (always win), the bank's
label where it is concrete and confident, a built-in list of known
chains, then — when the deployment provides one — a small local
classifier that fills in what the bank left vague. The classifier runs
on the instance itself (your transaction data never leaves it), only
answers when it is confident, and can never move a transaction out of a
concrete category the bank asserted. Operators enable it by mounting a
model file (`OIKONOME_CATEGORIZER_MODEL`); an optional LLM backend
remains supported and takes whatever the classifier abstains on.
The classifier also **learns from you**: each night the instance
retrains a small per-household model from your accumulated corrections,
so fixing one merchant teaches it about the next lookalike. That learned
model lives in your database (it rides backups, and rebuilds itself from
your corrections after a restore either way), never leaves the instance,
and works even if you never mounted a classifier model file. It holds
its own, stricter confidence bar than the shipped classifier (0.70 vs
0.55 by default) since it trains on far fewer examples; tune it with
`OIKONOME_CATEGORIZER_OVERLAY_MIN_CONF` independently of
`OIKONOME_CATEGORIZER_MIN_CONF`.

**Why does this transaction have the category it has?**
Ask the row. Open its **⋯** and one line under the details says who
decided: the bank's own label and how confident it was, a rule you taught
by correcting this merchant, a bill that categorizes the charges it
matches, a category you set on this one row, or the merchant's registered
line of business. Anything you set yourself outranks all of them.

**Where do merchant logos come from, and can I turn them off?**
From the bank connection: an aggregator that identifies the merchant
(Plaid does, for most national merchants) supplies the logo, and your
instance fetches it once, keeps a copy (the cache has a size cap and
drops the oldest first; a lookup that found no logo is retried after a
day), and serves it from
itself — your browser never talks to the provider, so a ledger full of
logos discloses nothing about you. Imported rows share the logo when
their merchant matches one the aggregator already identified, and a
chain outlet inherits its brand's. Untick **Show merchant logos** under
Settings → Merchants for a plainer look everywhere (the daily email
never carries images either way).

**What are the little chips on a transaction row — `#4417`, `via
PayPal`, `online`?**
Facts the bank sent with the charge: the check number for a written
check, the payment app that stood in front of the merchant, and how the
payment was made. The **⋯** panel has the rest — where it happened, the
authorized date when it differs from the posted one, and the merchant's
line of business. The **more ▾** filters can narrow the ledger to one
channel, or to checks only.

**Why does the category dropdown say "saving…"?**
That's a progress state, not a category. While your change is being
written the select shows **saving…** and locks; once the server
confirms, the row shows the new category. If the write fails you get an
error message and the row keeps its old category.

**I fixed a category — will the next sync undo it?**
No. Your override is permanent: syncs never touch it. See above for how
one fix propagates merchant-wide.

**What is the "biz" button on a transaction?**
A **business-expense tag** (think Schedule C) — deliberately separate
from categories. Flagging changes no budget math: the money still left
the account, so the verdict and budgets are unaffected. Flagged rows
collect on the business list with per-year totals and a CSV export;
refunds you flag net against the total.

**On the Year page, some months say "net" instead of a verdict — why?**
A verdict compares spending against the budget that month actually ran
under. Your budget — and your bill schedule with it — is frozen
("snapshotted") as each month closes, so later edits (a raised budget, a
changed bill amount, a resized envelope) never rewrite a finished
month's report card. Months from
before you set a budget have no snapshot to judge against, so they show
**net** — income minus spending, a cash-flow fact rather than a budget
verdict. A closed month opened on the Month page without a snapshot says
so: it's compared against your current budget, applied retroactively.
If you'd rather those months carry verdicts, the Budget page's **Budget
history** card can apply your current budget to them — an explicit
choice, and the history labels those months "backfilled".

**Why is a bill I paid early still listed this month?**
Paid occurrences show with a ✓ and stop reserving cash. If it looks
wrong, check the bill's cadence on the Bills page — a drifted due
date is the usual cause.

**One charge was several things — can I split it?**
Yes: the row's **⋯** menu → **split across categories** on the web, or
**Split…** on the transaction screen in the app. The parts must add up to
the charge, and every per-category number counts the parts. Only spending
can be split — a refund, a transfer or a card payment stays one row. See the
transactions guide.

**Who changed this category (or bill, or note)?**
Settings → Users → **Activity** on the web, More → **Activity** in the
app: every hand-made change in the household, with the person and the
time. What the app does on its own (syncs, the categorizer) is not listed.

**Can I get my numbers into Home Assistant, Grafana or an AI assistant?**
Yes — Settings → Integrations. Webhooks push events out, `/metrics` and
`/api/integrations/summary` answer a scraper or a sensor, and a one-file
MCP server in `integrations/mcp/` lets an assistant read the books. All on
a read-only token. See [integrations.md](integrations.md).

**What does my family do with this if something happens to me?**
Print the **continuity packet** (Settings → Data): one PDF of the
accounts, debts, bills, income and businesses, with a note in your words
about where the rest is. No passwords go in it. See the continuity packet
guide.

**How do I back up — and restore?**
`./oikonome.sh backup` writes a compressed `pg_dump` into `./backups`
inside the install folder; that one file is the whole state. Restore any
dump with `./oikonome.sh restore` — it lists what's available, confirms,
snapshots the current database first, and swaps the database in place.
Uninstalling offers to move `./backups` out to your home directory
before it removes the folder, so backups outlive the install.

**I restored an export and it's still going / an endpoint went missing.**
A full restore runs in the background: drop the ZIP on the Import page and
it reports progress while it merges, table by table. A large archive —
tens of thousands of transactions — takes a few minutes, and it keeps
going if you close the page. Two things you may see when it finishes:

* *"1 AI backend dropped — not reachable from this instance"*, or the same
  for a mail server. Endpoints in an export point at the machine it came
  from — a model running on your own network, a local mail relay — and
  those addresses mean nothing anywhere else, so they're left out rather
  than restored broken. Anything routed through one falls back to this
  instance's default; re-point it under Settings if you want something
  else. Nothing else in the archive is affected.
* *"N already present"*. Restoring the same archive twice is safe: rows
  the account already has are skipped, not duplicated, and the summary
  counts them separately from what actually landed.

**I'm locked out and never set up email.**
Run `./oikonome.sh reset-password` on the host — it prints a one-time
reset link, no SMTP needed. The troubleshooting doc has the details.

**The daily email isn't arriving.**
Check **Settings → Email & Push** first: each recipient is listed with whether
the instance's mail is reaching them. An address the mail server refused is
marked *not arriving* — with the reason it gave — and its scheduled emails
are paused until it's fixed, because retrying a dead address every morning
gets the rest of the instance's mail treated as spam too. Fix the address
(Settings → Security → Email / username) and confirm the link sent to it;
delivery restarts from there, and nothing you missed is lost — the app
always has the full picture.

If nothing is flagged, delivery itself may not be set up: Doctor → Email
shows whether it is. **Self-hosted:** delivery (SMTP) lives under Settings
→ Email or in `docker/.env`. **On an instance an operator runs for other
people:** the operator's own mail server sends it — there is nothing for
you to configure, and the SMTP fields are not shown. Either
way the send hour is in Settings → Email & Push — until you pick one, the daily
email goes out at 7am — kept in your household's time
zone (shown beside the hour; the first time the owner opens the web app
or the phone somewhere other than the instance's zone, that device's
zone is adopted silently, once — and you can change it there).

**I added someone and they aren't getting the email.**
Adding an address invites it — it doesn't sign anyone up. When you save,
that person is emailed once, explaining what the daily summary is, who
added them, and when it arrives; nothing else is sent to them until they
open that link and accept. Until they do, their row in **Settings →
Email** says *invite sent*. Once they accept it says *invite accepted*
and the summary starts the next morning.

This is on purpose: the daily email carries your real balances and
spending, and the only person who can agree to receive that is the person
whose mailbox it is. It also means a mistyped address can't quietly
deliver your finances to a stranger for a month — an address that never
answers never receives anything.

**How do I give my partner their own login? Why does /signup send me to
the sign-in page?**
A self-hosted instance is claimed once, through its setup link, and stays
one household — so it does not take signups, and the page says so by
sending you to sign-in. What you want is the invite under **Settings →
Users**: that gives the other person their own login, their
own password and second factor, on *this* household's accounts and
history. They can mute their own copy of the daily email, and remove
their own login later, without touching anything of yours. A household
holds up to ten logins; in hosted mode a newly claimed login also
confirms its email address by link before any household mail goes to it.

**Signup says accounts are capped — what does that mean?**
An operator can cap signups at a set number of accounts while an instance
grows (the page shows the live number). The refusal names the operator's
support address when one is configured, and offers an access-request form
when the operator's add-on has one. A self-hosted instance has no cap.

**I never clicked the confirmation email — what happens?**
On a hosted instance the account keeps working, but no household email
goes out until the address is confirmed, and the banner offers a resend.
After a week you get one reminder with a fresh link. After 30 days the
account is frozen for a week — a last email names the date and carries a
fresh link, and the frozen screen has a **Resend** button — and then it is
deleted with everything in it. Opening any of the links confirms the
address; while the account is frozen, the page that opens also carries a
**Reopen this account** button, and pressing it brings the account back
at once. Households with a subscription, or where anyone has
ever confirmed, are never touched; a self-hosted instance creates
accounts already confirmed. Operators set the window with
`OIKONOME_UNVERIFIED_REAP_DAYS` (0 turns it off).

**What do the other recipient badges mean?**
*invite expired* — the link timed out after 14 days; **Resend** sends a
fresh one. *declined* — they said no, and re-adding them will not ask
again. *not invited* — the address is saved but has never been asked;
**Invite** sends one. After a **restore**, answers carry across but links
don't: a *declined* stays declined, an *accepted* stays accepted for
anyone who has their own login on this household, and everybody else
lands here as *not invited* waiting for a fresh **Invite**. The restore's
own summary tells you how many that is. *not arriving* — the mail server is
refusing that address; see the entry above. The account owner's row
carries a plain *you* badge instead of an invite state: it's your own
summary and there's nobody to ask — but the
checkbox on each row (yours included) turns that person's copy on or
off, so the schedule can keep mailing the household while skipping you.

**How do I stop the emails without asking the owner?**
Every household email — the daily verdict, the weekly report, the
monthly report card, the year in review and the alerts — ends with an
**Unsubscribe** link for the address it was sent to. Opening it and
pressing the button is the same as the owner unticking your row: every
one of those emails stops for that address, immediately. Sign-in and
security emails are not affected, and the owner can turn you back on
under Settings → Email & Push. Most mail apps also show their own Unsubscribe
button for these messages; it does the same thing.

**Why does the daily email have no merchant logos in it?**
On purpose: nothing in the email is fetched from the web. An image loaded
from a server when you open a message is an open-tracking pixel by another
name, and it breaks anyway for a mail client behind an image proxy or a
self-hosted instance on a private network. The cash chart and the brand
mark are the two pictures the email does carry, and they travel inside the
message itself (as inline parts referenced by the HTML, not as downloads),
so opening the mail contacts no one. Merchant logos stay on the pages.

**Where do I change budgets now?**
The Budget page owns the whole plan — income, food/other budgets,
carve-outs, savings goals. Settings keeps only account-level things.

**A connection says "never synced" after a file import.**
One-time file imports aren't connections — they show as an
informational row. Only aggregator links (Plaid/MX/SimpleFIN) and
collector scripts have a heartbeat.

**A connection says it was removed ("reaped") — what happened?**
It failed for **30 consecutive days** with an error only you can fix —
expired login or revoked consent — so the app disconnected it
automatically (a dead aggregator link otherwise keeps costing forever).
Transient problems (institution outages, rate limits) never count
toward the 30 days, and any successful sync resets the clock. Your
accounts and history are untouched; the old credentials are gone, so
the fix is the **reconnect** button on the Accounts page — a fresh
link, not a repair. Self-host: the window is
`OIKONOME_PLAID_REAP_DAYS` (0 disables the reaper).

**Adding a bank says I've reached a limit.**
An instance may cap how many institutions can be connected; the cap, and
the wording around it, come from whoever runs the instance (the Accounts
page shows how many of the allowance are in use). Only *adding* a new one
blocks — syncing, repair, and re-auth of existing connections always work.
Disconnecting an institution on the Accounts page frees its slot; manual
accounts, file imports, and collector scripts never count. A self-hosted
instance has no cap.

**How do I undo an import?**
Import → Recent imports → roll back. Every import is batch-tagged and
reversible, including bulk runs.

**Why did deleting my account ask for a recovery code?**
An account that signs in with **only a passkey** (no authenticator
app) can't prove the passkey inside a plain form, and the password
alone isn't enough for an irreversible action — a stolen session plus
password could otherwise erase everything. Confirm with your passkey
when prompted, or spend a recovery code. The same step-up guards
password and email changes. Self-hosted instances and accounts with an
authenticator app enrolled use their one-time code instead.

**I've lost my password, my authenticator and my recovery codes. Am I
locked out for good?**
No, but not instantly either. Request a reset link; when the reset page
asks for a recovery code, choose **Start the 7-day recovery**. The
account is told on every channel it has, one click on that message (or
any sign-in) cancels it, and after seven days a fresh reset link works
without a code — for 30 days, after which the recovery goes stale and you
start another. Or ask the operator to clear the second factor after
checking who you are — email your instance's support address.
Details in the [account security guide](guides/security.md).

**How do I send feedback or report a bug?**
The **Feedback & bug reports** tab (`/feedback`) takes both: pick
**Feedback** for something confusing, missing, or worth changing, or
**Report a bug** for something broken — that kind prompts for what
happened, what you expected, and which page you were on. Attach a
screenshot, submit. With SMTP configured the report emails itself to the
inbox the operator named in `OIKONOME_FEEDBACK_TO` (unset, the tab hands
you the .zip instead), with
`[bug]` or `[feedback]` in the subject so the inbox sorts itself; without
SMTP you get a downloadable .zip to email manually. Either way you get a
report number to quote.

**What's "the one number to hit"?**
What you can spend per day for the rest of the month and still land on
budget: (month budget − spent so far) ÷ days left. It's the "left
today / daily rate" tiles on Today and the heart of the daily email.
The Today guide has the full math.

**What is "Excess cash"?**
The plan's leftover: income − average bills − budgets − savings plan.
It stays in checking (the forecast shows it piling up) and is not extra
spending headroom in the verdict. Setting Savings to an explicit $0 is
remembered — the field won't refill itself.

**What does the striped part of a money-map bar mean?**
Overspend. Each bar is a gauge of its own budget; spending past the
budget shows as a striped segment past a solid divider (solid darker
red in the email). Filled = spent, light = still to come, amber = a
bill awaiting its due date, and the small vertical tick through the
bar = today's pace.

**Only part of a charge was reimbursed — how do I record that?**
Link the deposit as a partial reimbursement: the charge keeps its
category and spending counts only the un-reimbursed remainder. One
insurance check can split across several charges. See the
reimbursements guide.

**Do receipt line items change my budget?**
No. The transaction's total is the only number the verdict and budgets
see — line items explain a charge (and drive the tag-based expense
report), never re-bucket it.

**A bill's amount changed and I didn't touch it.**
Amount drift auto-applies: when a bill settles at a new real amount
(subscription price hike), the bill updates and leaves an audit entry.
Your own hand-edited amounts are immune to drift for 60 days.

**I linked two sources for one account — will it double-count?**
No. The healthiest preferred source serves the numbers; the rest are
shadows, excluded from every money aggregate but still syncing so
failover is instant. See the accounts guide.

**What does the retirement "% of history" mean?**
Your plan replayed against every start year since 1928 with real
historical returns — 84% means the plan survived 84% of those start
years (98 sequences in all). It captures early-crash risk that
average-return projections hide. See the retirement guide.

**Why does the old part of my net worth chart look approximate?**
The trend is reconstructed backward from today's exact balances through
transaction and holdings history — recent years are sharp, early years
are estimates (and shaded as such). Today's number is always exact.

**Does the Assistant send my finances to a cloud AI?**
No — not unless you pointed it at one. The Assistant runs on the
backend the Assistant task is routed to (Settings → AI): the bundled
local model by default, so your data never leaves the box. Categorization
can stay on a local model even when the Assistant uses a cloud one. It can only call a fixed set of
read-only summaries (no free-form database access, no writes) and
phrases its answers from what those return. If you configured a remote
LLM endpoint in Settings, those summaries go to the endpoint *you*
chose. No backend configured → the Assistant hides entirely.

**Is my data sent anywhere else?**
No. Self-hosted instances talk only to the aggregators you configure
and (if set) your SMTP and LLM endpoints. The Doctor page lists every
outbound dependency.
