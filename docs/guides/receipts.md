# Receipts: photo to line items

Attach a receipt photo (or PDF) to any transaction and the app extracts
what you bought, line by line. One $214 grocery transaction in the
ledger, thirty named items behind it.

## How it works

- Upload from a transaction's detail — image (PNG/JPEG/WebP/GIF) or
  PDF, 5 MB max per file — or **snap it before the charge exists** (see
  the next section).
- If you've configured a **vision-capable LLM** (Settings → AI — the
  backend the "Receipts & documents" task is routed to, each backend
  can carry its own dedicated vision model), a
  background pass reads the receipt and extracts merchant, date,
  total, tax, tip, and the individual line items.
- **No LLM configured?** The receipt still stores and displays —
  attachment-only mode. It parses automatically whenever a vision
  model is configured later; nothing is lost. A receipt snapped before
  its charge can have its total and date typed in instead (next section).

## Snap first, match later

You don't have to wait for the charge to reach the ledger. **📷 Receipt**
— at the top of the Transactions page, on the Receipts page, or beside
Filter in the app — takes a photo right at the
till, or takes one you already have: a phone browser asks camera, photo
library or files, a desktop opens a file picker, and the app's Snap button
asks camera, photo library or PDF. The receipt is read as usual and **waits** on the
Receipts page under *Waiting receipts*, showing the store, total and date
read off it, and *waiting for a transaction*.

When the charge arrives — the next bank sync, a file import, anything that
adds transactions — the app pairs the two by itself, and the receipt
appears on the charge exactly as if you had attached it there. If the
charge was already in the ledger when you snapped, the pairing happens as
soon as the receipt has been read. How it decides:

- **Exact amount first.** A charge for exactly the receipt's total, that
  posted from 2 days before to 7 days after the receipt's date, money out
  and not already carrying a receipt, is the match. If several fit, the
  one from the **same store** wins (the store's name is compared the way
  the Merchants page compares names, so *SQ \*CORNERDELI* and *Corner
  Deli* are the same store).
- **Near amount, same store.** A charge from the same store that is up
  to 25% more than the receipt (at least $1 — a tip written on the slip
  after it printed) or up to 3% less (at least $0.50 — a currency
  conversion) matches, as long as only one does. The store's own charge
  wins over an exact-amount charge under a different name.
