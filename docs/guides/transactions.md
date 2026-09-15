# Transactions

The Transactions page is the ledger: every synced or imported charge,
one month at a time, with search across all accounts and the full
history — and the place to correct a category when the source got it
wrong.

## Month view and search

By default you browse one calendar month with the **‹ › arrows** (the
**today** button jumps back to the current month; you can't page past
it). The headline states the month's cash flow — **in · out · net** —
by the same definitions the Cash Flow page uses: income in, household
spending out. Transfers between your own accounts and credit-card
payments are money *moved*, not made or spent (the card's swipes
already counted), so they appear in the list but move none of the three
figures. Picking an **account** or **category** from the dropdowns, or
setting a **from/to** date, switches the page to universal search
immediately; the search box filters **as you type** (a beat after the
last keystroke, two characters minimum — press Enter to search a single
character). Universal search covers
every account, the whole history, newest first, **100 rows per page**,
with a summary line — result count, scope, date range, and the signed
total over the entire result set, not just the visible page.

Text search matches the merchant name — the one the page shows, so a
merchant you renamed is findable under its new name as well as the
bank's original spelling — the category (with or without
underscores), and **line items** — searching "shampoo" finds the store
run whose attached receipt lists it, and the same goes for the items in
an imported store order or receipt (Amazon orders, Costco receipts). A
query that looks like a number also matches transaction amounts. It
also matches **account
names**, by either name an account has — the name you gave it or the
bank's own — and returns that account's transactions; the summary line
then says so ("includes Groceries Card"), naming the bank's own name
when that is what matched, so a result that shows neither word explains
itself. **clear** returns to the monthly view.

More lenses sit behind **more ▾** in the toolbar:

- **reimb** — only reimbursement activity: matched pairs and
  awaiting-reimbursement flags. Works in both month view and search.
- An **owner** dropdown appears once any account or transaction has an
  owner label; it filters the month view by effective owner
  (per-transaction override, else the account's owner). Display only —
  it never changes budget math.
- A payment-channel dropdown — **any channel**, **in store**, **online**,
  **other** — narrows to how the bank says a purchase was paid.
- **checks only** keeps just the transactions the bank reports as checks.

The channel and check filters run as searches, so they cover the whole
ledger like an account or category pick does.

The **awaiting reimbursement** button carries a live count and opens the
Reimburse page; it's a shortcut, not a filter. The count of business
rows lives on the Business tab; the headline's own "*N* business (not in
the totals)" note explains why the three figures look the way they do.

## The date range

Beside the from/to boxes sit the same six range buttons every other
over-time surface uses — **3m · 6m · 1y · 3y · 5y · All**. They are a
shortcut into the same from/to filter, cut on calendar months: **3m** in
September means "from July 1", the same three months Cash Flow's 3m
covers. **All** clears both dates. Type a from/to by hand and none of the
six lights up — the boxes are still the precise control, the buttons are
the quick one.

On the phone the filters live in one sheet, and the same six buttons head
its **Date range** section, with **from** and **to** opening a calendar
rather than asking you to type a date. **Account** and **Category** there
are searchable drop-downs (closed accounts grouped under *Archived /
closed*) instead of long strips of chips, and the sheet's top line keeps
score as you go — "September 2026 · 128 transactions · $412.00 listed".
Back on the list, every filter in force shows as a chip under the search
box: tap one to drop that filter, or **clear all** to drop them together,
and a subhead reports what is left — "24 listed · $612.40 total for this
filter".

## Reading a row

- **Date** — MM/DD/YY.
- **Amount** — the display flips the stored sign: charges show
  negative, deposits positive (and highlighted). Cents always show
  here. Transfers render dimmed, as does the incoming (credit) side of
  a loan or credit-card payment — money moving, not spend; the
  outgoing payment row itself is not dimmed (see the concepts guide).
- **Merchant** — the merchant's logo (when your bank connection
  supplies one — Plaid does for most national merchants; otherwise a
  two-letter mark in a colour that stays the same for that merchant),
  then its name, linking to the merchant's history page (the last 24
  months of charges, lifetime and monthly totals, the add-as-bill
  form). Logos can be switched off in Settings → Merchants ("Show
  merchant logos") for a plainer ledger; the choice applies to the
  merchants, spending and accounts pages too (the daily email never
  carries images).
  A row whose charge has been matched to an imported store order or
  receipt shows **what was in it**, in italics after the name — an
  Amazon charge carries its order's summary, a Costco charge its
  receipt's breakdown ("COSTCO WHSE #0123 — *groceries $210 · household
  $62 · apparel $40*"). That is the whole point of importing them: the
  two merchants whose names never say what you bought finally do. No
  order data, no line — every other row is unchanged. See the receipts
  guide.
  Venmo and Zelle rows use the counterparty as the merchant,
  with the rail kept in the name — "Zelle — Jane Doe", not one giant
  "Venmo" bucket — so each person groups and totals like any other
  payee, and the same person paid via Venmo and via Zelle stays
  distinguishable.
- **Pills**: **pend** (still pending at the bank), **reimb** (paired
  with a reimbursing deposit), **awaiting reimbursement** (flagged as
  expected — the category is unchanged and it still counts as spend
  until the deposit is matched; see the reimbursements guide).
- **biz** — a toggle that tags the row a business expense (Schedule C
  worksheet). A tag only; budget math is unchanged.
- **a pill with a business's name** — the row *is* that business's money:
  its account is assigned to the business (see the business guide). Listed
  for reference, never counted in the page's household totals; the
  **more ▾** filters can hide or isolate these rows.
- **⟳ recurring** — a blue pill meaning an active bill's matcher counts
  this row: the bill's merchant tokens all appear in the row's text and
  the amount is within tolerance (the larger of $30 or 25% of the bill
  amount; envelope bills take any spend row on their merchant). It
  links to that bill's history. Unmatched spend rows instead show a
  faint **+⟳** shortcut — the merchant's history page with a bill /
  envelope form prefilled (nothing saves until you confirm; see the
  Bills guide).

## The row's detail (⋯)

Open a row's ⋯ and, above the actions, everything the bank sent about
it: the bank's own descriptor, where it happened (city, region, a map
link and the store number when known), how it was paid, the check
number, the authorized date when it differs from the posted date, the
merchant's registered line of business (MCC), and what Plaid itself
called it with its confidence. Below that, one line says **why the row
has the category it has** — "Plaid, very high confidence", "your rule
for this merchant", "the bill *Storage Unit* categorizes the charges it
matches", "you set it on this transaction", "matched to an Amazon
order", "matched to a Costco receipt", "the merchant's registered line
of business (MCC)". Rows from file imports show what they have (the
bank text and the why) and skip the rest.

