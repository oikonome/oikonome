# Tax & income documents — building your lifetime income history

The Import page's **Tax & income document** card fills a per-year income
table (`income_annual`) plus itemized documents (W-2 box detail). The Cash
Flow report uses it for years before your bank history starts, and
retirement analysis reads the earnings spine. SSA records and IRS
transcripts are parsed on your instance and never leave it. W-2s and 1040s
are read by the AI model set up under **Settings → AI** (the vision
model): with a local model — the bundled Ollama, or one on your own
network — they stay on your machines too. If that model is a **remote**
service, a W-2 or 1040 is only read after the owner turns on **allow
sensitive documents to be sent to my remote model** in Settings → AI, and
then the whole W-2 image, or up to 12,000 characters of the 1040's text,
**Social Security number included**, goes to that service. Without the
switch, the upload is refused and you enter the figures by hand. The same
switch also lets check images in receipts go to the remote model.

Every import shows a **preview table first**: edit any figure, untick
years you don't want, then save. Existing years merge column-by-column —
importing an IRS transcript never overwrites what an SSA record filled.

## Where to get each document

| Document | Where | What it fills |
|----------|-------|---------------|
| **SSA earnings record** (XML) | ssa.gov → *my Social Security* → *Review your full earnings record* → download XML | SS-taxed + Medicare-taxed earnings for every year you've worked — the deepest history available (Medicare-taxed ≈ gross wages) |
| **IRS return transcript** (PDF/text) | irs.gov → *Get transcript* → Return Transcript for each year | wages, total income, AGI, taxable income, total tax |
| **W-2** (PDF or photo) | your employer / payroll portal (current + past years) | itemized box detail per document; fills the year's wages when empty |
| **Tax return 1040** (PDF) | TurboTax → *Documents*, or wherever you keep filed returns | the 1040 summary lines, same columns as a transcript |

## Parsing notes

- SSA XML and IRS transcripts parse **exactly** — no LLM involved.
- 1040 PDFs (as text) and W-2 images/PDFs are both read through the
  **vision model**'s backend (Settings → AI → Vision model —
  `./oikonome.sh llm vision` sets one up on the bundled Ollama).
  Always check the preview against the paper before saving.
- SSA marks the current year `-1` until it's recorded — that year is
  skipped automatically.
- Re-importing the same document is safe: same years, same columns,
  same values.
- A transcript uploaded as plain text is capped at about 2 MB, the same
  ceiling as the text extracted from a PDF — far above any real
  document. A larger file is refused; upload the transcript, W-2 or
  return on its own.