- A lone exact-amount charge under a **different name** (a restaurant's
  card descriptor often shows its company's legal name) still matches when
  nothing from the store fits.
- **These last two wait out the 7 days.** Only an exact charge from the
  same store pairs at once; a near-amount charge or another name's exact
  one is paired once the 7 days are up — so the store's exact charge
  arriving a day later is never beaten to your receipt, and yesterday's
  coffee is never taken for today's.
- **A tie waits for you.** Two charges that fit equally well — two
  identical coffees at one café — are never guessed between. The receipt
  stays waiting, marked *more than one charge fits*, with both charges
  listed to pick from.

Each waiting receipt lists up to three **candidate charges** (from a week
before its date to three weeks after), best first; **match** attaches it
in one tap. To attach it to a charge that isn't listed, open that
transaction's receipt panel: receipts still waiting are offered there as
**＋ store $total** buttons, the likeliest first — one the app already
suggests for that charge, then the closest total, then the closest date.
The best three show; **more…** lists the rest with a search box (store,
total or date).

A long waiting list shows its first page with *Showing N of M* and
**show more** underneath.

**Matched recently** lists what the app paired by itself over the last
14 days. If it chose wrong, **unmatch** puts the receipt back to waiting
— and the app remembers: it never pairs that receipt with that charge
again, nor with the same charge once it posts (a pending charge and its
posted copy count as one). You can still match them by hand. A receipt on
a transaction says *matched automatically* or *matched by hand*, with
**unmatch** beside it.

A pending charge that matched keeps its receipt when it posts, tip and
all. If a pending charge disappears without a posted twin the app can
find, a receipt the app paired with it goes back to waiting and meets the
posted charge instead. One **you** matched by hand goes back to waiting
too, marked *was matched by hand — that charge was removed*, and the app
leaves it for you: pick its charge again.

**A receipt that never matches** stays waiting — nothing is ever deleted
for you. After **30 days** it is marked *no charge has matched in 30
days*: match it by hand, or remove it (a cash purchase, or a charge on a
card this household doesn't track).

**No AI model on this instance?** Then nothing reads the receipt, and the
*Waiting receipts* card says so at the top: *This instance has no AI
endpoint to read receipts*. Each such receipt is marked *needs its total*
and shows **Enter the total and date** — type the total, pick the date on
the receipt and, if you like, the store, then **Save**. The app looks for
the charge straight away: it pairs by itself under the same rules as a
receipt it read, or lists the candidates for you to pick. Or add a model
under Settings → AI and later receipts are read for you.

**The AI got it wrong, or couldn't read it?** A receipt marked *couldn't
read* shows the same form; on one it did read, **fix total** opens it with
what was read filled in. What you type replaces the total, date and store
it read (a store left blank keeps the one it read). From then on the
receipt says *total and date entered by hand*, and no later reading —
not **retry**, not the nightly pass — writes over what you typed.

Only receipts wait — a **check image** is attached to its check's
transaction, since its payee categorizes that row.

Snapping follows the same rules as attaching: a view-only login can see
the waiting list but not add to it. The demo lets you match and unmatch,
but doesn't take uploads, typed-in totals or removals.

## The optimized copy

Photo uploads get a cleaned-up copy stored **alongside the untouched
original**: the receipt is auto-detected in the frame, cropped,
perspective-straightened, and rendered as a sharpened high-contrast
grayscale scan. The panel shows both as equal buttons — **optimized**
and **original** — each one click away; neither hides the other.

The vision model reads the **optimized copy** when one exists —
cleaned-up text extracts better than a raw phone photo. If a photo
can't be cleaned up (or is unusually large), everything falls back to
the untouched original; an upload never fails because of this. PDFs
keep only the original.

## Line items and tags

Extracted items are stored under their transaction and can be tagged —
business, personal, or anything you like. The **expense report** takes
a tag plus a month and produces rows with linked receipt images —
a CSV/printable summary built for Schedule-C-style bookkeeping. Items
are also searchable and groupable on the Receipts page. A waiting
receipt's items are there too, as soon as it has been read — under the
store and date read off it (*Unknown store* if none was), marked
*waiting* — and can be tagged like any other; they move to the charge's
date and name once it matches.

The model **extracts only** — it never guesses categories or tags for
you (small vision models read receipts well but mis-label what they
read, so items are searchable and groupable instead of auto-filed).

## Check images

A written check's ledger row is just "CHECK #1234" — the attachment can
say who it was for. Use **attach check image** on a transaction to
upload a photo or scan of the check's front (in the app: take a photo,
**choose a check photo** from the library, or **attach a check PDF**); instead of line items, the
parse reads the **check number, payee** (the pay-to-the-order-of line),
**amount, date, memo**, and bank name, and the panel shows them under
the attachment (dates as MM/DD/YY).

The payee then drives **auto-categorization** through the same
machinery merchant categorization already uses: an existing rule for
that payee (your own corrections included) applies immediately;
otherwise the LLM classifies the payee once and the result is cached,
so the next check to the same payee is free. A category you set by hand
on the transaction is **never touched**.

If the amount read off the check disagrees with the transaction's
amount, the panel shows a quiet note — **the transaction itself never
changes**. Check amounts are always read as positive; the comparison
ignores the ledger sign (positive = money out — see the concepts
guide).

## Store receipts you never photographed

Two merchants post charges whose names say nothing about what you bought:
an Amazon charge posts as "AMAZON.COM", and a warehouse-club run as
"WAREHOUSE CLUB #0123". Both can be filled in without a camera, by
importing the store's own record of the order — see the
[community scripts guide](../community-scripts.md), which is where that
sort of collector lives (it runs on your machine, on your credentials).

Once imported, the app matches each order or receipt to the card charge
that paid for it — same amount, within a few days, the card's own digits
breaking ties. A costco.com order is billed per shipment, so when no single
charge equals the order, the app looks for two to four charges in the two
weeks after the order that add up to it exactly (and a return refunded in
parts the same way); a warehouse receipt is always one swipe and is never
matched to a sum. Then:

- The ledger row, the Today page and the daily email show **what was in
  it** after the merchant name: an order summary for Amazon, and for
  Costco a breakdown of the receipt by what was bought ("groceries $210 ·
  household $62 · apparel $40"). Costco's line items are categorized from
  the department number the register stamps on each line, so the
  breakdown is the store's own accounting, not a guess.
- The charge takes the receipt's dominant category, and its detail panel
  says why: *matched to an Amazon order*, *matched to a Costco receipt*.
  A category you set by hand still wins.
- The items become **searchable** from the Transactions page like any
  other line items.
- Charges an import can't explain are left alone. A Costco fuel or
  membership charge already has a sensible category from your bank, so
  nothing is stamped on it.

Re-importing refreshes a stored order's items, category and summary
without moving its date or amount, so a better category map reaches
everything already collected.

## What receipts never do

Budget math is unchanged: the **transaction's own total** is the only
number the verdict, budgets, and reports see. Line items explain a
charge; they never re-bucket or split it. To count one charge under
several categories, **split the row by hand** (the ✂ action in its ⋯
menu — see the Transactions guide); the receipt then documents what the
parts were.

## Gotchas

- Receipt images — both copies — live in the database, so a normal
  backup (`./oikonome.sh backup` or a dump download) carries them
  automatically.
- A failed parse (blurry photo, odd layout) leaves the receipt
  attached with its status and error shown; **retry parse** after a
  better photo or model. *"The model answered in prose, not the
  expected fields"* means the vision model ignored the request for
  structured output — pick a different vision model in Settings → AI.
- Parsing runs on the server — it's safe to navigate away mid-parse;
  the results are there when you come back.
- Only a couple of photos are decoded at once (a full-resolution image
  is a lot of memory), so a batch queues rather than running together.
  Anything still waiting after a couple of minutes is marked failed and
  picked up by the next sweep, and *"server is busy reading receipts"*
  simply means try again shortly. Operators can raise
  `OIKONOME_RECEIPT_PARSE_CONCURRENCY` / `OIKONOME_RECEIPT_PARSE_WAIT_S`.
- A check parse only auto-categorizes when the payee is a real
  merchant; transfer-like payees are left to the normal transfer
  handling.
