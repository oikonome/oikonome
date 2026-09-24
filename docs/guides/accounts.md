# Accounts and connections

The Accounts page lists every account grouped by institution, with a
status dot per connection, a per-connection sync (↻) action — plus a
fix (↗) action on Plaid connections — and two collapsed sections at the
bottom: **Hidden** for accounts you chose to set aside and **Archived**
for closed ones. Each institution shows its own mark beside its name —
the bank's logo when the provider supplied one at link time, otherwise a
monogram in the bank's brand colour. **Show merchant logos** under
Settings → Merchants turns these off along with the ledger's.

On an instance whose operator supplies the provider keys, the button that
adds one reads **Connect an account** — a card, a brokerage, and a bank all
arrive the same way. On self-host it reads **Add via Plaid**, with SimpleFIN
and other ways to connect tucked under a "more ways to connect" disclosure
beside it.

On the phone the same choices are two small doors in one row at the top
of the screen instead of a card of explanatory text: **＋ Connect
account ↗** goes to the bank's own sign-in, and **＋ Add account** opens
the manual-account form. They are not the same permission — connecting a
bank is the **owner's** door, while any **member** can add a manual
account, so a member sees only the second. A self-hosted owner also gets
a quiet **SimpleFIN…** link there that unfolds the token box in place. A
view-only login sees neither door. Where an institution cap applies and is
reached, the Connect door goes flat and the count says why. A connection
brought back by a restore and still waiting to be reconnected holds no
place in that count.

## Ways data gets in

- **Bank connections** — hourly automatic sync of transactions and
  balances, via SimpleFIN (recommended, US/CA) or Plaid/MX (bring your
  own keys on self-host). Where an operator runs the instance for other
  people, connections run through the operator's provider keys — you never
  add your own.
