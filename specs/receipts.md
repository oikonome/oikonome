# Receipts on transactions (spec)

Decisions:

1. **Parsing = the tenant's configured LLM** (the Settings smart-
   categorization endpoint, vision-capable model required), extracting
   merchant, date, tax, tip, and line items from an attached image/PDF.
   **No LLM = attachment-only mode**: the receipt stores and displays;
   parsing runs later once an LLM is configured. No tesseract/OCR
   dependency in the image.
2. **Line items are stored + taggable** (business / personal / custom
   tags) under their transaction. **Expense report** = tag + month
   filter → CSV/printable summary with receipt images linked — the
   Schedule-C flow. Budget math is UNCHANGED: the transaction's total
   still rules the verdict; line items explain, never re-bucket.
   (Item-level bucket splitting rejected — rewires verdict math. What
   shipped instead is a HAND split of the row, `specs/transaction-splits.md`:
   the person writes the parts, and they must add up.)
3. **Images live in Postgres** (bytea, 5 MB cap per receipt) — one
   backup story: pg_dump / oikonome.sh backup / the dump download all
   carry receipts automatically.

Sketch: receipts table (tenant, txn_id, image bytea, mime, parsed
JSONB, status uploaded|parsed|failed); receipt_items (receipt, line
no, description, qty, amount, tag); upload from the transaction detail
UI; nightly LLM pass parses 'uploaded' rows; /api/receipts endpoints +
report export.
