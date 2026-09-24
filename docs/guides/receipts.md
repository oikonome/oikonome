# Receipts: photo to line items

Attach a receipt photo (or PDF) to any transaction and the app extracts
what you bought, line by line. One $214 grocery transaction in the
ledger, thirty named items behind it.

## How it works

- Upload from a transaction's detail — image (PNG/JPEG/WebP/GIF) or
  PDF, 5 MB max per file.
- If you've configured a **vision-capable LLM** (Settings → AI — the
  backend the "Receipts & documents" task is routed to, each backend
  can carry its own dedicated vision model), a
  background pass reads the receipt and extracts merchant, date,
  total, tax, tip, and the individual line items.
- **No LLM configured?** The receipt still stores and displays —
  attachment-only mode. It parses automatically whenever a vision
  model is configured later; nothing is lost.

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
are also searchable and groupable on the Items view.

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
  better photo or model.
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
