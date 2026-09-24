# Importing your financial history

More history = better recurring detection, better baselines. Every path
below is idempotent (re-importing converges), deduplicated against your
connected banks (±3 days, exact amount — the bank wins), and **one-click
reversible** (Recent imports → undo).

## Live sync

- **SimpleFIN (recommended, US/CA)** — sign up at bridge.simplefin.org
  ($1.50/mo paid to them; the instance never sees your bank login), create a setup
  token, paste it in the setup wizard or on the Accounts page.
- **Plaid (bring your own)** — create a free Plaid account (Trial plan,
  10 connections), then enter its client ID and secret in the **Plaid**
  row of Settings → Connections (**set up**; later **replace keys**) and
  link via the API. Advanced users only; SimpleFIN is easier.

## Files (Import page)

| Format | How to get it | Notes |
|---|---|---|
| CSV | any bank's "download transactions" | headers auto-detected (incl. separate Debit/Credit columns); unknown headers get a column-mapping form (map one Amount column, or a Debit column, or a Credit column — a file with only withdrawals or only deposits is fine) |
| OFX / QFX | bank's "Quicken/Money" download | no mapping needed, stable ids |
| QIF | old Quicken files | 90s-era date quirks handled; multi-account exports split |
| Mint | your archived `transactions.csv` | categories mapped automatically |
| YNAB | register export | Outflow/Inflow handled |
| Monarch / Copilot / Simplifi | each app's CSV export | auto-detected |
| Oikonome export ZIP | Settings → Data & maintenance on another instance | runs in the background with progress on the page (a big archive takes minutes; leaving the page doesn't stop it) — full restore: transactions, bills, your answers to recurring-bill and merchant-merge offers (something you declined is not offered again), the import history (so a file import that came along can still be undone under Recent imports), overrides, settings, and business-entity data (entities, equity, mileage, 1099 vendors, compliance, classifications). **Note:** a business entity's full EIN is never included in the export for security — after restoring, re-enter it (only the last 4 digits carry over). The daily email's recipient answers travel, with one narrowing: a **decline** always carries over, so somebody who said no stays off the list; an **acceptance** carries over only for an address that already has its own login on the destination household (in hosted mode, one whose email is confirmed). Everyone else — and anybody who had not answered yet — lands as *not invited* with an **Invite** button, because the invite *link* itself never travels. The restore summary tells you how many people have to accept a fresh invite before their mail resumes; Settings → Email & Push is where you send it. |

**Amount signs:** most bank CSVs use negative-for-spending — that's the
default. If your import shows spending as income, undo it and re-import
with the other convention selected. CSVs with a Debit and/or Credit
column are unambiguous and ignore the convention picker: whichever side
is present is read as that direction, and the missing side is simply
absent rather than assumed.

**Multi-account files split automatically.** A whole-household export
(Mint / Monarch / Copilot / Simplifi with several account names, or an
OFX file carrying several statements) never merges into one account:
each source-side account gets its own, typed from its name or statement
(cards land as credit so spend math stays honest). Re-importing lands on
the same accounts, and renames/reclassifications you make are never
overwritten. Single-account files go to the account you picked.

**Limits worth knowing.** One file per import is capped at **50 MB**, and
a restore archive (an export ZIP, which carries a whole household's
history and its receipt images) at **500 MB** — a restore is streamed to
disk rather than held in memory, which is why it may be that much larger
(an instance behind a CDN or proxy may cap request bodies lower);
a folder analyze takes up to **200 files** at a time. The doors that take a
file are also budgeted so one client cannot monopolise the instance: a
folder analyze or a tax-document analyze runs **20 times an hour**, a
single-file import **60 times an hour**, and an instance takes only a few
uploads at once — see
[troubleshooting](troubleshooting.md#an-upload-is-refused-or-times-out)
for what the refusals look like.

Income history from **tax documents** (SSA earnings record, IRS
transcripts, W-2s, 1040 PDFs) has its own card on the Import page — see
[tax-documents.md](tax-documents.md).

## After importing

Run **Bills → Find bills & income**. Then set your budget on the
**Budget** page. That's what turns raw history into the daily verdict.
