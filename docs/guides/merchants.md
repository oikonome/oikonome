# Merchants

Banks spell the same shop several ways. One month it arrives as
"NORTHWIND HARDWARE", the next as "Northwind Hardware Group", the next in
lowercase — and each spelling becomes its own merchant, splitting one
shop's totals across three rows that never add up on any page.

The app consolidates the obvious cases automatically — including a name
the bank prints without its apostrophe ("Juniper's Market" and "JUNIPERS
MARKET" are one merchant), an initials brand stays whole ("Q&Z Market"
keeps its ampersand, "J.T. Outfitters" reads "JT Outfitters"),
accented letters fold to plain ones ("Café Northwind" and "CAFE
NORTHWIND" are one merchant), a short number that begins a brand name
is part of the brand ("9 OAKS MARKET" and "9-Oaks Market" are one
merchant, while a number of three or more digits is still read as a
store number and dropped), and when that cleanup improves, bills and
category rules attached to the old spelling move with it. If an improved
cleanup splits one merchant into several, each part gets a copy of its
bills, pending bill offers, category rules and 1099 contractor marks,
and each moved spelling is listed under
Recent changes so it can be undone — but the cleanup is
deliberately cautious: a wrong merge is worse than a missed one. The **Merchants** page is
where you settle the rest by hand — and your answer outranks every
automatic rule, because you were there and the app was only reading
strings.

A card or account number never becomes part of a merchant's name: when a
bank prints little but a number ("ONLINE PMT" and a card number), the
name the app derives keeps only its last four digits. A name you type
yourself is kept exactly as typed.

## The page

Reach it from **Settings → Merchants** (web and mobile — it is a
correction tool you visit now and then, not a daily page). Each row is a
merchant as the ledger displays it, with:

- its logo and website when the bank connection resolved the merchant
  (Plaid identifies most national merchants — the same identity is what
  lets an old imported "NORTHWIND WHSE #0555" row share the Northwind logo);
  a kind chip when it is not a plain shop (marketplace, payment app);
  a chain outlet (one store of a warehouse club or a gas chain) says which brand it is an
  outlet of, and the brand's logo carries over;
- how many transactions carry it and what they total, said with its
  direction — "$1,250 spent", or "$600 received" for a merchant that
  mostly pays you. A line that is mostly transfers and card or loan
  payments (a card issuer's line, "Transfer from Checking") says "in
  transfers and payments" ("mostly transfers and payments" when other
  charges sit among them but most of the line's dollars are payments),
  and a bank's interest line ("Interest paid", "Credit interest") says
  "in interest" —
  money moved or earned, not spent at a merchant — and how many of its
  rows belong to a business entity;
- the first and last time you paid it;
- the **raw variants** underneath — the exact strings your bank sent.
  That list is usually the explanation for a merchant looking wrong.

**Open a merchant** (tap its name) for everything the app knows about
it: website, phone and merchant code when the bank connection resolved
them; the category its rows carry and, in words, the rule that files
its new charges ("new charges go to Groceries (your rule)" — it can name
a different category from the one its past rows carry); its
activity span with a jump into Transactions; every source name; and
**where** it has been seen — the places from the bank's per-transaction
location, busiest first (one town however the bank spells it), each
with a map link (a chain lists several; an
online merchant says no location came through).

The list shows the most frequent merchants first; switch the order to
a → z, biggest spend or recently seen. Three filters answer the usual
questions: **one-off** (a single charge), **unnamed** (still wearing a
bank string as its name), **seen this month**. Either way the list shows
a page at a time — search to reach anything below that, which looks
through every merchant in the ledger, not just the visible rows. A row is
the name and one line of facts; open it (a panel beside the list on the
web, its own screen in the app) for the source strings, where it has been
seen, the category rule behind it, and the actions.

When the app thinks two spellings are one business it queues them under
**Needs a look** at the top of the page, one line per pair with the
reason. **details** opens three boxes that read as a sum — the new
detection, the history it would join, and the merchant you would have
after merging, each with its rows (every row the merge moves, refunds
and payouts included), total, dates and latest bank lines (a name opens
that merchant). The reasons are read from the ledger as it is when you
open the list, so a side said to stop in a month has no later rows; a
pair one of whose merchants holds no counted row right now (its rows
withdrawn by the bank, or only on a hidden account) is left off the list
until it has one again. Two merchants whose names agree on only one
word are never offered on the strength of their charges alone — the
same line of business in the same town, or a bank line that is only
that word, is two shops that share a word, not one business. **Merge** joins them, **Not the same** keeps them
apart for good, and nothing is merged unless you say so. A new batch of
offers also raises one Alerts line, "N new merchant pairs look like one
merchant", that links here. It counts the pairs that batch found; the
offers list here counts every pair still waiting, so the two can differ. A pair is
always offered as the smaller merchant joining the larger one — more
charges first, and with equal charges the one you have used longer — so
approving never renames a long history after a merchant that appeared
this week. (One exception: a name the bank cut short gives way to the
full name, whichever has more charges.) When more than one
pair is a name the bank cut short (or the same name spelled another
way), **Merge all N cut-off names** merges those pairs in one go, always
keeping the full name; a pair whose names merely share a word reads
*share a word — check the address* and is merged one at a time.

The counts and totals are your personal ledger's. A merchant you only
ever paid through a business entity still appears here — identity is one
thing across the whole app — but its business spending is not folded
into these numbers; the business pages own that.

## Rename, and merge

Open a merchant for its two actions. **Rename** gives every raw variant
now displaying under this name a new name. **Merge into…** searches your
merchants and moves every row under the one you pick — the target is
always a merchant that exists, so a typo can never fork a new one. Both
go through the same journal, and both are undone under Recent changes.

A rename covers the spelling family, not only the strings on file the
day you made it: a payee whose bank descriptor embeds the amount arrives
as a new string every time, and each new one follows the name you gave
the others. If you sent two spellings of one family to two different
names on purpose, that split is respected.

A rename applies everywhere the name is shown — the ledger, search,
merchant history and top-merchant totals — at once, with no re-sync.
Bills follow it too: a bill pays merchants by identity, so a renamed or
merged merchant keeps paying the same bill.

### One payee showing as two

A bank often enriches only some of a payee's charges — a refund, or a
late-arriving row, can come through with nothing but the raw descriptor
the bank printed. Such a row could land under a second merchant named
after the descriptor itself, so one payee shows up twice: once tidily
named, once as something like `ACME KITE SUPPLY RIVERTON`.

New charges are filed under the identified merchant. The same applies the
other way round: once your bank's line and your data provider have agreed
on a shop — a single earlier charge is enough — a charge the provider
names something else, something the line does not say, stays with the
shop the line means. So the charges from one card terminal stay one
merchant even when the feed names them differently from day to day. That one charge's name is set aside rather than remembered, so
a real business of that name still gets its own merchant the first time you
visit it — and the charge itself is searchable by the line your statement
shows, not by the name it was moved off.

A statement you import can write the same payment with the bank's word for
the kind of entry in front of the payee — `ACH Withdrawal ACME KITE
SUPPLY` — where the live feed writes `ACME KITE SUPPLY`. That lead-in
(**ACH Withdrawal**, **ACH Deposit**, **ACH Debit**, **ACH Credit**,
**Direct Deposit**) is not part of the name: an imported row joins the
merchant the feed's rows of the same line already have, so a merchant's
totals include its imported months. Rows imported before this are moved
by the nightly pass. A name you gave a merchant yourself is never
changed by it.

Pairs already split
are merged by the nightly job, which converges up to 25 such pairs a night, keeping the
better-sourced name — the one your bank connection identified, with its
logo — and folding the other into it. Every merge is journalled like any
rename, so you can undo an individual one from the merchant's page.

It is deliberately cautious: it merges only when the raw descriptor
actually names the surviving merchant, and it never touches a merchant
you renamed yourself. A generic descriptor your bank reuses for
unrelated purchases is left alone rather than guessed at.

The same sweep also folds back a merchant that was created only because
your bank's feed started spelling a payee you already had differently
("… Hardware Su" becoming "… Hardware Supply", or a pending charge printed
with the city and country after the name). It does that only for a
merchant whose every charge is still pending or is the posted form of a
pending charge, and only when the earlier charges point at one older
merchant whose name the new spelling plainly continues.

And it folds back a merchant that was created on the day your data
provider first attached its own identity to a charge from a shop you
already had: the newcomer held that week's charges while every earlier
one stayed under the merchant you know. New charges like that now stay
with the merchant the bank's line already belongs to — when the line
spells the provider's name for it and the line's earlier charges are all
one merchant — and a newcomer made before this is merged into that older
merchant, keeping the older name. It is left alone if you renamed either
one, taught them different categories, or answered **Not the same**.

To see or drive it by hand on a self-hosted install, run the app's own
command inside its container: `docker compose exec -T app oikonome
merchants-repair-splits` prints what it would merge and why it refused
the rest; add `--apply` to merge, `--tenant <id>` to scope it to one
household, and `--limit <n>` to cap the merges per household (the default
is the nightly job's own cap).

An operator can also name city names to strip from the end of every
merchant label (banks cut a store's city into the descriptor at
different lengths). There is no Settings card and no command for it: set
`merchant_strip_cities` — a JSON list of names — in the household's
`tenant_settings.config` row, and the nightly merchant pass applies it.
An export does not carry the list and a restore never sets it, so it stays
with the instance it was set on.

## Undo

Every rename is journalled, and the page's recent-changes list lets you
replay any of them backwards. This matters more than it sounds: your
own corrections are the ones made from memory about a shop you visited
two years ago, so they are exactly the ones that should be reversible.

An undo restores the name that was in force before that change. If a
later rename has since moved the same merchant somewhere else, the
older undo is refused rather than silently trampling the newer answer —
undo the newer change first.

One wrinkle worth knowing: a restored name is kept as *your* answer, not
handed back to the automatic layer that originally produced it. If the
name before your rename came from the app's own consolidation, undoing
pins that spelling rather than letting the nightly cleanup keep tuning
it. Rename it to what you want and the outcome is the same.

## Where logos come from

Logos are served by your own instance (`/api/logo`), which fetches each
one from the bank connection's provider once and keeps the copy —
indefinitely, there's no expiry on a successful fetch. What limits the
cache is size, not age: it's shared across the whole instance and
capped, so once it's full, fetching a new logo retires the oldest one
to make room. A lookup that comes up empty is remembered for a day
before it's tried again, rather than hitting the provider on every page
load. Your browser never talks to the provider directly, so a page full
of logos discloses nothing about you to a third party. Only the
provider's logo hosts are ever fetched. Switch logos off entirely in
Settings → Merchants ("Show merchant logos").

## What this does not change

- **Categories.** Renaming a merchant does not recategorize anything.
  Category rules are a separate layer (see the rules guide); a merchant
  rule keyed to the old name keeps applying to the same charges.
- **Your category overrides.** Never touched, here or anywhere.
- **The bank's own strings.** The raw variants are kept exactly as they
  arrived; the display name is a layer on top, which is what makes undo
  possible.

## Fuel arms

One split the app makes on purpose: a chain's filling station is its
own merchant. When the bank's statement line names the brand and then
the word *gas* or *fuel* — "NORTHWIND GAS", "A-B-C GAS" —
the charge is filed as **Northwind Gas** rather than **Northwind**, and
categorized as fuel, because a tank of petrol inside a grocery budget
makes both numbers wrong.

The rule is narrow by design: the fuel word must follow the brand
immediately, so "NORTHWIND WHSE" and "ACME PETRO PUMP 12" are
left alone, and a merchant already named for fuel (a natural-gas
utility, say) is never touched. If it splits something you would rather
see whole, merge the two on this page — your answer wins.