## Recategorizing

The category cell is a select. Picking one asks how far it should reach
— **Apply *Groceries* to… Just this one / All "SAFEWAY" transactions** —
in a panel that opens **directly under the row you just changed**, so a
correction made on row 300 of a long ledger answers itself where you are
looking rather than at the top of the page.

Either way the row gets a **permanent per-transaction override** — sync
never touches it again. **All** additionally teaches a merchant-wide
rule: every past row of that merchant updates immediately, future rows
land pre-categorized, and the rule shows up under Settings →
Categorization. **Just this one** creates no rule.

**All** never touches a row that carries its own override — that was
somebody's hand-made decision — and it says how many it is leaving
alone. It *does* reach rows the bank labelled a transfer, income or a
loan payment: your merchant rule outranks the bank's label (a Venmo
payment to the babysitter is a transfer to the bank and childcare to
you), and those rows start counting as spending. Because that changes
budget math, the panel names the count and asks a second time before
writing. Choosing a flow category yourself — transfers, income, loan
payments — teaches no merchant rule at all.

The same select also offers:

- **✎ custom category…** — a free-form name; it behaves like any other
  category, propagates merchant-wide, and joins the filter dropdowns.
  To **rename** a custom name later (everywhere it appears — overrides,
  merchant rules, and budget carve-outs), use **Rename a custom
  category** at the foot of the Rules page (the owner or a member).
  Only free-form names you created are listed;
  standard Plaid categories (Food and Drink, Transfers, …) cannot be
  renamed. There is no separate category registry; rename rewrites the
  string.
- **↺ reset to source category** — deletes the override; the row falls
  back to its underlying category. If a merchant rule exists for that
  merchant, the rule re-applies on the next categorization sweep.
- **↔ mark reimbursed…** — opens the Reimbursements page to pair the
  charge with the deposit that paid it back.
- When you have a business set up, each expense also lists that
  business with **capitalize (owner contribution)** and **reimburse from
  business** — for a business cost you paid from a personal account. The
  business guide explains what each records.

The rest of a row's actions live behind its **⋯** menu: flag it as
awaiting reimbursement, mark it a business expense, add a note, attach a
receipt, or make it a recurring bill. **Remove stuck pending** appears
there — and on the phone's transaction screen — only on a **pend** row
the bank has left pending for more than two weeks, and deletes that
abandoned authorization from the ledger so it stops counting as spend. Tick several rows and a bar appears
to recategorize, flag or unflag them in one go. Because you picked those
rows one by one, it recategorizes every one of them — bank-labelled
transfers and income included — and teaches no merchant rule. It then
reports how many it changed and, honestly, how many of those were
transfers or income that now count as spending.

## Receipts

Open a row's **⋯** menu and choose **attach a receipt** to add an image or
PDF (or a photo of a written check), view what's attached, and see parsed
line items. Parsed items become searchable from this page's search box. A
row that already has one says so in the menu. Full mechanics are in the
receipts guide.

## View-only logins

A view-only login gets the same table read-only: the category is
plain text, the biz toggle becomes a pill, the +⟳ shortcut is hidden,
and the clip appears only when a receipt is already attached (view
only). The server enforces it too — the writes are rejected, not just
hidden. A **member** login edits this page exactly as the owner does;
see the security guide for what each role can change.

## Gotchas

- **All "*merchant*" transactions** propagates by design — every row of
  that merchant that has no override of its own, transfer-labelled rows
  included. To move one row and teach nothing, take **Just this one**.
- "Reset to source" only deletes the per-transaction override — an
  existing merchant rule reasserts itself on the next sweep. To truly
  detach, delete or disable the rule from the merchant page.
- The search-mode total sums the **whole** result set across pages;
  refunds and deposits in the results offset the charges.
- If a fetch fails mid-browse, the page keeps the last loaded rows and
  says so — a stale banner with a Retry button, not silently old data.
