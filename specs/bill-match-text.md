# The text a bill is matched against

A bill either HAS an identity (the merchants attached to it — then its rows
are those merchants' rows and nothing below applies) or it matches by
TEXT: its words against a ledger row's words. This is about the second
kind, and about one question only: *which words does a row have?*

## The rule

A row's match text is its **identity key followed by its bank line**,
lowercased:

    lower( COALESCE(merchant_outlet, merchant_name, name) || ' ' || name )   -- apostrophes joined, below

The identity key is `engine/merchant_sql.RAW_KEY` — the same string the row
is grouped, aliased and displayed by. It is defined once, in
`engine/budget.py`:

| form | name | used by |
|---|---|---|
| SQL, the key column | `MATCH_PAYEE_SQL` | every loader that feeds a bill matcher selects it `AS payee` |
| SQL, the whole text | `MATCH_TEXT_SQL` | every pre-filter (`tokens_match_sql`, `merchant_match_sql`) |
| Python | `match_text(payee, name)` | every loop that hands a row to a matcher |

## An apostrophe joins its letters

Before anything is split into words, an apostrophe that follows a letter
or digit is DELETED (`'`, `’`, `ʼ`): "Juniper's Market" and the card line
"JUNIPERS MARKET" are the same words, where breaking on it made them
"juniper" and "junipers" and left a bill unpaid beside the charge that
paid it. A leading apostrophe is a quote mark and still breaks. This is
the rule the merchant string clean follows (`merchant-identity.md`), so a
name is the same words to identity and to bills.

It lives where the text is defined — `budget.join_apostrophes` in Python,
`budget.apostrophes_joined_sql` in SQL, already applied inside
`match_text` and `MATCH_TEXT_SQL` — so the token, phrase and
alternatives forms, the discovery query's letter runs, and the whole-word
comparison bills use to tell a shortening from a sibling all inherit it.
`tests/test_a_bill_reads_a_possessive_the_way_the_bank_prints_it.py`
checks the SQL forms select exactly what the Python matcher accepts.

## Why the outlet comes first

A chain's fuel arm is a merchant of its own (see `merchant-identity.md`):
the aggregator names the pump and the warehouse alike, and the outlet is
what tells them apart. The month plan and the Bills page have always read
the row that way. The history page, the category stamp, detection and the
SQL pre-filters read the aggregator's name instead, so for a fuel-arm row
they saw different words:

* a text-matched bill on the pump ("Northwind Club Fuel") was counted paid
  on the Bills page and listed no charges on its own history page;
* its category never reached the rows it paid;
* a pre-filter built on the narrower text could drop a row the Python
  matcher, given the row, would have kept — and a pre-filter is only
  correct while it admits a SUPERSET of what the matcher accepts.

For every row without an outlet the two readings are the same string, so
the rule changes nothing outside fuel arms.

## What follows from it

* A pre-filter and the matcher behind it read one string, so the superset
  property holds by construction rather than by care.
* Two surfaces cannot disagree about which rows pay a text-matched bill.
* A query that wants "exactly this merchant's raw strings" compares the
  identity key, never the match text: the text is the key *plus* the bank
  line and never equals a bare key.

`tests/test_bill_match_text_is_one_definition.py` fails when a module
spells the text for itself.
