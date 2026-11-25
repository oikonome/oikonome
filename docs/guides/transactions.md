# Transactions

The Transactions page is the ledger: every synced or imported charge,
one month at a time, with search across all accounts and the full
history — and the place to correct a category when the source got it
wrong.

## Primary checking balance

The top of the page shows your **primary checking** balance — the account
the cash forecast starts from, with its available balance when the bank
reports one. It is the account you pinned on the Accounts page (open it
by clicking the balance); with no pin, the checking account with the
largest balance. A checking account that has no balance yet reads
"balance not set"; a household with no checking account shows nothing
here.

## Month view and search

By default you browse one calendar month with the **‹ › arrows** (the
**today** button jumps back to the current month; you can't page past
it). The headline states the month's cash flow in four tiles — **In**,
**Out**, **Net** and **A day** —
by the same definitions the Cash Flow page uses: income in, household
spending out. Transfers between your own accounts and credit-card
payments are money *moved*, not made or spent (the card's swipes
already counted), so they appear in the list but move none of the
figures. In the current month, **In** is what has landed so far, with
the paychecks still due before month end shown under it as "+$X
expected" — the Budget page's plan counts the two together. **A day** is the month's spending per day: out
divided by the days so far in the current month, or by every day of a
closed month. A category opened for a month, a budget label (Food,
Everything else) or a from/to date range shows the same per-day figure
for its own spending over its window; a search with no start date has
no window, so it shows none. Picking an **account** or **category** from the dropdowns, or
setting a **from/to** date, switches the page to universal search
immediately; the search box filters **as you type** (a beat after the
last keystroke, two characters minimum — press Enter to search a single
character). Universal search covers
every account, the whole history, newest first, **100 rows per page**,
with a summary line — result count, scope, date range, and **out · in ·
net** over the entire result set, not just the visible page. Those three
use the same rules as the month tiles and Cash Flow: card payments and
transfers between your own accounts are in neither, so a date range
alone reads exactly what Cash Flow reads for the same days. On a
desktop the table's column headings stay pinned at the top of the
screen as you scroll down a long list.

Text search matches the merchant name — the one the page shows, so a
merchant you renamed is findable under its new name as well as the
bank's original spelling — the category (with or without
underscores), and **line items** — searching "shampoo" finds the store
run whose attached receipt lists it, and the same goes for the items in
an imported store order or receipt (Amazon orders, Costco receipts). A
query that looks like a number also matches transaction amounts, and a
written check whose image was read is found by its payee. It
also matches **account
names**, by either name an account has — the name you gave it or the
bank's own. When no transaction's own text matches, the term names an
account and you get that account's transactions; the summary line then
says so ("includes Groceries Card"), naming the bank's own name when
that is what matched. When transactions do match (searching a store
that a card is also named after), the result is those transactions only,
and the card is offered as its own link — "also: rows on …" — so the
card's other purchases never fold into the store's figures. **clear**
returns to the monthly view.

More lenses sit behind **more ▾** in the toolbar (it reads "more (2) ▾"
when two of them are on):

- **reimb** — only reimbursement activity: matched pairs and
  awaiting-reimbursement flags. Works in both month view and search.
