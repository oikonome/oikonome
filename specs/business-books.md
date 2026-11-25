# Business books: which figure each row counts in, and who decides

The Business page (web and phone), the P&L CSV and the year-end package
read one set of books. Each figure below is decided by ONE server
function; the clients and the downloads print what it sends and compute
nothing. Amounts in examples are invented.

Shared rules (as in `specs/today-figures.md`):

- **Additive.** New fields sit beside the old ones, which keep their
  meaning unless a section says otherwise.
- **Older phone** reads the old fields and shows what it showed before.
- **Older server** (a current phone against a server that has not
  upgraded): a new field is absent and the phone leaves that line out.
- **Stale tab**: every figure arrives in its own reply; a write
  invalidates the replies it moves.
- **Restore** brings back transactions, classes and equity movements;
  every figure here is derived from them on the next read. Nothing below
  is stored.

## What a row is: `books.row_roles`

`books.entity_transactions` gives every row a `role`, decided once by
`books.row_roles` from the rules the P&L already applies:

| role           | what it is                                              |
|----------------|---------------------------------------------------------|
| `cost`         | money out the P&L counts (any bucket)                   |
| `revenue`      | money in counted as gross receipts                      |
| `refund`       | money in that reverses a cost                           |
| `owner_in`     | a transfer INTO a business account from outside it      |
| `owner_out`    | a transfer OUT of a business account to outside it      |
| `owner_side`   | the owner's personal leg of such a transfer (a row on a personal account booked to the business) |
| `internal`     | a transfer between two of the business's own accounts   |
| `card_payment` | paying the business card — the charges were the costs   |
| `owner_tax`    | the owner's income or self-employment tax               |

Beside it, `paid_personally` (the row sits on an account that is not the
business's) and `owner_recorded` (the equity movement already written for
the row: `contribution`, `draw`, `distribution`, `reimbursement`, or
null).

- **Books list** shows `role == "cost"` rows only, with `effective_bucket`
  (never a client default). Owner transfers (`owner_in`, `owner_out`) are
  listed under Owner equity with "Record as a contribution" / "Record as a
  draw"; one tap posts `POST …/owner-transfer {txn_id}`, which records the
  movement at the row's own amount and date (`equity.record_owner_transfer`),
  refusing any row that is not an owner transfer of this business.
- **Older phone**: has no `role`; it lists `amount > 0` as before.
- **Year-end package and P&L CSV**: the ledger's "Schedule C line" column
  prints `books.ledger_line(row)` — the line for an operating cost,
  "Organizational cost (§248)" / "Start-up cost (§195)" for a capitalized
  one (never its stored line too, so a preparer cannot deduct it twice),
  and the role's words for every other row ("Owner contribution", "Owner
  money in — not yet recorded as a contribution", "Transfer between
  business accounts", …). No row reads "Unclassified".

## Schedule C lines in the P&L: `books._pnl_core`