- **Community scripts / collectors** — host-side scripts that push
  data for institutions no aggregator covers. They run on **your**
  machine (that's where the credentials stay); the **run** button shows
  the exact host command rather than pretending the app can run them.
- **File imports** (CSV/OFX/QFX/QIF and app exports) — free, works
  everywhere, manual, carries no balances. A file import is not a
  connection: it shows as an informational row with no heartbeat.
- **Manual accounts** — you type the balance; nothing syncs.

They mix freely: connect for the ongoing feed, import files for older
history — the importer deduplicates against connected sources.

## Connection states

The dot left of each institution carries most of the signal, though a
row can carry one extra line of context beside it:

- **Green** — healthy; last sync succeeded.
- **Amber** — nothing for you to do, for one of two reasons. Either the
  connection has not synced recently, or the bank itself is down or
  slow — Oikonome keeps retrying and the row says **bank having
  trouble — retrying**. Or the connection is syncing fine but the
  bank's own sign-in rail is down fleet-wide — Plaid tracks each
  bank's login health separately from your connection, refreshed by
  the hourly sync and only shown while the reading is under a day old
  — and the dot turns amber with a **bank-side trouble — may clear on
  its own** note even though nothing here is actually broken. (A bank
  merely *degraded* on Plaid's tracker leaves a healthy connection
  green — big banks sit degraded for weeks — but a broken row will
  still warn that its fix may not go through yet.) Either
  way, an outage is never offered a "fix" button, because
  re-authenticating cannot repair a bank that is not answering, and it
  clears on the bank's side on its own.
- **Red** — broken (expired login, revoked consent). On a Plaid
  connection a **fix ↗** button re-authenticates the existing
  connection; your accounts and history stay attached. Other broken
  connections are repaired from the connect section instead — e.g.
  pasting a fresh SimpleFIN token or using the provider's manage flow.
  You get an email when a connection breaks and another when it
  recovers. A **sign-in expired** or **needs attention** row can carry
  the same bank-side-trouble note as extra context — it means the fix
  may bounce off the bank's own error page until the outage clears, not
  that anything about the fix is wrong; worth trying again a little
  later if it does.
- Script-fed items get the same dot: green means a successful push
  within the last 26 hours; red means the collector has gone quiet —
  its error lives in its own logs on your machine.
- **New accounts to share** — your bank reports that you opened an account
  it is not sending yet. **share ↗** opens the bank's own checklist so
  you can decide whether Oikonome sees it.
- **card due dates not shared** — a healthy connection can still not
  be sharing one product (card due dates, investment holdings) because
  it was never granted at link time. The **grant ↗** button re-links so
  the bank can share it; for a credit card that is what puts the real
  due-date and autopay lines on the cash forecast. A self-hosted install
  asks the bank for these at link time by default, so this normally
  turns up only on connections made before that — or where the bank
  declined. An operator who would rather not pay their aggregator for
  them sets `OIKONOME_PLAID_PRODUCTS_EXTRA=none`: then nothing is
  asked for and nothing is pulled, and the note stays put. (Leaving the
  value blank is *not* the opt-out — a blank means "use the default",
  which for a self-hosted install is both.)

## Two clocks: "synced" and "bank sent data"

These are different, and confusing them is why a sync can look broken
when nothing is wrong:

- **synced 12m ago** — when *Oikonome* last read your aggregator's copy
  of the connection.
- **bank sent data 3 h ago** — when your *bank* last gave the
  aggregator anything new. Banks refresh on their own schedule, a few
  times a day; nothing in Oikonome can make them do it sooner.

So a sync that finds nothing is the ordinary case, not a failure — the
second clock is what tells you whether "nothing new" means nothing
happened or your bank simply has not reported since this morning.

The **↻** beside each institution reads the aggregator's copy for that
one bank and tells you what it found, naming that bank's own clock when
the answer is nothing. Whatever it pulls is categorized right away —
new merchants classified, bill charges matched — so the rows do not sit
under the bank's generic category until the next hourly run. There is no global "sync now" button: everything
it could reach is already on a schedule, and where webhooks are
available new transactions arrive within seconds of the bank sending
them.

## The 30-day auto-removal (Plaid)

A **Plaid** connection that stays **unrecoverably** broken — a state
only you can fix, like an expired login or revoked consent — for **30
consecutive days** is automatically released (a Plaid Item keeps
billing for as long as it exists, even while dead). Other connection
types are never auto-removed — they just stay red until you fix or
disconnect them. Institution outages and transient errors never count
toward the 30 days; they heal on their own and must never cost you a
re-link. Any successful sync resets the clock.

After removal, your transaction history is kept. The row shows a
persistent **connection removed after 30 days of failure — reconnect**
pill, and the daily email carries a one-line notice until you relink. Reconnecting is one
click — **reconnect ↗** starts a fresh link (the old connection no
longer exists, so there is nothing to "fix").

The window is configurable (`OIKONOME_PLAID_REAP_DAYS`; `0` disables
it).

## When an instance caps connected institutions

An instance run for other people can cap how many **institutions** may be
connected; the cap, and the wording it is explained in, come from whoever
runs it. A self-hosted instance has no cap. The check happens only when
you **add** an institution — re-authenticating or repairing an existing
connection is always allowed, even at the cap. Disconnecting an
institution frees its slot.

The connect card shows where you stand — "**N of M** institutions
connected" — and the figure comes from the server, so an account whose
ceiling has been raised sees its real one. At the cap the
**Connect** button is disabled rather than left live — better than
sending you through your bank's sign-in only to have the connection
refused at the end.

The same count and the same disabled button appear on the **Connect**
step of the welcome wizard, so what onboarding promises and what the add
door allows are one number.

## Multi-source linking: best serves, rest shadow

One real-world account can have several sources — say a checking
account on both an aggregator and a collector script. The page
suggests likely same-account pairs (same mask and type from different
connections); you confirm — nothing is ever merged silently. **Link**
them into a group and rank them by preference. The default order asks
what each source actually delivered first — one that brings transactions
or holdings goes ahead of one that brings only a balance — and only then
prefers the richer kind of connection. That is what you want when a
retirement plan is reached two ways at once: an exported statement with
the full ledger leads, an aggregator that returns a balance and nothing
else for that plan backs it up, and the plan is counted once instead of
appearing twice in net worth. A scheduled collector that leads a group
is held to a live feed's freshness clock, so a scraper that quietly
stops pushing fails over like any other source instead of staying
"healthy primary" forever. Then:

- The best healthy source (lowest rank whose feed is up) **serves** —
  marked with a **serving** pill; its transactions and balance are the
  ones every number uses.
- The rest show a **backup** pill: excluded from spending, budgets,
  net worth, and every other money aggregate, so nothing double-counts
  — but they **keep syncing** in the background.
- A source is considered down when it errors or a live feed goes
  silent for ~48 hours (a **source down** pill). Then the next rank's
  data is already complete and simply becomes visible. You get one
  email on failover; recovery is just as automatic — the old favorite
  wins again when it's healthy.

Reordering and unlinking live with the account: open its editor (✎ on
the web, tap the row on the phone) and the link line there says who it
is linked with and whether it serves or backs up, with **make primary**
(prefer this source) and **unlink** (separate them — each counts on its
own again, so a shared balance shows twice until you hide or remove
one). The **Linked sources** card at the top of the page is status, not
a control: once you have read it, **dismiss** puts it away. It comes
back by itself when there is something to decide — a new pair that looks
like the same account, or a linked source that went down.

Nothing is rewritten during failover — which source serves is computed
at read time, so flapping sources can't corrupt anything.

## Balances and the forecast

Connected accounts get balances from the provider each sync. File-only
and manual accounts start at $0 — edit the account and type the real
balance, and mark your **primary checking** account so the cash
forecast anchors on it. Until balances are set, forecast surfaces stay
hidden rather than show numbers seeded from $0.

When the bank shares them (from the liabilities pull), a card's next
due date and statement balance appear under its balance on the account
row; a bank that shares nothing shows nothing.

The ✎ edit row also sets the account's default **owner** — a reporting
filter for whose money it is (yours / mine / ours); it never affects
the budget.

## Hiding an account

The owner's ✎ edit row has a **hide account** button: one account off a
shared login stops pulling new data and leaves every list and total,
while the bank connection and the account's other siblings keep
syncing and the history stays. It is the middle ground between **excl.
from budget** (the account still shows, with its balance) and
disconnecting the whole institution. Hidden accounts sit in the
collapsed **Hidden (N)** section at the bottom of the page with a
**hidden** pill; open the ✎ row there and **unhide** brings one back.

Hiding changes only what you see — not what the bank shares or what
the aggregator bills for. On a Plaid connection the owner's ✎ row also
has **edit connected accounts ↗**, which reopens the bank's own
checklist of accounts to share; an account you untick there stops
syncing, stops counting toward Plaid usage, and is hidden automatically
when the checklist closes.

## Renaming an account

The first field in the ✎ edit row is the **display name** — rename
"CREDIT CARD" to "Groceries Card" and the new name shows everywhere;
sync never overwrites it. The bank's own name is kept underneath: the
editor shows it beside the field, and **revert** (or simply clearing
the field) drops the custom name and goes back to it — including any
future rename the bank makes. Transaction search finds the account by
**either** name (see the Transactions guide).

## Removing an account

Open the ✎ edit row → **remove…** (only the owner sees this button,
along with **hide account** and **edit connected accounts ↗**). Options
depend on whether the account still has a live bank connection:

- **Disconnect only — keep history** — stops syncing the whole
  institution login and, for **Plaid**, releases the Item
  (`/item/remove`) so it stops billing. Every account under that bank
  stops syncing; transaction history stays. Same effect as the ✕ on
  the connection in Connect.
- **Disconnect and delete data** — disconnect as above, then
  permanently delete all local accounts and transactions for that
  institution. Cannot be undone.
- **Hide this account only** — soft-removes one account from active
  lists while the bank connection (and its other accounts) keep
  syncing. History is kept. Does **not** free a Plaid slot — use
  disconnect for that. A **removed** pill marks it, and it lives in the
  collapsed **Hidden (N)** section (same as **hide account** above).
- **Delete account and its transactions** — hard-delete for manual or
  already-disconnected accounts only (a live Plaid Item would recreate
  the account on the next sync).

Plaid cannot drop a single sub-account under an Item — disconnect is
always per institution.

## Gotchas

- The ✎ edit row also reclassifies an account (checking / savings /
  credit / investment / …) — classification drives net worth grouping
  and the retirement tax buckets.
- **Excl. from budget** (in the edit row) removes an account's
  spending from budget math entirely — the **excl** pill marks it. The
  cash runway leaves it out too: an excluded checking account is never
  the runway's starting balance, and an excluded card's balance is not
  counted as debt to cover.
- An account with no current balance is treated as closed and moves to
  the collapsed **Archived (N)** section — hidden from daily surfaces,
  history kept. It carries a **closed** pill when its connection still
  exists and **archived** when it does not.
