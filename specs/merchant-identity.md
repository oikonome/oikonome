# The identity key, and when a bank line outranks a name (spec)

Implemented by `server/oikonome/engine/merchant_identity.py`; the key
itself is spelled once, in `server/oikonome/engine/merchant_sql.py`.

## The invariant: identity key → merchant is a function

Every transaction is filed under a merchant ROW, and every transaction has
an **identity key** — the string the whole system reasons about it by:

    COALESCE(merchant_outlet, merchant_name, name)

The outlet first (a chain's fuel arm is a merchant of its own), then the
aggregator's merchant name, then the bank's own descriptor.

Four separate mechanisms assume that one key maps to exactly one merchant:

* `merchant_canonical` is an alias table keyed by it, and a rename writes
  an alias for every key a merchant displays under;
* `reconcile_raws` moves rows **by key** after a rename, merge or undo;
* the split repair reasons about the keys under a descriptor;
* a whole-ledger re-resolve regroups the ledger by key, and must reach the
  answer the incremental passes reached.

A row filed under one merchant while it still answered to a key pointing at
another breaks all four at once: the rename writes an alias for a name the
row was moved OFF, which drags the next real business of that name into the
wrong merchant, and a rename of that business drags the row back out.

## The exception: a line with a habit

An aggregator's merchant name with no entity id behind it is a guess, and
it can guess differently for the same terminal on different days. When a
charge comes back named something its bank line does not say, and that line
has already **agreed** with the aggregator — a row under exactly that line
(a settled hold counts) where the aggregator named the payee and the line
names that name too, all such rows resolved to one ordinary merchant — the
line wins and the charge joins that merchant. One agreeing row is enough:
the line is the bank's own word for the terminal, and a name it contains
outranks a name it does not, however confident the aggregator is. (A
higher threshold would only set how many charges become twin merchants
before the rule applies, and a line whose posted charges all carry a
wallet prefix might never reach it.) No alias is written for the name: it
is one charge's guess, and
a real business of that name under its own line must still get its own
merchant. The **line** is aliased instead, because that is the key the
charge carries from then on; where the line is already mapped, that
mapping is the answer and the charge is filed under it.

A name the string clean reduces to a word the line does contain ("Harbor
Kite" under a "HARBOR KITE SPRINGF…" line) is not doubted at all — it is the
line's payee wearing noise — and is read as the line's payee under that
same clean form, so it cannot fall between the two rules and mint a
merchant named after the noise-stripped word.

### A posted charge that repeats the hold's guess

The feed links a posted charge to the hold it settles, and often guesses
the same stray name for both. The hold's line has already answered for
the name (it is set aside on the hold); the posted charge carrying that
very name is the same purchase, so it is filed the way the hold was — its
name set aside, its own line (usually the hold's line behind a wallet
prefix) aliased to the merchant — rather than judged afresh under a line
that has no history of its own.

That is decided per ROW, and on the exact name the feed repeated (never
its clean form). One sync can carry the posted charge beside an unrelated
charge the feed gave the same stray name under a line of its own; the two
share a key, but the hold vouches only for its own charge. The unrelated
charge keeps its name and its own merchant, its line is never aliased to
the hold's merchant, and the answer does not depend on which of the two
arrives first.

A row whose aggregator name is the processor itself — the feed listing
that name as a payment-app or terminal counterparty — is never a line
agreeing with the aggregator about a payee: a processor's line reads as
the processor's name, and counting it would fold every payee the
processor carries into one merchant.

### Why AGREEMENT, and not merely rows under the line

Two failures rule out the weaker test of "the line names whatever merchant
it has carried so far":

* A bank's constant ("POS DEBIT PURCHASE") mints a merchant named after
  itself out of its own unnamed rows. The line names that merchant
  trivially — it names *itself* — so every payee the household ever charges
  under the constant could be folded into whichever came first.
* Rows the aggregator never named settle LATER in a pass than this question
  is asked (they adopt a merchant by descriptor, which has to see the
  settled rows). Counting them would make a whole-ledger pass answer
  differently from a row-at-a-time one, for the same ledger.

Rows that agree are never doubted themselves — their own line names their
own name — so they settle in the first phase, before any verdict is
reached. Both passes then see the same evidence.

### The evidence is the ledger, not the pass

Whether a line overrules a stray name is asked once per (name, line) pair,
of that line's own history — never of how many lines the name has been seen
under. That count is a fact about the rows still keyed on the name, and
which rows those are depends on how the syncs bundled them: a charge an
earlier sync moved is no longer keyed on the name, so the next charge of the
name saw one line alone, while the same two charges delivered together saw
two and both kept the name. Judged per line, each charge lands where its own
line puts it in every order. A name the aggregator uses under a line that has
never agreed with it is still a payee it knows there: those rows keep the
name, so a name under two lines neither of which overrules it stays one
merchant. For the same reason the resolver's own earlier filing of the name
(its layer-1 alias) does not shield a later row — only a person's rename or a
reviewed merge has answered for a name.

The other facts about a key — which line, and whether it is an outlet — are
asked of every live row carrying the key, never of the rows one pass happens
to be holding.

A charge already moved is absent from its old key, because it carries its
line now. So a second charge of that name turning up later under another
line does not re-open the first decision: it is judged on its own evidence,
and a re-resolve reading both keys reaches both answers again.

## Why the name MOVES off the row

`transactions.merchant_name_set_aside` (migration 137) holds it, and
`merchant_name` is left NULL. The alternative — inferring the exception
wherever it matters — does not survive contact with the four
mechanisms above: each would need its own copy of the rule, and a
whole-ledger re-resolve cannot express it at all, because by then the row
is simply a row whose name disagrees with its line.

Storing the decision *changes the row's key*, so every mechanism that
already reads the key is correct without knowing the rule exists. But it
only works if the key really changes for every reader. A boolean beside the
name would not: it would leave the name in `merchant_name` and ask every
reader to consult the flag through one SQL
fragment, while a couple of dozen of them spell
`COALESCE(merchant_name, name)` by hand and would go on answering to the name.
Moving the name leaves nothing to remember — the row *is* a row the
aggregator never named, which is a shape every reader already handles.

The price is paid in search: `transactions.search_text` is a generated
column over `merchant_name`, so a set-aside name is no longer searchable.
The row is still found by its bank line, which is what the statement shows,
and putting the column into that expression would rewrite the whole ledger
and its indexes on every install.

## What ends it

* **The feed resolves the charge to an entity.** The line outvoted a
  guess; an entity id is not a guess. The name is restored before
  grouping, so the row cannot lend that entity to the line's unnamed rows
  on its way out.
* **A re-sync restates the line or the name.** The judgement was about the
  strings the row then carried, so the feed re-sending exactly those two
  changes nothing, and a feed sending either of them differently puts the
  new name back in `merchant_name`, clears the set-aside one, and drops the
  merchant so the row is resolved afresh (and may be judged the same way).
* **Nothing else.** A restore carries the column, because a restore
  reproduces a household rather than re-opening its decisions.

The column is internal: no API response carries it, and no client type
knows about it.

## When the string clean changes its mind

Layer 1 is recomputed every night, so an improvement to
`merchant_dedup.canonical_merchant` renames merchants already in the
ledger. One rule in it exists for identity's sake: an **apostrophe joins
the letters around it** — it is deleted, never turned into a word break —
because a feed resolves a payee as "Juniper's Market" while the card line
for the same purchase prints "JUNIPERS MARKET", and the two have to clean
to one string or one business is two merchants. It is the reading
`merchant_identity` already compares descriptors under.

A second rule of the same kind: a **short number that leads a brand name
is part of the brand**. A one or two digit number before a distinctive
word binds to it ("7 ELEVEN", "7-ELEVEN" and "SEVEN ELEVEN" are one
merchant, as are "99 RANCH MARKET" and its hyphenated spelling), rather
than being dropped as a store number, which would leave one business under
several names. A number of three or more digits is still a store number; one
before only generic words, before a street, or before more digits is
still a reference.

A changed canonical moves its rows to the merchant the new string names
(`merchant_identity.re_resolve_recleaned`), and two things attached to the
merchant they left do not live in the merchant tables:

* **A bill's identity** is a merchant id in the bill's own JSON. The
  emptied merchant is therefore marked `merged_into` the one its rows went
  to — the shape a person's merge leaves — and the bill follows the
  pointer at once when matching. The nightly bill pass rewrites the id,
  and it runs BEFORE the merchant pass, so `prune_empty` holds a
  merged-away merchant a bill still names; it goes the night after the
  bill let go.
* **A category rule** is stored under a name. A rule under the old name
  that no alias row and no live merchant answers to any more is moved to
  the survivor's name (`merchant_dedup._rekey_rules`); where the survivor
  has a rule already, a person's outranks a machine's and otherwise the
  survivor's stays.

## A statement's lead-in is not the payee's name

A bank's statement export may write its own word for the kind of entry
in front of the payee — the transaction-type column glued onto the
description: "ACH Withdrawal FABRIKAM TUTORING", "Direct Deposit —
CONTOSO PAYROLL". The live feed carries the same payment without it, and
names the payee besides. The rule: a payment imported from a statement and
the same payment pulled from the feed are ONE merchant, so every figure by
merchant — a window's total, a bill's history — counts both.

Two rules, and both are the string clean's and the resolver's own; no
reader learns anything new.

**The lead-in is cut by the one function that cleans a bank line**,
`merchant_dedup.canonical_merchant`, through `merchant_dedup.strip_lead_in`.
The list is short on purpose: `ACH Withdrawal`, `ACH Deposit`, `ACH
Debit`, `ACH Credit` and `Direct Deposit`, with or without a dash after
them — the entry kinds that are followed by a payee. An ATM withdrawal, a
transfer between the household's own accounts and a deposit "from"
somewhere are followed by a terminal, an account or a place, and cutting
those would mint a merchant per location; they are left alone. A line
that is nothing but the lead-in keeps it: a bank's constant is not turned
into an empty name.

**An unnamed row adopts the merchant its CLEANED line already belongs
to.** A row the aggregator never named is filed by its bank line, and
adopts the merchant other rows under that exact line were given
(`_merchant_by_bank_descriptor`). The statement's line is never that exact
line — the lead-in on one, the amount inside the other — so the same
question is asked once more of the cleaned line
(`merchant_identity._cleaned_line_evidence`, in `resolve`, after the exact
line and the hold have had their say): the rows the aggregator NAMED whose
own line cleans to the same string. The tests are the exact line's,
unchanged: those rows carry one aggregator name, resolve to one surviving
merchant, and the line names that merchant or that name
(`_descriptor_names`). A cleaned line with no distinctive word (four
letters, not a word for a kind of business or a bank mechanic) is never
looked up: generic words name nobody. Only named rows are evidence, never
another unnamed row, and never a row the pass is itself deciding by its
line (that includes a row whose aggregator name is simply its line), so
no adopter can see an adopter, a whole-ledger pass answers as a
row-at-a-time one does, and a row being re-resolved cannot vouch for the
merchant it is being moved off.

What is deliberately NOT done: the cleaned line never mints or renames a
merchant by itself. With no named row under it, the row is filed as
before, under the name its line cleans to — now without the lead-in.

### A person's choice wins

- A string a person renamed or a reviewer merged has an alias that is
  theirs (`manual`, `llm`): such a group is not an adopter at all, and the
  nightly recompute never rewrites those aliases. A merchant a person
  named keeps its name and its rows.
- A new string of a spelling family a person named (the same clean as
  strings they mapped, all to one name) follows their merchant, as before:
  the cleaned-line question is not asked for it.
- A category override is a property of the row and is untouched by which
  merchant the row is filed under.

### How the ledger already there is brought along

`merchant_dedup.apply` recomputes every machine-written (`layer1`) alias
each night. A key whose clean changed — here, every unnamed line that
carried a lead-in — is re-resolved by `re_resolve_recleaned`, which runs
`resolve` over its rows: they adopt the feed's merchant where the cleaned
line answers, and otherwise join (or mint) the merchant the shorter name
names. The merchant they left is retired into the one they went to, or its
bill, rule and marks are copied to each part when it split, exactly as for
any other change to the clean. A second run finds no clean that changed
and moves nothing: the pass is idempotent. An unnamed row whose clean did
NOT change keeps the merchant it has until something re-resolves it; rows
arriving from then on are decided by the rules above. No migration, no
new column, and nothing an older client reads changes shape.

## A line that has its merchant keeps it when the feed first sends an entity

A feed can go years naming a terminal's charges without an entity id, and
then start sending one. The id is new to the household, so no merchant
carries it, and taken alone it mints a merchant: the same bank line then
answers to two merchants from one day to the next, the new one holding
that week's charges and the old one the history. The merge offer that
follows asks the person to fix what the resolver did.

So before an entity seen for the FIRST time mints a merchant, the rows'
bank line is asked whose it already is. One function decides,
`merchant_identity._first_entity_answers`, read for every such group of a
pass before anything is written, and it answers with the line's merchant
only when all of these hold (each is a reason to refuse):

- no merchant carries the entity id yet, the feed calls it a merchant (not
  a marketplace, a payer or an institution), the group is not an outlet,
  and no person has renamed the key (their answer is taken first, as
  always);
- every live row keyed on the name sits under ONE bank line. A name the
  feed uses under several lines is a payee it knows across them, and it
  keeps its own merchant;
- the line names the entity's name (`_descriptor_names`): an entity the
  line does not spell is the feed saying the charge is somebody else's,
  and it still gets its own merchant;
- the line has already agreed with the feed about a payee (a row under
  exactly this line, a settled hold included, whose feed name the line
  names) — the test the stray-name rule above uses, so a bank's constant
  never qualifies;
- every other row under the line resolves to one surviving merchant, an
  ordinary one, that has no entity id of its own.

Then that merchant takes the entity id (and the logo and site, where it
has none) and the rows are filed under it. It keeps its name and who
named it. From then on the entity IS that merchant: a later charge of the
entity under any line goes to it by id, so the answer does not depend on
which pass sees the rows, and a whole-ledger re-resolve reaches it again.
The evidence is the ledger as the pass found it — rows the same pass has
yet to resolve carry no merchant and are not evidence — so two syncs
delivering the rows in either order agree.

What it deliberately does not do: an entity that already has a merchant is
never overruled by a line (that pair is two live merchants, and joining
them is a merge a person is offered); a line whose merchant has another
entity keeps both; nothing is renamed.

### The twins already minted

`merchant_split_repair` (nightly, bounded, journalled like a rename and
undone from the Merchants page) carries one more kind of candidate,
`first-entity-name`: a merchant the feed's entity minted, that nobody has
touched — named by the feed, no parent or child, nothing merged into it,
no alias a person or a review wrote — whose every live row sits under one
bank line that answers, by the rule above and on the evidence of the
line's OTHER rows only, to a single older merchant. It is merged into that
merchant through `merchant_dedup.rename`; the emptied row keeps the entity
id and points at the survivor, which is how the next charge of the entity
finds it. Never touched: a pair a person answered "not the same", a
merchant either side of which a person named, merchants a person taught
different categories. A merge the person undoes stays undone (the undo
leaves an alias of theirs on it, which takes it out of the candidates).

## Which side of a merge offer survives

`merchant_merge._rank` orders the two sides, and `propose` stores the
offer onto the higher: **the merchant with more rows survives; with equal
rows, the one whose first charge is older; then the name's quality as
before** (backed by an entity, not bank-prefixed, more than a kind of
business, the fuller name), and the id last so a run is reproducible. An
offer therefore never asks a person to rename a long history after a
merchant that appeared this week. One exception stands from the cut-name
rule (specs/settings-status-lines.md): a name that is another member's
name cut short never survives, whatever its rows, and the full name is
kept.

An offer's id is the pair, whichever way it points, so a pair a person
rejected stays rejected. A PENDING offer stored the other way round (the
ledger grew, or it predates this rule) is turned in place by the nightly
run, `merchant_merge.run`: same id, the two sides and the evidence
rewritten. Until that run a stored offer reads as it was stored; both
clients print the sides the server sends and decide nothing.

An approve names the survivor it was shown (`into_id`); one whose
survivor no longer names the merge the offer stands for is a 409 and
nothing merges (`merchant_merge._shown_is_this_merge`). An older app sends
no survivor, so its approve cannot be told from one listed before a turn.
The offer therefore keeps the fact that it was turned (`evidence.turned`),
written by the two things that turn one: the nightly, and an approve whose
fold leaves another offer's listed survivor no longer answering for it.
An approve without a survivor on a turned offer is a 409 too; on an offer
never turned it merges as stored, which is what every listing showed.

An offer is shown and counted (`merchant_merge.live_pair_sql`) only while
both merchants hold a counted row — not removed, not on a shadow account
(`links.shadow_ids`). That is a reading of today, never a stored verdict:
rows withdrawn and re-posted by a feed, an account hidden for a while or
a failover that swaps a card's primary feed all come back, so the offer
waits pending and reappears with its reasons worked out again. The
nightly retires an offer ('stale') only when a merchant was merged away
or deleted (`pair_merchants_sql`), which a pair never recovers from.

## The catalog is printed in the server's order

`GET /api/merchants/catalog` filters (`filter`: one-off, unnamed, seen
this month) and sorts (`order`/`sort`: count, name, spend, recency)
before it caps the page (`merchant_dedup.listing`), with "this month"
read as the household's month (`localtime.now_local`). Both clients print
`merchants` in the order it came and filter nothing again: a second pass
on the client, by the device's own clock and a client copy of the
"unnamed" test, would drop rows the server sent near midnight or in
another time zone. A filter switch shows the previous page until the new
one lands (the query keeps its last page as a placeholder), as a search
already did.

## The "After merge" box is the server's sum

Each pending offer carries `merged` (`merchant_merge.merged_facts`): both
sides' rows and total together, the first and last date they span, and
what the folded side adds as printed strings — `rows_delta` ("+12") and
`total_delta`, the change in the survivor's total as the difference of
the two totals AS PRINTED in whole dollars ("+$250", "−$50"), so the
three figures on the box agree to the dollar. It is null unless both
sides have facts. Both clients print these fields and add nothing: a
client typing its own "+" before the change would read "+-$50" below zero.
An older server sends no `merged`, and the box shows its words without
figures.
