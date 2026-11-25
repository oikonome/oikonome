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
Where two documents give the same figure for a year, the newer one's
counts.

**Stored tax & income documents** on the Import page lists every
document you have saved — earnings records, transcripts and returns,
W-2s, and other forms (collapsed) — with the years it covers, whose it
is, the file name and when it was stored. **Remove** one and each figure
it gave goes back to the next older document that gave it, or goes blank;
a person's wages go back to the sum of their W-2s. The confirm says what
moves ("Career earnings loses the years this record gave…"). Removing an earnings record,
or a document covering several years, asks you to confirm it is you
first. A removed document stays listed for 30 days with **put back**.
**rename payer** beside a document's payer corrects the name — an IRS
transcript's abbreviation, or a name read wrong from a photo — on that
document's forms. The name is shown exactly as you typed it, never as
an abbreviation, and other forms with the same EIN are shown by it. No
figure changes and nothing is asked first; the rename is in the activity
log.
Figures saved before this list existed appear as "Stored earlier, date
not kept"; a tax return's figures from then are one row per year.

## Where to get each document

| Document | Where | What it fills |
|----------|-------|---------------|
| **SSA earnings record** (XML) | ssa.gov → *my Social Security* → *Review your full earnings record* → download XML | SS-taxed + Medicare-taxed earnings for every year you've worked — the deepest history available (Medicare-taxed ≈ gross wages) |
| **IRS return transcript** (PDF/text) | irs.gov → *Get transcript* → Return Transcript for each year | wages, total income, AGI, taxable income, total tax |
| **W-2** (PDF or photo) | your employer / payroll portal (current + past years) | itemized box detail per document; each person's wages for the year are the sum of their W-2s (see below) |
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
- **Whose W-2 is it?** A W-2 doesn't say which member of the household
  it belongs to, so the preview asks (primary earner or spouse — both
  are always offered). Each
  person's wages for a year are the sum of their W-2s — unless a tax
  return, transcript or earnings record already gave that figure, which a
  W-2 never overwrites.
- **Uploading a W-2 that looks like one already stored** (same year,
  box 1, and an employer that isn't clearly a different one) asks whether it is the same form — replace it — or
  a second W-2 — keep both. Two people at one employer can earn the same
  amount, so this is never decided for you.
- **Stored W-2s** are in the stored documents list: change whose each
  is, or remove one, and that year's wages are summed again.
  A W-2 marked *whose?* (uploaded from an older app, or before this was
  asked, in a year that already had one) counts for nobody until you
  pick. Removing a person's last W-2 empties wages that only W-2s had
  set. Restoring an older export never adds a second copy of a W-2 you
  have replaced since: a copy that only looks like a stored one comes
  back marked *whose?*.
- A transcript uploaded as plain text is capped at about 2 MB, the same
  ceiling as the text extracted from a PDF — far above any real
  document. A larger file is refused; upload the transcript, W-2 or
  return on its own.
