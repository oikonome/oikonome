# Tax & income document import (spec)

Fills the lifetime income spine (`income_annual`, itemized
`income_documents`) that Cash Flow coverage and retirement analysis read.
The rules:

1. **All four document families**:
   - SSA earnings record (ssa.gov XML; the statement's earnings table) →
     `ss_earnings` / `medicare_earnings` per year
   - IRS transcripts (return transcript: AGI / taxable income / total tax
     / total income; PDF or text) → the 1040 columns
   - W-2s (PDF/photo) → `income_documents` box detail
   - Tax-return 1040 PDFs (TurboTax etc.) → the 1040 columns
2. **Deterministic first, LLM fallback.** SSA XML and IRS transcript text
   parse exactly. 1040 PDFs extract their text layer and go to the CHAT
   model (regexing every TurboTax vintage is brittle); W-2 images/PDFs go
   through the vision path (like receipts, including the vision-model
   override and image downscale). No LLM configured → SSA + transcripts
   still work; W-2/1040 say what to configure.
3. **Preview table, then commit.** Analyze stages the file (bulk-import
   token pattern) and returns per-year rows with new-vs-update marks;
   the user edits/deselects; nothing writes until commit. Commit merges
   per column — a transcript never blanks SSA columns and vice versa
   (`source` accumulates, e.g. "ssa+transcript").
4. **UI: Import page card** ("Tax & income documents") + how-to doc
   (`docs/tax-documents.md`) covering where each document comes from
   (ssa.gov → my Social Security → earnings record; irs.gov → Get
   Transcript; employer portals) and the everything-stays-local note.

## Mechanics

- `engine/taxdocs.py`: `analyze(conn, filename, data, mime)` →
  `{token, kind, rows, warnings}`; `commit(conn, token, rows)` →
  upserts. A W-2 commit stores `income_documents` under the person it
  names and sums that person's wages for the year — see
  `specs/w2-wages.md` for the rule, the same-form question and the
  stored-W-2 doors.
- API: `POST /api/import/taxdoc/analyze` (multipart),
  `POST /api/import/taxdoc/commit` — under `/api/import*`, so script
  tokens can push tax docs too.
- Detection: XML sniff (SSA namespace/tags) → transcript markers
  ("ADJUSTED GROSS INCOME", "TAX PERIOD") in the text layer → W-2 marker
  ("Wage and Tax Statement"/box structure) → else 1040-via-LLM when text
  has 1040 markers; images always vision-LLM.

## Stored documents and what each one wrote

Every committed document is a row of its own, so the Import page can list
each one and take one back. Merging an upload's columns into
`income_annual` with only a free-text `source` would let a later document
overwrite a column silently, with nothing to undo.

**Tables** (migration 231): `income_uploads` (one stored document: `uid`,
its identity across an export and a restore, set when it is created; `kind`
ssa / transcript / 1040 / w2 / document / legacy_ssa / legacy_return,
`filename` with card numbers, SSNs and EINs cut to their last four (read back through the same cut, so a name an earlier build kept whole is shown cut), `stored_at`, `stored_by`, `removed_at`,
`removed_by`, `held_documents`), `income_upload_values` (per upload and
year, `fields`: the columns it wrote and their values) and
`income_documents.upload_id` (the upload a form came in).

**The one deciding function** — `taxdocs.resolve_year(conn, year)`. Each
`income_annual` column an upload ever wrote for the year is the value the
NEWEST LIVE upload (stored_at, then id) wrote into it, else empty. A wage
column no live upload sets goes back to the W-2s (`resum`,
`specs/w2-wages.md`). A column no upload ever wrote (a demo seed, a write
by a build older than the list) is never touched. A year whose figures do
not change is left exactly as it is, `source` included. `_commit_rows` is
"record the upload, write its values, resolve each year"; it writes no
column itself. A removal (`prune`) deletes a year left with no figure, no
note, no filing status and no W-2.

**Backfill.** Migration 231 gives a household's stored figures uploads so
every figure resolves to what it is: the earnings record's columns of
every year → one `legacy_ssa` upload; a year's return columns → one
`legacy_return` upload, with no file name (a wage figure still equal to
what the W-2s wrote is the W-2s' and is not given to it); every form → an
upload of its own. All of them are dated at the epoch plus their id, so
any document stored since outranks them and the moment the migration ran
decides nothing.
`taxdocs.backfill_household` is the same rule in Python for a restore of
an older archive; `test_the_restore_backfill_gives_the_same_uploads_as_the_migration`
holds the two together. Legacy return figures are attributed per year,
not per file: removing a "Tax return figures" row removes every return
figure of that year; its row says "Stored before this list" and names no
file.

**Nothing is set back behind a resolve's back** — `taxdocs.adopt_unexplained`.
A form with no upload is given one (dated before the list), and a yearly
figure no live upload explains (a writer outside the list — an older
release after a rollback, a hand load — or rows a restore landed)
becomes an upload of its own dated now ("Written outside this list,
found …"), so the figure people read is kept, listed and removable. Every
door that resolves a year adopts for that year first, before it writes its
own upload (resolve_year cannot do it itself: a document just written would
read as drift); the restore adopts before and after landing; the nightly
job adopts over every year. A wage figure still equal to what the W-2s
wrote is theirs.

**Doors.**
- `GET /api/import/taxdoc/stored` → `{documents, groups}`: per document
  `kind_label`, `years_label`, `whose_label`, `filename`, `stored_label`
  (or `removed_label`), `employer_shown` + `abbreviated` (an IRS name
  control — a few short capital words, or any number cut to four letters,
  judged before a "/" that joins a second name line — shows the name in
  its parentheses, else another form's with the same EIN last four, else
  itself — the client then says "as the IRS abbreviates it"), the W-2s it holds (`w2s`, for the whose-picker, which
  keeps using the W-2 owner door) and `seen`. Groups: earnings records,
  tax returns, W-2s, other forms (collapsed). Server words only.
- `GET …/stored/{id}/effect` → `{effect_line, step_up}`. The line is the
  removal's own result: the removal runs in a savepoint, Career earnings
  and Reported income are read before and after, and the savepoint is
  rolled back. It names only those two figures — nothing else reads stored
  income (retirement does not) — each once ("Career earnings loses 2001
  and changes for 2019"). When neither moves it says why: a W-2 whose
  wages a return's figure owns is said to be stood in for, and to back
  the wages if that return is removed; a W-2 nobody has claimed counts
  toward nobody's wages (`whose_label` says so too); a removal that
  changes stored income (`income_annual`, compared inside the same
  savepoint) but no figure shown — a primary's W-2 in a year SSA
  earnings or a return's household wages give the figures — says no
  figure shown changes and that the W-2 backs the wages if those go;
  only a removal that changes nothing stored is "nothing reads this
  document's numbers". A removed
  document has no removal to work out: 404, and so is one whose years
  moved while their locks were waited for (the words are worked out over
  the years locked, and read again under them).
- **A document's years** are one rule (`taxdocs._years_by`), read by the
  list and checked by every door that takes `seen`: each year a values
  row gives a figure for, each year of a form stored under the upload,
  and, once removed, each year of a held form a put-back would write
  back. A values row giving no figure covers no year, and the restore
  never lands one.
- A put-back returns each held W-2 to the person it was filed under; it
  comes back as nobody's only when a W-2 stored since the removal, in
  another upload, is the same person's same form (box 1, EIN last four).
- `DELETE …/stored/{id}` `{seen: {kind, years, stored_at}}`: a stale list
  is refused 409. Under the years' locks (sorted, bounded behind a
  restore; the document-ids lock, which a form without an upload needs
  before it is adopted, is asked for only then and bounded the same way): `removed_at` set, the upload's forms moved into
  `held_documents`, every year resolved. An earnings record, or a document
  that wrote more than one year, needs the step-up window
  (`_require_elevation`; both clients run the elevate sheet on its 403).
  The door asks early, and the removal asks again under the years' locks
  over exactly what it takes (`taxdocs._step_up_for`), so a document
  whose years moved after the early answer is never taken without it.
- `POST …/stored/{id}/restore` `{seen}`: within 30 days the upload is
  live again, its forms come back (see the put-back rule above), its
  years resolve. **A put-back asks for no step-up**,
  whatever the document is — it only restores what the household already
  had, and a session thief who puts a document back has taken nothing.
  Removal keeps its step-up. Test:
  `test_elevation.py::test_put_back_needs_no_step_up_while_removal_does`.
- `POST …/stored/{id}/payer` `{seen, payer, name}` (`taxdocs.rename_payer`):
  the payer named on the document's forms whose stored payer is the
  `payer` the list sent (a transcript holds several; only the one shown
  is renamed), under the years' locks and checked again there; a stale
  list or a payer since renamed is 409. No step-up and no resolve: no
  figure reads a payer (matching compares box 1 and the EIN). `name` is
  120 characters at most. A removed document is not renamed. The
  renamed forms are marked `payer_named_by_person` (migration 233): a
  marked name is shown as typed with `abbreviated` false, before the
  parenthesis, name-control and same-EIN steps, and it is the name the
  same-EIN lookup gives other forms first. The mark is held by a removal,
  written back by a put-back and carried by export and restore (an older
  archive lands unmarked). The list's
  `payer` is the stored name behind `employer_shown`; a client offers
  "rename payer" only when it is sent, so an older server offers none.
- The nightly job purges uploads removed more than 30 days ago
  (`taxdocs.purge_removed`), each under its years' locks and checked again
  there, so a put-back that won the race keeps its document, and one whose
  years moved before the locks waits for the next night; their years
  were resolved at removal.
- Activity log: "removed / put back the stored <kind> <years>", "renamed
  the payer on the stored <kind> <years>", never the file name.
- `GET /api/import/batches` gains `older_imports_line`: accounts filled
  from files that no import batch here holds (a restore, an import from
  before undo history), printed under "No file imports yet".

**What each client reads.** Web `Import.tsx` and phone `imports.tsx`,
`StoredDocuments`: one card "Stored tax & income documents" in place of
"Stored W-2s"; rows grouped as the server groups them; the confirm prints
`effect_line`; a removed row offers "put back". The W-2 doors
(`/import/taxdoc/w2s…`) stay for older apps.

**Older client / stale tab / rollback / restore.**
- An older app reads and edits W-2s through the W-2 doors, unchanged; a W-2
  it removes leaves its upload empty, and an empty upload is not listed.
- A stale tab's removal is refused by `seen` (409, "reload").
- After a rollback, an older build writes columns directly; the next
  resolve of that year sets back the columns an upload wrote. Forms it
  stores have no upload and are not listed until a restore's backfill
  gives them one.
- Export carries both tables with `removed_at`; who stored or removed a
  document is this instance's person and is not carried. A restore lands
  uploads by (kind, file name, stored_at) — one here already, removed or
  not, is not landed again, and nor are its values or forms, so a removal
  is never undone by a restore — renumbers a taken id, maps values and
  forms onto the id the upload actually landed under (a value or form whose
  upload did not land never names one), then resolves every year the
  archive brought income for. Merged into a household that holds uploads
  of its own: the household's own figures are adopted first (before
  anything lands), so each has an upload; then the newest document across
  both wins each column — an archived real document stored later than the
  household's own figure replaces it — with one exception: a figure from
  before the list (`legacy_*`) in the archive never replaces a figure the
  household's own documents give for the same year and column, so a local
  hand or legacy figure is never silently replaced by an archived legacy
  figure. After landing, only the years whose income row the archive
  wrote (years the household did not hold) have their unexplained figures
  adopted; a year the household held is never adopted a second time. An
  archive without the tables runs the backfill (an empty household) on
  what it landed; every form it brought without an upload is given one.
  Archived moments later than now are read as now, so no document is
  dated ahead, and one before 1900 refuses its upload row, and a figure adopted from an outside writer is dated after
  every upload of its year.