`operating_by_line` keys are Schedule C lines only. An operating cost with
no line is summed under one key, `books.UNLINED` ("Uncategorized — needs a
line"); a raw ledger category never becomes a line. The CSVs print the
keys as sent. An older client prints the keys it is sent, so it shows the
new key too.

## Suggested lines: `books.suggest_lines`

Two sources, in order, and never the bank's category:

1. `merchant` — the owner's own earlier line for this merchant in this
   business. A saved line that is exactly what the retired
   category-guess would have offered for that row is not counted as the
   owner's choice (it was most likely a guess accepted), so it cannot be
   "learned" and repeated.
2. `kind` — the built-in list of business kinds (`business_kinds.KINDS`):
   the merchant's registered card-network code (MCC) when the bank sent
   one, else words in its name or website ("hosting", "ads", "service
   fee"). Each kind names a line, and `kind_label` says which kind matched
   ("software and hosting").

No match, no suggestion. `source` is `merchant`, `kind` or null; the
"learned" badge is for `merchant` only. An older client shows no badge
for `kind` (it knows only `merchant` and `category`).

## The capital account: `equity.capital_summary`

A business cost paid on a personal card or account (`paid_personally`,
role `cost` or `refund`) is money the owner put in. Unless the row
already carries a contribution or a reimbursement recorded by hand, it
counts as a contribution automatically: `contributions` includes it, and
`paid_personally` says how much of `contributions` it is. Derived on every
read — deleting the charge, moving it back to the household, or recording
a reimbursement for it takes it out again; nothing is written.

The year-end capital (`api_business._year_end_sheet`) counts the same
rows dated on or before the year end.

## Why capital and cash differ: `selfemploy.balance_sheet`

`reconcile` is a list of sentences the page, the phone and
`balance-sheet.csv` print under the balance sheet:

- the part of capital that is costs paid personally;
- when net assets and capital differ by a cent or more: by how much, the
  part that is owner transfers not yet recorded as contributions or draws,
  and the rest, which the books cannot see (balances from before the
  ledger begins, money moved outside linked accounts).

`capital_gap` is the difference itself (net less capital). Older clients
ignore both.

## Social Security after the owner's W-2 wages: `selfemploy.owner_w2_wages`

Schedule SE charges Social Security (12.4%) on net self-employment
earnings only up to what is left of the year's wage base after the
owner's own W-2 Social Security wages for the same tax year. Medicare
(2.9%) has no base and is unchanged. The ONE function that decides which
wages count is `selfemploy.owner_w2_wages(conn, entity_id, year)`;
`_se_tax` takes its figure, and `estimated_tax` (the Tax tab, the
set-aside card and the quarterly installment, on web and phone) is its
only caller.

### Whose business it is: `business_entity.owner`

Each business says whose it is (migration 239): `owner` is `primary`
(the primary earner), `spouse` or `unknown`, default `unknown`. It is
the only fact that says whose W-2 wages may close the business's wage
base, because a W-2 says whose it is and a business does not otherwise.

- `primary` nets the primary earner's W-2 Social Security wages; `spouse`
  the spouse's. `unknown` nets NOTHING: the figure may be high, never low,
  and the note says so and where to set it.
- A sole proprietorship or single-member LLC: the setting is the owner. A
  multi-member LLC: the setting names the person who is the member marked
  as this household, and only that member's share is netted; with no
  member marked, or the shares incomplete (`_household_unavailable`),
  nothing is netted and the note says which of the two it is. The other
  members' wages are never known and never netted. An S corporation owes
  no SE tax here.
- Every business that existed before migration 239 is `unknown`, so its
  figure is the un-netted one until the owner sets it. The demo's
  business is seeded as the primary earner's (the demo cannot save it),
  with a W-2 for each finished year: a past year says why the primary
  earner's W-2 is not netted, the running year says its W-2 is not in
  yet.
- The door: `POST` and `PATCH /api/business/entities` take `owner`
  (optional). A body that does not name it leaves the stored value as it
  is, so an older client never resets it. The reply carries `owner` and
  `owner_options` (`[{value, label}]`: Primary earner, Spouse, Not set),
  which the web and phone pickers print in order. An older server sends
  neither, and the clients draw no picker.
- Export and restore carry the column; an archive written before 239 has
  none and its businesses land `unknown`; a value outside the three lands
  `unknown` and the summary says so.

### Which wages

- The W-2s marked as the setting's person (`income_documents.owner`).
  **Box 3** is the Social Security wage, and only box 3: a box 3 read as
  0 is a real 0 (a job outside Social Security — some public employers,
  clergy, some students — has a real box 1 and no Social Security wages).
  A W-2 whose box 3 was not read counts as 0 Social Security wages, and
  the note says the box was not read, so the estimate may be high. Box 1
  never stands in, and neither does `income_annual.primary_wages` /
  `spouse_wages` (a return's box-1 wages).
- **Which year: the estimate's own tax year, only.** A year's W-2
  arrives the next January, so the year in progress — the year the
  clients ask about — has none, and nets NOTHING; the note says no W-2 of
  that year is on file yet, that the estimate may be high, and that wages
  count once the year's W-2 is added. No earlier year stands in: an owner
  who left the W-2 job would have last year's wages netted against a year
  with none (the estimate low), and a box 3 capped at an earlier year's
  lower base would reopen room in this year's (Social Security charged
  that a capped earner does not owe). A figure that can read a tax
  estimate low is not guessed. A later year's W-2 never counts either.
- **Only a W-2 a person said is theirs.** `spouse` on a W-2 is only ever
  a person's answer (the upload's whose-is-it question, the stored list's
  owner change, an archive of either). `primary` is not: an app older
  than the question, and a restore of an older archive, file a W-2 under
  the primary earner when the year has none yet (`taxdocs.unsaid_owner`),
  a spouse's included, and the stored row does not record which happened.
  So a primary earner's W-2 is never netted, and the note says why; a
  business set as the spouse's nets the spouse's W-2s. Netting the primary
  earner's would need a stored mark that a person chose the owner
  (`income_documents`, set by the asking upload and the owner change,
  carried by a replace and an archive), and there is none.
- **One form, once.** A second upload of the same W-2 is its own row
  (migration 225). The tax documents' own "looks like the same W-2"
  (`taxdocs.w2_matches`: the same read box 1, no EIN last four telling
  them apart, a missing EIN matching) groups them, and a group counts
  once, at its smallest box 3 (an unread box 3 in it counts the group as
  none), and the note says so: counting a form twice would read the
  estimate low. Nothing stored records a person's "a second W-2" answer,
  so two real identical forms count once too — the estimate may read
  high, never low.
- **A box 3 out of bounds** (beyond what the tax-document doors accept,
  or negative) is a box 3 not read.
- A W-2 not yet marked whose counts for nobody.
- **Several businesses of one person are not combined.** Schedule SE
  adds one person's SE earnings across all their businesses before the
  wage base applies; each estimate here is one business's, so two
  businesses set to the same person each charge Social Security up to the
  room left. The note says how
  many other active businesses (S corporations aside) are set as the same
  person and that the estimate may be high.

### Said beside the figure

`ss_wages_note` is the server's sentence, which both clients print as
sent under the SE tax line (below the loss note; an older server sends
none and nothing is printed). It names the person from the setting:

- wages counted (a business set as the spouse's): "This business is set
  as the spouse's" (a multi-member LLC: "The member marked as this
  household is set as the spouse"), then "their 2026 W-2 Social Security
  wages of $X", and either the part of the wage base they leave or that
  they already reach it (Medicare only);
- set as the primary earner's, with their W-2 of the year on file: "This
  business is set as the primary earner's, but their 2026 W-2 wages are
  not counted: a W-2 filed under the primary earner cannot yet be told
  apart from one an older app filed there without asking whose it was.
  The estimate may be high.";
- set, with no W-2 of that person for the year: "This business is set as
  the primary earner's, but no 2026 W-2 of theirs is on file yet, so no
  W-2 wages are counted and the estimate may be high. Wages count once
  the year's W-2 is added.";
- `unknown` while the household has W-2 wages on file: that they are not
  counted because the business does not say whose it is, the estimate may
  be high, and the setting is on the business's details;
- a multi-member LLC with no member marked, or with the shares
  incomplete: that the wages are not counted and why, the estimate may be
  high;
- a W-2 counted once for a duplicate, a box 3 not read, a W-2 not marked
  whose, other businesses of the same person: a line each;
- not set and no W-2 wages on file: null.

- **Not modelled:** the 0.9% Additional Medicare Tax (it is not charged at
  all, so it needs no Medicare wages yet).

## Deadlines: `compliance.obligations`

Beside the state's annual report and the owner's own entries, the
calendar derives the federal dates for the entity's structure and year:

- the quarterly estimated-tax payments (owners of a pass-through:
  sole proprietor, single-member LLC, partnership, multi-member LLC,
  S corporation; a C corporation's own estimates for a C corporation);
- the 1099-NEC/1096 filing, January 31, when the business paid any
  vendor the 1099-NEC amount for the year before;
- the annual return: Schedule C with the owner's Form 1040 (April 15)
  for a sole proprietor or single-member LLC, Form 1065 (March 15) for a
  partnership or multi-member LLC, Form 1120-S (March 15) for an S
  corporation, Form 1120 (April 15) for a C corporation.

Every federal date that falls on a weekend or a federal legal holiday
(Washington, D.C.'s Emancipation Day included, observed on the nearest
weekday) moves to the next business day — `compliance.business_day`, the
one rule `selfemploy.quarterly_deadlines` reads too, so the estimate's
`next_deadline` and the calendar name the same day.
