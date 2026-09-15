# Import history and rollback

Every file import is **batch-tagged**: the Import page's **Recent
imports** list shows each batch — **When**, **File**, **Source**,
**Rows** — with an **undo** button. Undo asks you to confirm first,
naming the row count and anything else that would go with them.

## Rollback is safe by construction

Imported row ids are content-hashed, so re-importing the same or an
overlapping file converges on the same rows instead of duplicating
them; each batch that touched a row is remembered on it. Rolling back
a batch removes that batch's claim and deletes **only rows no other
batch still owns** — undoing a re-import can't destroy rows an earlier
import legitimately created.

## Deduplication against live sources

A file that overlaps a connected account's window must not
double-count. The importer matches file rows against aggregator rows
one-to-one (exact amount, a few days of posting-date drift tolerated,
description-aware) and keeps the connected source's copy.

## The usual fixes

- **Spending imported as income** (or vice versa): the file's amount
  sign didn't match. Roll the batch back and re-import with the other
  sign option — most bank CSVs use negative-for-spending, which is the
  default.
- **Wrong account**: roll back, re-import into the right one.
- **Bulk imports**: the whole-folder flow (analyze → editable plan →
  run) tags each file as its own batch, so one bad file rolls back
  alone.

## Describing imported rows with Plaid

Rows that came from files (or a provider other than Plaid) carry only
what the file said. If a Plaid connection is configured, Settings →
Connections has **Describe imported history with Plaid**: it sends the
newest undescribed rows (100 at a time) and brings back the merchant,
logo, location and category the way live Plaid rows have them. Plaid
bills this per row, so it never runs on its own — a monthly cap
(default 2,000 rows; set it under the button, 0 turns Enrich off) bounds
it, and the card shows what is left. Only one run at a time; a second
click while one is in progress says so. Nothing about
the rows' amounts, dates or accounts changes.

## Gotchas

- Rollback removes transactions, not the account they landed in.
- Category overrides you made on imported rows are lost with the rows
  if you roll their batch back — and so are receipts, notes and
  reimbursement pairings added to those rows since the import. The
  confirmation counts them before you agree.
- If a later import covered an overlapping date window on the same
  account, it may have skipped rows as duplicates of this batch. Undo
  cannot bring those back; it warns you, and re-importing the later
  file recovers them.
- Aggregator syncs aren't batches — rollback is for file imports.
  Connected data corrects itself on the next sync.
