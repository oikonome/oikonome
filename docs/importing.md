# Importing your financial history

More history = better recurring detection, better baselines. Every path
below is idempotent (re-importing converges), deduplicated against your
connected banks (±3 days, exact amount — the bank wins), and **one-click
reversible** (Recent imports → undo).

## How much history comes in

One household setting decides how far back new history reaches: **Last
month**, **3 months**, **6 months**, **1 year** or **Everything
available** (the default). Pick it in the setup wizard's Connect step or
on the Import page (**How much history should come in?**); it saves on
the tap.

- **Bank links** — a new Plaid link asks the bank for that much history.
  Banks share up to two years, so "Everything available" means two. The
  window is read when the link starts; a bank already linked keeps what
  it brought.
- **File imports** — every file format honours it. Rows dated before the
  window (counted back from today in the household's time zone) are left
  out, and the import result says so: "N older rows left out — the
  history window is 3 months; widen it on the Accounts page or in the
  wizard to bring them in." (`skipped_older` in the API result; a folder
  import shows the line per file). Re-importing a statement you imported before is still
  recognised and corrected as usual.

More history means better bill detection and a budget seeded from real
months; less keeps the first sync quick. To bring older history in
later, widen the window (or choose **Everything available**) and import
the older files from Accounts → Import.

## Live sync

- **SimpleFIN (recommended, US/CA)** — sign up at bridge.simplefin.org
  ($1.50/mo paid to them; the instance never sees your bank login), create a setup
  token, paste it in the setup wizard or on the Accounts page. A setup
  token works once. If the bank connects but the first download does not
  finish, the connection is kept and the next sync retries it — no new
  token needed.
- **Plaid (bring your own)** — create a free Plaid account (Trial plan,
  10 connections), then enter its client ID and secret in the **Plaid**
  row of Settings → Connections (**set up**; later **replace keys**) and
  link via the API. Advanced users only; SimpleFIN is easier.

## Files (Import page)

