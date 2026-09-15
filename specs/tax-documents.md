# Tax & income document import (spec)

Fills the lifetime income spine (`income_annual`, itemized
`income_documents`) that Cash Flow coverage and retirement analysis read.
Decisions:

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
  upserts. W-2 commit inserts `income_documents` (id = max+1) and can
  bump the year's `wages`/`primary_wages` only when the columns are
  empty.
- API: `POST /api/import/taxdoc/analyze` (multipart),
  `POST /api/import/taxdoc/commit` — under `/api/import*`, so script
  tokens can push tax docs too.
- Detection: XML sniff (SSA namespace/tags) → transcript markers
  ("ADJUSTED GROSS INCOME", "TAX PERIOD") in the text layer → W-2 marker
  ("Wage and Tax Statement"/box structure) → else 1040-via-LLM when text
  has 1040 markers; images always vision-LLM.
