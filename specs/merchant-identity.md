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
has a **habit** — `ESTABLISHED_ROWS` rows under exactly that line where the
aggregator named the payee and the line names that name too, all resolved
to one ordinary merchant — the line wins and the charge joins that
merchant. No alias is written for the name: it was one charge's guess, and
a real business of that name under its own line must still get its own
merchant. The **line** is aliased instead, because that is the key the
charge carries from then on; where the line is already mapped, that
mapping is the answer and the charge is filed under it.

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

How many bank lines carry a key, which line that is, and whether the key is
an outlet are all asked of every live row carrying the key — never of the
rows one pass happens to be holding. A sync resolves what it just
delivered, so of two charges sharing a stray name it can hold only the one
whose line has the habit: asked of that batch the name is a one-off of that
line and the charge is moved, while a single pass over the ledger sees the
name under two lines, reads it as a payee the aggregator knows, and leaves
both alone. Nothing brings the row back to be asked again — a resolved row
is not revisited — so the two passes would disagree about one ledger
forever.

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
