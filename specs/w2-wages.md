# A person's W-2 wages (spec)

A W-2 does not print which member of the household it belongs to. The
person says whose each W-2 is, and a person's wages for a year are the sum
of their W-2s **whenever W-2s are what set that figure**. This note is the
contract; `engine/taxdocs.py` is the one place that decides it.

## The figure

`income_annual.primary_wages` and `income_annual.spouse_wages` are the two
per-person wage figures. Each has a companion that records the value the
W-2s last wrote into it: `primary_wages_w2` and `spouse_wages_w2`
(migration 226).

**A figure belongs to the W-2s when it is empty, or when it still equals
the value the W-2s last wrote** (`figure IS NULL OR figure IS NOT DISTINCT
FROM figure_w2`). Only then may a W-2 change it. Anything else wrote it
— a return, a transcript, an earnings record, an older build — and it is
left alone.

The ONE function that sets it is `taxdocs._resum(conn, year)`, a single
statement per person:

```
figure    := sum of box 1 over that person's W-2s of the year whose box 1 was read
figure_w2 := the same sum
   WHERE the figure belongs to the W-2s
```

A sum over no W-2 is NULL: removing a person's last W-2 empties a figure
the W-2s set, and never touches one they did not.

Why a stored value and not a flag (migration 221's `*_from_w2` booleans,
now unread): a flag survives a writer that does not know it. An older build
after a rollback writes `primary_wages` from a return and leaves the flag
set, and the next W-2 would overwrite the return. A stored value cannot be
fooled that way — any other writer moves the figure away from it. Every
writer in this build that sets a wage figure also clears its companion, so
that holds even when the other writer happens to write the same number.

Readers: `primary_wages` is read where it always was (lifetime income
coverage, `COALESCE(medicare, ss, primary_wages, wages)`). `spouse_wages`
has no reader yet; it is kept correct for when one exists. No client
computes either figure.

## Whose a W-2 is

`income_documents.owner` is `primary`, `spouse` or `unassigned`. An
`unassigned` W-2 is listed and counted for nobody.

- The W-2 preview asks whose it is (web + mobile). A commit row from this
  build carries `owner` = `primary` | `spouse`.
- **An older app sends no `owner`.** Its W-2 is the primary earner's when
  the year holds no primary W-2 yet (what every W-2 was before the
  question existed), and `unassigned` otherwise — the second W-2 of a year
  is exactly the case where it may be the spouse's. Nothing is inferred
  beyond that; the stored list names it.
- An archive written before migration 221 (no `owner` column) lands by the
  same rule, per year, in archive order.

## Who a W-2 can belong to

- **The one function**: `taxdocs.w2_people(conn)` names the people a W-2
  can be filed under. It offers **both, always** — the primary earner and
  the spouse — whatever the household holds.
- Nothing stored is evidence of who is in the household: logins are not
  (a couple may share one), and no import writes a return's filing status
  or joint mark, so a rule on them would fire only on old or restored rows
  and could not be undone from the page. A narrower list would need real
  evidence of who is in the household.
