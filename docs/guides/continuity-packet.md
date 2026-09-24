# Continuity packet

Settings → Data → Continuity packet answers one question: **if something
happens to you, can the person left running the household find the
money?** It prints one PDF from what Oikonome already knows — a map they
can read cold, without your logins and without your memory.

## What is in it

- **Where the rest is** — a note in your own words, printed first: where
  the passwords are kept, where the will and the insurance policies are,
  who the lawyer and the accountant are, who to call. Oikonome cannot
  know any of this; you write it once and it prints every time.
- **Who can sign in** — every account on the household, with its role, so
  the reader knows which login opens today's numbers.
- **What it adds up to** — financial accounts, property and mortgages, net
  worth on the day it was printed.
- **Accounts, by institution** — every connected account: type, the last
  four digits, the balance that day, the owner tag, and which business it
  belongs to. Hidden accounts and linked duplicates are left out.
- **Debts** — every card and loan with a balance, its rate and what must be
  paid each month: the lender's minimum on a card or loan, and on a
  mortgage the servicer's whole payment with escrow, principal and interest
  shown beside it (marked *est.* where Oikonome had to estimate).
- **Recurring bills** — what comes due, how often, the next date, and the
  account that pays it where Oikonome knows.
- **Income** — what arrives on a schedule and where it lands.
- **Businesses** — each entity's structure, state, formation date, the
  last four of the EIN, the registered agent, the members, its accounts
  and the next filings due.
- **If you are reading this because I am gone** — a short list of what to
  do first: keep the bills paid, sign in, call each institution, ask who
  the beneficiaries are.

## What is not in it

No passwords, no bank credentials, no full EIN, nothing that opens an
account. The packet is a map of the money, not a key to it — that is
what makes it safe to print and leave where the other person will look.
The note is where you say *where* the keys are.

## Printing it

Fill in **Prepared for** (optional — it goes in the title) and the
**Where the rest is** note, save, then **Download PDF**. You confirm it is
you before the download starts: the packet is the whole shape of the
household's money, so a signed-in tab alone is not enough, exactly like
the full export. Print a fresh copy whenever the accounts change; the
footer carries the date it was printed.

On the phone the same card is under More → Settings; the PDF goes to the
share sheet, so it can be saved, printed, or sent from there.

## Emailing it

**Email the packet** sends the PDF as an attachment, with the same
confirmation as the download. Where it may go depends on the instance:

- **Self-hosted**: any address the instance's mail relay will carry.
- **Hosted**: only an address with an account on this household — the
  same rule as the daily email. Invite the person under Settings → Users
  first, or download the PDF and hand it over.

Each download and each emailed copy is written to the household's
activity log (Settings → Users → Activity), so everyone can see when a
copy left the instance and where it went.

## Gotchas

- **The note is the part that matters most.** Balances and institutions
  print themselves; nothing else in the packet can say where the will is.
- **Balances are a snapshot.** They are right for the day in the footer
  and drift from there. The institutions, bills and logins are what to
  act on; the reader gets today's balances by signing in.
- **A survivor still needs a login.** Give them a viewer account now
  (Settings → Users → invite) so "Who can sign in" lists them; the packet
  points at Oikonome but does not open it.
- **Email is not configured** on every self-hosted instance. When it is
  not, the card says so and the download is the way.
