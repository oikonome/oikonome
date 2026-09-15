# Tax & income documents — building your lifetime income history

The Import page's **Tax & income document** card fills a per-year income
table (`income_annual`) plus itemized documents (W-2 box detail). The Cash
Flow report uses it for years before your bank history starts, and
retirement analysis reads the earnings spine. Everything parses and stays
on **your** instance — nothing is sent anywhere.

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
- 1040 PDFs are read by the **chat model** (text only); W-2 images/PDFs
  need the **vision model** (Settings → AI → Vision
  model — `./oikonome.sh llm vision` sets one up on the bundled Ollama).
  Always check the preview against the paper before saving.
- SSA marks the current year `-1` until it's recorded — that year is
  skipped automatically.
- Re-importing the same document is safe: same years, same columns,
  same values.
