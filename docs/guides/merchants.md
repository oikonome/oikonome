# Merchants

Banks spell the same shop several ways. One month it arrives as
"HARBOR REHAB", the next as "Harbor Rehab Valley", the next in
lowercase — and each spelling becomes its own merchant, splitting one
provider's totals across three rows that never add up on any page.

The app consolidates the obvious cases automatically, but it is
deliberately cautious: a wrong merge is worse than a missed one. The **Merchants** page is
where you settle the rest by hand — and your answer outranks every
automatic rule, because you were there and the app was only reading
strings.

## The page

Reach it from **Settings → Merchants** (web and mobile — it is a
correction tool you visit now and then, not a daily page). Each row is a
merchant as the ledger displays it, with:

- its logo and website when the bank connection resolved the merchant
  (Plaid identifies most national merchants — the same identity is what
  lets an old imported "COSTCO WHSE #0007" row share the Costco logo);
  a kind chip when it is not a plain shop (marketplace, payment app);
  a chain outlet (a specific Costco or Shell) says which brand it is an
  outlet of, and the brand's logo carries over;
- how many transactions carry it and what they total, and how many of
  its rows belong to a business entity;
- the first and last time you paid it;
- the **raw variants** underneath — the exact strings your bank sent.
  That list is usually the explanation for a merchant looking wrong.

**Open a merchant** (tap its name) for everything the app knows about
it: website, phone and merchant code when the bank connection resolved
them; the category its rows carry and the rule that sets it; its
activity span with a jump into Transactions; every source name; and
**where** it has been seen — the places from the bank's per-transaction
location, busiest first, each with a map link (a chain lists several; an
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
reason behind "why?"; **Merge** joins them, **Not the same** keeps them
apart for good, and nothing is merged unless you say so.

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
named, once as something like `ACME TELECOM NEW YORK USA`.

New charges are filed under the identified merchant. Pairs already split
are merged by the nightly job, which converges up to 25 such pairs a night, keeping the
better-sourced name — the one your bank connection identified, with its
logo — and folding the other into it. Every merge is journalled like any
rename, so you can undo an individual one from the merchant's page.

It is deliberately cautious: it merges only when the raw descriptor
actually names the surviving merchant, and it never touches a merchant
you renamed yourself. A generic descriptor your bank reuses for
unrelated purchases is left alone rather than guessed at.

To see or drive it by hand on a self-hosted install, run the app's own
command inside its container: `docker compose exec -T app oikonome
merchants-repair-splits` prints what it would merge and why it refused
the rest; add `--apply` to merge, `--tenant <id>` to scope it to one
household, and `--limit <n>` to cap the merges per household (the default
is the nightly job's own cap).

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
the word *gas* or *fuel* — "COSTCO GAS #0007", "H-E-B GAS/CARWASH" —
the charge is filed as **Costco Gas** rather than **Costco**, and
categorized as fuel, because a tank of petrol inside a grocery budget
makes both numbers wrong.

The rule is narrow by design: the fuel word must follow the brand
immediately, so "COSTCO WHSE" and "SHELL OIL … AUTO FUEL DISPEN" are
left alone, and a merchant already named for fuel (a natural-gas
utility, say) is never touched. If it splits something you would rather
see whole, merge the two on this page — your answer wins.