- Sent as `w2_people: [{owner, label}]` on `GET /api/import/taxdoc/stored`
  (the stored W-2s' picker) and on `POST /api/import/taxdoc/analyze` (the
  preview's). Both clients' pickers print exactly that list, in order.
- **Older server** (the phone only): no `w2_people`, and both people are
  offered (one marked stand-in) — the same list.

## Is it the same form? — asked, never inferred

Two spouses with the same employer and the same box 1 hold two W-2s, so
year + employer + box 1 is not an identity. Nothing is ever merged on it.

- The preview lists, per row, the stored W-2s of the same year that look
  like it: the same read box 1, and no EIN saying it is another employer
  (the last fours agree, or either side's EIN was not read — a re-read
  that missed the EIN is still the form; asking about a stranger costs one
  question) (`matches: [{id, owner, employer, box1}]`). An unread box 1 is
  never comparable: it matches nothing and nothing matches it.
- When there are matches the client asks: **"The same form again —
  replace it"** or **"A second W-2 — keep both"**, and sends `replaces:
  <id>` or `replaces: null`.
- A row from this build (it carries `owner`) whose W-2 has matches at
  commit time and carries no `replaces` key is refused (409, "say whether
  this is the W-2 already stored", with `matches` per committed row);
  nothing is written, the preview token survives, and the client asks
  about those matches in place. That covers a lookalike the person's own
  edit of the preview created, which analyzing again would never show.
- `replaces: <id>` updates that stored W-2 in place (employer, EIN, boxes,
  owner). It must still be one of the row's matches at commit time: an id
  comes back after a removal, so a stale answer may name another form —
  asked again (409 with the current matches), or refused (400) when
  nothing looks like the row any more. Two rows of one commit never
  replace the same W-2. Re-uploading under the other person and answering
  "the same form" is a move.
- An older app is never asked: its W-2 is always stored as a new row, as
  it always was (and lands per the owner rule above).

## Writes

Every write that changes a year's W-2s — store, replace, remove, owner
change — runs in one transaction under a per-(household, year) advisory
lock, then calls `_resum` once for that year. Years are locked in sorted
order. A person's request waits a bounded few seconds behind a restore
(`bounded_lock_wait`) and answers busy rather than holding a pooled
connection.

Doors: `POST /api/import/taxdoc/commit` (rows may carry `owner`,
`replaces`); `GET /api/import/taxdoc/w2s` (the stored list, EIN masked to
the last four); `DELETE /api/import/taxdoc/w2s/{id}` `{seen}`; `POST
/api/import/taxdoc/w2s/{id}/owner` `{owner, seen}`. `seen` is what the
list showed — year, box 1, employer, EIN, whose — in the body, never the
URL (wages do not belong in an access log). When the id now holds another
form (a removal elsewhere freed it; two spouses' twins share year and box
1, so the rest is compared too) the answer is 409 "this W-2 changed —
reload". The owner door never refuses an identical
twin under the other person — that is two spouses' W-2s.

An annual commit (SSA, transcript, 1040) that writes a wage figure clears
that figure's companion in the same statement — it now goes through
`taxdocs.resolve_year`, which also hands a wage figure back to the W-2s
when the document that wrote it is removed (`specs/tax-documents.md`,
"Stored documents and what each one wrote"). A W-2 commit is an upload
too, and removing it from the stored list holds its forms for 30 days.

## Old clients, stale tabs, rollback, restore

- **Older client**: no `owner`, no `replaces` → stored as a new row under
  the owner rule; the sum runs as for any write. Its response gains
  nothing it must read. It has no stored list either: a second W-2 of a
  year it uploads lands `unassigned` and counts for nobody until the
  person names it in a current app or on the web.
- **Stale tab**: a preview committed after another device stored a
  matching W-2 is refused by the commit-time check and the person
  re-answers; an owner change or removal of a W-2 already gone is a 404.
- **Rollback** to a build before this one: it ignores the companions;
  any wage figure it writes moves away from its companion and stops
  belonging to the W-2s. A W-2 it stores into an empty figure fills it
  without a companion — that figure then belongs to nobody's sum, the
  safe direction.
- **Document ids** are MAX+1 across the household, allocated under one
  household lock (after the year locks) that the live store and the
  restore share, so a store of another year never takes the id a restore
  is landing. A person's store waits a bounded time behind a restore.
- **Restore** reads the archive's income years ahead (parsed as the rows
  are, so "2021.0" is 2021) and takes those year locks (a W-2 year is 1900–2100; a W-2 outside it is refused) before its
  first income write, so it cannot deadlock with a live store and holds a
  handful of locks, not two hundred. It lands each figure and its
  companion as archived. Presence is COUNTED per identity (year, form,
  employer, EIN last four, box 1, boxes, notes — not the owner): each copy
  the household held before the restore accounts for one archive copy,
  the same person's first, then any — so identical twins round-trip, two
  spouses' twins never both land under one of them, and a copy named
  since is not landed again. An archived W-2 the household does not hold
  but that LOOKS like more stored W-2s than its own exact copies (the same
  form replaced or re-read since the export, even beside its twin) lands
  `unassigned`: the restore cannot ask, and
  summing it would double the wages. Each year that received a W-2 is
  summed once at the end.
- **Archives older than 226** (`archive_upgrades.archive_schema`): the
  restore seeds a figure on a row whose source is exactly `w2` as the
  migration does. In a year whose row the restore landed, the W-2s land
  as archived and the year is reconciled (below); in a year the household
  already had, they land by the unsaid-owner rule.

## A W-2 stored before it named whose it is (migration 227)

A W-2 stored before a W-2 named whose it is carries the column default
`primary`, whoever's form it was. Where a W-2-owned primary figure disagrees with
what its primary W-2s add up to, only the W-2 the figure stands for (box 1
= the figure; failing that the first stored) stays primary and the rest
become `unassigned`; a figure that still disagrees stops belonging to the
W-2s. A consistent year is untouched and no figure moves.
`taxdocs.reconcile_unmarked` is the same rule for one year (restore).