- An **owner** dropdown appears once any account or transaction has an
  owner label; it filters the month view by effective owner
  (per-transaction override, else the account's owner). Display only —
  it never changes budget math.
- A payment-channel dropdown — **any channel**, **in store**, **online**,
  **other** — narrows to how the bank says a purchase was paid.
- **checks only** keeps just the transactions the bank reports as checks.
  A written check the bank filed as a transfer counts as spending under
  **Checks**, and its row asks once **who was this to?** — the answer is
  saved as the check's note and, where a rule or the assistant's model
  knows the payee, files the check under that payee's category. The
  answer is up to 120 characters; answering again replaces it (a "Paid
  to …" line you wrote yourself is kept), and it survives the bank
  re-sending the row. A check whose money lands in another of your own
  accounts — the same amount, within ten days, such as a check that
  funds your IRA or savings — is a transfer, not spending, and does not
  ask; so does a check whose answer says the money only moved.

The channel and check filters run as searches, so they cover the whole
ledger like an account or category pick does.

The **awaiting reimbursement** button carries a live count and opens the
Reimburse page; it's a shortcut, not a filter. The count of business
rows lives on the Business tab; the headline's own "*N* business (not in
the totals)" note explains why the figures look the way they do.

## Downloading what you see

The **CSV · XLSX** links at the right of the headline (on the phone, the
**download CSV / XLSX** chip above the filter strip) save the rows the
page is showing — the month on screen, or a search with every filter
you have applied, all pages of it, not just the visible one. Columns are
the ones you read on the page: date, payee, amount (money in positive,
money out negative), category, account, pending, note, owner, business,
reimbursable, channel, check number, city and region, the Amazon or
store items on the row, the bill it pays, the bank's own text, and the
row id last. A payee that starts like a spreadsheet formula is written
with a leading quote so Excel and Calc show it as text. One download
holds up to 50,000 rows; the full-household export under Settings is the
way to take everything. A household prepares one download at a time: a
second one asked for while the first is still being written is refused
with "an export is already running", and a busy instance may answer
"try again in a minute"; wait for the first, then click again. Downloading
is the owner's: a member or view-only login reads the same rows on the
page but is not offered the file.

## The date range

Beside the from/to boxes sit the same eight range buttons every other
over-time surface uses — **This month · 1m · 3m · 6m · 1y · 3y · 5y ·
All**. They are a shortcut into the same from/to filter, and they cover
exactly the windows Cash Flow's buttons cover: every range but This month
holds complete months — **3m** in September is June 1 through August 31,
**1y** the twelve months that have closed — so the figures above the list
match Cash Flow's for the same button. **This month** runs from the 1st
to today; **1m** is the last complete month. Hover a button to see its
dates. **All** clears both dates. Type a from/to by hand and none of the eight
lights up — the boxes are still the precise control, the buttons are the
quick one.

On the phone the filters live in one sheet, and the same eight buttons head
its **Date range** section, with **from** and **to** opening a calendar
rather than asking you to type a date. **Account** and **Category** there
are searchable drop-downs (closed accounts grouped under *Archived /
closed*) instead of long strips of chips, and the sheet's top line keeps
score as you go — "September 2026 · 128 transactions", with "· $412.00
spent" once a filter narrows the month (a search reads "42 results · out
$X · in $Y").
A listing opened from a plan label (a Today tile, a money-map row, a Why
line) says what it is in a chip — "Food · Sep 2026", or "Food · 2026"
from the year lens — and, when the raw sum of the listed rows is not the
number the label wore, the headline adds **counted as $X**: the plan
nets reimbursements and counts an envelope's overflow only past the
envelope, while the list shows whole rows.

Back on the list, every filter in force shows as a chip under the search
box: tap one to drop that filter, or **clear all** to drop them together,
and a subhead reports what is left — "24 listed · $612.40 spent", the
spending among the listed rows.

## Reading a row

- **Date** — MM/DD/YY.
- **Amount** — the display flips the stored sign: charges show
  negative, deposits positive (and highlighted), on the web and the
  phone alike. Cents always show here. Transfers render dimmed, and so
  does a credit-card payment on both sides — the checking row that pays
  the card as well as the card row it lands on — which reads **Card
  payment** rather than "Loan Payments", the name it shares with a
  mortgage that is spending (see the concepts guide).
- **Merchant** — the merchant's logo (when your bank connection
  supplies one — Plaid does for most national merchants; otherwise a
  two-letter mark in a colour that stays the same for that merchant),
  then its name, linking to the merchant's history page (every charge, over
  the This month … All picker, All by default; lifetime and monthly
  totals; the add-as-bill form). Logos can be switched off in Settings → Merchants ("Show
  merchant logos") for a plainer ledger; the choice applies to the
  merchants, spending and accounts pages too (the daily email never
  carries images).
  A row whose charge has been matched to an imported store order or
  receipt shows **what was in it**, in italics after the name — an
  Amazon charge carries its order's summary, a Costco charge its
  receipt's breakdown ("WAREHOUSE CLUB #0123 — *groceries $210 · household
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
  this row: the row is one of the bill's merchants (a bill with no
  merchants matches by words in the row's text) and the amount is
  within tolerance — a quarter of the bill, at least $30 but never more
  than half the bill ($15 → $7.50, $60 → $30, $200 → $50); envelope
  bills take any spend row on their merchant. It links to that bill's
  history. An unmatched spending row offers **⟳ make recurring** in its
  ⋯ menu instead — the merchant's history page with a bill / envelope
  form (nothing saves until you confirm; see the Bills guide).

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
payment to a roommate for rent is a transfer to the bank and rent to
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
  merchant, the rule re-applies on the next categorization sweep. A
  category you set yourself also has a one-tap way back: the **↺** beside
  the category on the web row, and **Use automatic** on the transaction
  screen in the app. Overrides a bill or a store match wrote keep only the
  picker's reset.
- **↔ mark reimbursed…** — flags the charge as awaiting reimbursement
  (if it isn't already) and opens the Reimbursements page with its
  candidate deposits showing, to pair it with the deposit that paid it
  back.
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

## Splitting one charge across categories

A single charge is often several things — the big-box run that was
groceries and a lawn chair, the payment to a roommate that was rent and
the power bill. Open the row's **⋯** menu and choose **✂ split across
categories**: a panel opens under the row with one line per part
(category + amount). Add parts, put the remainder on a line with the
**←** button, and **Save split** once the parts add up to the charge — the
panel refuses to save until they do, and so does the server. In the app
the same editor is the **Split…** button on the transaction screen; its
category sheet picks for one part at a time. A part takes a spending
category or one of your own, never a transfer or income category.

Only spending can be split: a refund, a transfer, a card payment or a
row on a loan account counts as one whole row, and the action is not
offered there.

A split row shows its parts where the category select was, with a
**✂ split** pill; tap the parts to edit. **Remove split** hands the row
back whole. Both the web and the app mark a split row in the ledger.

What a split changes: **every per-category figure counts the parts**
instead of the row — the Today verdict's food / everything-else buckets
and any custom bucket, the daily email, the Cash Flow and Spending
category totals, the Month and Year lenses, and a category filter on this
page (the row answers to each of its parts' categories). What it leaves
alone: totals by month, by merchant and by account, the reimbursement
netting (a partial reimbursement is shared across the parts pro rata),
the anomaly checks (a charge that is unusually large is judged whole),
and the row's own category, which stays as the bank, a rule or you set
it and comes back the moment the split is removed. A bill's matcher sees
the parts as separate charges, so a split bill payment matches its bill
only if one part carries the bill's amount.

Parts must be spending categories — transfers, income and loan payments
are decided for the whole row, never for a part. A pending charge that
posts for a different amount drops its split (the parts no longer add
up); split it again once it settles.

### Splits from a receipt

A charge matched to a Costco receipt or an Amazon order whose items are
of more than one kind is split for you: a receipt that was $50 of
groceries and a $10 household item counts $50 under **Costco - Food &
Drink** and $10 under **Costco - Home**, so the food budget sees the
groceries and only the groceries. The parts follow the items' prices, scaled to what the
card was charged (tax and shipping shared pro rata), and the row shows
them with a **✂ split by receipt** pill. Costco items carry their
category from the receipt's department; Amazon items get theirs from the
AI backend that does categorization, the same pass that names the order
(it reads up to six items of an order, so a larger order is not split
and keeps its single category). Money in — a refund — is never split by
a receipt.

A receipt's split is recomputed whenever the store's records are matched
again, so it tracks a corrected receipt. Your own answer always wins:
editing the parts makes the split yours and the receipt stops touching
it; **Remove split** pins the row's category so the whole charge counts
there and the receipt does not split it again (clear that category pin
to get the receipt's parts back). A receipt whose items all read as one
category, or an item without a price or a category, leaves the row
whole under the receipt's single category as before.

## Receipts

Open a row's **⋯** menu and choose **attach a receipt** to add an image or
PDF (or a photo of a written check), view what's attached, and see parsed
line items. Parsed items become searchable from this page's search box. A
row that already has one says so in the menu. Full mechanics are in the
receipts guide.

**📷 Receipt** at the top of the page takes a receipt before its
charge has reached the ledger; it waits on the Receipts page and attaches
itself to the charge when the charge arrives. While any are waiting, the
top of the page says how many, and a row's receipt panel offers them for
you to attach by hand — the three likeliest for that charge first, the
rest under **more…** with a search box.

## View-only logins

A view-only login gets the same table read-only: the category is
plain text, the biz toggle becomes a pill, the ⟳ make recurring shortcut is hidden,
and the clip appears only when a receipt is already attached (view
only). The server enforces it too — the writes are rejected, not just
hidden. A **member** login edits this page exactly as the owner does;
see the security guide for what each role can change.

## Who changed it

Every hand edit on a row — a category correction, a split, a note, a
receipt, a flag — is recorded with the person and the time in the
household's activity log: **Settings → Household → Activity** on the web,
**More → Activity** in the app. See [Account security → Activity](security.md).

## Gotchas

- **All "*merchant*" transactions** propagates by design — every row of
  that merchant that has no override of its own, transfer-labelled rows
  included. To move one row and teach nothing, take **Just this one**.
- "Reset to source" only deletes the per-transaction override — an
  existing merchant rule reasserts itself on the next sweep. To truly
  detach, delete or disable the rule from the merchant page.
- The search-mode out · in · net covers the **whole** result set across
  pages; a refund on a card is money in, a card payment or a transfer is
  neither.
- If a fetch fails mid-browse, the page keeps the last loaded rows and
  says so — a stale banner with a Retry button, not silently old data.