| Format | How to get it | Notes |
|---|---|---|
| CSV | any bank's "download transactions" | headers auto-detected (incl. separate Debit/Credit columns); unknown headers get a column-mapping form (map one Amount column, or a Debit column, or a Credit column — a file with only withdrawals or only deposits is fine; two columns with the same name are listed separately, by position) |
| OFX / QFX | bank's "Quicken/Money" download | no mapping needed, stable ids; two different transactions that share a bank id (FITID) are both kept — including a bank that numbers its FITIDs per download, so next week's file never overwrites last week's charges; a re-import updates a stored line in place only when the bank plainly restated that same line (same payee and direction, the same day or the same amount a few days apart, inside the dates the new file covers), and otherwise keeps both |
| QIF | old Quicken files | 90s-era date quirks handled; multi-account exports split; a whole-file export's category, class and security lists are skipped |
| Mint | your archived `transactions.csv` | categories mapped automatically |
| YNAB | register export | Outflow/Inflow handled; a register holding several YNAB accounts asks where each one goes (see below) |
| PDF statement | bank, card or brokerage statement with a text layer (not a scan) | pick the statement kind on the Import page; the cycle is dated from the statement's period line, or from separate **Opening Date** / **Closing Date** lines; the sign of an unsigned line comes from the statement itself: on a bank or brokerage statement that prints a running balance, the one reading of in/out that makes the balances add up exactly (a statement whose balances could add up two ways says nothing); then the column it is printed under, when the statement has a withdrawals/debits and a deposits/credits column (a figure under Deposits is money in whatever the words say); then the section heading it sits under (*Withdrawals*, *Checks paid*, *Deposits and additions*, …). A printed minus, parentheses or CR/DR marker always wins. Only a line none of these covers is read by its words (deposit, withdrawal, dividend, …), where an unsigned line with no such word reads as money out — cash back taken with a purchase is money out, a cash-back *reward* is money in, and a retirement account's own distribution (IRA, RMD, Roth …) is money out while a fund's dividend or capital-gain distribution is money in; the import's warnings say how many lines were read by their words, so you can check them |
| Monarch / Copilot / Simplifi | each app's CSV export | auto-detected |
| Oikonome export ZIP | Settings → Data & maintenance on another instance | runs in the background with progress on the page (a big archive takes minutes; leaving the page doesn't stop it) — full restore: transactions, bills, your answers to recurring-bill and merchant-merge offers (something you declined is not offered again), the import history (so a file import that came along can still be undone under Recent imports), overrides, settings, and business-entity data (entities, equity, mileage, 1099 vendors, compliance, classifications). **Note:** a business entity's full EIN is never included in the export for security — after restoring, re-enter it (only the last 4 digits carry over). The daily email's recipient answers travel, with one narrowing: a **decline** always carries over, so somebody who said no stays off the list; an **acceptance** carries over only for an address that already has its own login on the destination household (in hosted mode, one whose email is confirmed). Everyone else — and anybody who had not answered yet — lands as *not invited* with an **Invite** button, because the invite *link* itself never travels. The restore summary tells you how many people have to accept a fresh invite before their mail resumes; Settings → Email & Push is where you send it. |

**Restoring after you have already linked your banks.** You can restore
an export before or after linking the same banks here; either way you end
up with one copy of each account. When a bank is already linked, every
restored account that matches exactly one linked account at that bank (same
last four digits and type) is merged into it: the older history comes
across, the days both copies cover are kept once, under the linked
connection's transactions, and your notes, categories and receipts on those
days stay on them. If two accounts at one bank share the last four digits
and type, nothing is guessed — the restore summary names the accounts that
may now appear twice, so you can link or remove the extra copy on the
Accounts page.

**Amount signs:** most bank CSVs use negative-for-spending — that's the
default. If your import shows spending as income, undo it and re-import
with the other convention selected. CSVs with a Debit and/or Credit
column are unambiguous and ignore the convention picker: whichever side
is present is read as that direction, and the missing side is simply
absent rather than assumed. A sign printed inside such a column is
ignored — a card export that writes a refund as `-150.00` in its Credit
column is still money in. YNAB's Outflow/Inflow columns are read the
same way.

**Re-importing after a reading changed.** When the importer reads a line
differently from the way an earlier version did — the sign a Debit/Credit
column, a statement's running balance or its wording gives, or the year a
December→January statement's January lines fall in — importing the same
file again corrects the rows that earlier import made, in place, keeping
your notes, receipts and categories on them; it never adds the corrected
row beside the old one. Only a row proved to come from the same line of the
same file is touched: a CSV row whose cells match exactly, or a PDF row read
from the same printed line (for a re-dated January row, one owned only by
earlier imports of this same statement — last year's statement prints the
very same January line, so its row is never moved). If an earlier re-import
already added the corrected row beside the old one, the old one is retired
onto it. Rows from a file you never import again keep their old reading,
and a statement printing only January lines cannot prove which year's row
is its own, so it adds the right row and leaves the old one for you to undo
under Recent imports.

**Multi-account files split automatically.** A whole-household export
(Mint / Monarch / Copilot / Simplifi with several account names, or an
OFX file carrying several statements) never merges into one account:
each source-side account gets its own, typed from its name or statement
(cards land as credit so spend math stays honest). Re-importing lands on
the same accounts, and renames/reclassifications you make are never
overwritten. Single-account files go to the account you picked.

**A YNAB register with several accounts asks where each goes.** A YNAB
register export names its account on every line (a checking account and a
card in one file). Import it on its own and the Import page lists each YNAB
account with its row count and dates; choose an existing account, a new
one, or skip it for each, and nothing is imported until every one has an
answer. Nothing is matched by name. Each account's lines are checked for
duplicates against the account they land on, so a card purchase your card's
bank feed already has is skipped there. Re-importing the same register into
the same choices updates the rows it made before. If an earlier import put
the whole register on the account you picked, the card offers to remove the
rows it put there for the accounts now going elsewhere; it removes only rows
whose id and recorded YNAB account both prove they came from that import,
keeps any you have added notes, receipts or other changes to, and leaves
rows another export put there for you to undo under Recent imports. Undoing
the new import does not bring removed rows back. A folder import, an older
version of the app and a script token are refused such a file by name —
import it on its own, or export one YNAB account at a time.

**Limits worth knowing.** One file per import is capped at **50 MB**, and
a restore archive (an export ZIP, which carries a whole household's
history and its receipt images) at **500 MB** — a restore is streamed to
disk rather than held in memory, which is why it may be that much larger
(an instance behind a CDN or proxy may cap request bodies lower — see
[the reverse-proxy guide](reverse-proxy.md#upload-size-and-time-limits)).
Drop the export ZIP **on its own** to use that limit: mixed in with other
files it is read with the folder.
Some formats carry a tighter limit of their own, because of how much
work each one is to read: an OFX/QFX file is capped at **10 MB**, a
register exported from YNAB, Mint, Monarch, Copilot or Simplifi at **40
columns**, and any other CSV at **200 columns**, and no single row of
any CSV may run past **128 KB**. A file past one of these
is refused with a message saying which, before anything is imported.
Receipts travel inside the export as the photos themselves, so they are
most of a big archive: the receipts file inside it has no size cap of its
own, but the whole ZIP must stay under the 500 MB upload, which is
roughly 500 MB of receipt photos less the rest of the history. The rest
of the archive is held to **128 MB** per file and **512 MB** unpacked.
An export that is over any of these still downloads complete — it is
your copy — but carries a `RESTORE-NOTE.txt` saying what a restore would
refuse.
A folder analyze takes up to **200 files** and **60 MB** in all at a time
(a bigger folder is refused with *"import the folder in smaller
batches"* — split it and analyze each part). The doors that take a
file are also budgeted so one client cannot monopolise the instance: a
folder analyze or a tax-document analyze runs **20 times an hour**, a
single-file import **60 times an hour**, and an instance takes only a few
uploads at once — see
[troubleshooting](troubleshooting.md#an-upload-is-refused-or-times-out)
for what the refusals look like.

**Account numbers in file names.** A folder import names a new account
after its file, and banks often name an export after the full account
number. Only the last four digits of any run of eight or more are kept
in the account's name and in the import history, and older names and
archives are cut the same way. The account's internal id is permanent,
though, so an account created before this rule still carries the number
there, and it travels in exports; a restore lists any such account by
name. To be rid of it, undo that account's imports under *Recent
imports*, rename the files without the number, import them again, and
remove the old, now empty account.

**A folder import runs in the background.** Pressing *Import* hands the
plan to the server and the page follows it file by file — *N of M files*,
each file's result as it lands, with the same warnings and corrections a
single upload shows (lines whose direction was read from their wording,
rows an earlier import read differently) — so a folder of PDFs never has to finish
inside one request, and leaving or reloading the page does not stop it
(the Import page picks it up again; so does the app). Pressing the button
again, or retrying after a dropped connection, answers the same import
rather than starting a second. What it does is fixed when you press the
button: changing the plan afterwards changes nothing, and an export ZIP in
the folder is restored right then (after the step-up a restore asks for),
never later. One folder import runs at a time per household; a second is
refused until the first finishes. While it runs:

- **Cancel the rest** lets the file in hand finish and skips the others,
  including one still waiting its turn.
- It takes turns with the hourly sync, a restore or an account deletion
  between files, so a file may wait its turn; one that waits more than
  ten minutes is skipped (*"the account stayed busy"*). A restore is
  waited out for as long as the import's own time allows (about an hour
  and three quarters in all; past that the remaining files are reported
  not reached) — the files of a folder that carried an export ZIP start
  once its restore has finished.
- It stops for good if whoever started it is signed out — a password
  change or *sign out everywhere* — or loses permission to import, or if
  the account is locked or becomes read-only; the files it had not
  reached are listed so you can drop them again.
- After **45 minutes** the files not yet reached are reported as *not
  reached* — drop those again. A file the server was in the middle of
  when it restarted is reported as *interrupted* and is not run twice.
- Files waiting to be imported are held only until they run (and at most
  a couple of hours if the import never starts — it then stops, saying it
  *waited too long for the server*, and lists the files it never reached);
  they count against the same staging space as an analyzed folder.

A PDF statement is also bounded by what it **expands to**, not just its
file size: a single page may unpack to at most about **1.5 MB** of
drawing content, images embedded in a page to **40 megapixels** in all,
a whole document to 500 pages, and the extracted text to about 2 MB. A
real statement is far inside all of these; a file past one is refused
with *"this PDF expands past the size limit for a statement — export a
CSV or OFX/QFX from your bank instead"* (see
[troubleshooting](troubleshooting.md#a-pdf-is-refused-as-too-large)).

Income history from **tax documents** (SSA earnings record, IRS
transcripts, W-2s, 1040 PDFs) has its own card on the Import page — see
[tax-documents.md](tax-documents.md).

## After importing

Run **Bills → Find bills & income**. Then set your budget on the
**Budget** page. That's what turns raw history into the daily verdict.
